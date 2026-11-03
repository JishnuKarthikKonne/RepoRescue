from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Protocol

from .config import GitHubConfig
from .models import GitHubPRRequest, GitHubPRResult

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
    """Masks secret token from strings and error messages to prevent leakage."""
    if not secret or len(secret) < 4:
        return message
    return message.replace(secret, "***")


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
