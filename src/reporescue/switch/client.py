from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import SwitchMessageEvent


class SwitchError(Exception):
    """Base exception for Switch API communication errors."""


class SwitchAuthError(SwitchError):
    """Raised when authentication with Switch server fails (HTTP 401/403)."""


@dataclass
class SwitchConfig:
    """Holds configuration parameters for connecting to the Switch Agent Bridge."""

    server_url: str = "http://localhost:65008"
    api_key: str = ""
    agent_id: str = "reporescue"
    default_room_id: str = ""
    registration_token: str = ""

    @classmethod
    def from_env(cls) -> SwitchConfig:
        """Loads configuration from environment variables or local switch-core.env."""
        server_url = os.getenv("SWITCH_SERVER_URL", "http://127.0.0.1:65008").rstrip(
            "/"
        )
        api_key = os.getenv("SWITCH_API_KEY", "")
        agent_id = os.getenv("SWITCH_AGENT_ID", "reporescue")
        default_room_id = os.getenv("SWITCH_ROOM_ID", "")
        registration_token = os.getenv("SWITCH_REGISTRATION_TOKEN", "") or os.getenv(
            "AGENT_REGISTRATION_TOKEN", ""
        )

        # Fallback to local switch-core.env if running against local switch server
        if not registration_token and not api_key:
            appdata = os.environ.get("APPDATA", "")
            candidates = [
                Path(appdata)
                / "switchdash"
                / "local-switch-server"
                / "native"
                / "run"
                / "switch-core.env"
                if appdata
                else None,
                Path.home()
                / ".config"
                / "switchdash"
                / "local-switch-server"
                / "native"
                / "run"
                / "switch-core.env",
            ]
            for candidate in candidates:
                if candidate and candidate.is_file():
                    try:
                        for line in candidate.read_text(encoding="utf-8").splitlines():
                            line = line.strip()
                            if (
                                line.startswith("AGENT_REGISTRATION_TOKEN=")
                                and not registration_token
                            ):
                                registration_token = line.split("=", 1)[1].strip()
                    except OSError:
                        pass
                if registration_token:
                    break

        return cls(
            server_url=server_url,
            api_key=api_key,
            agent_id=agent_id,
            default_room_id=default_room_id,
            registration_token=registration_token,
        )


class SwitchClientProtocol(Protocol):
    """Defines the messaging contract for interacting with Switch rooms."""

    def send_message(
        self, room_id: str, content: str, *, metadata: dict[str, Any] | None = None
    ) -> str:
        """Sends a markdown message to the specified Switch room."""
        ...

    def poll_events(
        self, room_id: str | None = None, *, timeout: float = 2.0
    ) -> list[SwitchMessageEvent]:
        """Polls for new message events from Switch."""
        ...

    def register_agent(
        self,
        token: str | None = None,
        *,
        name: str = "reporescue",
        description: str = "RepoRescue Autonomous Engineering Teammate",
        display_name: str = "RepoRescue",
        overwrite: bool = True,
    ) -> tuple[str, str]:
        """Registers the agent with Switch server."""
        ...

    def create_room(
        self,
        name: str,
        description: str = "",
        *,
        internal_only: bool = True,
    ) -> dict[str, Any]:
        """Creates a Switch room for the agent."""
        ...

    def list_rooms(self) -> list[dict[str, Any]]:
        """Lists available Switch rooms for the agent."""
        ...

    def get_room_history(self, room_id: str) -> dict[str, Any]:
        """Fetches room message history."""
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

    def register_agent(
        self,
        token: str | None = None,
        *,
        name: str = "reporescue",
        description: str = "RepoRescue Autonomous Engineering Teammate",
        display_name: str = "RepoRescue",
        overwrite: bool = True,
    ) -> tuple[str, str]:
        """Registers the agent with Switch server using a registration token.

        Returns:
            tuple[str, str]: (agent_id, api_key)
        """
        reg_token = (
            token
            or self.config.registration_token
            or os.getenv("SWITCH_REGISTRATION_TOKEN", "")
            or os.getenv("AGENT_REGISTRATION_TOKEN", "")
        )
        if not reg_token:
            raise SwitchAuthError(
                "Cannot register agent: no registration token provided."
            )

        payload = {
            "name": name,
            "description": description,
            "display_name": display_name,
            "connector_type": "hosted",
            "integration_profile": {
                "connection_model": "always_on",
                "message_exchange": True,
                "pre_invocation_mediation": [],
                "post_invocation_mediation": [],
                "event_reporting": [],
            },
            "overwrite": overwrite,
        }

        url = f"{self.config.server_url}/agents"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {reg_token}",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                agent_id = result["id"]
                api_key = result["api_key"]
                self.config.agent_id = agent_id
                self.config.api_key = api_key
                return agent_id, api_key
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", "replace")
            if err.code in (401, 403):
                raise SwitchAuthError(
                    f"Agent registration failed ({err.code}): {err_body}"
                ) from err
            raise SwitchError(
                f"Agent registration error ({err.code}): {err_body}"
            ) from err
        except urllib.error.URLError as err:
            raise SwitchError(
                f"Failed to connect to Switch server at {url}: {err.reason}"
            ) from err

    def create_room(
        self,
        name: str,
        description: str = "",
        *,
        internal_only: bool = True,
    ) -> dict[str, Any]:
        """Creates a Switch room for the agent via POST /agents/{agent_id}/moderation/rooms."""
        endpoint = f"/agents/{self.config.agent_id}/moderation/rooms"
        payload = {
            "name": name,
            "description": description,
            "agent_names": [self.config.agent_id],
            "internal_only": internal_only,
        }
        return self._make_request(endpoint, method="POST", body=payload)

    def list_rooms(self) -> list[dict[str, Any]]:
        """Lists available Switch rooms for the agent."""
        endpoint = f"/agents/{self.config.agent_id}/moderation/rooms"
        res = self._make_request(endpoint, method="GET")
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("rooms", [])
        return []

    def get_room_history(self, room_id: str) -> dict[str, Any]:
        """Fetches room message history from Switch."""
        endpoint = f"/agents/{self.config.agent_id}/rooms/{room_id}/history"
        return self._make_request(endpoint, method="GET")

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

    def poll_events(
        self, room_id: str | None = None, *, timeout: float = 2.0
    ) -> list[SwitchMessageEvent]:
        """Retrieves pending room events for the agent via GET /agents/{agent_id}/events."""
        endpoint = (
            f"/agents/{self.config.agent_id}/rooms/{room_id}/events?timeout={timeout}"
            if room_id
            else f"/agents/{self.config.agent_id}/events?timeout={timeout}"
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
        self.rooms: list[dict[str, Any]] = []

    def register_agent(
        self,
        token: str | None = None,
        *,
        name: str = "reporescue",
        description: str = "RepoRescue Autonomous Engineering Teammate",
        display_name: str = "RepoRescue",
        overwrite: bool = True,
    ) -> tuple[str, str]:
        """Simulates agent registration."""
        agent_id = f"mock-agent-{name}"
        api_key = "mock-api-key"
        self.config.agent_id = agent_id
        self.config.api_key = api_key
        return agent_id, api_key

    def create_room(
        self,
        name: str,
        description: str = "",
        *,
        internal_only: bool = True,
    ) -> dict[str, Any]:
        """Simulates creating a Switch room."""
        room_id = f"mock-room-{len(self.rooms)}"
        room = {"id": room_id, "name": name, "description": description}
        self.rooms.append(room)
        return room

    def list_rooms(self) -> list[dict[str, Any]]:
        """Simulates listing rooms."""
        return list(self.rooms)

    def get_room_history(self, room_id: str) -> dict[str, Any]:
        """Simulates fetching room history."""
        return {"events": []}

    def queue_event(self, event: SwitchMessageEvent) -> None:
        """Injects an inbound event to be returned on the next poll."""
        self._inbound_queue.append(event)

    def send_message(
        self, room_id: str, content: str, *, metadata: dict[str, Any] | None = None
    ) -> str:
        """Records an outbound message in memory."""
        self.sent_messages.append((room_id, content, metadata or {}))
        return f"mock_event_{len(self.sent_messages)}"

    def poll_events(
        self, room_id: str | None = None, *, timeout: float = 2.0
    ) -> list[SwitchMessageEvent]:
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
