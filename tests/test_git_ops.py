import subprocess
from pathlib import Path

import pytest

from reporescue.git_ops import (
    GitError,
    InvalidWorktreePathError,
    UncommittedChangesError,
    WorktreeError,
    WorktreeManager,
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Creates a temporary initialized Git repository with an initial commit."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Initialize repo
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "RepoRescue Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@reporescue.local"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Initial commit
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    return repo


def test_worktree_manager_init_validation(git_repo: Path, tmp_path: Path):
    non_git = tmp_path / "empty_dir"
    non_git.mkdir()
    with pytest.raises(GitError, match="not a Git repository"):
        WorktreeManager(non_git)

    # Worktrees dir cannot be the repo root
    with pytest.raises(
        InvalidWorktreePathError, match="cannot be the main repository root"
    ):
        WorktreeManager(git_repo, worktrees_dir=git_repo)

    # Worktrees dir cannot be ancestor of the repo
    with pytest.raises(InvalidWorktreePathError, match="cannot be an ancestor"):
        WorktreeManager(git_repo, worktrees_dir=tmp_path)


def test_create_worktree_success(git_repo: Path):
    mgr = WorktreeManager(git_repo)
    wt = mgr.create_worktree("reporescue/issue-101")

    assert wt.branch == "reporescue/issue-101"
    assert wt.path.exists()
    assert wt.is_main is False
    assert len(wt.commit_hash) == 40
    assert (wt.path / "README.md").exists()

    # Main repo must still be on 'main'
    status = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == "main"


def test_create_worktree_invalid_branch(git_repo: Path):
    mgr = WorktreeManager(git_repo)
    with pytest.raises(ValueError, match="Invalid branch name"):
        mgr.create_worktree("-bad-branch")

    with pytest.raises(ValueError, match="Invalid branch name"):
        mgr.create_worktree("branch with spaces")

    with pytest.raises(ValueError, match="Invalid characters"):
        mgr.create_worktree("branch#invalid")

    with pytest.raises(ValueError, match="Invalid branch name"):
        mgr.create_worktree("branch/../escape")


def test_create_worktree_duplicate_rejected(git_repo: Path):
    mgr = WorktreeManager(git_repo)
    mgr.create_worktree("reporescue/issue-dup")
    with pytest.raises(WorktreeError, match="already exists"):
        mgr.create_worktree("reporescue/issue-dup")


def test_list_worktrees(git_repo: Path):
    mgr = WorktreeManager(git_repo)
    wt1 = mgr.create_worktree("reporescue/issue-1")
    wt2 = mgr.create_worktree("reporescue/issue-2")

    all_wts = mgr.list_worktrees()
    branches = {w.branch for w in all_wts}
    paths = {w.path.resolve() for w in all_wts}

    assert "main" in branches
    assert "reporescue/issue-1" in branches
    assert "reporescue/issue-2" in branches
    assert git_repo.resolve() in paths
    assert wt1.path.resolve() in paths
    assert wt2.path.resolve() in paths


def test_has_uncommitted_changes_detection(git_repo: Path):
    mgr = WorktreeManager(git_repo)
    wt = mgr.create_worktree("reporescue/issue-mod")

    # Clean worktree
    assert mgr.has_uncommitted_changes(wt.path) is False

    # Untracked file
    new_file = wt.path / "reproduce_bug.py"
    new_file.write_text("print('bug')", encoding="utf-8")
    assert mgr.has_uncommitted_changes(wt.path) is True

    # Staged file
    subprocess.run(
        ["git", "add", "reproduce_bug.py"], cwd=wt.path, check=True, capture_output=True
    )
    assert mgr.has_uncommitted_changes(wt.path) is True

    # Committed file
    subprocess.run(
        ["git", "commit", "-m", "Add test"],
        cwd=wt.path,
        check=True,
        capture_output=True,
    )
    assert mgr.has_uncommitted_changes(wt.path) is False


def test_remove_worktree_safe_cleanup(git_repo: Path):
    mgr = WorktreeManager(git_repo)
    wt = mgr.create_worktree("reporescue/issue-clean")
    assert wt.path.exists()

    mgr.remove_worktree(wt.path)
    assert not wt.path.exists()

    # Ensure removed from worktree list
    remaining_paths = {w.path.resolve() for w in mgr.list_worktrees()}
    assert wt.path.resolve() not in remaining_paths


def test_remove_worktree_uncommitted_changes_prevented(git_repo: Path):
    mgr = WorktreeManager(git_repo)
    wt = mgr.create_worktree("reporescue/issue-dirty")

    # Add uncommitted modification
    (wt.path / "dirty.txt").write_text("dirty content", encoding="utf-8")

    # Deletion without force must fail
    with pytest.raises(UncommittedChangesError, match="contains uncommitted changes"):
        mgr.remove_worktree(wt.path, force=False)
    assert wt.path.exists()

    # Deletion with explicit approval (force=True) must succeed
    mgr.remove_worktree(wt.path, force=True)
    assert not wt.path.exists()


def test_refuse_removal_outside_worktrees_dir(git_repo: Path, tmp_path: Path):
    mgr = WorktreeManager(git_repo)
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()

    with pytest.raises(
        InvalidWorktreePathError, match="outside allowed worktrees directory"
    ):
        mgr.remove_worktree(outside_dir)

    # Refuse removing main repo itself
    with pytest.raises(
        InvalidWorktreePathError, match="outside allowed worktrees directory"
    ):
        mgr.remove_worktree(git_repo)


def test_cleanup_all(git_repo: Path):
    mgr = WorktreeManager(git_repo)
    wt1 = mgr.create_worktree("reporescue/clean-1")
    wt2 = mgr.create_worktree("reporescue/clean-2")

    cleaned = mgr.cleanup_all(force=True)
    assert wt1.path in cleaned
    assert wt2.path in cleaned
    assert not wt1.path.exists()
    assert not wt2.path.exists()

    # Main repo must remain untouched
    assert git_repo.exists()
    assert (git_repo / "README.md").exists()
