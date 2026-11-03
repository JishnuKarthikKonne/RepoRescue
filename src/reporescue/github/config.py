from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def parse_repo_from_remote_url(url: str) -> str | None:
    """Extracts owner/repo from a GitHub Git remote URL.

    Supports:
    - https://github.com/owner/repo.git
    - https://github.com/owner/repo
    - git@github.com:owner/repo.git
    - ssh://git@github.com/owner/repo.git
    """
    if not url or not isinstance(url, str):
        return None
    clean = url.strip().rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]

    # SSH format: git@github.com:owner/repo or ssh://git@github.com/owner/repo
    ssh_match = re.match(r"^(?:ssh://)?git@[\w.-]+[:/]([^/]+)/([^/]+)$", clean)
    if ssh_match:
        return f"{ssh_match.group(1)}/{ssh_match.group(2)}"

    # HTTPS format: https://[user:token@]github.com/owner/repo
    https_match = re.match(r"^https?://(?:[^@]+@)?[\w.-]+/([^/]+)/([^/]+)$", clean)
    if https_match:
        return f"{https_match.group(1)}/{https_match.group(2)}"

    return None


def is_demo_repository(path: Path | str | None) -> bool:
    """Returns True if the path belongs to or is inside the isolated demo repository."""
    if not path:
        return False
    resolved = Path(path).resolve().as_posix().lower()
    return "/.reporescue/demo_repo" in resolved or resolved.endswith("/demo_repo")


def detect_repository_origin(repo_path: Path | str | None) -> str | None:
    """Extracts owner/repo from git remote origin of the specified repository path."""
    if not repo_path:
        return None
    try:
        from ..runner import run_command

        res = run_command(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path,
            timeout_seconds=5.0,
        )
        if res.passed and res.stdout.strip():
            return parse_repo_from_remote_url(res.stdout.strip())
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def get_orchestrator_repository(anchor_path: Path | str | None = None) -> str | None:
    """Detects the GitHub repository of the RepoRescue orchestrator codebase itself."""
    candidate = Path(anchor_path).resolve() if anchor_path else Path.cwd().resolve()
    while candidate.parent != candidate:
        if (candidate / ".git").exists() and not is_demo_repository(candidate):
            origin = detect_repository_origin(candidate)
            if origin:
                return origin
        candidate = candidate.parent
    return None


def resolve_gcm_token(
    repo_path: Path | str | None = None,
) -> tuple[str | None, str | None]:
    """Resolves (username, token) from Git Credential Manager via `git credential fill`."""
    try:
        from ..runner import run_command

        res = run_command(
            ["git", "credential", "fill"],
            cwd=repo_path or Path.cwd(),
            input="protocol=https\nhost=github.com\n\n",
            timeout_seconds=5.0,
        )
        if res.passed and res.stdout.strip():
            data: dict[str, str] = {}
            for line in res.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
            username = data.get("username")
            password = data.get("password")
            if password:
                return username, password
    except (OSError, RuntimeError, ValueError):
        pass
    return None, None


@dataclass
class GitHubConfig:
    """Configuration for GitHub Pull Request publishing."""

    token: str | None = None
    repository: str | None = None  # Format: "owner/repo"
    base_branch: str = "main"
    api_url: str = "https://api.github.com"
    draft: bool = True
    push_timeout_seconds: float = 60.0
    repository_allowlist: list[str] = field(default_factory=list)
    dry_run: bool = False
    is_demo: bool = False
    orchestrator_repository: str | None = None
    token_source: str | None = None
    token_username: str | None = None
    source_repository_path: Path | None = None
    source_repository_origin: str | None = None

    @classmethod
    def from_env(
        cls,
        repo_path: Path | str | None = None,
        *,
        is_demo: bool | None = None,
    ) -> GitHubConfig:
        """Constructs configuration from environment variables and local git context.

        Strictly enforces multi-repository isolation: demo repositories never inherit
        the parent orchestrator repository target.
        """
        base_branch = (
            os.getenv("GITHUB_BASE_BRANCH", "").strip()
            or os.getenv("REPORESCUE_BASE_BRANCH", "").strip()
            or "main"
        )
        api_url = (
            os.getenv("GITHUB_API_URL", "https://api.github.com").strip().rstrip("/")
        )
        dry_run = os.getenv("GITHUB_DRY_RUN", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )

        cred_pref = os.getenv("GITHUB_CREDENTIAL_SOURCE", "").strip().lower()
        token: str | None = None
        token_source: str | None = None
        token_username: str | None = None

        if cred_pref == "gcm":
            gcm_user, gcm_token = resolve_gcm_token(repo_path)
            if gcm_token:
                token = gcm_token
                token_source = "gcm"
                token_username = gcm_user
        elif cred_pref == "env":
            token = (
                os.getenv("GITHUB_TOKEN", "").strip()
                or os.getenv("GH_TOKEN", "").strip()
                or None
            )
            token_source = "environment" if token else None
        else:
            token = (
                os.getenv("GITHUB_TOKEN", "").strip()
                or os.getenv("GH_TOKEN", "").strip()
                or None
            )
            token_source = "environment" if token else None

            # Fallback to local Git Credential Manager if no explicit token is in environment
            if not token and not dry_run:
                gcm_user, gcm_token = resolve_gcm_token(repo_path)
                if gcm_token:
                    token = gcm_token
                    token_source = "gcm"
                    token_username = gcm_user

        allowlist_raw = os.getenv("GITHUB_REPO_ALLOWLIST", "").strip()
        repository_allowlist = (
            [r.strip() for r in allowlist_raw.split(",") if r.strip()]
            if allowlist_raw
            else []
        )

        # Detect demo mode
        demo_detected = (
            is_demo if is_demo is not None else is_demo_repository(repo_path)
        )
        target_path: Path | None = Path(repo_path).resolve() if repo_path else None
        source_origin: str | None = None
        orchestrator_repo: str | None = None

        if target_path is not None:
            source_origin = detect_repository_origin(target_path)
            orchestrator_repo = get_orchestrator_repository(target_path)
        else:
            # When repo_path is not explicitly provided, check if GITHUB_REPOSITORY is unset
            env_repo_check = (
                os.getenv("GITHUB_REPOSITORY", "").strip()
                or os.getenv("REPORESCUE_GITHUB_REPO", "").strip()
                or None
            )
            if not env_repo_check and not demo_detected:
                cwd_path = Path.cwd().resolve()
                cwd_origin = detect_repository_origin(cwd_path)
                if cwd_origin:
                    target_path = cwd_path
                    source_origin = cwd_origin
                    orchestrator_repo = get_orchestrator_repository(cwd_path)

        repository: str | None = None
        if demo_detected:
            # Multi-repository isolation: Demo repo uses DEMO_GITHUB_REPOSITORY or its own git origin
            demo_env_repo = os.getenv("DEMO_GITHUB_REPOSITORY", "").strip() or None
            if demo_env_repo:
                repository = demo_env_repo
            else:
                repository = source_origin
            # Never inherit parent orchestrator repo for demo
        else:
            # Standard task: GITHUB_REPOSITORY or local git remote origin
            env_repo = (
                os.getenv("GITHUB_REPOSITORY", "").strip()
                or os.getenv("REPORESCUE_GITHUB_REPO", "").strip()
                or None
            )
            if env_repo:
                repository = env_repo
            else:
                repository = source_origin

        return cls(
            token=token,
            repository=repository,
            base_branch=base_branch,
            api_url=api_url,
            draft=True,  # Safety invariant: strictly enforced
            repository_allowlist=repository_allowlist,
            dry_run=dry_run,
            is_demo=demo_detected,
            orchestrator_repository=orchestrator_repo,
            token_source=token_source,
            token_username=token_username,
            source_repository_path=target_path,
            source_repository_origin=source_origin,
        )

    @property
    def is_configured(self) -> bool:
        """Returns True if minimum configuration requirements are met."""
        return bool(self.repository and (self.token or self.dry_run))

    @property
    def masked_token(self) -> str:
        """Returns a securely masked representation of the token for logging/diagnostics."""
        if not self.token:
            return "<none>"
        if len(self.token) <= 8:
            prefix_str = "***"
        else:
            prefix_str = f"{self.token[:4]}...{self.token[-4:]}"
        if self.token_source == "gcm":
            user_lbl = f" for '{self.token_username}'" if self.token_username else ""
            return f"{prefix_str} (Windows Git Credential Manager{user_lbl})"
        if self.token_source == "environment":
            return f"{prefix_str} (Explicit Token)"
        return prefix_str

    def validate(self) -> list[str]:
        """Validates configuration and returns any error messages."""
        errors: list[str] = []

        if not self.dry_run and not self.token:
            errors.append(
                "GitHub token is missing. Set GITHUB_TOKEN or GH_TOKEN environment variable."
            )

        if not self.repository:
            if self.is_demo:
                errors.append(
                    "Demo repository (.reporescue/demo_repo) has no configured GitHub remote origin. "
                    "The demo coupon patch cannot be published until an independent GitHub repository "
                    "is configured (set DEMO_GITHUB_REPOSITORY or configure git remote origin)."
                )
            else:
                errors.append(
                    "GitHub repository is missing. Set GITHUB_REPOSITORY (e.g. 'owner/repo') or configure git remote origin."
                )
        elif not re.match(r"^[\w.-]+/[\w.-]+$", self.repository):
            errors.append(
                f"Invalid GitHub repository format: '{self.repository}'. Expected 'owner/repo'."
            )
        else:
            if (
                self.is_demo
                and self.orchestrator_repository
                and self.repository.lower() == self.orchestrator_repository.lower()
            ):
                errors.append(
                    f"Repository Isolation Violation: Demo repository patches cannot be published "
                    f"to the RepoRescue orchestrator repository ('{self.repository}'). "
                    f"Set DEMO_GITHUB_REPOSITORY to an independent demo repository target."
                )
            if (
                not self.is_demo
                and self.source_repository_origin
                and self.source_repository_origin.lower() != self.repository.lower()
            ):
                errors.append(
                    f"Repository Identity Mismatch: Local git remote origin '{self.source_repository_origin}' "
                    f"does not match configured target GitHub repository '{self.repository}'."
                )
            if self.repository_allowlist:
                # Check allowlist compliance
                matched = any(
                    self.repository.lower() == allowed.lower()
                    for allowed in self.repository_allowlist
                )
                if not matched:
                    errors.append(
                        f"Repository '{self.repository}' is not permitted by GITHUB_REPO_ALLOWLIST: {self.repository_allowlist}"
                    )

        if not self.base_branch or not re.match(r"^[\w.-]+$", self.base_branch):
            errors.append(f"Invalid base branch name: '{self.base_branch}'")

        if not self.draft:
            errors.append(
                "Safety invariant violation: RepoRescue PRs must strictly remain in draft state."
            )

        return errors
