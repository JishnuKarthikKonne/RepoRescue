from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class GitHubPRRequest:
    """Request payload to create a GitHub Draft Pull Request."""

    task_id: str
    title: str
    body: str
    head_branch: str
    base_branch: str = "main"
    draft: bool = True


@dataclass(frozen=True)
class GitHubPRResult:
    """Result returned after successful Draft Pull Request creation or discovery."""

    pr_url: str
    pr_number: int
    html_url: str
    branch_name: str
    base_branch: str
    commit_sha: str
    title: str
    patch_fingerprint: str
    draft: bool = True
    is_existing: bool = False
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(self, "created_at", datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class GitPushPreflightResult:
    """Detailed preflight assessment distinguishing REST API role from Git Smart HTTP authorization."""

    api_authenticated: bool
    api_username: str | None = None
    api_push_permission: bool = False
    git_smart_http_authorized: bool = False
    status_code: int = 200
    error_message: str | None = None
    diagnostic_guidance: str | None = None
