from __future__ import annotations

from unittest.mock import MagicMock

from reporescue.github.models import GitHubPRResult
from reporescue.models import BugTask, TaskStatus
from reporescue.switch.client import MockSwitchClient
from reporescue.switch.dispatcher import RepoRescueDispatcher
from reporescue.switch.models import SwitchMessageEvent


def _create_awaiting_approval_task(
    task_id: str = "task-sw-001", room_id: str = "!room:localhost"
) -> BugTask:
    task = BugTask(
        title="Payment gateway timeout",
        description="Checkout times out on slow connections",
        reporter="carol",
        task_id=task_id,
        status=TaskStatus.REPORTED,
    )
    task.room_id = room_id  # type: ignore[attr-defined]
    task.proposed_branch = "reporescue/task-task-sw"
    task.verified_patch_fingerprint = "sha256:1111222233334444"

    # Advance to AWAITING_APPROVAL
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
    return task


def test_switch_approval_publishes_draft_pr_card():
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    task = _create_awaiting_approval_task("task-42", "!dev-room:localhost")
    dispatcher.tasks[task.task_id] = task
    dispatcher.active_task_by_room["!dev-room:localhost"] = task.task_id

    # Mock orchestrator with publish_draft_pr
    mock_orchestrator = MagicMock()
    mock_orchestrator.publish_draft_pr.return_value = GitHubPRResult(
        pr_url="https://api.github.com/repos/org/repo/pulls/10",
        pr_number=10,
        html_url="https://github.com/org/repo/pull/10",
        branch_name="reporescue/task-42",
        base_branch="main",
        commit_sha="abcdef1234567890",
        title="[RepoRescue] Payment gateway timeout",
        patch_fingerprint="sha256:1111222233334444",
        draft=True,
    )
    dispatcher.orchestrator = mock_orchestrator

    # Human user sends /approve
    approve_evt = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!dev-room:localhost",
            "payload": {
                "body": f"/approve {task.task_id}",
                "sender": "@lead_engineer:localhost",
                "sender_name": "Lead Engineer",
                "sender_kind": "user",
            },
        }
    )
    assert approve_evt is not None
    res = dispatcher.handle_message_event(approve_evt)

    assert res is not None
    assert res.status == TaskStatus.APPROVED
    assert res.approval_by == "Lead Engineer"
    mock_orchestrator.publish_draft_pr.assert_called_once_with(task)

    # Verify messages posted to Switch room
    sent_bodies = [content for room, content, meta in mock_client.sent_messages]
    assert any("Human Approval Granted" in b for b in sent_bodies)
    assert any("Draft Pull Request Created" in b for b in sent_bodies)
    assert any("https://github.com/org/repo/pull/10" in b for b in sent_bodies)
    assert any("Zero Auto-Merge / Zero Auto-Deploy Guarantee" in b for b in sent_bodies)


def test_switch_approval_handles_pr_failure_gracefully():
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    task = _create_awaiting_approval_task("task-55", "!room1")
    dispatcher.tasks[task.task_id] = task
    dispatcher.active_task_by_room["!room1"] = task.task_id

    mock_orchestrator = MagicMock()
    mock_orchestrator.publish_draft_pr.side_effect = RuntimeError(
        "Gate H Failed: Bad credentials"
    )
    dispatcher.orchestrator = mock_orchestrator

    approve_evt = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room1",
            "payload": {
                "body": "/approve",
                "sender": "@user:localhost",
                "sender_name": "Dev Lead",
                "sender_kind": "user",
            },
        }
    )
    assert approve_evt is not None
    res = dispatcher.handle_message_event(approve_evt)

    # Status remains APPROVED
    assert res is not None
    assert res.status == TaskStatus.APPROVED

    # Failure notification posted to room
    sent_bodies = [content for room, content, meta in mock_client.sent_messages]
    assert any("GitHub PR Publication Failed" in b for b in sent_bodies)
    assert any("Approval preserved (safe to retry)" in b for b in sent_bodies)
    assert any("/publish-pr task-55" in b for b in sent_bodies)


def test_switch_publish_pr_command():
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    task = _create_awaiting_approval_task("task-77", "!room2")
    task.transition(TaskStatus.APPROVED, actor="LeadEngineer")
    dispatcher.tasks[task.task_id] = task
    dispatcher.active_task_by_room["!room2"] = task.task_id

    mock_orchestrator = MagicMock()
    mock_orchestrator.publish_draft_pr.return_value = GitHubPRResult(
        pr_url="https://api.github.com/repos/org/repo/pulls/77",
        pr_number=77,
        html_url="https://github.com/org/repo/pull/77",
        branch_name="reporescue/task-77",
        base_branch="main",
        commit_sha="sha77",
        title="[RepoRescue] Title",
        patch_fingerprint="sha256:777",
        draft=True,
    )
    dispatcher.orchestrator = mock_orchestrator

    # 1. Human sends /publish-pr
    pub_evt = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room2",
            "payload": {
                "body": "/publish-pr task-77",
                "sender": "@developer:localhost",
                "sender_name": "Developer",
                "sender_kind": "user",
            },
        }
    )
    assert pub_evt is not None
    res = dispatcher.handle_message_event(pub_evt)
    assert res is not None
    mock_orchestrator.publish_draft_pr.assert_called_once_with(task)

    sent_bodies = [content for room, content, meta in mock_client.sent_messages]
    assert any("Draft Pull Request Created" in b for b in sent_bodies)
    assert any("https://github.com/org/repo/pull/77" in b for b in sent_bodies)

    # 2. Bot sending /publish-pr is rejected
    bot_evt = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room2",
            "payload": {
                "body": "/publish-pr task-77",
                "sender": "@switch-agent-bot:localhost",
                "sender_name": "Bot Agent",
                "sender_kind": "agent",
            },
        }
    )
    assert bot_evt is not None
    mock_orchestrator.publish_draft_pr.reset_mock()
    res2 = dispatcher.handle_message_event(bot_evt)
    assert res2 is None
    mock_orchestrator.publish_draft_pr.assert_not_called()
    assert (
        "Command ignored: Pull Request creation must be initiated by a verified human"
        in mock_client.last_message_content
    )


def test_switch_publish_pr_prefix_matching():
    """Verifies that /publish-pr accepts 8-character prefix for full task UUIDs."""
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    full_uuid = "6eb75ff8-a462-44ee-91ab-e54e1bf0729a"
    task = _create_awaiting_approval_task(full_uuid, "!room:prefix")
    task.transition(TaskStatus.APPROVED, actor="LeadEngineer")
    dispatcher.tasks[task.task_id] = task

    mock_orchestrator = MagicMock()
    mock_orchestrator.publish_draft_pr.return_value = GitHubPRResult(
        pr_url="https://api.github.com/repos/org/repo/pulls/88",
        pr_number=88,
        html_url="https://github.com/org/repo/pull/88",
        branch_name="reporescue/task-6eb75ff8",
        base_branch="main",
        commit_sha="sha88",
        title="[RepoRescue] Title",
        patch_fingerprint="sha256:888",
        draft=True,
    )
    dispatcher.orchestrator = mock_orchestrator

    # User uses short 8-char prefix
    pub_evt = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room:prefix",
            "payload": {
                "body": "/publish-pr 6eb75ff8",
                "sender": "@developer:localhost",
                "sender_name": "Developer",
                "sender_kind": "user",
            },
        }
    )
    assert pub_evt is not None
    res = dispatcher.handle_message_event(pub_evt)
    assert res is not None
    assert res.task_id == full_uuid
    mock_orchestrator.publish_draft_pr.assert_called_once_with(task)


def test_switch_publish_pr_retry_after_push_failure():
    """Verifies that an approved task whose PR push fails can be retried via /publish-pr."""
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    full_uuid = "6eb75ff8-a462-44ee-91ab-e54e1bf0729a"
    task = _create_awaiting_approval_task(full_uuid, "!room:retry")
    task.transition(TaskStatus.APPROVED, actor="LeadEngineer")
    dispatcher.tasks[task.task_id] = task

    mock_orchestrator = MagicMock()
    # 1st attempt fails with push auth error
    mock_orchestrator.publish_draft_pr.side_effect = [
        RuntimeError(
            "fatal: Authentication failed for 'https://github.com/org/repo.git'"
        ),
        GitHubPRResult(
            pr_url="https://api.github.com/repos/org/repo/pulls/99",
            pr_number=99,
            html_url="https://github.com/org/repo/pull/99",
            branch_name="reporescue/task-6eb75ff8",
            base_branch="main",
            commit_sha="sha99",
            title="[RepoRescue] Title",
            patch_fingerprint="sha256:999",
            draft=True,
        ),
    ]
    dispatcher.orchestrator = mock_orchestrator

    # 1. First attempt fails
    evt1 = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room:retry",
            "payload": {
                "body": "/publish-pr 6eb75ff8",
                "sender": "@lead:localhost",
                "sender_name": "Lead",
                "sender_kind": "user",
            },
        }
    )
    res1 = dispatcher.handle_message_event(evt1)
    assert res1 is not None
    assert res1.status == TaskStatus.APPROVED
    assert any(
        "GitHub PR Publication Failed" in b for _, b, _ in mock_client.sent_messages
    )

    # 2. Retry succeeds without re-running fixer
    evt2 = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room:retry",
            "payload": {
                "body": "/publish-pr 6eb75ff8",
                "sender": "@lead:localhost",
                "sender_name": "Lead",
                "sender_kind": "user",
            },
        }
    )
    res2 = dispatcher.handle_message_event(evt2)
    assert res2 is not None
    assert any(
        "Draft Pull Request Created" in b for _, b, _ in mock_client.sent_messages
    )
    assert any(
        "https://github.com/org/repo/pull/99" in b
        for _, b, _ in mock_client.sent_messages
    )
