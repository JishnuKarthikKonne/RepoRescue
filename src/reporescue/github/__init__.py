"""RepoRescue GitHub Integration Package.

Provides secure, human-gated GitHub Draft Pull Request integration for verified bug fixes.
"""

from .client import GitHubClient, GitHubClientProtocol, MockGitHubClient
from .config import GitHubConfig
from .fingerprint import compute_patch_fingerprint, verify_patch_fingerprint
from .manager import GitHubPRManager, PatchTamperedError, SafetyGateViolationError
from .models import GitHubPRRequest, GitHubPRResult

__all__ = [
    "GitHubClient",
    "GitHubClientProtocol",
    "GitHubConfig",
    "GitHubPRManager",
    "GitHubPRRequest",
    "GitHubPRResult",
    "MockGitHubClient",
    "PatchTamperedError",
    "SafetyGateViolationError",
    "compute_patch_fingerprint",
    "verify_patch_fingerprint",
]
