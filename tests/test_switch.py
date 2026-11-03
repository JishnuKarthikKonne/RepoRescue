from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from reporescue.models import TaskStatus
from reporescue.switch.client import (
    MockSwitchClient,
    SwitchAuthError,
    SwitchClient,
    SwitchConfig,
    SwitchError,
)
from reporescue.switch.dispatcher import RepoRescueDispatcher
from reporescue.switch.models import SwitchMessageEvent


def test_switch_message_event_from_official_agent_event():
    raw_event = {
        "type": "message",
        "room_id": "!reporescue:localhost",
        "channel_type": "mattermost",
        "payload": {
            "addressed": True,
            "sender": "@jishnu:localhost",
            "sender_name": "Jishnu",
            "on_behalf_of": "Jishnu",
            "message_id": "$event_101",
            "body": "Bug: checkout returns HTTP 500 when coupon is applied",
            "timestamp": 1730615000000,
            "thread_id": "thread_42",
        },
    }

    event = SwitchMessageEvent.from_agent_event(raw_event)
    assert event is not None
    assert event.room_id == "!reporescue:localhost"
    assert event.channel_type == "mattermost"
    assert event.sender_name == "Jishnu"
    assert event.message_id == "$event_101"
    assert "checkout returns HTTP 500" in event.body
    assert event.thread_id == "thread_42"
    assert event.addressed is True


def test_switch_message_event_invalid_payloads():
    # Non-message type
    assert SwitchMessageEvent.from_agent_event({"type": "room_join"}) is None

    # Empty payload
    assert (
        SwitchMessageEvent.from_agent_event({"type": "message", "room_id": "!room"})
        is None
    )

    # Missing body
    assert (
        SwitchMessageEvent.from_agent_event(
            {"type": "message", "room_id": "!room", "payload": {}}
        )
        is None
    )


def test_switch_config_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SWITCH_SERVER_URL", "http://127.0.0.1:65008/")
    monkeypatch.setenv("SWITCH_API_KEY", "sw_test_secret_key")
    monkeypatch.setenv("SWITCH_AGENT_ID", "custom_agent")
    monkeypatch.setenv("SWITCH_ROOM_ID", "!testroom:localhost")

    cfg = SwitchConfig.from_env()
    assert cfg.server_url == "http://127.0.0.1:65008"
    assert cfg.api_key == "sw_test_secret_key"
    assert cfg.agent_id == "custom_agent"
    assert cfg.default_room_id == "!testroom:localhost"


def test_switch_client_send_message_success():
    cfg = SwitchConfig(
        server_url="http://switch.local:65008", api_key="secret", agent_id="rescue"
    )
    client = SwitchClient(cfg)

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {"ok": True, "event_id": "$evt_999"}
    ).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        event_id = client.send_message("!room_1", "Test message")
        assert event_id == "$evt_999"
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.get_full_url() == "http://switch.local:65008/agents/rescue/message"
        assert req.headers["Authorization"] == "Bearer secret"
        assert req.headers["X-switch-agent-id"] == "rescue"


def test_switch_client_auth_error_handling():
    client = SwitchClient(SwitchConfig(server_url="http://switch.local", api_key="bad"))

    http_err = urllib.error.HTTPError(
        url="http://switch.local/agents/reporescue/message",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"detail": "Invalid API Key"}'),
    )

    with (
        patch("urllib.request.urlopen", side_effect=http_err),
        pytest.raises(SwitchAuthError, match="Authentication failed"),
    ):
        client.send_message("!room", "Hello")


def test_switch_client_connection_error_handling():
    client = SwitchClient(SwitchConfig(server_url="http://nonexistent.switch.local"))

    url_err = urllib.error.URLError("Connection refused")

    with (
        patch("urllib.request.urlopen", side_effect=url_err),
        pytest.raises(SwitchError, match="Failed to connect to Switch server"),
    ):
        client.send_message("!room", "Hello")


def test_mock_switch_client_queue_and_poll():
    mock_client = MockSwitchClient()
    event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room_1",
            "payload": {"body": "Test message", "sender": "@dev", "sender_name": "Dev"},
        }
    )
    assert event is not None
    mock_client.queue_event(event)

    polled = mock_client.poll_events("!room_1")
    assert len(polled) == 1
    assert polled[0].body == "Test message"

    # Queue should now be empty
    assert len(mock_client.poll_events("!room_1")) == 0


def test_dispatcher_workflow_lifecycle_end_to_end():
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    # 1. User reports bug in Switch room
    report_event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!reporescue:localhost",
            "payload": {
                "body": "Bug: Payment gateway throws 500 when currency is EUR\nSteps to reproduce: POST /checkout with currency=EUR",
                "sender": "@alice:localhost",
                "sender_name": "Alice",
            },
        }
    )
    assert report_event is not None
    task = dispatcher.handle_message_event(report_event)
    assert task is not None
    assert task.status == TaskStatus.REPORTED
    assert "Payment gateway throws 500" in task.title
    assert task.reporter == "Alice"
    assert "RepoRescue Received Bug Report" in mock_client.last_message_content

    # 2. Triage card published
    dispatcher.publish_triage(
        task.task_id,
        report_event.room_id,
        severity="P1",
        acceptance_criteria="EUR checkout returns HTTP 200 with converted rate",
    )
    assert task.status == TaskStatus.TRIAGED
    assert "Issue Triaged" in mock_client.last_message_content

    # 3. Reproduction card published
    dispatcher.publish_reproduction(
        task.task_id,
        report_event.room_id,
        test_file="tests/test_currency.py::test_eur_checkout",
        exit_code=1,
        output_snippet="FAILED tests/test_currency.py - ValueError: Unsupported currency EUR",
    )
    assert task.status == TaskStatus.REPRODUCED
    assert "Bug Reproduced" in mock_client.last_message_content

    # 4. Fix proposed card published
    dispatcher.publish_fix_proposed(
        task.task_id,
        report_event.room_id,
        branch_name="reporescue/issue-eur-fix",
        diff_snippet="+ if currency == 'EUR': rate = 1.08",
        rationale="Add currency conversion table for EUR",
    )
    assert task.status == TaskStatus.FIX_PROPOSED
    assert task.proposed_branch == "reporescue/issue-eur-fix"
    assert "Fix Proposed" in mock_client.last_message_content

    # 5. Verification report published
    dispatcher.publish_verification(
        task.task_id,
        report_event.room_id,
        passed_count=12,
        failed_count=0,
        duration_s=1.45,
        evidence_text="Pytest output: 12 passed in 1.45s",
        verified=True,
    )
    assert task.status == TaskStatus.AWAITING_APPROVAL
    assert "Human Approval Required" in mock_client.last_message_content

    # 6. Human approves in the Switch room: /approve
    approve_event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!reporescue:localhost",
            "payload": {
                "body": "/approve",
                "sender": "@bob:localhost",
                "sender_name": "Bob (Lead Dev)",
            },
        }
    )
    assert approve_event is not None
    approved_task = dispatcher.handle_message_event(approve_event)
    assert approved_task is not None
    assert approved_task.status == TaskStatus.APPROVED
    assert approved_task.approval_by == "Bob (Lead Dev)"
    assert "Human Approval Granted" in mock_client.last_message_content
    assert "@Bob (Lead Dev)" in mock_client.last_message_content


def test_dispatcher_rejection_flow():
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    # Ingest bug report
    evt = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room",
            "payload": {
                "body": "Bug: calculation error",
                "sender": "@dev",
                "sender_name": "Dev",
            },
        }
    )
    assert evt is not None
    task = dispatcher.handle_message_event(evt)
    assert task is not None

    # Advance to AWAITING_APPROVAL
    task.transition(TaskStatus.TRIAGED, actor="t")
    task.transition(TaskStatus.REPRODUCING, actor="r")
    task.transition(TaskStatus.REPRODUCED, actor="r")
    task.transition(TaskStatus.FIX_PROPOSED, actor="f")
    task.transition(TaskStatus.VERIFYING, actor="v")
    task.transition(TaskStatus.VERIFIED, actor="v")
    task.transition(TaskStatus.AWAITING_APPROVAL, actor="v")

    # User rejects: /reject Needs edge case test
    reject_evt = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room",
            "payload": {
                "body": "/reject Needs edge case test",
                "sender": "@reviewer",
                "sender_name": "Reviewer",
            },
        }
    )
    assert reject_evt is not None
    rejected_task = dispatcher.handle_message_event(reject_evt)
    assert rejected_task is not None
    assert rejected_task.status == TaskStatus.REJECTED
    assert "Task Rejected" in mock_client.last_message_content


def test_approval_rejected_from_agent_or_bot():
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    # Ingest task and transition to awaiting approval
    task = dispatcher.handle_message_event(
        SwitchMessageEvent.from_agent_event(
            {
                "type": "message",
                "room_id": "!room",
                "payload": {
                    "body": "Bug: memory leak",
                    "sender": "@dev",
                    "sender_name": "Dev",
                    "sender_kind": "user",
                },
            }
        )
    )
    assert task is not None
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

    # Bot attempts to approve: sender_kind="agent"
    bot_event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room",
            "payload": {
                "body": "/approve",
                "sender": "agent:auto-fixer",
                "sender_name": "AutoFixerAgent",
                "sender_kind": "agent",
            },
        }
    )
    assert bot_event is not None
    result = dispatcher.handle_message_event(bot_event)
    assert result is None
    # Task must NOT have transitioned to APPROVED
    assert task.status == TaskStatus.AWAITING_APPROVAL
    assert task.approval_by is None
    assert "Approval rejected" in mock_client.last_message_content
    assert "automated agent or bot" in mock_client.last_message_content


def test_approval_rejected_from_bot_display_name():
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    task = dispatcher.handle_message_event(
        SwitchMessageEvent.from_agent_event(
            {
                "type": "message",
                "room_id": "!room",
                "payload": {
                    "body": "Bug: crash",
                    "sender": "@dev",
                    "sender_name": "Dev",
                    "sender_kind": "user",
                },
            }
        )
    )
    assert task is not None
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

    # Name ending in 'bot' or matching 'reporescue'
    bot_name_event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room",
            "payload": {
                "body": "/approve",
                "sender": "@ci_bot:localhost",
                "sender_name": "AutoMergeBot",
            },
        }
    )
    assert bot_name_event is not None
    result = dispatcher.handle_message_event(bot_name_event)
    assert result is None
    assert task.status == TaskStatus.AWAITING_APPROVAL


def test_approval_accepted_from_verified_human_records_timestamp():
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    task = dispatcher.handle_message_event(
        SwitchMessageEvent.from_agent_event(
            {
                "type": "message",
                "room_id": "!room",
                "payload": {
                    "body": "Bug: crash",
                    "sender": "@dev",
                    "sender_name": "Dev",
                    "sender_kind": "user",
                },
            }
        )
    )
    assert task is not None
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

    human_event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room",
            "payload": {
                "body": "/approve",
                "sender": "@jishnu:localhost",
                "sender_name": "Jishnu",
                "sender_kind": "user",
                "timestamp": 1730616000000,
            },
        }
    )
    assert human_event is not None
    result = dispatcher.handle_message_event(human_event)
    assert result is not None
    assert result.status == TaskStatus.APPROVED
    assert result.approval_by == "Jishnu"
    assert result.approved_at is not None
    assert result.approved_at == human_event.timestamp


def test_approval_rejected_when_not_in_awaiting_approval_state():
    mock_client = MockSwitchClient()
    dispatcher = RepoRescueDispatcher(mock_client)

    task = dispatcher.handle_message_event(
        SwitchMessageEvent.from_agent_event(
            {
                "type": "message",
                "room_id": "!room",
                "payload": {
                    "body": "Bug: premature approval test",
                    "sender": "@dev",
                    "sender_name": "Dev",
                    "sender_kind": "user",
                },
            }
        )
    )
    assert task is not None
    assert task.status == TaskStatus.REPORTED

    # Attempting to approve while still in REPORTED state
    premature_event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room",
            "payload": {
                "body": "/approve",
                "sender": "@lead:localhost",
                "sender_name": "Lead Dev",
                "sender_kind": "user",
            },
        }
    )
    assert premature_event is not None
    result = dispatcher.handle_message_event(premature_event)
    assert result is not None
    assert result.status == TaskStatus.REPORTED  # Did not change
    assert "Approval rejected" in mock_client.last_message_content
    assert "Mandatory verification tests must pass" in mock_client.last_message_content
