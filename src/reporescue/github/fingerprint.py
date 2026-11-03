from __future__ import annotations

import hashlib
from pathlib import Path


def compute_patch_fingerprint(diff_text: str | bytes) -> str:
    """Computes a stable SHA-256 cryptographic fingerprint of a Git patch.

    Normalizes line endings (CRLF -> LF) and trims trailing whitespace on each line
    to ensure deterministic hashes across operating systems and Git line-ending configurations.

    Args:
        diff_text: The diff string or bytes to fingerprint.

    Returns:
        Hex-encoded SHA-256 fingerprint prefixed with 'sha256:'.
    """
    if isinstance(diff_text, bytes):
        text = diff_text.decode("utf-8", errors="replace")
    else:
        text = str(diff_text)

    # Normalize line endings and whitespace per line
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").splitlines()]
    normalized = "\n".join(lines).strip()

    sha = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{sha}"


def verify_patch_fingerprint(diff_text: str | bytes, expected_fingerprint: str) -> bool:
    """Validates that diff_text matches the expected cryptographic patch fingerprint."""
    if not expected_fingerprint:
        return False
    actual = compute_patch_fingerprint(diff_text)
    return actual == expected_fingerprint


def compute_worktree_patch_fingerprint(
    worktree_path: Path | str,
    base_ref: str | None = None,
) -> tuple[str, str, str]:
    """Inspects a worktree, extracts the committed diff against base_ref, and computes fingerprint.

    Returns:
        tuple[str, str, str]: (patch_fingerprint, diff_text, commit_sha)
    """
    from ..runner import run_command

    wt_path = Path(worktree_path).resolve()

    # Get HEAD commit hash
    head_res = run_command(["git", "rev-parse", "HEAD"], cwd=wt_path)
    commit_sha = head_res.stdout.strip() if head_res.passed else ""

    # Determine diff against base_ref (or parent commit HEAD~1)
    diff_cmd = (
        ["git", "diff", f"{base_ref}..HEAD"]
        if base_ref
        else ["git", "diff", "HEAD~1", "HEAD"]
    )
    diff_res = run_command(diff_cmd, cwd=wt_path)

    # If HEAD~1 fails (e.g. root commit) or produces empty diff, inspect uncommitted or show HEAD
    diff_text = (
        diff_res.stdout.strip() if diff_res.passed and diff_res.stdout.strip() else ""
    )
    if not diff_text:
        show_res = run_command(["git", "show", "--format=", "HEAD"], cwd=wt_path)
        diff_text = show_res.stdout.strip() if show_res.passed else ""

    fingerprint = compute_patch_fingerprint(diff_text)
    return fingerprint, diff_text, commit_sha
