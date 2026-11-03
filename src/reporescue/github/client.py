from __future__ import annotations

import base64
import contextlib
import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .config import GitHubConfig
from .models import GitHubPRRequest, GitHubPRResult, GitPushPreflightResult

logger = logging.getLogger("reporescue.github")


class GitHubError(Exception):
    """Base exception for GitHub API errors."""


class GitHubAuthError(GitHubError):
    """Raised when GitHub authentication fails (HTTP 401/403)."""


class GitHubNotFoundError(GitHubError):
    """Raised when GitHub repository or resource is not found (HTTP 404)."""


class GitHubRateLimitError(GitHubError):
    """Raised when GitHub API rate limit is exceeded."""


class GitHubValidationError(GitHubError):
    """Raised when GitHub API returns validation error (HTTP 422)."""


def _sanitize_message(message: str, secret: str | None) -> str:
    """Masks secret tokens and basic auth strings from messages to prevent leakage."""
    if not message:
        return ""
    sanitized = message
    if secret and len(secret) >= 4:
        sanitized = sanitized.replace(secret, "***")
        with contextlib.suppress(Exception):
            auth_bytes = f"x-access-token:{secret}".encode()
            b64_val = base64.b64encode(auth_bytes).decode("ascii")
            if b64_val in sanitized:
                sanitized = sanitized.replace(b64_val, "***")
    # Mask common token patterns as defense in depth
    sanitized = re.sub(r"gh[pousr]_[0-9a-zA-Z]{30,}", "***", sanitized)
    sanitized = re.sub(r"github_pat_[0-9a-zA-Z_]{60,}", "***", sanitized)
    return sanitized


class GitHubClientProtocol(Protocol):
    """Protocol for interacting with GitHub Pull Requests API."""

    def create_draft_pr(self, request: GitHubPRRequest) -> GitHubPRResult:
        """Creates a Draft Pull Request on GitHub."""
        ...

    def find_existing_pr(
        self, head_branch: str, base_branch: str | None = None
    ) -> GitHubPRResult | None:
        """Finds an existing Pull Request for the given branch if already open."""
        ...

    def check_auth_and_repo(self) -> dict[str, Any]:
        """Validates credentials and repository access permissions."""
        ...

    def preflight_git_receive_pack(self) -> GitPushPreflightResult:
        """Performs a safe preflight check for Git Smart HTTP push capability."""
        ...


class GitHubClient:
    """Official HTTP client for the GitHub REST API v3."""

    def __init__(self, config: GitHubConfig) -> None:
        self.config = config

    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Executes an authenticated HTTP request against GitHub REST API with secret masking."""
        url = f"{self.config.api_url}{endpoint}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "RepoRescue/0.1.0",
        }
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(
                req, timeout=self.config.push_timeout_seconds
            ) as response:
                content = response.read().decode("utf-8")
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as err:
            raw_err = err.read().decode("utf-8", "replace")
            sanitized_err = _sanitize_message(raw_err, self.config.token)
            sanitized_url = _sanitize_message(url, self.config.token)

            # Check for rate limit
            remaining = err.headers.get("x-ratelimit-remaining")
            if (
                err.code in (403, 429)
                and remaining == "0"
                or "rate limit" in sanitized_err.lower()
            ):
                raise GitHubRateLimitError(
                    f"GitHub API rate limit exceeded: {sanitized_err}"
                ) from err

            if err.code in (401, 403):
                raise GitHubAuthError(
                    f"GitHub authentication failed ({err.code}): {sanitized_err}"
                ) from err
            if err.code == 404:
                raise GitHubNotFoundError(
                    f"GitHub repository or resource not found at {sanitized_url}: {sanitized_err}"
                ) from err
            if err.code == 422:
                raise GitHubValidationError(
                    f"GitHub validation error ({err.code}): {sanitized_err}"
                ) from err
            raise GitHubError(
                f"GitHub API error ({err.code}) on {sanitized_url}: {sanitized_err}"
            ) from err
        except urllib.error.URLError as err:
            sanitized_reason = _sanitize_message(str(err.reason), self.config.token)
            raise GitHubError(
                f"Failed to connect to GitHub at {self.config.api_url}: {sanitized_reason}"
            ) from err

    def check_auth_and_repo(self) -> dict[str, Any]:
        """Validates credentials and repository access permissions."""
        if not self.config.repository:
            raise GitHubValidationError(
                "Cannot check repository: repository is not configured."
            )
        endpoint = f"/repos/{self.config.repository}"
        return self._make_request(endpoint, method="GET")

    def preflight_git_receive_pack(self) -> GitPushPreflightResult:
        """Performs a safe, read-only preflight check for Git Smart HTTP push capability.

        Queries GET /info/refs?service=git-receive-pack to verify that the token
        holds Git data write permissions (e.g. Contents: Read & write or repo scope),
        distinguishing account-level repository roles from actual token push authorization.
        """
        if not self.config.repository:
            return GitPushPreflightResult(
                api_authenticated=False,
                api_push_permission=False,
                git_smart_http_authorized=False,
                error_message="Repository is not configured.",
            )

        # 1. Verify REST API connectivity & account permissions
        api_username: str | None = None
        api_push = False

        try:
            repo_info = self.check_auth_and_repo()
            perms = repo_info.get("permissions", {})
            api_push = bool(perms.get("push", False))
        except GitHubError as err:
            return GitPushPreflightResult(
                api_authenticated=False,
                api_push_permission=False,
                git_smart_http_authorized=False,
                error_message=str(err),
            )

        with contextlib.suppress(Exception):
            user_data = self._make_request("/user", method="GET")
            api_username = user_data.get("login")

        if self.config.dry_run:
            return GitPushPreflightResult(
                api_authenticated=True,
                api_username=api_username or "dry-run-user",
                api_push_permission=api_push,
                git_smart_http_authorized=True,
                status_code=200,
            )

        # 2. Check Git Smart HTTP receive-pack service advertisement
        repo_url = f"https://github.com/{self.config.repository}.git/info/refs?service=git-receive-pack"
        headers = {
            "User-Agent": "git/2.43.0 (RepoRescue-Preflight)",
            "Accept": "application/x-git-receive-pack-advertisement",
        }
        user = self.config.token_username or "x-access-token"
        if self.config.token:
            auth_bytes = f"{user}:{self.config.token}".encode()
            basic_auth = base64.b64encode(auth_bytes).decode("ascii")
            headers["Authorization"] = f"Basic {basic_auth}"

        guidance = (
            "Root Cause: The configured GitHub token lacks Git write authorization for this repository.\n"
            "  • Fine-Grained Personal Access Token:\n"
            "    Verify 'Repository permissions' -> 'Contents' is set to 'Read and write'.\n"
            "  • Classic Personal Access Token:\n"
            "    Verify the 'repo' scope is enabled (Full control of private repositories).\n"
            "  • Organization / SAML Single Sign-On:\n"
            "    Verify the token is authorized for SAML SSO in your organization."
        )

        req = urllib.request.Request(repo_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                req, timeout=self.config.push_timeout_seconds
            ) as resp:
                content_type = resp.headers.get("Content-Type", "")
                body = resp.read().decode("utf-8", "replace")
                if resp.status == 200 and (
                    "git-receive-pack" in body or "git-receive-pack" in content_type
                ):
                    target_repo = self.config.source_repository_path or Path.cwd()
                    if (target_repo / ".git").exists() and not self.config.dry_run:
                        from ..runner import run_command

                        test_env = {
                            "GIT_TERMINAL_PROMPT": "0",
                            "GCM_INTERACTIVE": "never",
                        }
                        if self.config.token:
                            test_env["GIT_CONFIG_PARAMETERS"] = (
                                f"'credential.helper=' 'http.extraHeader=Authorization: Basic {basic_auth}'"
                            )
                            test_env["GITHUB_TOKEN"] = self.config.token
                            test_env["GH_TOKEN"] = self.config.token
                        dry_res = run_command(
                            [
                                "git",
                                "push",
                                "--dry-run",
                                f"https://github.com/{self.config.repository}.git",
                                "HEAD:refs/heads/reporescue-preflight-test",
                            ],
                            cwd=target_repo,
                            env=test_env,
                            timeout_seconds=15.0,
                        )
                        if not dry_res.passed and any(
                            x in dry_res.stderr.lower()
                            for x in ("403", "denied", "permission", "unauthorized")
                        ):
                            sanitized = _sanitize_message(
                                dry_res.stderr, self.config.token
                            )
                            return GitPushPreflightResult(
                                api_authenticated=True,
                                api_username=api_username,
                                api_push_permission=api_push,
                                git_smart_http_authorized=False,
                                status_code=403,
                                error_message=f"Git push subprocess denied (HTTP 403): {sanitized.strip()}",
                                diagnostic_guidance=guidance,
                            )

                    return GitPushPreflightResult(
                        api_authenticated=True,
                        api_username=api_username,
                        api_push_permission=api_push,
                        git_smart_http_authorized=True,
                        status_code=200,
                    )
                return GitPushPreflightResult(
                    api_authenticated=True,
                    api_username=api_username,
                    api_push_permission=api_push,
                    git_smart_http_authorized=False,
                    status_code=resp.status,
                    error_message=f"Unexpected Git Smart HTTP response (HTTP {resp.status})",
                )
        except urllib.error.HTTPError as err:
            raw_err = err.read().decode("utf-8", "replace")
            sanitized_err = _sanitize_message(raw_err, self.config.token)
            if not sanitized_err.strip():
                sanitized_err = f"HTTP {err.code}: {err.reason}"

            guidance = None
            if err.code == 403:
                guidance = (
                    "Root Cause: The configured Personal Access Token lacks Git write authorization for this repository.\n"
                    "  • Fine-Grained Personal Access Token:\n"
                    "    Verify 'Repository permissions' -> 'Contents' is set to 'Read and write'.\n"
                    "  • Classic Personal Access Token:\n"
                    "    Verify the 'repo' scope is enabled (Full control of private repositories).\n"
                    "  • Organization / SAML Single Sign-On:\n"
                    "    Verify the token is authorized for SAML SSO in your organization."
                )

            return GitPushPreflightResult(
                api_authenticated=True,
                api_username=api_username,
                api_push_permission=api_push,
                git_smart_http_authorized=False,
                status_code=err.code,
                error_message=f"Git Smart HTTP push permission denied (HTTP {err.code}): {sanitized_err.strip()}",
                diagnostic_guidance=guidance,
            )
        except urllib.error.URLError as err:
            sanitized_reason = _sanitize_message(str(err.reason), self.config.token)
            return GitPushPreflightResult(
                api_authenticated=True,
                api_username=api_username,
                api_push_permission=api_push,
                git_smart_http_authorized=False,
                status_code=0,
                error_message=f"Failed to connect to Git Smart HTTP service: {sanitized_reason}",
            )

    def find_existing_pr(
        self, head_branch: str, base_branch: str | None = None
    ) -> GitHubPRResult | None:
        """Finds an existing Pull Request for the head branch if one exists."""
        if not self.config.repository:
            raise GitHubValidationError(
                "Cannot search PRs: repository is not configured."
            )

        owner = self.config.repository.split("/")[0]
        # GitHub search format: head=owner:branch
        head_query = (
            f"{owner}:{head_branch}"
            if not head_branch.startswith(f"{owner}:")
            else head_branch
        )
        base = base_branch or self.config.base_branch

        endpoint = f"/repos/{self.config.repository}/pulls?head={head_query}&base={base}&state=all"
        try:
            res = self._make_request(endpoint, method="GET")
            prs = (
                res
                if isinstance(res, list)
                else (res.get("items", []) if isinstance(res, dict) else [])
            )
            if prs and isinstance(prs[0], dict):
                pr = prs[0]
                return GitHubPRResult(
                    pr_url=pr.get("url", ""),
                    pr_number=int(pr.get("number", 0)),
                    html_url=pr.get("html_url", ""),
                    branch_name=head_branch,
                    base_branch=base,
                    commit_sha=pr.get("head", {}).get("sha", ""),
                    title=pr.get("title", ""),
                    patch_fingerprint="",
                    draft=bool(pr.get("draft", True)),
                    is_existing=True,
                    created_at=pr.get("created_at", ""),
                )
        except (GitHubError, OSError) as err:
            logger.debug(
                "Existing PR lookup failed: %s",
                _sanitize_message(str(err), self.config.token),
            )
        return None

    def create_draft_pr(self, request: GitHubPRRequest) -> GitHubPRResult:
        """Creates a DRAFT Pull Request on GitHub.

        Strict Safety Invariant: draft is always enforced as True.
        """
        if not self.config.repository:
            raise GitHubValidationError(
                "Cannot create PR: repository is not configured."
            )

        # 1. Check if PR already exists before attempting creation
        existing = self.find_existing_pr(request.head_branch, request.base_branch)
        if existing:
            logger.info(
                "Found existing Pull Request #%d for branch '%s': %s",
                existing.pr_number,
                request.head_branch,
                existing.html_url,
            )
            return existing

        payload = {
            "title": request.title,
            "body": request.body,
            "head": request.head_branch,
            "base": request.base_branch or self.config.base_branch,
            "draft": True,  # Strict Safety Invariant
        }

        endpoint = f"/repos/{self.config.repository}/pulls"
        try:
            res = self._make_request(endpoint, method="POST", body=payload)
            return GitHubPRResult(
                pr_url=res.get("url", ""),
                pr_number=int(res.get("number", 0)),
                html_url=res.get("html_url", ""),
                branch_name=request.head_branch,
                base_branch=request.base_branch,
                commit_sha=res.get("head", {}).get("sha", ""),
                title=res.get("title", request.title),
                patch_fingerprint="",
                draft=True,
                is_existing=False,
                created_at=res.get("created_at", ""),
            )
        except GitHubValidationError as err:
            # If GitHub rejected because a PR already exists, attempt resolution
            if "already exists" in str(err).lower():
                existing = self.find_existing_pr(
                    request.head_branch, request.base_branch
                )
                if existing:
                    return existing
            raise


class MockGitHubClient:
    """Deterministic in-memory GitHub client for unit testing and offline demo workflows."""

    def __init__(self, config: GitHubConfig | None = None) -> None:
        self.config = config or GitHubConfig(
            token="mock_gh_token",
            repository="switch-hackathon/RepoRescue",
            dry_run=True,
        )
        self.created_prs: list[GitHubPRResult] = []
        self.should_fail_auth: bool = False
        self.should_fail_not_found: bool = False
        self.should_rate_limit: bool = False
        self.simulate_receive_pack_403: bool = False

    def preflight_git_receive_pack(self) -> GitPushPreflightResult:
        """Simulates Git Smart HTTP receive-pack preflight check."""
        if self.should_fail_auth:
            return GitPushPreflightResult(
                api_authenticated=False,
                api_push_permission=False,
                git_smart_http_authorized=False,
                status_code=401,
                error_message="Authentication failed (HTTP 401).",
            )
        if self.simulate_receive_pack_403:
            return GitPushPreflightResult(
                api_authenticated=True,
                api_username="mock-user",
                api_push_permission=True,
                git_smart_http_authorized=False,
                status_code=403,
                error_message=f"Git Smart HTTP push permission denied (HTTP 403) on '{self.config.repository}.git'.",
                diagnostic_guidance=(
                    "Root Cause: The configured Personal Access Token lacks Git write authorization for this repository.\n"
                    "  • Fine-Grained Personal Access Token:\n"
                    "    Verify 'Repository permissions' -> 'Contents' is set to 'Read and write'."
                ),
            )
        return GitPushPreflightResult(
            api_authenticated=True,
            api_username="mock-user",
            api_push_permission=True,
            git_smart_http_authorized=True,
            status_code=200,
        )

    def check_auth_and_repo(self) -> dict[str, Any]:
        """Simulates GitHub repo metadata verification."""
        if self.should_fail_auth:
            raise GitHubAuthError("Simulated 401: Bad credentials")
        if self.should_fail_not_found:
            raise GitHubNotFoundError(
                f"Simulated 404: Not found {self.config.repository}"
            )
        if self.should_rate_limit:
            raise GitHubRateLimitError("Simulated 403: API rate limit exceeded")

        repo_name = (self.config.repository or "switch-hackathon/RepoRescue").split(
            "/"
        )[-1]
        return {
            "name": repo_name,
            "full_name": self.config.repository or "switch-hackathon/RepoRescue",
            "default_branch": self.config.base_branch,
            "permissions": {"push": True, "pull": True, "admin": False},
        }

    def find_existing_pr(
        self, head_branch: str, base_branch: str | None = None
    ) -> GitHubPRResult | None:
        """Finds simulated existing PR in memory."""
        for pr in self.created_prs:
            if pr.branch_name == head_branch:
                return pr
        return None

    def create_draft_pr(self, request: GitHubPRRequest) -> GitHubPRResult:
        """Simulates Draft PR creation in memory."""
        if self.should_fail_auth:
            raise GitHubAuthError("Simulated 401: Bad credentials")
        if self.should_fail_not_found:
            raise GitHubNotFoundError(
                f"Simulated 404: Not found {self.config.repository}"
            )
        if self.should_rate_limit:
            raise GitHubRateLimitError("Simulated 403: API rate limit exceeded")

        # Invariant check
        if not request.draft:
            raise GitHubValidationError(
                "Safety invariant violation: Draft must be True"
            )

        existing = self.find_existing_pr(request.head_branch, request.base_branch)
        if existing:
            return existing

        pr_num = len(self.created_prs) + 1
        repo = self.config.repository or "switch-hackathon/RepoRescue"
        html_url = f"https://github.com/{repo}/pull/{pr_num}"
        pr_url = f"https://api.github.com/repos/{repo}/pulls/{pr_num}"

        res = GitHubPRResult(
            pr_url=pr_url,
            pr_number=pr_num,
            html_url=html_url,
            branch_name=request.head_branch,
            base_branch=request.base_branch,
            commit_sha=f"mock_sha_{pr_num:04d}",
            title=request.title,
            patch_fingerprint="",
            draft=True,
            is_existing=False,
        )
        self.created_prs.append(res)
        return res
