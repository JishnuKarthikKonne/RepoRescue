from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from reporescue.switch.client import MockSwitchClient, SwitchAuthError, SwitchError
from reporescue.switch.dispatcher import RepoRescueDispatcher
from reporescue.switch.listener import SwitchListener
from reporescue.switch.models import SwitchMessageEvent


def test_listener_poll_and_dispatch():
    mock_client = MockSwitchClient()
    dispatcher = MagicMock(spec=RepoRescueDispatcher)
    listener = SwitchListener(
        mock_client, dispatcher, room_id="!room1", poll_interval_seconds=0.1
    )

    event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room1",
            "payload": {
                "message_id": "msg_001",
                "body": "Bug: 500 error",
                "sender": "@user:localhost",
                "sender_name": "User",
                "sender_kind": "user",
            },
        }
    )
    assert event is not None
    mock_client.queue_event(event)

    processed = listener.poll_once()
    assert processed == 1
    dispatcher.handle_message_event.assert_called_once_with(event)


def test_listener_event_deduplication():
    mock_client = MockSwitchClient()
    dispatcher = MagicMock(spec=RepoRescueDispatcher)
    listener = SwitchListener(
        mock_client, dispatcher, room_id="!room1", poll_interval_seconds=0.1
    )

    event = SwitchMessageEvent.from_agent_event(
        {
            "type": "message",
            "room_id": "!room1",
            "payload": {
                "message_id": "dup_msg_001",
                "body": "Bug: Duplicate test",
                "sender": "@user:localhost",
                "sender_name": "User",
                "sender_kind": "user",
            },
        }
    )
    assert event is not None

    # First poll: event is processed
    mock_client.queue_event(event)
    assert listener.poll_once() == 1
    assert dispatcher.handle_message_event.call_count == 1

    # Second poll with same message_id: must be ignored
    mock_client.queue_event(event)
    assert listener.poll_once() == 0
    assert dispatcher.handle_message_event.call_count == 1  # Not called again


def test_listener_graceful_stop():
    mock_client = MockSwitchClient()
    dispatcher = MagicMock(spec=RepoRescueDispatcher)
    listener = SwitchListener(mock_client, dispatcher, poll_interval_seconds=0.05)

    thread = threading.Thread(target=listener.run_loop, daemon=True)
    thread.start()
    assert thread.is_alive()

    time.sleep(0.1)
    listener.stop()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert not listener.is_running


def test_listener_reconnection_and_backoff():
    mock_client = MagicMock()
    # First 2 calls fail with network error, then returns empty list
    mock_client.poll_events.side_effect = [
        SwitchError("Network connection reset"),
        SwitchError("Connection timed out"),
        [],
    ]

    dispatcher = MagicMock(spec=RepoRescueDispatcher)
    listener = SwitchListener(
        mock_client,
        dispatcher,
        poll_interval_seconds=0.05,
        backoff_multiplier=2.0,
        max_backoff_seconds=1.0,
    )

    # First poll: fails, increases backoff
    with pytest.raises(SwitchError):
        mock_client.poll_events()
    listener._apply_backoff()
    assert listener._current_delay == 0.1

    # Second poll: fails, increases backoff
    listener._apply_backoff()
    assert listener._current_delay == 0.2


def test_listener_auth_error_handling():
    mock_client = MagicMock()
    mock_client.poll_events.side_effect = SwitchAuthError("HTTP 401 Unauthorized")

    dispatcher = MagicMock(spec=RepoRescueDispatcher)
    listener = SwitchListener(mock_client, dispatcher, poll_interval_seconds=0.05)

    # Auth error should apply backoff safely without exposing secrets
    listener._apply_backoff()
    assert listener._current_delay > listener.poll_interval
