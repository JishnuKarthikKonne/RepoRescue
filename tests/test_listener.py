from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from reporescue.switch.client import (
    MockSwitchClient,
    SwitchAuthError,
    SwitchError,
    SwitchMembershipError,
    SwitchRoomNotFoundError,
)
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
    assert 0.08 <= listener._current_delay <= 0.12

    # Second poll: fails, increases backoff
    listener._apply_backoff()
    assert 0.16 <= listener._current_delay <= 0.25


def test_listener_auth_error_handling():
    mock_client = MagicMock()
    mock_client.poll_events.side_effect = SwitchAuthError("HTTP 401 Unauthorized")

    dispatcher = MagicMock(spec=RepoRescueDispatcher)
    listener = SwitchListener(mock_client, dispatcher, poll_interval_seconds=0.05)

    # Auth error should stop the listener immediately in run_loop
    listener.run_loop()
    assert not listener.is_running


def test_listener_404_room_not_found_stops_immediately():
    """Verifies that HTTP 404 Room not found stops the listener loop immediately without infinite retries."""
    mock_client = MagicMock()
    mock_client.poll_events.side_effect = SwitchRoomNotFoundError(
        "Room not found: !reporescue:localhost"
    )

    dispatcher = MagicMock(spec=RepoRescueDispatcher)
    listener = SwitchListener(
        mock_client,
        dispatcher,
        room_id="!reporescue:localhost",
        poll_interval_seconds=0.01,
    )

    listener.run_loop()
    assert not listener.is_running
    # Must only have been called once before stopping
    assert mock_client.poll_events.call_count == 1


def test_listener_membership_error_stops_immediately():
    """Verifies that SwitchMembershipError stops listener without endless retries."""
    mock_client = MagicMock()
    mock_client.poll_events.side_effect = SwitchMembershipError(
        "Agent lacks membership"
    )

    dispatcher = MagicMock(spec=RepoRescueDispatcher)
    listener = SwitchListener(
        mock_client, dispatcher, room_id="room-x", poll_interval_seconds=0.01
    )

    listener.run_loop()
    assert not listener.is_running
    assert mock_client.poll_events.call_count == 1


def test_listener_transient_failures_stop_after_max_retries():
    """Verifies that transient errors retry up to 10 times then stop gracefully."""
    mock_client = MagicMock()
    mock_client.poll_events.side_effect = SwitchError("Transient connection drop")

    dispatcher = MagicMock(spec=RepoRescueDispatcher)
    listener = SwitchListener(
        mock_client, dispatcher, room_id="room-t", poll_interval_seconds=0.001
    )

    listener.run_loop()
    assert not listener.is_running
    assert mock_client.poll_events.call_count == 10


def test_mock_client_resolve_and_verify_room():
    mock_client = MockSwitchClient()
    mock_client.rooms = [{"id": "room-123", "name": "RepoRescue Bug Triage"}]

    # 1. Matching existing room
    room_id, room_name = mock_client.resolve_and_verify_room("RepoRescue Bug Triage")
    assert room_id == "room-123"
    assert room_name == "RepoRescue Bug Triage"

    # 2. Room discovery when None requested
    room_id, room_name = mock_client.resolve_and_verify_room(None)
    assert room_id == "room-123"

    # 3. Missing room with auto_create=False raises SwitchRoomNotFoundError
    with pytest.raises(SwitchRoomNotFoundError):
        mock_client.resolve_and_verify_room("non-existent-room", auto_create=False)

    # 4. Missing room with auto_create=True creates room
    room_id, room_name = mock_client.resolve_and_verify_room(
        "new-triage-room", auto_create=True
    )
    assert room_id == "new-triage-room"
    assert room_name == "new-triage-room"
    assert any(r["id"] == "new-triage-room" for r in mock_client.rooms)
