from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class TaskStatus(StrEnum):
    REPORTED = "reported"
    TRIAGED = "triaged"
    INVESTIGATING = "investigating"
    INVESTIGATED = "investigated"
    REPRODUCING = "reproducing"
    REPRODUCED = "reproduced"
    FIX_PROPOSED = "fix_proposed"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PR_CREATED = "pr_created"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class BugTask:
    title: str
    description: str
    reporter: str
    task_id: str = field(default_factory=lambda: str(uuid4()))
    status: TaskStatus = TaskStatus.REPORTED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    evidence: list[str] = field(default_factory=list)
    proposed_branch: str | None = None
    approval_by: str | None = None
    approved_at: datetime | None = None
    verified_patch_fingerprint: str | None = None
    approved_patch_fingerprint: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    pr_created_at: datetime | None = None

    def transition(
        self, new_status: TaskStatus, *, actor: str, timestamp: datetime | None = None
    ) -> None:
        allowed = {
            TaskStatus.REPORTED: {TaskStatus.TRIAGED, TaskStatus.FAILED},
            TaskStatus.TRIAGED: {
                TaskStatus.INVESTIGATING,
                TaskStatus.REPRODUCING,
                TaskStatus.FAILED,
            },
            TaskStatus.INVESTIGATING: {TaskStatus.INVESTIGATED, TaskStatus.FAILED},
            TaskStatus.INVESTIGATED: {TaskStatus.REPRODUCING, TaskStatus.FAILED},
            TaskStatus.REPRODUCING: {TaskStatus.REPRODUCED, TaskStatus.FAILED},
            TaskStatus.REPRODUCED: {TaskStatus.FIX_PROPOSED, TaskStatus.FAILED},
            TaskStatus.FIX_PROPOSED: {TaskStatus.VERIFYING, TaskStatus.FAILED},
            TaskStatus.VERIFYING: {TaskStatus.VERIFIED, TaskStatus.FAILED},
            TaskStatus.VERIFIED: {TaskStatus.AWAITING_APPROVAL},
            TaskStatus.AWAITING_APPROVAL: {TaskStatus.APPROVED, TaskStatus.REJECTED},
            TaskStatus.APPROVED: {
                TaskStatus.PR_CREATED,
                TaskStatus.FIX_PROPOSED,
                TaskStatus.FAILED,
            },
            TaskStatus.PR_CREATED: {TaskStatus.FAILED},
            TaskStatus.REJECTED: {TaskStatus.FIX_PROPOSED},
        }
        if new_status not in allowed.get(self.status, set()):
            raise ValueError(f"Invalid transition: {self.status} -> {new_status}")
        self.status = new_status
        if new_status == TaskStatus.APPROVED:
            if not actor.strip():
                raise ValueError("Approval actor must be named")
            self.approval_by = actor
            self.approved_at = timestamp or datetime.now(UTC)
        elif new_status == TaskStatus.PR_CREATED:
            self.pr_created_at = timestamp or datetime.now(UTC)
