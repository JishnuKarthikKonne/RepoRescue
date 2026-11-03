from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .models import SwitchMessageEvent


class SwitchError(Exception):
    """Base exception for Switch API communication errors."""


class SwitchAuthError(SwitchError):
    """Raised when authentication with Switch server fails (HTTP 401/403)."""


@dataclass(frozen=True)
class SwitchConfig:
    """Holds configuration parameters for connecting to the Switch Agent Bridge."""

    server_url: str = "http://localhost:65008"
    api_key: str = ""
    agent_id: str = "reporescue"
    default_room_id: str = ""

    @classmethod
    def from_env(cls) -> SwitchConfig:
        """Loads configuration from environment variables."""
        return cls(
            server_url=os.getenv("SWITCH_SERVER_URL", "http://localhost:65008").rstrip(
                "/"
            ),
            api_key=os.getenv("SWITCH_API_KEY", ""),
            agent_id=os.getenv("SWITCH_AGENT_ID", "reporescue"),
            default_room_id=os.getenv("SWITCH_ROOM_ID", ""),
        )


class SwitchClientProtocol(Protocol):
    """Defines the messaging contract for interacting with Switch rooms."""

    def send_message(
        self, room_id: str, content: str, *, metadata: dict[str, Any] | None = None
    ) -> str:
        """Sends a markdown message to the specified Switch room."""
        ...

    def poll_events(self, room_id: str | None = None) -> list[SwitchMessageEvent]:
        """Polls for new message events from Switch."""
        ...


class SwitchClient:
    """Official HTTP client for the Switch Agent Bridge API (0.31.0+).

    Communicates with the Switch server via standard HTTP requests with Bearer token authentication.
    """

    def __init__(self, config: SwitchConfig | None = None) -> None:
        self.config = config or SwitchConfig.from_env()

    def _make_request(
        self, endpoint: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Executes an authenticated HTTP request against the Switch server."""
        url = f"{self.config.server_url}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.agent_id:
            headers["X-Switch-Agent-Id"] = self.config.agent_id

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=10.0) as response:
                content = response.read().decode("utf-8")
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", "replace")
            if err.code in (401, 403):
                raise SwitchAuthError(
                    f"Authentication failed ({err.code}): {err_body}"
                ) from err
            raise SwitchError(
                f"Switch API error ({err.code}) on {url}: {err_body}"
            ) from err
        except urllib.error.URLError as err:
            raise SwitchError(
                f"Failed to connect to Switch server at {url}: {err.reason}"
            ) from err

    def send_message(
        self, room_id: str, content: str, *, metadata: dict[str, Any] | None = None
    ) -> str:
        """Posts a message to a Switch room via POST /agents/{agent_id}/message."""
        endpoint = f"/agents/{self.config.agent_id}/message"
        payload = {
            "room_id": room_id,
            "content": content,
            "metadata": metadata or {},
        }
        res = self._make_request(endpoint, method="POST", body=payload)
        return str(res.get("event_id", ""))

    def poll_events(self, room_id: str | None = None) -> list[SwitchMessageEvent]:
        """Retrieves pending room events for the agent via GET /agents/{agent_id}/events."""
        endpoint = (
            f"/agents/{self.config.agent_id}/rooms/{room_id}/events"
            if room_id
            else f"/agents/{self.config.agent_id}/events"
        )
        res = self._make_request(endpoint, method="GET")
        raw_events = (
            res.get("events", [])
            if isinstance(res, dict)
            else (res if isinstance(res, list) else [])
        )

        parsed: list[SwitchMessageEvent] = []
        for raw in raw_events:
            if isinstance(raw, dict):
                evt = SwitchMessageEvent.from_agent_event(raw)
                if evt is not None:
                    parsed.append(evt)
        return parsed


class MockSwitchClient:
    """In-memory mock Switch client for deterministic unit testing and offline development."""

    def __init__(self, config: SwitchConfig | None = None) -> None:
        self.config = config or SwitchConfig()
        self.sent_messages: list[tuple[str, str, dict[str, Any]]] = []
        self._inbound_queue: list[SwitchMessageEvent] = []

    def queue_event(self, event: SwitchMessageEvent) -> None:
        """Injects an inbound event to be returned on the next poll."""
        self._inbound_queue.append(event)

    def send_message(
        self, room_id: str, content: str, *, metadata: dict[str, Any] | None = None
    ) -> str:
        """Records an outbound message in memory."""
        self.sent_messages.append((room_id, content, metadata or {}))
        return f"mock_event_{len(self.sent_messages)}"

    def poll_events(self, room_id: str | None = None) -> list[SwitchMessageEvent]:
        """Pops and returns queued events matching the room."""
        if room_id is None:
            events = list(self._inbound_queue)
            self._inbound_queue.clear()
            return events

        matched = [e for e in self._inbound_queue if e.room_id == room_id]
        self._inbound_queue = [e for e in self._inbound_queue if e.room_id != room_id]
        return matched

    @property
    def last_message_content(self) -> str | None:
        """Returns the content of the most recently sent message, if any."""
        return self.sent_messages[-1][1] if self.sent_messages else None
