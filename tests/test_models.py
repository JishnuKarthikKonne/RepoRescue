import pytest

from reporescue.models import BugTask, TaskStatus


def test_valid_workflow_transition():
    task = BugTask("Bug", "Description", "Jishnu")
    task.transition(TaskStatus.TRIAGED, actor="triager")
    assert task.status == TaskStatus.TRIAGED


def test_invalid_transition_rejected():
    task = BugTask("Bug", "Description", "Jishnu")
    with pytest.raises(ValueError):
        task.transition(TaskStatus.APPROVED, actor="Jishnu")


def test_approval_records_human():
    task = BugTask("Bug", "Description", "Jishnu")
    for status in [
        TaskStatus.TRIAGED,
        TaskStatus.REPRODUCING,
        TaskStatus.REPRODUCED,
        TaskStatus.FIX_PROPOSED,
        TaskStatus.VERIFYING,
        TaskStatus.VERIFIED,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.APPROVED,
    ]:
        task.transition(status, actor="Jishnu")
    assert task.approval_by == "Jishnu"
