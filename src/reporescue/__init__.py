"""RepoRescue: Human-approved collaborative AI bug-fixing workflow."""

from .git_ops import (
    GitError,
    InvalidWorktreePathError,
    UncommittedChangesError,
    WorktreeError,
    WorktreeInfo,
    WorktreeManager,
)
from .models import BugTask, TaskStatus
from .runner import ExecutionResult, run_command, run_pytest
from .switch import (
    MockSwitchClient,
    RepoRescueDispatcher,
    SwitchAuthError,
    SwitchClient,
    SwitchClientProtocol,
    SwitchConfig,
    SwitchError,
    SwitchListener,
    SwitchMessageEvent,
    WorkflowFormatter,
)

__all__ = [
    "BugTask",
    "ExecutionResult",
    "GitError",
    "InvalidWorktreePathError",
    "MockSwitchClient",
    "RepoRescueDispatcher",
    "SwitchAuthError",
    "SwitchClient",
    "SwitchClientProtocol",
    "SwitchConfig",
    "SwitchError",
    "SwitchListener",
    "SwitchMessageEvent",
    "TaskStatus",
    "UncommittedChangesError",
    "WorkflowFormatter",
    "WorktreeError",
    "WorktreeInfo",
    "WorktreeManager",
    "run_command",
    "run_pytest",
]
