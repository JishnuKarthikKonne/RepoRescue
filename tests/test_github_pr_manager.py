from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from reporescue.github.client import MockGitHubClient
from reporescue.github.config import GitHubConfig
from reporescue.github.fingerprint import compute_worktree_patch_fingerprint
from reporescue.github.manager import (
    GitHubPRManager,
    PatchTamperedError,
    SafetyGateViolationError,
)
from reporescue.models import BugTask, TaskStatus
from reporescue.runner import run_command


@pytest.fixture
def test_git_worktree(tmp_path: Path):
    """Sets up a real isolated Git repo and worktree for safety gate testing."""
    repo = tmp_path / "main_repo"
    repo.mkdir()
    run_command(["git", "init"], cwd=repo)
    run_command(["git", "config", "user.name", "Test User"], cwd=repo)
    run_command(["git", "config", "user.email", "test@example.com"], cwd=repo)

    main_file = repo / "shop.py"
    main_file.write_text("def checkout(): return False\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=repo)
    run_command(["git", "commit", "-m", "initial commit"], cwd=repo)

    # Create worktree
    wt_dir = tmp_path / "worktrees"
    wt_dir.mkdir()
    branch_name = "reporescue/task-test0001"
    wt_path = wt_dir / "wt-test0001"
    run_command(
        ["git", "worktree", "add", "-b", branch_name, str(wt_path), "HEAD"],
        cwd=repo,
    )

    # Apply committed fix in worktree
    wt_file = wt_path / "shop.py"
    wt_file.write_text("def checkout(): return True\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=wt_path)
    run_command(["git", "commit", "-m", "fix: checkout returns true"], cwd=wt_path)

    return wt_path, branch_name


def _create_approved_task(
    task_id: str = "test0001-uuid",
    branch_name: str = "reporescue/task-test0001",
    fp: str = "sha256:abc",
) -> BugTask:
    task = BugTask(
        title="Checkout failure",
        description="Checkout returns False",
        reporter="alice",
        task_id=task_id,
        status=TaskStatus.REPORTED,
    )
    task.proposed_branch = branch_name
    task.evidence.append("Verification PASSED: 3 passed in 0.5s")
    task.verified_patch_fingerprint = fp

    # Transition up to APPROVED
    task.transition(TaskStatus.TRIAGED, actor="system")
    task.transition(TaskStatus.REPRODUCING, actor="system")
    task.transition(TaskStatus.REPRODUCED, actor="system")
    task.transition(TaskStatus.FIX_PROPOSED, actor="system")
    task.transition(TaskStatus.VERIFYING, actor="system")
    task.transition(TaskStatus.VERIFIED, actor="system")
    task.transition(TaskStatus.AWAITING_APPROVAL, actor="system")
    task.transition(
        TaskStatus.APPROVED, actor="LeadDeveloper", timestamp=datetime.now(UTC)
    )
    task.approved_patch_fingerprint = fp
    return task


def test_gate_a_unapproved_task_fails(test_git_worktree):
    wt_path, branch = test_git_worktree
    mgr = GitHubPRManager(config=GitHubConfig(repository="owner/repo", dry_run=True))

    task = BugTask(title="Bug", description="Desc", reporter="user")
    task.proposed_branch = branch

    with pytest.raises(
        SafetyGateViolationError, match="Gate A Failed: Task status must be 'approved'"
    ):
        mgr.publish_draft_pr(task, wt_path)


def test_gate_b_bot_approval_fails(test_git_worktree):
    wt_path, branch = test_git_worktree
    mgr = GitHubPRManager(config=GitHubConfig(repository="owner/repo", dry_run=True))

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task(branch_name=branch, fp=fp)

    # Set bot as approver
    task.approval_by = "@switch-agent-triager"

    with pytest.raises(SafetyGateViolationError, match="Gate B Failed"):
        mgr.publish_draft_pr(task, wt_path)


def test_gate_c_missing_verified_patch_record_fails(test_git_worktree):
    wt_path, branch = test_git_worktree
    mgr = GitHubPRManager(config=GitHubConfig(repository="owner/repo", dry_run=True))

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task(branch_name=branch, fp=fp)
    task.verified_patch_fingerprint = None
    task.evidence = []  # Clear evidence

    with pytest.raises(SafetyGateViolationError, match="Gate C Failed"):
        mgr.publish_draft_pr(task, wt_path)


def test_gate_d_failed_verification_fails(test_git_worktree):
    wt_path, branch = test_git_worktree
    mgr = GitHubPRManager(config=GitHubConfig(repository="owner/repo", dry_run=True))

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task(branch_name=branch, fp=fp)
    task.evidence = ["Verification FAILED: exit=1"]

    with pytest.raises(SafetyGateViolationError, match="Gate D Failed"):
        mgr.publish_draft_pr(task, wt_path)


def test_gate_e_modified_patch_after_approval_invalidates_approval(test_git_worktree):
    wt_path, branch = test_git_worktree
    mgr = GitHubPRManager(config=GitHubConfig(repository="owner/repo", dry_run=True))

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task(branch_name=branch, fp=fp)

    # TAMPERING: Modify worktree and commit after approval!
    wt_file = wt_path / "shop.py"
    wt_file.write_text("def checkout(): return 'tampered'\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=wt_path)
    run_command(["git", "commit", "-m", "tamper: backdoor"], cwd=wt_path)

    with pytest.raises(
        PatchTamperedError,
        match="Gate E Failed: Worktree patch fingerprint.*does not match",
    ):
        mgr.publish_draft_pr(task, wt_path)

    # Verify approval was invalidated and task transitioned back
    assert task.status == TaskStatus.FIX_PROPOSED
    assert task.approval_by is None
    assert task.approved_at is None
    assert any("Approval invalidated" in ev for ev in task.evidence)


def test_gate_f_branch_mismatch_fails(test_git_worktree):
    wt_path, _ = test_git_worktree
    mgr = GitHubPRManager(config=GitHubConfig(repository="owner/repo", dry_run=True))

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task(branch_name="reporescue/task-other-branch", fp=fp)

    with pytest.raises(
        SafetyGateViolationError, match="Gate F Failed: Worktree branch mismatch"
    ):
        mgr.publish_draft_pr(task, wt_path)


def test_gate_g_protected_paths_and_secrets_fails(test_git_worktree):
    wt_path, branch = test_git_worktree
    mgr = GitHubPRManager(config=GitHubConfig(repository="owner/repo", dry_run=True))

    # Commit a protected .env file
    env_file = wt_path / ".env"
    env_file.write_text("SECRET_KEY=12345\n", encoding="utf-8")
    run_command(["git", "add", "."], cwd=wt_path)
    run_command(["git", "commit", "-m", "leak: add .env"], cwd=wt_path)

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task(branch_name=branch, fp=fp)

    with pytest.raises(
        SafetyGateViolationError, match="Gate G Failed: Modification of protected file"
    ):
        mgr.publish_draft_pr(task, wt_path)


def test_gate_h_allowlist_violation_fails(test_git_worktree):
    wt_path, branch = test_git_worktree
    cfg = GitHubConfig(
        repository="untrusted/repo",
        repository_allowlist=["trusted/repo"],
        dry_run=True,
    )
    mgr = GitHubPRManager(config=cfg)

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task(branch_name=branch, fp=fp)

    with pytest.raises(SafetyGateViolationError, match="Gate H Failed"):
        mgr.publish_draft_pr(task, wt_path)


def test_gate_i_dirty_worktree_fails(test_git_worktree):
    wt_path, branch = test_git_worktree
    mgr = GitHubPRManager(config=GitHubConfig(repository="owner/repo", dry_run=True))

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task(branch_name=branch, fp=fp)

    # Leave an uncommitted file in worktree
    (wt_path / "dirty.txt").write_text("uncommitted changes\n", encoding="utf-8")

    with pytest.raises(
        SafetyGateViolationError,
        match="Gate I Failed: Worktree contains uncommitted changes",
    ):
        mgr.publish_draft_pr(task, wt_path)


def test_happy_path_publish_draft_pr_and_idempotency(test_git_worktree):
    wt_path, branch = test_git_worktree
    mock_client = MockGitHubClient()
    mgr = GitHubPRManager(
        config=GitHubConfig(repository="owner/repo", dry_run=True),
        client=mock_client,
    )

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task(branch_name=branch, fp=fp)

    # 1. Publish draft PR
    result = mgr.publish_draft_pr(task, wt_path)

    assert result.pr_number == 1
    assert result.draft is True
    assert result.is_existing is False
    assert task.status == TaskStatus.PR_CREATED
    assert task.pr_url == result.html_url
    assert task.pr_number == 1
    assert any("GitHub Draft PR #1 created" in ev for ev in task.evidence)

    # 2. Gate J: Idempotency test (publishing already published task reuses PR)
    result2 = mgr.publish_draft_pr(task, wt_path)
    assert result2.pr_number == 1
    assert result2.is_existing is True
    assert len(mock_client.created_prs) == 1  # No duplicate created


def test_push_branch_uses_basic_auth_in_env_and_never_in_cmd_args(
    monkeypatch, tmp_path
):
    """Verifies Git Smart HTTP Basic Auth is passed via GIT_CONFIG_PARAMETERS and never in sys.argv."""
    fake_wt = tmp_path / "wt"
    fake_wt.mkdir()

    cfg = GitHubConfig(
        repository="owner/repo",
        token="ghp_testsecrettoken12345",
        dry_run=False,
    )
    # Using real-like client type so it doesn't short-circuit dry_run/mock check
    from reporescue.github.client import GitHubClient

    mgr = GitHubPRManager(config=cfg, client=GitHubClient(cfg))

    captured_args = {}

    def mock_run_cmd(cmd, cwd=None, timeout_seconds=None, env=None):
        captured_args["cmd"] = cmd
        captured_args["env"] = env
        captured_args["cwd"] = cwd
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

    mgr.push_branch(fake_wt, "reporescue/task-001")

    cmd = captured_args["cmd"]
    env = captured_args["env"]

    # Invariant: Secret token is NEVER in command line arguments (sys.argv)
    assert "ghp_testsecrettoken12345" not in " ".join(cmd)
    assert "Authorization" not in " ".join(cmd)
    assert cmd == [
        "git",
        "push",
        "-u",
        "https://github.com/owner/repo.git",
        "reporescue/task-001:reporescue/task-001",
    ]

    # Invariant: Basic auth is provided in GIT_CONFIG_PARAMETERS in environment
    assert "GIT_CONFIG_PARAMETERS" in env
    assert "credential.helper=" in env["GIT_CONFIG_PARAMETERS"]
    assert "Authorization: Basic" in env["GIT_CONFIG_PARAMETERS"]
    assert "GIT_TERMINAL_PROMPT" in env and env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GCM_INTERACTIVE" in env and env["GCM_INTERACTIVE"] == "never"
    assert env.get("GITHUB_TOKEN") == "ghp_testsecrettoken12345"


def test_push_branch_fallback_to_windows_gcm_when_no_token(monkeypatch, tmp_path):
    """Verifies that without a token, Git push falls back cleanly to Windows GCM."""
    fake_wt = tmp_path / "wt"
    fake_wt.mkdir()

    cfg = GitHubConfig(
        repository="owner/repo",
        token=None,
        dry_run=False,
    )
    from reporescue.github.client import GitHubClient

    mgr = GitHubPRManager(config=cfg, client=GitHubClient(cfg))

    captured_args = {}

    def mock_run_cmd(cmd, cwd=None, timeout_seconds=None, env=None):
        captured_args["cmd"] = cmd
        captured_args["env"] = env
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

    mgr.push_branch(fake_wt, "reporescue/task-002")

    env = captured_args["env"]
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert env.get("GCM_INTERACTIVE") == "never"


def test_push_branch_failure_leaves_task_approved_and_retryable(
    test_git_worktree, monkeypatch
):
    """Verifies that if push fails, the task remains APPROVED and can be retried without rerunning fix."""
    from reporescue.github.manager import GitHubManagerError

    wt_path, branch = test_git_worktree
    mock_client = MockGitHubClient()
    mgr = GitHubPRManager(
        config=GitHubConfig(
            repository="owner/repo",
            token="ghp_validtokendummy",
            dry_run=False,
        ),
        client=mock_client,
    )

    fp, _, _ = compute_worktree_patch_fingerprint(wt_path)
    task = _create_approved_task(branch_name=branch, fp=fp)

    fail_push = True

    def mock_push(worktree_path, branch_name):
        nonlocal fail_push
        if fail_push:
            raise GitHubManagerError(
                "Failed to push branch 'reporescue/task-test0001' to GitHub: "
                "remote: invalid credentials\nfatal: Authentication failed"
            )
        return "pushed"

    monkeypatch.setattr(mgr, "push_branch", mock_push)

    # 1. First attempt fails due to invalid credentials
    with pytest.raises(GitHubManagerError, match="fatal: Authentication failed"):
        mgr.publish_draft_pr(task, wt_path)

    # Invariant: Task remains APPROVED with identical fingerprint and approval actor
    assert task.status == TaskStatus.APPROVED
    assert task.approved_patch_fingerprint == fp
    assert task.approval_by == "LeadDeveloper"
    assert task.pr_url is None
    assert len(mock_client.created_prs) == 0

    # 2. Second attempt succeeds (credentials fixed / retry)
    fail_push = False
    result = mgr.publish_draft_pr(task, wt_path)

    assert result.pr_number == 1
    assert task.status == TaskStatus.PR_CREATED
    assert task.pr_url == result.html_url
    assert len(mock_client.created_prs) == 1
