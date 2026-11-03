from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reporescue.github.client import GitHubClient, MockGitHubClient
from reporescue.github.config import GitHubConfig
from reporescue.github.fingerprint import compute_worktree_patch_fingerprint
from reporescue.github.manager import (
    GitHubPRManager,
    SafetyGateViolationError,
)
from reporescue.models import BugTask, TaskStatus
from reporescue.runner import run_command


@pytest.fixture
def isolated_git_worktree(tmp_path: Path):
    """Sets up an isolated Git repo and worktree."""
    repo = tmp_path / "app_repo"
    repo.mkdir()
    run_command(["git", "init"], cwd=repo)
    run_command(["git", "config", "user.name", "Test Dev"], cwd=repo)
    run_command(["git", "config", "user.email", "dev@example.com"], cwd=repo)

    file_a = repo / "coupon.py"
    file_a.write_text("def apply_coupon(): return 0\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=repo)
    run_command(["git", "commit", "-m", "initial coupon commit"], cwd=repo)

    wt_dir = tmp_path / "worktrees"
    wt_dir.mkdir()
    branch = "reporescue/task-iso001"
    wt_path = wt_dir / "wt-iso001"
    run_command(
        ["git", "worktree", "add", "-b", branch, str(wt_path), "HEAD"], cwd=repo
    )

    file_wt = wt_path / "coupon.py"
    file_wt.write_text("def apply_coupon(): return 20\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=wt_path)
    run_command(["git", "commit", "-m", "fix: apply coupon discount"], cwd=wt_path)

    return repo, wt_path, branch


def _create_approved_task(task_id: str, branch: str, fp: str) -> BugTask:
    task = BugTask(
        title="Coupon calculation error",
        description="Coupon does not discount properly",
        reporter="tester",
        task_id=task_id,
        status=TaskStatus.REPORTED,
    )
    task.proposed_branch = branch
    task.evidence.append("Verification PASSED: 3 passed in 0.2s")
    task.verified_patch_fingerprint = fp

    for st in [
        TaskStatus.TRIAGED,
        TaskStatus.REPRODUCING,
        TaskStatus.REPRODUCED,
        TaskStatus.FIX_PROPOSED,
        TaskStatus.VERIFYING,
        TaskStatus.VERIFIED,
        TaskStatus.AWAITING_APPROVAL,
    ]:
        task.transition(st, actor="system")
    task.transition(
        TaskStatus.APPROVED, actor="LeadEngineer", timestamp=datetime.now(UTC)
    )
    task.approved_patch_fingerprint = fp
    return task


def test_worktree_origin_mismatch_fails_gate_k(isolated_git_worktree):
    """Verifies that if a worktree origin remote does not match the target repository, Gate K fails."""
    repo, wt_path, branch = isolated_git_worktree
    run_command(
        ["git", "remote", "add", "origin", "https://github.com/my-org/my-app.git"],
        cwd=repo,
    )

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task("task-iso-1", branch, fp)

    # Config points to a different repository than origin
    cfg = GitHubConfig(
        repository="other-org/other-app",
        token="ghp_test_token_12345",
        dry_run=True,
    )
    mgr = GitHubPRManager(config=cfg, client=MockGitHubClient(cfg))

    with pytest.raises(
        SafetyGateViolationError, match="Gate K Failed: Repository Isolation Violation"
    ):
        mgr.publish_draft_pr(task, wt_path)


def test_demo_repo_targeting_orchestrator_fails_gate_k(tmp_path):
    """Verifies that demo repository patches cannot be published to the orchestrator repository."""
    demo_repo = tmp_path / ".reporescue" / "demo_repo"
    demo_repo.mkdir(parents=True)
    run_command(["git", "init"], cwd=demo_repo)
    run_command(["git", "config", "user.name", "Test Dev"], cwd=demo_repo)
    run_command(["git", "config", "user.email", "dev@example.com"], cwd=demo_repo)

    main_f = demo_repo / "shoppy.py"
    main_f.write_text("def checkout(): pass\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=demo_repo)
    run_command(["git", "commit", "-m", "init shoppy"], cwd=demo_repo)

    wt_path = demo_repo / ".reporescue" / "worktrees" / "reporescue-task-demo1"
    wt_path.parent.mkdir(parents=True)
    branch = "reporescue/task-demo1"
    run_command(
        ["git", "worktree", "add", "-b", branch, str(wt_path), "HEAD"], cwd=demo_repo
    )

    wt_f = wt_path / "shoppy.py"
    wt_f.write_text("def checkout(): return 'discounted'\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=wt_path)
    run_command(["git", "commit", "-m", "fix: coupon discount"], cwd=wt_path)

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task("task-demo1", branch, fp)

    # Config mistakenly targets the parent orchestrator repo
    orchestrator_repo = "JishnuKarthikKonne/RepoRescue"
    cfg = GitHubConfig(
        repository=orchestrator_repo,
        orchestrator_repository=orchestrator_repo,
        token="ghp_test_token_12345",
        dry_run=True,
        is_demo=True,
    )
    mgr = GitHubPRManager(config=cfg, client=MockGitHubClient(cfg))

    with pytest.raises(
        SafetyGateViolationError,
        match="Demo repository patches cannot be published to the RepoRescue orchestrator repository",
    ):
        mgr.publish_draft_pr(task, wt_path)


def test_demo_repo_missing_remote_origin_fails_gate_k(tmp_path, monkeypatch):
    """Verifies that live publishing fails Gate K if demo repo has no independent remote origin."""
    monkeypatch.delenv("DEMO_GITHUB_REPOSITORY", raising=False)

    demo_repo = tmp_path / ".reporescue" / "demo_repo"
    demo_repo.mkdir(parents=True)
    run_command(["git", "init"], cwd=demo_repo)
    run_command(["git", "config", "user.name", "Test Dev"], cwd=demo_repo)
    run_command(["git", "config", "user.email", "dev@example.com"], cwd=demo_repo)

    main_f = demo_repo / "coupon.py"
    main_f.write_text("code = 0\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=demo_repo)
    run_command(["git", "commit", "-m", "init"], cwd=demo_repo)

    wt_path = demo_repo / ".reporescue" / "worktrees" / "reporescue-task-demo2"
    wt_path.parent.mkdir(parents=True)
    branch = "reporescue/task-demo2"
    run_command(
        ["git", "worktree", "add", "-b", branch, str(wt_path), "HEAD"], cwd=demo_repo
    )

    wt_f = wt_path / "coupon.py"
    wt_f.write_text("code = 20\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=wt_path)
    run_command(["git", "commit", "-m", "fix: coupon"], cwd=wt_path)

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task("task-demo2", branch, fp)

    cfg = GitHubConfig(
        repository="demo-owner/demo-repo",
        token="ghp_test_token_12345",
        dry_run=False,  # Live mode
        is_demo=True,
    )
    # Real-like client so dry_run / mock check does not skip
    mgr = GitHubPRManager(config=cfg, client=GitHubClient(cfg))

    with pytest.raises(
        SafetyGateViolationError,
        match="Demo repository \\(\\.reporescue/demo_repo\\) has no configured GitHub remote origin",
    ):
        mgr.publish_draft_pr(task, wt_path)


def test_demo_repo_with_matching_independent_remote_succeeds(tmp_path):
    """Verifies that a demo repository with its own independent remote succeeds."""
    demo_repo = tmp_path / ".reporescue" / "demo_repo"
    demo_repo.mkdir(parents=True)
    run_command(["git", "init"], cwd=demo_repo)
    run_command(["git", "config", "user.name", "Test Dev"], cwd=demo_repo)
    run_command(["git", "config", "user.email", "dev@example.com"], cwd=demo_repo)
    run_command(
        [
            "git",
            "remote",
            "add",
            "origin",
            "https://github.com/my-username/shoppy-demo.git",
        ],
        cwd=demo_repo,
    )

    main_f = demo_repo / "coupon.py"
    main_f.write_text("code = 0\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=demo_repo)
    run_command(["git", "commit", "-m", "init"], cwd=demo_repo)

    wt_path = demo_repo / ".reporescue" / "worktrees" / "reporescue-task-demo3"
    wt_path.parent.mkdir(parents=True)
    branch = "reporescue/task-demo3"
    run_command(
        ["git", "worktree", "add", "-b", branch, str(wt_path), "HEAD"], cwd=demo_repo
    )

    wt_f = wt_path / "coupon.py"
    wt_f.write_text("code = 20\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=wt_path)
    run_command(["git", "commit", "-m", "fix: coupon"], cwd=wt_path)

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task("task-demo3", branch, fp)

    cfg = GitHubConfig(
        repository="my-username/shoppy-demo",
        orchestrator_repository="JishnuKarthikKonne/RepoRescue",
        token="ghp_test_token_12345",
        dry_run=True,
        is_demo=True,
    )
    mock_client = MockGitHubClient(cfg)
    mgr = GitHubPRManager(config=cfg, client=mock_client)

    result = mgr.publish_draft_pr(task, wt_path)
    assert result.pr_number == 1
    assert result.draft is True
    assert task.status == TaskStatus.PR_CREATED
    assert "https://github.com/my-username/shoppy-demo/pull/1" in result.html_url


def test_preflight_git_receive_pack_distinguishes_rest_push_from_smart_http(
    monkeypatch,
):
    """Verifies that preflight distinguishes REST permissions.push=True from Smart HTTP 403."""
    cfg = GitHubConfig(
        repository="owner/repo",
        token="ghp_read_only_pat",
        dry_run=False,
    )
    client = GitHubClient(cfg)

    # 1. Mock check_auth_and_repo to return REST role permissions: push=True
    monkeypatch.setattr(
        client,
        "check_auth_and_repo",
        lambda: {
            "permissions": {"push": True, "pull": True},
            "full_name": "owner/repo",
        },
    )
    monkeypatch.setattr(
        client,
        "_make_request",
        lambda endpoint, method="GET": {"login": "JishnuKarthikKonne"},
    )

    # 2. Mock urllib.request.urlopen to simulate Smart HTTP 403 on git-receive-pack
    import urllib.error

    http_403 = urllib.error.HTTPError(
        url="https://github.com/owner/repo.git/info/refs?service=git-receive-pack",
        code=403,
        msg="Forbidden",
        hdrs={},
        fp=MagicMock(
            read=lambda: b"Permission to owner/repo.git denied to JishnuKarthikKonne."
        ),
    )

    with patch("urllib.request.urlopen", side_effect=http_403):
        preflight = client.preflight_git_receive_pack()

        assert preflight.api_authenticated is True
        assert preflight.api_username == "JishnuKarthikKonne"
        assert preflight.api_push_permission is True  # REST reports push=True
        assert preflight.git_smart_http_authorized is False  # Git Smart HTTP is DENIED!
        assert preflight.status_code == 403
        assert (
            "Permission to owner/repo.git denied to JishnuKarthikKonne"
            in preflight.error_message
        )
        assert preflight.diagnostic_guidance is not None
        assert "Contents" in preflight.diagnostic_guidance
        assert "repo" in preflight.diagnostic_guidance


def test_explicit_token_disables_credential_helper(tmp_path, monkeypatch):
    """Verifies that when GITHUB_TOKEN is provided, Git's credential.helper is cleared."""
    fake_wt = tmp_path / "wt"
    fake_wt.mkdir()

    cfg = GitHubConfig(
        repository="owner/repo",
        token="ghp_explicit_secret_12345",
        dry_run=False,
    )
    mgr = GitHubPRManager(config=cfg, client=GitHubClient(cfg))

    captured_env = {}

    def mock_run_cmd(cmd, cwd=None, timeout_seconds=None, env=None):
        captured_env.update(env or {})
        from reporescue.runner import ExecutionResult

        return ExecutionResult(
            command=list(cmd),
            exit_code=0,
            stdout="Everything up-to-date",
            stderr="",
            duration_seconds=0.1,
            timed_out=False,
            truncated=False,
        )

    monkeypatch.setattr("reporescue.github.manager.run_command", mock_run_cmd)

    mgr.push_branch(fake_wt, "reporescue/task-cleargcm")

    params = captured_env.get("GIT_CONFIG_PARAMETERS", "")
    assert "'credential.helper='" in params
    assert "'http.extraHeader=Authorization: Basic" in params
    assert captured_env.get("GITHUB_TOKEN") == "ghp_explicit_secret_12345"


def test_gcm_fallback_logs_warning_when_no_token(tmp_path, monkeypatch, caplog):
    """Verifies that falling back to Windows GCM logs an explicit user-facing warning."""
    fake_wt = tmp_path / "wt"
    fake_wt.mkdir()

    cfg = GitHubConfig(
        repository="owner/repo",
        token=None,
        dry_run=False,
    )
    mgr = GitHubPRManager(config=cfg, client=GitHubClient(cfg))

    def mock_run_cmd(cmd, cwd=None, timeout_seconds=None, env=None):
        from reporescue.runner import ExecutionResult

        return ExecutionResult(
            command=list(cmd),
            exit_code=0,
            stdout="Everything up-to-date",
            stderr="",
            duration_seconds=0.1,
            timed_out=False,
            truncated=False,
        )

    monkeypatch.setattr("reporescue.github.manager.run_command", mock_run_cmd)

    with caplog.at_level(logging.WARNING):
        mgr.push_branch(fake_wt, "reporescue/task-gcmwarn")

    assert any(
        "Falling back to local Git Credential Manager (GCM)" in rec.message
        for rec in caplog.records
    )
