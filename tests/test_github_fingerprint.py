from __future__ import annotations

from pathlib import Path

from reporescue.github.fingerprint import (
    compute_patch_fingerprint,
    compute_worktree_patch_fingerprint,
    verify_patch_fingerprint,
)
from reporescue.runner import run_command


def test_fingerprint_deterministic_and_line_ending_invariant():
    diff_crlf = (
        "--- a/file.py\r\n+++ b/file.py\r\n@@ -1 +1 @@\r\n-old line   \r\n+new line\r\n"
    )
    diff_lf = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old line\n+new line\n"

    fp_crlf = compute_patch_fingerprint(diff_crlf)
    fp_lf = compute_patch_fingerprint(diff_lf)

    assert fp_crlf.startswith("sha256:")
    assert fp_lf.startswith("sha256:")
    assert fp_crlf == fp_lf


def test_fingerprint_tamper_detection():
    diff1 = "--- a/file.py\n+++ b/file.py\n+def safe_fix(): pass\n"
    diff2 = "--- a/file.py\n+++ b/file.py\n+def backdoor(): pass\n"

    fp1 = compute_patch_fingerprint(diff1)
    fp2 = compute_patch_fingerprint(diff2)

    assert fp1 != fp2
    assert verify_patch_fingerprint(diff1, fp1) is True
    assert verify_patch_fingerprint(diff2, fp1) is False


def test_compute_worktree_patch_fingerprint(tmp_path: Path):
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Initialize a Git repository with an initial commit
    run_command(["git", "init"], cwd=repo)
    run_command(["git", "config", "user.name", "Test User"], cwd=repo)
    run_command(["git", "config", "user.email", "test@example.com"], cwd=repo)

    test_file = repo / "main.py"
    test_file.write_text("initial content\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=repo)
    run_command(["git", "commit", "-m", "initial commit"], cwd=repo)

    # Make a fix commit
    test_file.write_text("fixed content\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=repo)
    run_command(["git", "commit", "-m", "fix: apply patch"], cwd=repo)

    fp, diff_text, commit_sha = compute_worktree_patch_fingerprint(repo)

    assert fp.startswith("sha256:")
    assert "fixed content" in diff_text
    assert len(commit_sha) == 40
    assert verify_patch_fingerprint(diff_text, fp) is True
