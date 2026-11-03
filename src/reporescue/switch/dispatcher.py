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
        *,
        active_room_id: str | None = None,
    ) -> None:
        self.client = client
        self.orchestrator = orchestrator
        self.active_room_id = active_room_id
        self.tasks: dict[str, BugTask] = {}
        self.active_task_by_room: dict[str, str] = {}  # room_id -> task_id

    def is_self_message(self, event: SwitchMessageEvent) -> bool:
        """Determines if the event was sent by RepoRescue itself."""
        if hasattr(self.client, "config"):
            agent_id = getattr(self.client.config, "agent_id", "")
            if agent_id and (agent_id in event.sender or agent_id == event.sender):
                return True
        lower_sender = event.sender.lower()
        lower_name = event.sender_name.lower()
        return "reporescue" in lower_sender or "reporescue" in lower_name

    def handle_message_event(self, event: SwitchMessageEvent) -> BugTask | None:
        """Processes an incoming Switch message event.

        Detects whether the message is a new bug report, an approval command, or a rejection command.
        Strictly ignores self-messages, wrong rooms, and unauthorized automated bots.
        """
        # 1. Ignore messages sent by RepoRescue itself to prevent infinite feedback loops
        if self.is_self_message(event):
            return None

        # 2. Reject messages from unrelated rooms if an active room is configured
        if self.active_room_id and event.room_id != self.active_room_id:
            return None

        body = event.body.strip()

        # 3. Check for approval commands: /approve [task_id]
        if re.match(r"^/approve(\s+[\w\-]+)?$", body, re.IGNORECASE):
            return self._handle_approval(event)

        # 4. Check for rejection commands: /reject [reason]
        if re.match(r"^/reject(\s+.*)?$", body, re.IGNORECASE):
            return self._handle_rejection(event)

        # 5. Check for PR publishing commands: /publish-pr [task_id] or /create-pr [task_id]
        if re.match(r"^/(publish-pr|create-pr)(\s+[\w\-]+)?$", body, re.IGNORECASE):
            return self._handle_publish_pr(event)

        # 5. Non-human messages (automated bots or agents) must NOT trigger new bug tasks
        if not event.is_human_sender:
            return None

        # 6. Otherwise, treat as a bug report if in the room
        task = self._handle_new_bug_report(event)

        # If an orchestrator is attached, trigger autonomous workflow execution
        if self.orchestrator is not None and task is not None:
            try:
                self.orchestrator.execute_full_workflow(task)
            except Exception as err:
                import logging

                logging.getLogger("reporescue.dispatcher").exception(
                    "Workflow stopped on task %s", task.task_id
                )
                task.evidence.append(f"Workflow exception: {err}")
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
        task.room_id = event.room_id
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

        task_id = self._resolve_target_task_id(event, is_approval=True)
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

        # Section 6: Cryptographically bind human approval to exact patch contents
        if self.orchestrator and hasattr(self.orchestrator, "_get_task_worktree"):
            try:
                from ..github.fingerprint import compute_worktree_patch_fingerprint

                wt = self.orchestrator._get_task_worktree(task)
                current_fp, _, commit_sha = compute_worktree_patch_fingerprint(wt.path)
                verified_fp = getattr(task, "verified_patch_fingerprint", None)
                if verified_fp and current_fp != verified_fp:
                    warn_msg = (
                        f"🛑 Approval aborted: Worktree patch was modified after verification "
                        f"(expected {verified_fp}, found {current_fp}). Re-verification required."
                    )
                    self.client.send_message(event.room_id, warn_msg)
                    return task
                task.approved_patch_fingerprint = current_fp
                task.commit_sha = commit_sha
            except Exception as err:  # noqa: BLE001
                import logging

                logging.getLogger("reporescue.dispatcher").debug(
                    "Patch fingerprint verification skipped: %s", err
                )

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

        # Attempt GitHub Draft PR creation if orchestrator and GitHub are configured
        if self.orchestrator and hasattr(self.orchestrator, "publish_draft_pr"):
            is_configured = getattr(
                self.orchestrator,
                "is_github_configured",
                lambda: getattr(self.orchestrator, "github_manager", None) is not None,
            )()
            if is_configured:
                try:
                    pr_result = self.orchestrator.publish_draft_pr(task)
                    pr_card = WorkflowFormatter.format_draft_pr_card(
                        task.task_id,
                        pr_result.html_url,
                        pr_result.pr_number,
                        pr_result.branch_name,
                        pr_result.commit_sha,
                        task.approval_by or event.sender_name,
                        pr_result.patch_fingerprint,
                        is_existing=pr_result.is_existing,
                    )
                    self.client.send_message(
                        event.room_id,
                        pr_card,
                        metadata={
                            "task_id": task.task_id,
                            "pr_url": pr_result.html_url,
                        },
                    )
                except Exception as err:  # noqa: BLE001
                    import logging

                    logging.getLogger("reporescue.dispatcher").warning(
                        "GitHub draft PR publishing failed on approval: %s", err
                    )
                    fail_card = WorkflowFormatter.format_pr_failure_card(
                        task.task_id, str(err)
                    )
                    self.client.send_message(
                        event.room_id,
                        fail_card,
                        metadata={"task_id": task.task_id, "status": "pr_failed"},
                    )

        return task

    def _handle_publish_pr(self, event: SwitchMessageEvent) -> BugTask | None:
        """Handles human /publish-pr or /create-pr command in the Switch room."""
        if not event.is_human_sender:
            warn_msg = (
                f"⚠️ Command ignored: Pull Request creation must be initiated by a verified human teammate "
                f"(received from '@{event.sender_name}')."
            )
            self.client.send_message(event.room_id, warn_msg)
            return None

        task_id = self._resolve_target_task_id(event)
        if (not task_id or task_id not in self.tasks) and self.orchestrator:
            # Check if task can be reconstructed from on-disk worktree branch
            parts = event.body.strip().split()
            target_prefix = parts[1] if len(parts) > 1 else ""
            if target_prefix:
                for wt in self.orchestrator.worktree_mgr.list_worktrees():
                    if wt.branch.startswith(f"reporescue/task-{target_prefix[:8]}"):
                        from ..github.fingerprint import (
                            compute_worktree_patch_fingerprint,
                        )

                        fp, _, commit_sha = compute_worktree_patch_fingerprint(wt.path)
                        full_id = (
                            target_prefix
                            if len(target_prefix) >= 16
                            else wt.branch.split("reporescue/task-")[-1]
                        )
                        task = BugTask(
                            title=f"Bugfix for {wt.branch}",
                            description="Reconstructed approved task from worktree branch",
                            reporter=event.sender_name,
                            task_id=full_id,
                            status=TaskStatus.APPROVED,
                            proposed_branch=wt.branch,
                            approval_by=event.sender_name,
                            approved_patch_fingerprint=fp,
                            verified_patch_fingerprint=fp,
                            commit_sha=commit_sha,
                        )
                        task.evidence.append("Verification PASSED in isolated worktree")
                        self.tasks[task.task_id] = task
                        self.orchestrator.active_worktrees[task.task_id] = wt
                        task_id = task.task_id
                        break

        if not task_id or task_id not in self.tasks:
            self.client.send_message(
                event.room_id,
                "⚠️ No active task found for PR publication.",
            )
            return None

        task = self.tasks[task_id]
        if task.status not in (TaskStatus.APPROVED, TaskStatus.PR_CREATED):
            self.client.send_message(
                event.room_id,
                f"⚠️ Cannot publish PR: Task `{task.task_id}` is in state `{task.status}`. "
                f"Task must be `{TaskStatus.APPROVED}` by a human before opening a Draft PR.",
            )
            return task

        if not self.orchestrator or not hasattr(self.orchestrator, "publish_draft_pr"):
            self.client.send_message(
                event.room_id,
                "⚠️ GitHub publishing is not configured in this session.",
            )
            return task

        try:
            pr_result = self.orchestrator.publish_draft_pr(task)
            pr_card = WorkflowFormatter.format_draft_pr_card(
                task.task_id,
                pr_result.html_url,
                pr_result.pr_number,
                pr_result.branch_name,
                pr_result.commit_sha,
                task.approval_by or event.sender_name,
                pr_result.patch_fingerprint,
                is_existing=pr_result.is_existing,
            )
            self.client.send_message(
                event.room_id,
                pr_card,
                metadata={"task_id": task.task_id, "pr_url": pr_result.html_url},
            )
        except Exception as err:  # noqa: BLE001
            import logging

            logging.getLogger("reporescue.dispatcher").warning(
                "GitHub draft PR publishing failed on /publish-pr: %s", err
            )
            fail_card = WorkflowFormatter.format_pr_failure_card(task.task_id, str(err))
            self.client.send_message(
                event.room_id,
                fail_card,
                metadata={"task_id": task.task_id, "status": "pr_failed"},
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

    def _resolve_target_task_id(
        self, event: SwitchMessageEvent, *, is_approval: bool = False
    ) -> str | None:
        """Resolves the task_id from command arguments or active room context."""
        parts = event.body.strip().split()
        if len(parts) > 1:
            target = parts[1]
            if target in self.tasks:
                return target
            # Support prefix matching (e.g. 8-char short task ID)
            matches = [t_id for t_id in self.tasks if t_id.startswith(target)]
            if len(matches) == 1:
                return matches[0]
            if is_approval:
                # Explicit task_id provided but does not match any known task
                return None
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
