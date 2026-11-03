from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import SwitchMessageEvent

logger = logging.getLogger("reporescue.switch")


class SwitchError(Exception):
    """Base exception for Switch API communication errors."""


class SwitchAuthError(SwitchError):
    """Raised when authentication with Switch server fails (HTTP 401/403)."""


class SwitchRoomNotFoundError(SwitchError):
    """Raised when the specified room or resource is not found (HTTP 404)."""


class SwitchMembershipError(SwitchError):
    """Raised when agent lacks membership or permission to access/post to a room."""


@dataclass
class SwitchConfig:
    """Holds configuration parameters for connecting to the Switch Agent Bridge."""

    server_url: str = "http://localhost:65008"
    api_key: str = ""
    agent_id: str = "reporescue"
    agent_name: str = "reporescue-agent"
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

    def check_health(self) -> bool:
        """Verifies health of Switch Core."""
        ...

    def is_room_accessible(self, room_id: str) -> bool:
        """Verifies if the agent has valid membership/access in the room without raising."""
        ...

    def get_room_detail(self, room_id: str) -> dict[str, Any]:
        """Fetches room detail by ID."""
        ...

    def invite_agent(self, room_id: str, agent_name: str) -> dict[str, Any]:
        """Invites an agent to a room."""
        ...

    def get_room_participants(self, room_id: str) -> list[dict[str, Any]]:
        """Lists participants in a room."""
        ...

    def update_status(
        self,
        room_id: str | None = None,
        *,
        presence: str = "online",
        status: str = "RepoRescue Active",
    ) -> dict[str, Any]:
        """Updates agent status in Switch."""
        ...

    def send_heartbeat(self) -> dict[str, Any]:
        """Sends periodic heartbeat to Switch."""
        ...

    def resolve_and_verify_room(
        self,
        requested_room: str | None = None,
        *,
        auto_create: bool = True,
    ) -> tuple[str, str]:
        """Resolves target room, verifies membership, and auto-creates if needed."""
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
                    f"Authentication failed with Switch server ({err.code}): {err_body}"
                ) from err
            if err.code == 404:
                raise SwitchRoomNotFoundError(
                    f"Switch room or resource not found ({err.code}): {err_body}"
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
                self.config.agent_name = name
                return agent_id, api_key
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", "replace")
            # If 409 conflict and name is "reporescue", retry once with "reporescue-agent"
            if err.code == 409 and name == "reporescue":
                logger.info(
                    "Agent name 'reporescue' has owner conflict; retrying registration with 'reporescue-agent'"
                )
                return self.register_agent(
                    token=token,
                    name="reporescue-agent",
                    description=description,
                    display_name=display_name,
                    overwrite=overwrite,
                )
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

    def update_status(
        self,
        room_id: str | None = None,
        *,
        presence: str = "online",
        status: str = "RepoRescue Active",
    ) -> dict[str, Any]:
        """Updates agent connector status in Switch Console via POST /agents/{agent_id}/status."""
        endpoint = f"/agents/{self.config.agent_id}/status"
        payload = {
            "room_id": room_id or self.config.default_room_id or "default",
            "presence": presence,
            "status": status,
        }
        try:
            return self._make_request(endpoint, method="POST", body=payload)
        except Exception as err:  # noqa: BLE001
            logger.debug("Failed to update status on Switch: %s", err)
            return {"ok": False}

    def send_heartbeat(self) -> dict[str, Any]:
        """Refreshes agent watching heartbeat via POST /agents/{agent_id}/watch/heartbeat."""
        endpoint = f"/agents/{self.config.agent_id}/watch/heartbeat"
        try:
            return self._make_request(endpoint, method="POST", body={})
        except Exception as err:  # noqa: BLE001
            logger.debug("Failed to send heartbeat to Switch: %s", err)
            return {"ok": False}

    def create_room(
        self,
        name: str,
        description: str = "",
        *,
        internal_only: bool = True,
    ) -> dict[str, Any]:
        """Creates a Switch room for the agent via POST /agents/{agent_id}/moderation/rooms."""
        endpoint = f"/agents/{self.config.agent_id}/moderation/rooms"
        target_name = getattr(self.config, "agent_name", None) or "reporescue-agent"
        payload = {
            "name": name,
            "description": description,
            "agent_names": [target_name],
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

    def check_health(self) -> bool:
        """Verifies health of Switch Core via GET /health."""
        try:
            req = urllib.request.Request(
                f"{self.config.server_url}/health", method="GET"
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("status") == "ok"
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def is_room_accessible(self, room_id: str) -> bool:
        """Verifies if the agent has valid membership/access in the room without raising."""
        try:
            self.get_room_history(room_id)
            return True
        except (SwitchError, OSError):
            return False

    def get_room_detail(self, room_id: str) -> dict[str, Any]:
        """Fetches room detail via GET /agents/{agent_id}/moderation/rooms/{room_id}."""
        endpoint = f"/agents/{self.config.agent_id}/moderation/rooms/{room_id}"
        return self._make_request(endpoint, method="GET")

    def invite_agent(self, room_id: str, agent_name: str) -> dict[str, Any]:
        """Invites an agent to a room via POST /agents/{agent_id}/moderation/rooms/{room_id}/invite."""
        endpoint = f"/agents/{self.config.agent_id}/moderation/rooms/{room_id}/invite"
        payload = {"agent_name": agent_name}
        return self._make_request(endpoint, method="POST", body=payload)

    def get_room_participants(self, room_id: str) -> list[dict[str, Any]]:
        """Fetches room participants via GET /agents/rooms/{room_id}/participants."""
        endpoint = f"/agents/rooms/{room_id}/participants"
        res = self._make_request(endpoint, method="GET")
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("participants", [])
        return []

    def resolve_and_verify_room(
        self,
        requested_room: str | None = None,
        *,
        auto_create: bool = True,
    ) -> tuple[str, str]:
        """Resolves target room, verifies membership, and auto-creates if needed.

        Returns:
            tuple[str, str]: (room_id, room_name)
        """
        # Step 1: Discover available rooms for the agent
        available_rooms = self.list_rooms()

        # Step 2: If a specific room ID/name was requested
        if requested_room and requested_room.strip():
            req_clean = requested_room.strip()
            # Match by ID or Name
            for r in available_rooms:
                r_id = str(r.get("id", ""))
                r_name = str(r.get("name", ""))
                if req_clean in (r_id, r_name) or req_clean.lower() == r_name.lower():
                    self._verify_room_access(r_id)
                    return r_id, r_name

            # Not found in list_rooms; try direct detail fetch
            try:
                detail = self.get_room_detail(req_clean)
                r_id = str(detail.get("id", req_clean))
                r_name = str(detail.get("name", req_clean))
                self._verify_room_access(r_id)
                return r_id, r_name
            except SwitchRoomNotFoundError:
                pass

            # Room does not exist; auto-create if permitted
            if auto_create:
                logger.info(
                    "Requested room '%s' not found on Switch. Creating dedicated room...",
                    req_clean,
                )
                created = self.create_room(
                    name=req_clean, description="RepoRescue Bug Triage Room"
                )
                new_id = str(created.get("id", ""))
                new_name = str(created.get("name", req_clean))
                if not new_id:
                    raise SwitchRoomNotFoundError(
                        f"Failed to create room '{req_clean}': response missing room ID"
                    )
                self._verify_room_access(new_id)
                return new_id, new_name
            else:
                raise SwitchRoomNotFoundError(
                    f"Room '{req_clean}' does not exist on Switch server. "
                    f"Available rooms: {[r.get('name') for r in available_rooms]}"
                )

        # Step 3: No specific room requested; select or create
        if available_rooms:
            # Check candidate rooms that the agent is actually a member of
            # Prefer room with 'reporescue' in name
            prioritized = sorted(
                available_rooms,
                key=lambda r: (
                    0 if "reporescue" in str(r.get("name", "")).lower() else 1
                ),
            )
            for cand in prioritized:
                cand_id = str(cand.get("id", ""))
                cand_name = str(cand.get("name", "Switch Room"))
                if self.is_room_accessible(cand_id):
                    return cand_id, cand_name

        # If no accessible rooms exist: auto-create dedicated triage room
        if auto_create:
            logger.info(
                "No accessible rooms found on Switch for agent. Creating 'RepoRescue Bug Triage'..."
            )
            created = self.create_room(
                name="RepoRescue Bug Triage",
                description="Autonomous Bug Triage & Engineering Room",
            )
            new_id = str(created.get("id", ""))
            new_name = str(created.get("name", "RepoRescue Bug Triage"))
            if not new_id:
                raise SwitchRoomNotFoundError(
                    "Failed to create default triage room: response missing room ID"
                )
            self._verify_room_access(new_id)
            return new_id, new_name

        raise SwitchRoomNotFoundError(
            "No Switch rooms available for agent and auto_create is False."
        )

    def _verify_room_access(self, room_id: str) -> None:
        """Verifies that the agent has valid access/membership in the room."""
        try:
            self.get_room_history(room_id)
        except SwitchAuthError as err:
            raise SwitchMembershipError(
                f"Agent '{self.config.agent_id}' lacks membership or access in room '{room_id}': {err}"
            ) from err
        except SwitchRoomNotFoundError as err:
            raise SwitchRoomNotFoundError(
                f"Room '{room_id}' not found during membership verification: {err}"
            ) from err
        except (SwitchError, OSError, ValueError) as err:
            logger.warning("Room access check warning for '%s': %s", room_id, err)


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

    def check_health(self) -> bool:
        """Simulates checking Switch health."""
        return True

    def is_room_accessible(self, room_id: str) -> bool:
        """Simulates checking if room is accessible."""
        return any(r.get("id") == room_id for r in self.rooms)

    def get_room_detail(self, room_id: str) -> dict[str, Any]:
        """Simulates fetching room detail."""
        for r in self.rooms:
            if r.get("id") == room_id or r.get("name") == room_id:
                return r
        raise SwitchRoomNotFoundError(f"Room not found: {room_id}")

    def invite_agent(self, room_id: str, agent_name: str) -> dict[str, Any]:
        """Simulates inviting agent to room."""
        return {"ok": True}

    def get_room_participants(self, room_id: str) -> list[dict[str, Any]]:
        """Simulates getting room participants."""
        return [{"id": self.config.agent_id, "name": "RepoRescue"}]

    def resolve_and_verify_room(
        self,
        requested_room: str | None = None,
        *,
        auto_create: bool = True,
    ) -> tuple[str, str]:
        """Simulates resolving and verifying target room."""
        if requested_room and requested_room.strip():
            req_clean = requested_room.strip()
            for r in self.rooms:
                if r.get("id") == req_clean or r.get("name") == req_clean:
                    return r["id"], r.get("name", req_clean)
            if auto_create:
                new_room = {"id": req_clean, "name": req_clean}
                self.rooms.append(new_room)
                return req_clean, req_clean
            raise SwitchRoomNotFoundError(f"Mock room '{req_clean}' not found.")
        if self.rooms:
            return self.rooms[0]["id"], self.rooms[0].get("name", "Mock Room")
        if auto_create:
            new_room = {"id": "room-mock-001", "name": "Mock Triage Room"}
            self.rooms.append(new_room)
            return "room-mock-001", "Mock Triage Room"
        raise SwitchRoomNotFoundError("No mock rooms available.")

    def update_status(
        self,
        room_id: str | None = None,
        *,
        presence: str = "online",
        status: str = "RepoRescue Active",
    ) -> dict[str, Any]:
        """Simulates updating agent status in Switch."""
        return {"ok": True, "presence": presence, "status": status}

    def send_heartbeat(self) -> dict[str, Any]:
        """Simulates sending a heartbeat in Switch."""
        return {"ok": True}

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
