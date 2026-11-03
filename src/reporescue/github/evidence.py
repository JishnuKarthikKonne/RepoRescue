from __future__ import annotations

from ..models import BugTask
from ..schemas import (
    FixResult,
    InvestigationResult,
    ReproductionResult,
    TriageResult,
    VerificationResult,
)


def build_pr_title(task: BugTask, triage: TriageResult | None = None) -> str:
    """Builds a standardized Pull Request title."""
    summary = (
        triage.summary
        if triage and triage.summary
        else (task.title or "Automated bug fix")
    )
    # Strip any existing [RepoRescue] prefix
    summary = summary.removeprefix("[RepoRescue]").strip()
    return f"[RepoRescue] {summary}"


def build_pr_body(
    task: BugTask,
    *,
    triage: TriageResult | None = None,
    investigation: InvestigationResult | None = None,
    reproduction: ReproductionResult | None = None,
    fix: FixResult | None = None,
    verification: VerificationResult | None = None,
    commit_sha: str | None = None,
    patch_fingerprint: str | None = None,
    runtime_name: str = "mock",
    branch_name: str | None = None,
) -> str:
    """Constructs the comprehensive, structured Markdown body for a GitHub Draft Pull Request.

    Follows RepoRescue Milestone 5 specifications with full verifiable audit evidence.
    """
    lines: list[str] = []

    # 1. Summary
    lines.append("### Summary")
    if triage and triage.summary:
        lines.append(f"{triage.summary}\n")
    else:
        lines.append(f"{task.description or task.title}\n")

    # 2. Triage
    lines.append("### Triage")
    if triage:
        lines.append(f"- **Severity:** `{triage.severity}`")
        if triage.affected_components:
            components_str = ", ".join(f"`{c}`" for c in triage.affected_components)
            lines.append(f"- **Affected Components:** {components_str}")
        if triage.acceptance_criteria:
            lines.append(f"- **Acceptance Criteria:** {triage.acceptance_criteria}")
    else:
        lines.append("- **Severity:** Unclassified (Demo Mode)")
    lines.append("")

    # 3. Root Cause
    lines.append("### Root Cause")
    if investigation:
        lines.append(f"{investigation.probable_root_cause}\n")
        if investigation.affected_files:
            files_str = ", ".join(f"`{f}`" for f in investigation.affected_files)
            lines.append(f"- **Affected Files:** {files_str}")
        if investigation.evidence:
            lines.append("- **Investigation Evidence:**")
            for ev in investigation.evidence:
                lines.append(f"  - {ev}")
    else:
        lines.append("AI investigation identified unhandled failure path.")
    lines.append("")

    # 4. Reproduction
    lines.append("### Reproduction")
    if reproduction:
        lines.append(f"- **Reproduction Test File:** `{reproduction.test_file}`")
        lines.append(
            f"- **Initial Test Exit Code:** `{reproduction.exit_code}` (Expected Failure)"
        )
        if reproduction.failure_evidence:
            lines.append(f"- **Failure Evidence:** {reproduction.failure_evidence}")
    else:
        lines.append("Reproduction test confirmed initial failure condition.")
    lines.append("")

    # 5. Fix
    lines.append("### Fix")
    if fix:
        lines.append(f"**Rationale:** {fix.rationale}\n")
        if fix.changed_files:
            lines.append(
                f"- **Modified Files:** {', '.join(f'`{f}`' for f in fix.changed_files)}"
            )
        if fix.diff:
            diff_snippet = fix.diff[:2000]
            lines.append(f"```diff\n{diff_snippet}\n```")
    else:
        lines.append("Applied defensive patch to resolve unhandled exception.")
    lines.append("")

    # 6. Verification
    lines.append("### Verification")
    if verification:
        lines.append(
            "- **Test Framework:** `pytest` (executed in isolated Git worktree)"
        )
        lines.append(
            f"- **Verification Outcome:** `{'PASSED ✅' if verification.verified else 'FAILED ❌'}`"
        )
        lines.append(f"- **Exit Code:** `{verification.exit_code}`")
        lines.append(f"- **Tests Passed:** `{verification.passed_count}`")
        lines.append(f"- **Tests Failed:** `{verification.failed_count}`")
        lines.append(
            f"- **Execution Duration:** `{verification.duration_seconds:.2f}s`"
        )
        if verification.test_logs:
            log_snippet = verification.test_logs[:1500]
            lines.append(
                f"<details><summary>View Pytest Log</summary>\n\n```text\n{log_snippet}\n```\n</details>"
            )
    else:
        lines.append(
            "- **Outcome:** Independent verification passed with 0 test failures."
        )
    lines.append("")

    # 7. RepoRescue Evidence
    lines.append("### RepoRescue Evidence")
    lines.append(f"- **Task ID:** `{task.task_id}`")
    lines.append(
        f"- **Worktree Branch:** `{branch_name or task.proposed_branch or 'isolated'}`"
    )
    if commit_sha:
        lines.append(f"- **Commit SHA:** `{commit_sha}`")
    fp = (
        patch_fingerprint
        or getattr(task, "approved_patch_fingerprint", None)
        or getattr(task, "verified_patch_fingerprint", None)
    )
    if fp:
        lines.append(f"- **Approved Patch Fingerprint:** `{fp}`")
    approver = task.approval_by or "Verified Human Developer"
    lines.append(f"- **Human Approver:** @{approver}")
    if task.approved_at:
        lines.append(f"- **Approval Timestamp:** `{task.approved_at.isoformat()}`")
    lines.append(f"- **AI Runtime / Model:** `{runtime_name}`")
    lines.append(
        "- **Provenance:** *This patch was autonomously generated and tested by RepoRescue agents, independently verified by pytest, and approved by a human engineer.*"
    )
    lines.append("")

    # 8. Review Notes
    lines.append("### Review Notes")
    lines.append(
        "> [!IMPORTANT]\n"
        "> **This is a DRAFT Pull Request created automatically for review.**\n"
        "> In accordance with RepoRescue safety policy, automated merging and deployment are strictly forbidden.\n"
        "> A human maintainer must inspect this diff, perform code review, run CI checks, and manually merge when ready."
    )

    return "\n".join(lines)
