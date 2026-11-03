from __future__ import annotations

import re
from typing import Any

from reporescue.models import BugTask, TaskStatus

from .client import SwitchClientProtocol
from .models import SwitchMessageEvent, WorkflowFormatter


class RepoRescueDispatcher:
    """Dispatches Switch room events to RepoRescue workflow tasks and publishes status updates."""

    def __init__(
        self,
        client: SwitchClientProtocol,
        orchestrator: Any | None = None,
    ) -> None:
        self.client = client
        self.orchestrator = orchestrator
        self.tasks: dict[str, BugTask] = {}
        self.active_task_by_room: dict[str, str] = {}  # room_id -> task_id

    def handle_message_event(self, event: SwitchMessageEvent) -> BugTask | None:
        """Processes an incoming Switch message event.

        Detects whether the message is a new bug report, an approval command, or a rejection command.
        """
        body = event.body.strip()

        # Check for approval commands: /approve [task_id]
        if re.match(r"^/approve(\s+[\w\-]+)?$", body, re.IGNORECASE):
            return self._handle_approval(event)

        # Check for rejection commands: /reject [reason]
        if re.match(r"^/reject(\s+.*)?$", body, re.IGNORECASE):
            return self._handle_rejection(event)

        # Otherwise, treat as a bug report if in the room
        task = self._handle_new_bug_report(event)

        # If an orchestrator is attached, trigger autonomous workflow execution
        if self.orchestrator is not None and task is not None:
            try:
                self.orchestrator.execute_full_workflow(task)
            except Exception as err:  # noqa: BLE001
                self.client.send_message(
                    event.room_id,
                    f"⚠️ Workflow stopped on task `{task.task_id}`: {err}",
                    metadata={"task_id": task.task_id, "status": "failed"},
                )

        return task

    def _handle_new_bug_report(self, event: SwitchMessageEvent) -> BugTask:
        """Creates a new BugTask and publishes initial acknowledgment to the room."""
        body = event.body.strip()
        lines = [line.strip() for line in body.splitlines() if line.strip()]

        title = lines[0] if lines else "Bug report from Switch"
        # Clean prefix if user typed "Bug: ..." or "!reporescue ..."
        title = re.sub(r"^(bug:?|!reporescue\s+)\s*", "", title, flags=re.IGNORECASE)
        description = "\n".join(lines[1:]) if len(lines) > 1 else title

        task = BugTask(
            title=title,
            description=description,
            reporter=event.sender_name,
        )
        self.tasks[task.task_id] = task
        self.active_task_by_room[event.room_id] = task.task_id

        ack_message = (
            f"### 🛡️ RepoRescue Received Bug Report\n\n"
            f"**Task ID:** `{task.task_id}`\n"
            f"**Reported by:** @{task.reporter}\n"
            f"**Title:** {task.title}\n\n"
            f"Beginning autonomous investigation, reproduction, and isolated fixing..."
        )
        self.client.send_message(
            event.room_id, ack_message, metadata={"task_id": task.task_id}
        )
        return task

    def _handle_approval(self, event: SwitchMessageEvent) -> BugTask | None:
        """Handles human /approve command in the Switch room."""
        # Task 3 Hardening: Reject approvals from Switch agents, bots, or unverified automated sources
        if not event.is_human_sender:
            warn_msg = (
                f"⚠️ Approval rejected: Approvals must be submitted by a verified human teammate, "
                f"not an automated agent or bot (received from '@{event.sender_name}' [kind={event.sender_kind or 'unknown'}])."
            )
            self.client.send_message(event.room_id, warn_msg)
            return None

        task_id = self._resolve_target_task_id(event)
        if not task_id or task_id not in self.tasks:
            self.client.send_message(
                event.room_id,
                "⚠️ No active task awaiting approval found for this room.",
            )
            return None

        task = self.tasks[task_id]
        if task.status != TaskStatus.AWAITING_APPROVAL:
            self.client.send_message(
                event.room_id,
                f"⚠️ Approval rejected: Task `{task.task_id}` is in state `{task.status}`, not `awaiting_approval`. "
                f"Mandatory verification tests must pass before approval can be granted.",
            )
            return task

        # Enforce human actor and timestamp in state machine
        task.transition(
            TaskStatus.APPROVED, actor=event.sender_name, timestamp=event.timestamp
        )
        confirm_card = WorkflowFormatter.format_approval_confirmation(
            task.task_id, event.sender_name, timestamp=task.approved_at
        )
        self.client.send_message(
            event.room_id, confirm_card, metadata={"task_id": task.task_id}
        )
        return task

    def _handle_rejection(self, event: SwitchMessageEvent) -> BugTask | None:
        """Handles human /reject command in the Switch room."""
        if not event.is_human_sender:
            warn_msg = (
                f"⚠️ Rejection ignored: Rejections must be submitted by a verified human teammate "
                f"(received from '@{event.sender_name}')."
            )
            self.client.send_message(event.room_id, warn_msg)
            return None

        task_id = self._resolve_target_task_id(event)
        if not task_id or task_id not in self.tasks:
            return None

        task = self.tasks[task_id]
        if task.status != TaskStatus.AWAITING_APPROVAL:
            self.client.send_message(
                event.room_id,
                f"⚠️ Rejection rejected: Task `{task.task_id}` is in state `{task.status}`, not `awaiting_approval`.",
            )
            return task

        task.transition(TaskStatus.REJECTED, actor=event.sender_name)
        rejection_msg = (
            f"### 🛑 Task Rejected\n\n"
            f"**Task ID:** `{task.task_id}`\n"
            f"**Rejected By:** @{event.sender_name}\n"
            f"Fix was not approved. Revision required before PR creation."
        )
        self.client.send_message(
            event.room_id, rejection_msg, metadata={"task_id": task.task_id}
        )
        return task

    def _resolve_target_task_id(self, event: SwitchMessageEvent) -> str | None:
        """Resolves the task_id from command arguments or active room context."""
        parts = event.body.strip().split()
        if len(parts) > 1 and parts[1] in self.tasks:
            return parts[1]
        return self.active_task_by_room.get(event.room_id)

    def publish_triage(
        self,
        task_id: str,
        room_id: str,
        *,
        severity: str,
        acceptance_criteria: str,
        affected_components: list[str] | None = None,
        actor: str = "triager",
    ) -> None:
        """Advances task to TRIAGED and posts triage card."""
        task = self.tasks.get(task_id)
        if task and task.status == TaskStatus.REPORTED:
            task.transition(TaskStatus.TRIAGED, actor=actor)
        title = task.title if task else "Bug Report"

        card = WorkflowFormatter.format_triage_card(
            task_id,
            title,
            severity,
            acceptance_criteria,
            affected_components=affected_components,
        )
        self.client.send_message(
            room_id, card, metadata={"task_id": task_id, "status": "triaged"}
        )

    def publish_investigation(
        self,
        task_id: str,
        room_id: str,
        *,
        root_cause: str,
        evidence: list[str],
        confidence: str = "medium",
        actor: str = "investigator",
    ) -> None:
        """Advances task through INVESTIGATING to INVESTIGATED and posts root cause evidence."""
        task = self.tasks.get(task_id)
        if task and task.status == TaskStatus.TRIAGED:
            task.transition(TaskStatus.INVESTIGATING, actor=actor)
            task.transition(TaskStatus.INVESTIGATED, actor=actor)
            for ev in evidence:
                task.evidence.append(f"Investigation: {ev}")

        card = WorkflowFormatter.format_investigation_card(
            task_id, root_cause, evidence, confidence=confidence
        )
        self.client.send_message(
            room_id,
            card,
            metadata={"task_id": task_id, "status": "investigated"},
        )

    def publish_reproduction(
        self,
        task_id: str,
        room_id: str,
        *,
        test_file: str,
        exit_code: int,
        output_snippet: str,
        actor: str = "reproducer",
    ) -> None:
        """Advances task to REPRODUCING then REPRODUCED and posts reproduction evidence."""
        task = self.tasks.get(task_id)
        if task and task.status in (TaskStatus.TRIAGED, TaskStatus.INVESTIGATED):
            task.transition(TaskStatus.REPRODUCING, actor=actor)
            task.transition(TaskStatus.REPRODUCED, actor=actor)
            task.evidence.append(
                f"Reproduction: {test_file} (exit={exit_code})\n{output_snippet}"
            )

        card = WorkflowFormatter.format_reproduction_card(
            task_id, test_file, exit_code, output_snippet
        )
        self.client.send_message(
            room_id, card, metadata={"task_id": task_id, "status": "reproduced"}
        )

    def publish_fix_proposed(
        self,
        task_id: str,
        room_id: str,
        *,
        branch_name: str,
        diff_snippet: str,
        rationale: str,
        actor: str = "fixer",
    ) -> None:
        """Advances task to FIX_PROPOSED and posts proposed patch."""
        task = self.tasks.get(task_id)
        if task and task.status in (TaskStatus.REPRODUCED, TaskStatus.REJECTED):
            task.proposed_branch = branch_name
            task.transition(TaskStatus.FIX_PROPOSED, actor=actor)
            task.evidence.append(f"Fix on branch {branch_name}:\n{diff_snippet}")

        card = WorkflowFormatter.format_fix_proposed_card(
            task_id, branch_name, diff_snippet, rationale
        )
        self.client.send_message(
            room_id, card, metadata={"task_id": task_id, "status": "fix_proposed"}
        )

    def publish_verification(
        self,
        task_id: str,
        room_id: str,
        *,
        passed_count: int,
        failed_count: int,
        duration_s: float,
        evidence_text: str,
        verified: bool = True,
        actor: str = "verifier",
    ) -> None:
        """Advances task to VERIFYING, then VERIFIED and AWAITING_APPROVAL, and asks human for approval."""
        task = self.tasks.get(task_id)
        if task and task.status == TaskStatus.FIX_PROPOSED:
            task.transition(TaskStatus.VERIFYING, actor=actor)
            if not verified:
                task.transition(TaskStatus.FAILED, actor=actor)
            else:
                task.transition(TaskStatus.VERIFIED, actor=actor)
                task.transition(TaskStatus.AWAITING_APPROVAL, actor=actor)
                task.evidence.append(evidence_text)

        if not verified:
            self.client.send_message(
                room_id,
                f"❌ Task `{task_id}` verification failed. Tests did not pass.",
                metadata={"task_id": task_id, "status": "failed"},
            )
            return

        card = WorkflowFormatter.format_verification_card(
            task_id, passed_count, failed_count, duration_s, verified=True
        )
        self.client.send_message(
            room_id, card, metadata={"task_id": task_id, "status": "awaiting_approval"}
        )
