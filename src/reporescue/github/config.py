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
    url = url.strip()

    # SSH format: git@github.com:owner/repo.git or ssh://git@github.com/owner/repo.git
    ssh_match = re.match(
        r"^(?:ssh://)?git@[\w.-]+[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$", url
    )
    if ssh_match:
        return f"{ssh_match.group(1)}/{ssh_match.group(2)}"

    # HTTPS format: https://[user:token@]github.com/owner/repo[.git]
    https_match = re.match(
        r"^https?://(?:[^@]+@)?[\w.-]+/([^/]+)/([^/.]+)(?:\.git)?/?$", url
    )
    if https_match:
        return f"{https_match.group(1)}/{https_match.group(2)}"

    return None


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

    @classmethod
    def from_env(cls, repo_path: Path | str | None = None) -> GitHubConfig:
        """Constructs configuration from environment variables and local git context."""
        token = (
            os.getenv("GITHUB_TOKEN", "").strip()
            or os.getenv("GH_TOKEN", "").strip()
            or None
        )
        repository = (
            os.getenv("GITHUB_REPOSITORY", "").strip()
            or os.getenv("REPORESCUE_GITHUB_REPO", "").strip()
            or None
        )
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

        allowlist_raw = os.getenv("GITHUB_REPO_ALLOWLIST", "").strip()
        repository_allowlist = (
            [r.strip() for r in allowlist_raw.split(",") if r.strip()]
            if allowlist_raw
            else []
        )

        # Fallback to local git remote if repository is not explicitly set
        if not repository and repo_path:
            try:
                from ..runner import run_command

                res = run_command(
                    ["git", "remote", "get-url", "origin"],
                    cwd=repo_path,
                    timeout_seconds=5.0,
                )
                if res.passed and res.stdout.strip():
                    parsed = parse_repo_from_remote_url(res.stdout.strip())
                    if parsed:
                        repository = parsed
            except (OSError, RuntimeError, ValueError) as err:
                import logging

                logging.getLogger("reporescue.github.config").debug(
                    "Failed to detect remote origin: %s", err
                )

        return cls(
            token=token,
            repository=repository,
            base_branch=base_branch,
            api_url=api_url,
            draft=True,  # Safety invariant: strictly enforced
            repository_allowlist=repository_allowlist,
            dry_run=dry_run,
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
            return "***"
        prefix = self.token[:4]
        suffix = self.token[-4:]
        return f"{prefix}...{suffix}"

    def validate(self) -> list[str]:
        """Validates configuration and returns any error messages."""
        errors: list[str] = []

        if not self.dry_run and not self.token:
            errors.append(
                "GitHub token is missing. Set GITHUB_TOKEN or GH_TOKEN environment variable."
            )

        if not self.repository:
            errors.append(
                "GitHub repository is missing. Set GITHUB_REPOSITORY (e.g. 'owner/repo') or configure git remote origin."
            )
        elif not re.match(r"^[\w.-]+/[\w.-]+$", self.repository):
            errors.append(
                f"Invalid GitHub repository format: '{self.repository}'. Expected 'owner/repo'."
            )
        elif self.repository_allowlist:
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
