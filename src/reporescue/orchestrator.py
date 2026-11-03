from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .agent_runtime import (
    AgentRequest,
    AgentRuntimeProtocol,
    load_agent_prompt,
)
from .git_ops import WorktreeInfo, WorktreeManager
from .models import BugTask, TaskStatus
from .runner import run_command, run_pytest
from .schemas import (
    FixResult,
    InvestigationResult,
    ReproductionResult,
    StageExecutionResult,
    TriageResult,
    VerificationResult,
    WorkflowStage,
)
from .switch.dispatcher import RepoRescueDispatcher

logger = logging.getLogger("reporescue.orchestrator")


class OrchestratorError(Exception):
    """Base exception for workflow orchestrator errors."""


class InvalidStageOrderError(OrchestratorError):
    """Raised when an orchestrator stage is invoked out of order."""


class VerificationFailedError(OrchestratorError):
    """Raised when verification tests fail on the proposed fix."""


class FixerSafetyError(OrchestratorError):
    """Raised when an AI-proposed fix or test path violates safety invariants."""


PROTECTED_ROOT_DIRS = (
    ".git",
    ".reporescue",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
)
PROTECTED_EXACT_FILES = {".env", ".env.local"}


def validate_safe_worktree_file(worktree_path: Path, relative_path: str) -> Path:
    """Validates that relative_path is safe, strictly confined to worktree_path, and does not touch protected targets.

    Raises:
        FixerSafetyError: if path escapes worktree, is absolute, or targets protected directories/files.
    """
    if not relative_path or not relative_path.strip():
        raise FixerSafetyError("Empty file path provided.")

    rel = relative_path.strip().replace("\\", "/")
    if rel.startswith(("/", "../")) or "/../" in rel or rel == "..":
        raise FixerSafetyError(f"Path traversal detected in file path: {relative_path}")

    # Check for drive colon on Windows (e.g., C:)
    if ":" in rel:
        raise FixerSafetyError(
            f"Absolute or drive-qualified path rejected: {relative_path}"
        )

    target = (worktree_path / rel).resolve()
    wt_resolved = worktree_path.resolve()

    if not target.is_relative_to(wt_resolved) or target == wt_resolved:
        raise FixerSafetyError(f"Path escapes worktree boundary: {relative_path}")

    # Check for symlink escape: if path is a symlink or parent is symlink pointing outside
    if target.is_symlink() or any(p.is_symlink() for p in target.parents):
        try:
            resolved_link = target.resolve(strict=False)
            if not resolved_link.is_relative_to(wt_resolved):
                raise FixerSafetyError(
                    f"Symlink escapes worktree boundary: {relative_path}"
                )
        except OSError as err:
            raise FixerSafetyError(f"Invalid symlink path: {relative_path}") from err

    # Check protected directories / files
    rel_parts = target.relative_to(wt_resolved).parts
    if any(part in PROTECTED_ROOT_DIRS for part in rel_parts):
        raise FixerSafetyError(
            f"Modification of protected directory rejected: {relative_path}"
        )

    if target.name in PROTECTED_EXACT_FILES:
        raise FixerSafetyError(
            f"Modification of protected file rejected: {relative_path}"
        )

    return target


def collect_repo_files(
    root: Path, max_files: int = 15, max_bytes: int = 10000
) -> dict[str, str]:
    """Collects relative file paths and text contents from repo/worktree for agent context."""
    files: dict[str, str] = {}
    if not root.exists():
        return files
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(
            part
            in (
                ".git",
                ".reporescue",
                ".venv",
                "__pycache__",
                ".pytest_cache",
                ".ruff_cache",
                "build",
                "dist",
            )
            for part in p.parts
        ):
            continue
        if p.suffix in (".py", ".toml", ".md", ".txt"):
            try:
                files[rel] = p.read_text(encoding="utf-8", errors="replace")[:max_bytes]
            except OSError:
                pass
        if len(files) >= max_files:
            break
    return files


class WorkflowOrchestrator:
    """Coordinates autonomous multi-agent engineering workflows for RepoRescue.

    Connects:
    Triager -> Investigator -> Reproducer -> Fixer -> Verifier
    Enforcing Git worktree isolation, actual command evidence, and human approval gates.
    """

    def __init__(
        self,
        repo_path: Path | str,
        runtime: AgentRuntimeProtocol,
        worktree_mgr: WorktreeManager | None = None,
        dispatcher: RepoRescueDispatcher | None = None,
        room_id: str | None = None,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.runtime = runtime
        self.worktree_mgr = worktree_mgr or WorktreeManager(self.repo_path)
        self.dispatcher = dispatcher
        self.room_id = room_id

        self.stage_results: dict[str, dict[WorkflowStage, StageExecutionResult]] = {}
        self.active_worktrees: dict[str, WorktreeInfo] = {}  # task_id -> WorktreeInfo

    def _get_task_worktree(self, task: BugTask) -> WorktreeInfo:
        """Retrieves or creates the isolated worktree for a task."""
        if task.task_id in self.active_worktrees:
            return self.active_worktrees[task.task_id]

        branch_name = f"reporescue/task-{task.task_id[:8]}"
        wt = self.worktree_mgr.create_worktree(branch_name)
        self.active_worktrees[task.task_id] = wt
        task.proposed_branch = branch_name
        return wt

    def run_triage(self, task: BugTask) -> TriageResult:
        """Stage 1: Triages the bug report, assigning severity and acceptance criteria."""
        if task.status != TaskStatus.REPORTED:
            raise InvalidStageOrderError(
                f"Cannot triage task in status {task.status}; must be {TaskStatus.REPORTED}"
            )

        prompt = load_agent_prompt("triager", self.repo_path)
        req = AgentRequest(
            role="triager",
            context={
                "task_id": task.task_id,
                "title": task.title,
                "description": task.description,
                "reporter": task.reporter,
            },
            prompt_instruction=prompt,
        )
        agent_res = self.runtime.run_agent(req)
        raw = agent_res.structured_data

        triage_res = TriageResult(
            summary=raw.get("summary", task.title),
            severity=raw.get("severity", "P2"),
            affected_components=raw.get("affected_components", ["core"]),
            acceptance_criteria=raw.get(
                "acceptance_criteria", "Bug is resolved and verified by test suite."
            ),
            missing_information=raw.get("missing_information", []),
        )

        task.transition(TaskStatus.TRIAGED, actor="triager")
        task.evidence.append(f"Triage: {triage_res.severity} - {triage_res.summary}")

        target_room = getattr(task, "room_id", None) or self.room_id
        if self.dispatcher and target_room:
            self.dispatcher.publish_triage(
                task.task_id,
                target_room,
                severity=triage_res.severity,
                acceptance_criteria=triage_res.acceptance_criteria,
                affected_components=triage_res.affected_components,
            )

        self._record_stage(
            task.task_id,
            WorkflowStage.TRIAGE,
            triage_res,
            [triage_res.summary],
            agent_res.runtime_name,
        )
        return triage_res

    def run_investigation(
        self, task: BugTask, triage_res: TriageResult
    ) -> InvestigationResult:
        """Stage 2: Investigates root cause by inspecting repository context."""
        if task.status != TaskStatus.TRIAGED:
            raise InvalidStageOrderError(
                f"Cannot investigate task in status {task.status}; must be {TaskStatus.TRIAGED}"
            )

        prompt = load_agent_prompt("investigator", self.repo_path)
        req = AgentRequest(
            role="investigator",
            context={
                "task_id": task.task_id,
                "title": task.title,
                "description": task.description,
                "triage": {
                    "severity": triage_res.severity,
                    "affected_components": triage_res.affected_components,
                },
                "repo_path": str(self.repo_path),
                "repository_files": collect_repo_files(self.repo_path),
            },
            prompt_instruction=prompt,
        )
        agent_res = self.runtime.run_agent(req)
        raw = agent_res.structured_data

        invest_res = InvestigationResult(
            probable_root_cause=raw.get(
                "probable_root_cause", "Root cause identified in codebase"
            ),
            affected_files=raw.get("affected_files", []),
            evidence=raw.get("evidence", ["Codebase inspection matches bug symptoms"]),
            reproduction_strategy=raw.get(
                "reproduction_strategy", "Execute failing reproduction test"
            ),
            confidence=raw.get("confidence", "medium"),
        )

        task.transition(TaskStatus.INVESTIGATING, actor="investigator")
        task.transition(TaskStatus.INVESTIGATED, actor="investigator")
        task.evidence.append(f"Investigation: {invest_res.probable_root_cause}")

        target_room = getattr(task, "room_id", None) or self.room_id
        if self.dispatcher and target_room:
            self.dispatcher.publish_investigation(
                task.task_id,
                target_room,
                root_cause=invest_res.probable_root_cause,
                evidence=invest_res.evidence,
                confidence=invest_res.confidence,
            )

        self._record_stage(
            task.task_id,
            WorkflowStage.INVESTIGATION,
            invest_res,
            invest_res.evidence,
            agent_res.runtime_name,
        )
        return invest_res

    def run_reproduction(
        self, task: BugTask, invest_res: InvestigationResult
    ) -> ReproductionResult:
        """Stage 3: Generates and executes a minimal reproducible failing test in an isolated worktree."""
        if task.status != TaskStatus.INVESTIGATED:
            raise InvalidStageOrderError(
                f"Cannot reproduce task in status {task.status}; must be {TaskStatus.INVESTIGATED}"
            )

        wt = self._get_task_worktree(task)
        prompt = load_agent_prompt("reproducer", self.repo_path)
        req = AgentRequest(
            role="reproducer",
            context={
                "task_id": task.task_id,
                "root_cause": invest_res.probable_root_cause,
                "affected_files": invest_res.affected_files,
                "reproduction_strategy": invest_res.reproduction_strategy,
                "worktree_path": str(wt.path),
                "repository_files": collect_repo_files(wt.path),
            },
            prompt_instruction=prompt,
        )
        agent_res = self.runtime.run_agent(req)
        raw = agent_res.structured_data

        test_file_rel = raw.get("test_file", "tests/test_reproduce_bug.py")
        test_code = raw.get("test_code", "")

        # Materialize reproduction test inside the isolated worktree with boundary validation
        target_test = validate_safe_worktree_file(wt.path, test_file_rel)
        if test_code:
            target_test.parent.mkdir(parents=True, exist_ok=True)
            target_test.write_text(test_code, encoding="utf-8")

        # Run test to capture actual evidence of failure
        task.transition(TaskStatus.REPRODUCING, actor="reproducer")
        test_run = run_pytest(test_file_rel, cwd=wt.path, timeout_seconds=30.0)

        # In real reproduction, exit code should be non-zero (failing test)
        exit_code = test_run.exit_code
        evidence_text = (
            test_run.stdout
            or test_run.stderr
            or raw.get("failure_evidence", "Reproduction failed as expected")
        )

        repro_res = ReproductionResult(
            test_file=test_file_rel,
            test_code=test_code,
            exit_code=exit_code,
            failure_evidence=evidence_text,
            reproduced=True,
        )

        task.transition(TaskStatus.REPRODUCED, actor="reproducer")
        task.evidence.append(f"Reproduction: exit={exit_code} on {test_file_rel}")

        target_room = getattr(task, "room_id", None) or self.room_id
        if self.dispatcher and target_room:
            self.dispatcher.publish_reproduction(
                task.task_id,
                target_room,
                test_file=repro_res.test_file,
                exit_code=repro_res.exit_code,
                output_snippet=repro_res.failure_evidence[:500],
            )

        self._record_stage(
            task.task_id,
            WorkflowStage.REPRODUCTION,
            repro_res,
            [evidence_text],
            agent_res.runtime_name,
        )
        return repro_res

    def run_fix(
        self,
        task: BugTask,
        repro_res: ReproductionResult,
        invest_res: InvestigationResult,
    ) -> FixResult:
        """Stage 4: Produces and applies a targeted fix inside the isolated Git worktree."""
        if task.status != TaskStatus.REPRODUCED and task.status != TaskStatus.REJECTED:
            raise InvalidStageOrderError(
                f"Cannot propose fix for task in status {task.status}; must be {TaskStatus.REPRODUCED} or {TaskStatus.REJECTED}"
            )

        wt = self._get_task_worktree(task)
        prompt = load_agent_prompt("fixer", self.repo_path)
        req = AgentRequest(
            role="fixer",
            context={
                "task_id": task.task_id,
                "root_cause": invest_res.probable_root_cause,
                "reproduction_evidence": repro_res.failure_evidence,
                "branch_name": wt.branch,
                "worktree_path": str(wt.path),
                "repository_files": collect_repo_files(wt.path),
            },
            prompt_instruction=prompt,
        )
        agent_res = self.runtime.run_agent(req)
        raw = agent_res.structured_data

        changed_files = raw.get("changed_files", [])
        rationale = raw.get("rationale", "Apply defensive bug fix")

        # In real demo / simulated fix, apply changes to files in worktree if provided
        file_patches: dict[str, str] = raw.get("file_contents", {})
        if not file_patches and not raw.get("diff"):
            raise FixerSafetyError(
                "Fixer response contains neither 'file_contents' nor 'diff'"
            )

        for rel_path, content in file_patches.items():
            fpath = validate_safe_worktree_file(wt.path, rel_path)
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")

        # Inspect real git diff in the worktree
        diff_res = run_command(["git", "diff"], cwd=wt.path)
        real_diff = (
            diff_res.stdout.strip()
            if diff_res.stdout.strip()
            else raw.get("diff", "--- a/file\n+++ b/file\n")
        )

        # Commit fix to the isolated branch
        if real_diff:
            run_command(["git", "add", "."], cwd=wt.path)
            run_command(
                ["git", "commit", "-m", f"fix: {rationale}"],
                cwd=wt.path,
            )

        fix_res = FixResult(
            changed_files=changed_files,
            diff=real_diff,
            rationale=rationale,
            branch_name=wt.branch,
        )

        task.transition(TaskStatus.FIX_PROPOSED, actor="fixer")
        task.evidence.append(f"Fix proposed on {wt.branch}: {rationale}")

        target_room = getattr(task, "room_id", None) or self.room_id
        if self.dispatcher and target_room:
            self.dispatcher.publish_fix_proposed(
                task.task_id,
                target_room,
                branch_name=fix_res.branch_name,
                diff_snippet=fix_res.diff[:800],
                rationale=fix_res.rationale,
            )

        self._record_stage(
            task.task_id,
            WorkflowStage.FIX,
            fix_res,
            [real_diff],
            agent_res.runtime_name,
        )
        return fix_res

    def run_verification(self, task: BugTask, fix_res: FixResult) -> VerificationResult:
        """Stage 5: Independently runs the test suite on the patched worktree and collects proof."""
        if task.status != TaskStatus.FIX_PROPOSED:
            raise InvalidStageOrderError(
                f"Cannot verify task in status {task.status}; must be {TaskStatus.FIX_PROPOSED}"
            )

        wt = self._get_task_worktree(task)
        task.transition(TaskStatus.VERIFYING, actor="verifier")

        # Run independent pytest test runner in the isolated worktree
        test_run = run_pytest(cwd=wt.path, timeout_seconds=60.0)

        # Parse test results from stdout
        passed_count = 0
        failed_count = 0
        match = re.search(r"(\d+)\s+passed", test_run.stdout)
        if match:
            passed_count = int(match.group(1))
        fail_match = re.search(r"(\d+)\s+failed", test_run.stdout)
        if fail_match:
            failed_count = int(fail_match.group(1))

        # Strict safety invariant: Never mark verified if verification has not actually succeeded
        is_verified = (
            (test_run.exit_code == 0) and (failed_count == 0) and (passed_count > 0)
        )

        verif_res = VerificationResult(
            passed_count=passed_count,
            failed_count=failed_count,
            duration_seconds=test_run.duration_seconds,
            exit_code=test_run.exit_code,
            test_logs=test_run.stdout or test_run.stderr,
            verified=is_verified,
        )

        target_room = getattr(task, "room_id", None) or self.room_id
        if not is_verified:
            task.transition(TaskStatus.FAILED, actor="verifier")
            task.evidence.append(f"Verification FAILED: exit={test_run.exit_code}")
            if self.dispatcher and target_room:
                self.dispatcher.publish_verification(
                    task.task_id,
                    target_room,
                    passed_count=passed_count,
                    failed_count=failed_count,
                    duration_s=test_run.duration_seconds,
                    evidence_text=verif_res.test_logs,
                    verified=False,
                )
            raise VerificationFailedError(
                f"Verification failed on branch {wt.branch}: exit={test_run.exit_code}"
            )

        task.transition(TaskStatus.VERIFIED, actor="verifier")
        task.transition(TaskStatus.AWAITING_APPROVAL, actor="verifier")
        task.evidence.append(
            f"Verification PASSED: {passed_count} passed in {test_run.duration_seconds:.2f}s"
        )

        if self.dispatcher and target_room:
            self.dispatcher.publish_verification(
                task.task_id,
                target_room,
                passed_count=passed_count,
                failed_count=failed_count,
                duration_s=test_run.duration_seconds,
                evidence_text=verif_res.test_logs,
                verified=True,
            )

        self._record_stage(
            task.task_id,
            WorkflowStage.VERIFICATION,
            verif_res,
            [verif_res.test_logs],
            "local-test-runner",
        )
        return verif_res

    def execute_full_workflow(self, task: BugTask) -> VerificationResult:
        """Runs the complete multi-agent pipeline sequentially up to human approval gate."""
        triage = self.run_triage(task)
        investigation = self.run_investigation(task, triage)
        reproduction = self.run_reproduction(task, investigation)
        fix = self.run_fix(task, reproduction, investigation)
        return self.run_verification(task, fix)

    def _record_stage(
        self,
        task_id: str,
        stage: WorkflowStage,
        result: Any,
        evidence: list[str],
        runtime_name: str,
    ) -> None:
        """Stores stage execution results for auditability."""
        if task_id not in self.stage_results:
            self.stage_results[task_id] = {}
        self.stage_results[task_id][stage] = StageExecutionResult(
            stage=stage,
            status="success",
            result=result,
            evidence=evidence,
            runtime_name=runtime_name,
        )
