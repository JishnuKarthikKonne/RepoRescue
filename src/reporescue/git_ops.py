from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .runner import run_command


class GitError(Exception):
    """Base exception for Git operations."""


class WorktreeError(GitError):
    """Base exception for worktree operations."""


class InvalidWorktreePathError(WorktreeError):
    """Raised when a worktree path is invalid or violates directory boundaries."""


class UncommittedChangesError(WorktreeError):
    """Raised when attempting to delete a worktree with uncommitted changes without explicit approval."""


@dataclass(frozen=True)
class WorktreeInfo:
    """Represents metadata about an active Git worktree."""

    path: Path
    branch: str
    commit_hash: str
    is_main: bool = False
    created_at: datetime | None = None


def _validate_branch_name(branch_name: str) -> None:
    """Validates that the proposed branch name is safe and valid for Git."""
    if not branch_name or not branch_name.strip():
        raise ValueError("Branch name cannot be empty.")
    # Reject leading dashes, path traversal, control chars, spaces
    if branch_name.startswith("-") or ".." in branch_name or " " in branch_name:
        raise ValueError(f"Invalid branch name: {branch_name}")
    if not re.match(r"^[a-zA-Z0-9_\-\./]+$", branch_name):
        raise ValueError(f"Invalid characters in branch name: {branch_name}")


class WorktreeManager:
    """Manages isolated Git worktrees within a strictly bounded workspace directory.

    Invariants:
    1. Never modifies the main repository's active branch or working tree.
    2. Never creates or deletes worktrees outside the configured `worktrees_dir`.
    3. Never deletes a worktree with uncommitted changes unless `force=True` is explicitly provided.
    """

    def __init__(
        self, repo_path: Path | str, worktrees_dir: Path | str | None = None
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        if not (self.repo_path / ".git").exists():
            raise GitError(f"Directory is not a Git repository: {self.repo_path}")

        # Default to a dedicated hidden directory inside the repository
        if worktrees_dir is None:
            self.worktrees_dir = (
                self.repo_path / ".reporescue" / "worktrees"
            ).resolve()
        else:
            self.worktrees_dir = Path(worktrees_dir).resolve()

        # Safety verification: worktrees_dir must not be the repo root or parent of repo
        if self.worktrees_dir == self.repo_path:
            raise InvalidWorktreePathError(
                "Worktrees directory cannot be the main repository root."
            )
        if self.repo_path.is_relative_to(self.worktrees_dir):
            raise InvalidWorktreePathError(
                "Worktrees directory cannot be an ancestor of the repository."
            )

        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

        # Ensure repo-local exclude file ignores worktree dir if located inside repository
        if self.worktrees_dir.is_relative_to(self.repo_path):
            try:
                exclude_file = self.repo_path / ".git" / "info" / "exclude"
                if exclude_file.exists():
                    exclude_content = exclude_file.read_text(encoding="utf-8")
                    rel_root = (
                        self.worktrees_dir.relative_to(self.repo_path)
                        .as_posix()
                        .split("/")[0]
                        + "/"
                    )
                    if rel_root not in exclude_content:
                        exclude_file.write_text(
                            exclude_content.rstrip() + f"\n{rel_root}\n",
                            encoding="utf-8",
                        )
            except OSError:
                pass

    def _run_git(self, args: Sequence[str], *, cwd: Path | None = None) -> str:
        """Executes a git command using run_command, checking for errors."""
        working_dir = cwd or self.repo_path
        res = run_command(["git", *args], cwd=working_dir)
        if not res.passed:
            raise GitError(
                f"Git command failed: git {' '.join(args)}\nStderr: {res.stderr}\nStdout: {res.stdout}"
            )
        return res.stdout.strip()

    def _resolve_safe_path(self, target_path: Path | str) -> Path:
        """Ensures that the target path resolves strictly inside worktrees_dir."""
        resolved = Path(target_path).resolve()
        try:
            resolved.relative_to(self.worktrees_dir)
        except ValueError as err:
            raise InvalidWorktreePathError(
                f"Path '{resolved}' is outside allowed worktrees directory '{self.worktrees_dir}'"
            ) from err

        if resolved == self.worktrees_dir:
            raise InvalidWorktreePathError(
                "Target path cannot be the root worktrees container directory."
            )
        return resolved

    def create_worktree(
        self,
        branch_name: str,
        *,
        base_ref: str = "HEAD",
        worktree_name: str | None = None,
    ) -> WorktreeInfo:
        """Creates an isolated Git worktree and checks out a new branch.

        Args:
            branch_name: Name of the new branch (e.g. 'reporescue/issue-42').
            base_ref: Commit or branch to base the new worktree on (defaults to 'HEAD').
            worktree_name: Directory name under worktrees_dir. If omitted, derived from branch_name.

        Returns:
            WorktreeInfo describing the created worktree.
        """
        _validate_branch_name(branch_name)

        safe_name = worktree_name or branch_name.replace("/", "-")
        target_path = self._resolve_safe_path(self.worktrees_dir / safe_name)

        if target_path.exists():
            raise WorktreeError(
                f"Worktree destination path already exists: {target_path}"
            )

        # git worktree add -b <branch_name> <target_path> <base_ref>
        self._run_git(
            ["worktree", "add", "-b", branch_name, str(target_path), base_ref]
        )

        # Get HEAD commit of new worktree
        commit_hash = self._run_git(["rev-parse", "HEAD"], cwd=target_path)

        return WorktreeInfo(
            path=target_path,
            branch=branch_name,
            commit_hash=commit_hash,
            is_main=False,
            created_at=datetime.now(UTC),
        )

    def list_worktrees(self) -> list[WorktreeInfo]:
        """Lists all active Git worktrees for this repository."""
        raw_output = self._run_git(["worktree", "list", "--porcelain"])
        worktrees: list[WorktreeInfo] = []

        current_path: Path | None = None
        current_head: str = ""
        current_branch: str = ""

        for line in raw_output.splitlines():
            line = line.strip()
            if not line:
                if current_path:
                    is_main = current_path.resolve() == self.repo_path
                    worktrees.append(
                        WorktreeInfo(
                            path=current_path,
                            branch=current_branch or "detached",
                            commit_hash=current_head,
                            is_main=is_main,
                        )
                    )
                current_path = None
                current_head = ""
                current_branch = ""
                continue

            if line.startswith("worktree "):
                current_path = Path(line.split(" ", 1)[1]).resolve()
            elif line.startswith("HEAD "):
                current_head = line.split(" ", 1)[1]
            elif line.startswith("branch "):
                ref = line.split(" ", 1)[1]
                # Strip refs/heads/ if present
                current_branch = ref.removeprefix("refs/heads/")

        if current_path:
            is_main = current_path.resolve() == self.repo_path
            worktrees.append(
                WorktreeInfo(
                    path=current_path,
                    branch=current_branch or "detached",
                    commit_hash=current_head,
                    is_main=is_main,
                )
            )

        return worktrees

    def has_uncommitted_changes(self, worktree_path: Path | str) -> bool:
        """Checks if a worktree has uncommitted modifications, staged changes, or untracked files."""
        safe_path = self._resolve_safe_path(worktree_path)
        if not safe_path.exists():
            raise WorktreeError(f"Worktree path does not exist: {safe_path}")

        status_output = self._run_git(["status", "--porcelain"], cwd=safe_path)
        return bool(status_output.strip())

    def remove_worktree(
        self, worktree_path: Path | str, *, force: bool = False
    ) -> None:
        """Safely removes an isolated worktree.

        Args:
            worktree_path: Path to the worktree to remove.
            force: If True, allows removal even if uncommitted changes exist.

        Raises:
            InvalidWorktreePathError: If path is outside worktrees_dir or targets the main repo.
            UncommittedChangesError: If uncommitted changes exist and force is False.
        """
        safe_path = self._resolve_safe_path(worktree_path)
        if not safe_path.exists():
            # Already gone or never existed
            self._run_git(["worktree", "prune"])
            return

        has_changes = self.has_uncommitted_changes(safe_path)
        if has_changes and not force:
            raise UncommittedChangesError(
                f"Worktree at '{safe_path}' contains uncommitted changes. Deletion requires explicit approval (force=True)."
            )

        remove_cmd = ["worktree", "remove"]
        if force:
            remove_cmd.append("--force")
        remove_cmd.append(str(safe_path))

        self._run_git(remove_cmd)
        self._run_git(["worktree", "prune"])

        # Fallback if Windows file lock kept directory lingering
        if safe_path.exists():
            shutil.rmtree(safe_path, ignore_errors=True)

    def cleanup_all(self, *, force: bool = False) -> list[Path]:
        """Cleans up all managed worktrees inside the configured worktrees_dir."""
        cleaned: list[Path] = []
        for wt in self.list_worktrees():
            if wt.is_main:
                continue
            try:
                # Only clean if inside our managed worktrees_dir
                wt.path.relative_to(self.worktrees_dir)
                self.remove_worktree(wt.path, force=force)
                cleaned.append(wt.path)
            except (ValueError, InvalidWorktreePathError):
                continue
        return cleaned
