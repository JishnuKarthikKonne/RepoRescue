from __future__ import annotations

from pathlib import Path

import pytest

from reporescue.agent_runtime import AgentRequest, MockAgentRuntime
from reporescue.demo import init_demo_repository
from reporescue.git_ops import WorktreeManager
from reporescue.models import BugTask, TaskStatus
from reporescue.orchestrator import (
    InvalidStageOrderError,
    VerificationFailedError,
    WorkflowOrchestrator,
)
from reporescue.switch.client import MockSwitchClient
from reporescue.switch.dispatcher import RepoRescueDispatcher
from reporescue.switch.models import SwitchMessageEvent


@pytest.fixture
def demo_git_repo(tmp_path: Path) -> Path:
    """Creates a standalone demo Git repository with the seeded coupon bug."""
    repo_dir = tmp_path / "test_demo_shop"
    return init_demo_repository(repo_dir)


def test_mock_agent_runtime_structured_output():
    runtime = MockAgentRuntime()

    # Triage
    res_triage = runtime.run_agent(
        AgentRequest(
            role="triager",
            context={"title": "Checkout 500 error", "description": "Crash"},
        )
    )
    assert res_triage.is_simulation is True
    assert res_triage.structured_data["severity"] in ("P0", "P1", "P2", "P3")
    assert len(res_triage.structured_data["affected_components"]) > 0

    # Custom override
    runtime.set_custom_response("triager", {"summary": "Overridden", "severity": "P0"})
    res_override = runtime.run_agent(AgentRequest(role="triager", context={}))
    assert res_override.structured_data["severity"] == "P0"


def test_orchestrator_stage_sequencing_enforcement(demo_git_repo: Path):
    runtime = MockAgentRuntime()
    orchestrator = WorkflowOrchestrator(demo_git_repo, runtime)
    task = BugTask(title="Bug", description="Desc", reporter="Dev")

    # Attempting reproduction before triage & investigation must fail
    with pytest.raises(InvalidStageOrderError, match="must be investigated"):
        orchestrator.run_reproduction(task, None)  # type: ignore[arg-type]

    # Run triage
    triage_res = orchestrator.run_triage(task)
    assert task.status == TaskStatus.TRIAGED

    # Attempting fix directly from triaged must fail
    with pytest.raises(InvalidStageOrderError, match="must be reproduced"):
        orchestrator.run_fix(task, None, None)  # type: ignore[arg-type]

    # Run investigation
    invest_res = orchestrator.run_investigation(task, triage_res)
    assert task.status == TaskStatus.INVESTIGATED
    assert invest_res.confidence in ("low", "medium", "high")


def test_orchestrator_end_to_end_on_demo_repo(demo_git_repo: Path):
    runtime = MockAgentRuntime()
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)
    orchestrator = WorkflowOrchestrator(
        repo_path=demo_git_repo,
        runtime=runtime,
        dispatcher=dispatcher,
        room_id="!shop:localhost",
    )

    task = BugTask(
        title="Checkout returns HTTP 500 when coupon is applied",
        description="Invoking calculate_order_total with coupon_code='EXPIRED' crashes with ValueError",
        reporter="QA Engineer",
    )

    # Execute full autonomous workflow
    verif_res = orchestrator.execute_full_workflow(task)

    # Assert verification succeeded
    assert verif_res.verified is True
    assert verif_res.exit_code == 0
    assert verif_res.passed_count >= 2
    assert verif_res.failed_count == 0

    # Assert task is awaiting human approval
    assert task.status == TaskStatus.AWAITING_APPROVAL
    assert task.proposed_branch.startswith("reporescue/task-")

    # Verify that worktree exists and main repository working tree was NOT modified
    main_git_status = WorktreeManager(demo_git_repo)._run_git(["status", "--porcelain"])
    assert main_git_status == ""  # Main tree remains pristine clean!


def test_orchestrator_verification_failure_guard(demo_git_repo: Path):
    runtime = MockAgentRuntime()
    # Configure fixer to produce a broken fix that doesn't resolve the bug
    runtime.set_custom_response(
        "fixer",
        {
            "changed_files": ["src/shop/checkout.py"],
            "diff": "--- a/src/shop/checkout.py\n+++ b/src/shop/checkout.py\n",
            "file_contents": {
                "src/shop/checkout.py": "def calculate_order_total(*args, **kwargs): raise RuntimeError('Still broken')\n"
            },
            "rationale": "Incomplete broken fix",
        },
    )

    orchestrator = WorkflowOrchestrator(demo_git_repo, runtime)
    task = BugTask(title="Bug", description="Desc", reporter="Dev")

    # Workflow must raise VerificationFailedError and mark task FAILED
    with pytest.raises(VerificationFailedError, match="Verification failed"):
        orchestrator.execute_full_workflow(task)

    assert task.status == TaskStatus.FAILED
    assert any("Verification FAILED" in ev for ev in task.evidence)


def test_dispatcher_orchestrator_integration_with_human_approval(demo_git_repo: Path):
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)
    runtime = MockAgentRuntime()
    orchestrator = WorkflowOrchestrator(
        repo_path=demo_git_repo,
        runtime=runtime,
        dispatcher=dispatcher,
        room_id="!reporescue:localhost",
    )
    dispatcher.orchestrator = orchestrator

    # 1. Developer submits bug report in Switch room
    report_event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!reporescue:localhost",
            "payload": {
                "message_id": "msg_autonomous_001",
                "body": "Bug: Checkout returns HTTP 500 on expired coupon",
                "sender": "@developer:localhost",
                "sender_name": "Developer Alex",
                "sender_kind": "user",
            },
        }
    )
    assert report_event is not None
    task = dispatcher.handle_message_event(report_event)
    assert task is not None

    # Assert workflow ran through and stopped at AWAITING_APPROVAL
    assert task.status == TaskStatus.AWAITING_APPROVAL

    # Check that cards were published to the Switch room
    messages = [msg[1] for msg in mock_client.sent_messages]
    assert any("RepoRescue |" in m for m in messages)
    assert any("Issue Triaged" in m for m in messages)
    assert any("Root cause identified" in m for m in messages)
    assert any("Reproduction successful" in m for m in messages)
    assert any("Fix Proposed" in m or "Fix proposed" in m for m in messages)
    assert any("Verification complete" in m for m in messages)
    assert any(
        "Human Approval Required" in m or "Human approval required" in m
        for m in messages
    )

    # 2. Verified human approves in room
    approve_event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!reporescue:localhost",
            "payload": {
                "message_id": "msg_autonomous_002",
                "body": "/approve",
                "sender": "@lead:localhost",
                "sender_name": "Lead Dev Sarah",
                "sender_kind": "user",
            },
        }
    )
    assert approve_event is not None
    approved_task = dispatcher.handle_message_event(approve_event)
    assert approved_task is not None
    assert approved_task.status == TaskStatus.APPROVED
    assert approved_task.approval_by == "Lead Dev Sarah"
    assert approved_task.approved_at is not None
    assert "Human Approval Granted" in mock_client.last_message_content
