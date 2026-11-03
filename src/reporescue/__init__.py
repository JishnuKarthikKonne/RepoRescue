"""RepoRescue: Human-approved collaborative AI bug-fixing workflow."""

from .agent_runtime import (
    AgentRequest,
    AgentResult,
    AgentRuntimeFactory,
    AgentRuntimeProtocol,
    MockAgentRuntime,
)
from .demo import init_demo_repository
from .git_ops import (
    GitError,
    InvalidWorktreePathError,
    UncommittedChangesError,
    WorktreeError,
    WorktreeInfo,
    WorktreeManager,
)
from .models import BugTask, TaskStatus
from .orchestrator import (
    InvalidStageOrderError,
    OrchestratorError,
    VerificationFailedError,
    WorkflowOrchestrator,
)
from .runner import ExecutionResult, run_command, run_pytest
from .schemas import (
    FixResult,
    InvestigationResult,
    ReproductionResult,
    StageExecutionResult,
    TriageResult,
    VerificationResult,
    WorkflowEvidence,
    WorkflowStage,
)
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
    "AgentRequest",
    "AgentResult",
    "AgentRuntimeFactory",
    "AgentRuntimeProtocol",
    "BugTask",
    "ExecutionResult",
    "FixResult",
    "GitError",
    "InvalidStageOrderError",
    "InvalidWorktreePathError",
    "InvestigationResult",
    "MockAgentRuntime",
    "MockSwitchClient",
    "OrchestratorError",
    "RepoRescueDispatcher",
    "ReproductionResult",
    "StageExecutionResult",
    "SwitchAuthError",
    "SwitchClient",
    "SwitchClientProtocol",
    "SwitchConfig",
    "SwitchError",
    "SwitchListener",
    "SwitchMessageEvent",
    "TaskStatus",
    "TriageResult",
    "UncommittedChangesError",
    "VerificationFailedError",
    "VerificationResult",
    "WorkflowEvidence",
    "WorkflowFormatter",
    "WorkflowOrchestrator",
    "WorkflowStage",
    "WorktreeError",
    "WorktreeInfo",
    "WorktreeManager",
    "init_demo_repository",
    "run_command",
    "run_pytest",
]
