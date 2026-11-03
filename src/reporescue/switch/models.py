from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class SwitchMessageEvent:
    """Represents a validated inbound message event received from a Switch Room."""

    room_id: str
    message_id: str
    sender: str
    sender_name: str
    body: str
    timestamp: datetime
    addressed: bool = True
    thread_id: str | None = None
    channel_type: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_agent_event(cls, data: dict[str, Any]) -> SwitchMessageEvent | None:
        """Parses an official Switch AgentEvent payload (type='message').

        Returns None if the event is not a message event or lacks required fields.
        """
        if data.get("type") != "message":
            return None

        room_id = data.get("room_id")
        channel_type = data.get("channel_type")
        payload = data.get("payload", {})
        if not isinstance(payload, dict) or not room_id:
            return None

        body = payload.get("body", "")
        if not body:
            return None

        sender = payload.get("sender", "unknown")
        # Switch populates on_behalf_of or sender_name when proxying via Mattermost/Slack
        sender_name = (
            payload.get("on_behalf_of") or payload.get("sender_name") or sender
        )
        message_id = payload.get("message_id") or str(payload.get("timestamp", ""))

        raw_ts = payload.get("timestamp")
        if isinstance(raw_ts, (int, float)):
            # Switch timestamps are milliseconds since epoch
            event_ts = datetime.fromtimestamp(raw_ts / 1000.0, tz=UTC)
        else:
            event_ts = datetime.now(UTC)

        return cls(
            room_id=room_id,
            message_id=str(message_id),
            sender=sender,
            sender_name=sender_name,
            body=body.strip(),
            timestamp=event_ts,
            addressed=bool(payload.get("addressed", True)),
            thread_id=payload.get("thread_id"),
            channel_type=channel_type,
            raw_payload=data,
        )


class WorkflowFormatter:
    """Formats standardized markdown cards for RepoRescue updates in Switch rooms."""

    @staticmethod
    def format_triage_card(
        task_id: str, title: str, severity: str, acceptance_criteria: str
    ) -> str:
        return (
            f"### 🛡️ RepoRescue — Issue Triaged\n\n"
            f"**Task ID:** `{task_id}`\n"
            f"**Title:** {title}\n"
            f"**Severity:** `{severity}`\n"
            f"**Acceptance Criteria:** {acceptance_criteria}\n\n"
            f"👉 *Reproducer agent assigned to create a minimal failing test.*"
        )

    @staticmethod
    def format_reproduction_card(
        task_id: str, test_file: str, exit_code: int, output_snippet: str
    ) -> str:
        return (
            f"### 🔬 RepoRescue — Bug Reproduced\n\n"
            f"**Task ID:** `{task_id}`\n"
            f"**Reproduction Script:** `{test_file}`\n"
            f"**Exit Code:** `{exit_code}` (Expected failure)\n\n"
            f"```text\n{output_snippet.strip()}\n```\n\n"
            f"👉 *Fix agent dispatched to isolated Git worktree.*"
        )

    @staticmethod
    def format_fix_proposed_card(
        task_id: str, branch: str, diff_snippet: str, rationale: str
    ) -> str:
        return (
            f"### 🛠️ RepoRescue — Fix Proposed\n\n"
            f"**Task ID:** `{task_id}`\n"
            f"**Isolated Branch:** `{branch}`\n"
            f"**Rationale:** {rationale}\n\n"
            f"```diff\n{diff_snippet.strip()}\n```\n\n"
            f"👉 *Verifier agent running independent test suite...*"
        )

    @staticmethod
    def format_verification_card(
        task_id: str,
        passed_count: int,
        failed_count: int,
        duration_s: float,
        verified: bool,
    ) -> str:
        status_icon = "✅" if verified else "❌"
        status_text = (
            "VERIFIED — All tests passed" if verified else "FAILED — Tests did not pass"
        )
        return (
            f"### {status_icon} RepoRescue — Verification Result\n\n"
            f"**Task ID:** `{task_id}`\n"
            f"**Status:** {status_text}\n"
            f"**Test Results:** {passed_count} passed, {failed_count} failed in {duration_s:.2f}s\n\n"
            f"> [!IMPORTANT]\n"
            f"> **Human Approval Required**: RepoRescue never automatically merges or deploys code.\n"
            f"> Reply with `/approve` to approve opening a draft Pull Request, or `/reject <reason>` to request revisions."
        )

    @staticmethod
    def format_approval_confirmation(task_id: str, approver: str) -> str:
        return (
            f"### 👤 Human Approval Granted\n\n"
            f"**Task ID:** `{task_id}`\n"
            f"**Approved By:** @{approver}\n"
            f"**Next Step:** Ready to open Pull Request with full reproduction and verification evidence."
        )
