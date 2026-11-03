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
    sender_kind: str | None = None
    on_behalf_of: str | None = None
    sender_role: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_human_sender(self) -> bool:
        """Determines if the message originated from a verified human teammate.

        Strictly rejects automated agents, bots, or unverified automated messages.
        """
        if not self.sender_name or not self.sender_name.strip():
            return False

        # Switch protocol explicit sender_kind: 'user', 'agent', 'platform'
        if self.sender_kind in ("agent", "bot"):
            return False

        if self.sender_kind == "platform":
            # Platform message only allowed if explicitly sent on behalf of a human
            return bool(self.on_behalf_of and self.on_behalf_of.strip())

        # Check sender URI / identifier
        lower_sender = self.sender.lower().strip()
        if (
            lower_sender.startswith(
                ("agent:", "bot:", "switch:", "system:", "@switch-agent-")
            )
            or "reporescue" in lower_sender
        ):
            return False

        # Check display name
        lower_name = self.sender_name.lower().strip()
        return not (
            lower_name in ("reporescue", "switch", "system")
            or "reporescue" in lower_name
            or lower_name.endswith("bot")
        )

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
        sender_kind = payload.get("sender_kind")
        on_behalf_of = payload.get("on_behalf_of")
        sender_role = payload.get("sender_role")

        # Switch populates on_behalf_of or sender_name when proxying via Mattermost/Slack
        sender_name = on_behalf_of or payload.get("sender_name") or sender
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
            sender_kind=sender_kind,
            on_behalf_of=on_behalf_of,
            sender_role=sender_role,
            raw_payload=data,
        )


class WorkflowFormatter:
    """Formats standardized, professional cards for RepoRescue updates in Switch rooms."""

    @staticmethod
    def format_ack_card(task_id: str, title: str, reporter: str) -> str:
        return (
            f"### RepoRescue | {task_id}\n\n"
            f"**Bug accepted**\n"
            f"**Title:** {title}\n"
            f"**Reported by:** @{reporter}\n"
            f"**Status:** Investigating & Triaging\n"
        )

    @staticmethod
    def format_triage_card(
        task_id: str,
        title: str,
        severity: str,
        acceptance_criteria: str,
        affected_components: list[str] | None = None,
    ) -> str:
        comp_str = (
            f"\n**Components:** {', '.join(affected_components)}"
            if affected_components
            else ""
        )
        return (
            f"### RepoRescue | {task_id}\n\n"
            f"**Issue Triaged**\n"
            f"**Title:** {title}\n"
            f"**Severity:** `{severity}`"
            f"{comp_str}\n"
            f"**Acceptance Criteria:** {acceptance_criteria}\n\n"
            f"👉 *Investigator assigned to determine root cause.*"
        )

    @staticmethod
    def format_investigation_card(
        task_id: str,
        root_cause: str,
        evidence: list[str],
        confidence: str = "medium",
    ) -> str:
        evidence_lines = (
            "\n".join(f"- {e}" for e in evidence)
            if evidence
            else "- Codebase inspection completed"
        )
        return (
            f"### RepoRescue | {task_id}\n\n"
            f"**Root cause identified**\n"
            f"**Cause:** {root_cause}\n"
            f"**Evidence:**\n{evidence_lines}\n"
            f"**Confidence:** `{confidence}`\n\n"
            f"👉 *Reproducer agent creating isolated failing test.*"
        )

    @staticmethod
    def format_reproduction_card(
        task_id: str, test_file: str, exit_code: int, output_snippet: str
    ) -> str:
        return (
            f"### RepoRescue | {task_id}\n\n"
            f"**Bug Reproduced** (Reproduction successful)\n"
            f"**Failing test:** `{test_file}`\n"
            f"**Exit code:** `{exit_code}` (Expected reproduction failure)\n\n"
            f"```text\n{output_snippet.strip()}\n```\n\n"
            f"👉 *Fix agent dispatched to isolated Git worktree.*"
        )

    @staticmethod
    def format_fix_proposed_card(
        task_id: str,
        branch: str,
        diff_snippet: str,
        rationale: str,
        files_changed_count: int = 1,
    ) -> str:
        return (
            f"### RepoRescue | {task_id}\n\n"
            f"**Fix Proposed**\n"
            f"**Branch:** `{branch}`\n"
            f"**Files changed:** {files_changed_count}\n"
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
        status_text = "Verification complete" if verified else "Verification failed"
        status_icon = "✅" if verified else "❌"
        return (
            f"### RepoRescue | {task_id}\n\n"
            f"**{status_icon} {status_text}**\n"
            f"**Tests passed:** {passed_count}\n"
            f"**Tests failed:** {failed_count}\n"
            f"**Duration:** {duration_s:.2f}s\n\n"
            f"> [!IMPORTANT]\n"
            f"> **Human Approval Required**\n"
            f"> Reply:\n"
            f"> `/approve`\n"
            f"> or\n"
            f"> `/reject <reason>`"
        )

    @staticmethod
    def format_approval_confirmation(
        task_id: str, approver: str, timestamp: datetime | None = None
    ) -> str:
        ts_str = f" at `{timestamp.isoformat()}`" if timestamp else ""
        return (
            f"### RepoRescue | {task_id}\n\n"
            f"**👤 Human Approval Granted**\n"
            f"**Approved By:** @{approver}{ts_str}\n"
            f"**Next Step:** Ready to open Pull Request with full reproduction and verification evidence."
        )
