from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any

from ..git_ops import WorktreeManager
from ..models import BugTask, TaskStatus
from ..runner import run_command
from .client import (
    GitHubClient,
    GitHubClientProtocol,
    MockGitHubClient,
    _sanitize_message,
)
from .config import (
    GitHubConfig,
    detect_repository_origin,
    get_orchestrator_repository,
    is_demo_repository,
)
from .evidence import build_pr_body, build_pr_title
from .fingerprint import compute_worktree_patch_fingerprint
from .models import GitHubPRRequest, GitHubPRResult

logger = logging.getLogger("reporescue.github.manager")

PROTECTED_ROOT_DIRS = (
    ".git",
    ".reporescue",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
)
PROTECTED_EXACT_FILES = {".env", ".env.local"}


class GitHubManagerError(Exception):
    """Base exception for GitHub publishing manager errors."""


class SafetyGateViolationError(GitHubManagerError):
    """Raised when one of the 10 strict safety gates fails."""


class PatchTamperedError(SafetyGateViolationError):
    """Raised when worktree patch does not match approved patch fingerprint."""


def _check_secret_patterns(text: str) -> list[str]:
    """Scans text for common credential patterns to prevent secret leakage in PRs."""
    found: list[str] = []
    patterns = [
        (r"ghp_[0-9a-zA-Z]{36}", "GitHub Personal Access Token (classic)"),
        (r"github_pat_[0-9a-zA-Z_]{82}", "GitHub Fine-Grained Token"),
        (r"-----BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY-----", "Private Key"),
        (r"xox[baprs]-[0-9a-zA-Z]{10,48}", "Slack Token"),
    ]
    for pattern, name in patterns:
        if re.search(pattern, text):
            found.append(name)
    return found


class GitHubPRManager:
    """Orchestrates the secure publishing of verified bug fixes as GitHub Draft PRs.

    Strictly enforces all 10 Safety Gates (Gates A through J).
    """

    def __init__(
        self,
        config: GitHubConfig | None = None,
        client: GitHubClientProtocol | None = None,
        worktree_mgr: WorktreeManager | None = None,
    ) -> None:
        self.config = config or GitHubConfig.from_env()
        self.client = client or (
            MockGitHubClient(self.config)
            if self.config.dry_run
            else GitHubClient(self.config)
        )
        self.worktree_mgr = worktree_mgr

    def validate_safety_gates(
        self,
        task: BugTask,
        worktree_path: Path,
        *,
        stage_results: dict[str, Any] | None = None,
    ) -> tuple[str, str, str]:
        """Evaluates all 10 Safety Gates prior to any Git push or PR creation.

        Returns:
            tuple[str, str, str]: (patch_fingerprint, diff_text, commit_sha)

        Raises:
            PatchTamperedError: If Gate E fails (approval is invalidated).
            SafetyGateViolationError: If any other safety gate fails.
        """
        # Gate A: Task status must be APPROVED
        if task.status != TaskStatus.APPROVED:
            raise SafetyGateViolationError(
                f"Gate A Failed: Task status must be '{TaskStatus.APPROVED}', currently '{task.status}'."
            )

        # Gate B: Approval came from authenticated human
        if not task.approval_by or not task.approval_by.strip():
            raise SafetyGateViolationError(
                "Gate B Failed: Approval must be granted by a verified human teammate (approval_by is empty)."
            )
        lower_approver = task.approval_by.strip().lower()
        if lower_approver.startswith(
            ("bot:", "agent:", "system:", "@switch-agent-")
        ) or lower_approver in ("bot", "agent", "system", "reporescue"):
            raise SafetyGateViolationError(
                f"Gate B Failed: Approver '{task.approval_by}' is identified as an automated agent or bot."
            )

        # Gate C: Approved task contains a real verified patch record
        if not getattr(task, "verified_patch_fingerprint", None):
            # Fallback: check if verified in evidence
            has_verif_evidence = any(
                "Verification PASSED" in ev for ev in task.evidence
            )
            if not has_verif_evidence:
                raise SafetyGateViolationError(
                    "Gate C Failed: Approved task contains no verified patch record."
                )

        # Gate D: Independent verification passed
        verif_passed = any("Verification PASSED" in ev for ev in task.evidence)
        if stage_results:
            verif_stage = stage_results.get("verification")
            if verif_stage and getattr(verif_stage, "status", "") != "success":
                verif_passed = False
        if not verif_passed:
            raise SafetyGateViolationError(
                "Gate D Failed: Independent verification test suite did not pass."
            )

        # Gate F: Isolated worktree exists and belongs to the expected task
        wt_resolved = Path(worktree_path).resolve()
        if not wt_resolved.exists():
            raise SafetyGateViolationError(
                f"Gate F Failed: Worktree path does not exist: {wt_resolved}"
            )
        expected_branch = task.proposed_branch or f"reporescue/task-{task.task_id[:8]}"
        branch_res = run_command(["git", "branch", "--show-current"], cwd=wt_resolved)
        current_branch = branch_res.stdout.strip() if branch_res.passed else ""
        if current_branch != expected_branch:
            raise SafetyGateViolationError(
                f"Gate F Failed: Worktree branch mismatch. Expected '{expected_branch}', found '{current_branch}'."
            )

        # Gate I: No unapproved / dirty changes in worktree
        status_res = run_command(["git", "status", "--porcelain"], cwd=wt_resolved)
        if status_res.stdout.strip():
            raise SafetyGateViolationError(
                f"Gate I Failed: Worktree contains uncommitted changes:\n{status_res.stdout.strip()}"
            )

        # Compute current worktree patch fingerprint
        fp, diff_text, commit_sha = compute_worktree_patch_fingerprint(wt_resolved)

        # Gate E: Approval must bind to exact patch fingerprint
        approved_fp = getattr(task, "approved_patch_fingerprint", None) or getattr(
            task, "verified_patch_fingerprint", None
        )
        if not approved_fp or fp != approved_fp:
            # TAMPER DETECTED: Invalidate approval and transition task back
            task.approval_by = None
            task.approved_at = None
            task.approved_patch_fingerprint = None
            try:
                task.transition(TaskStatus.FIX_PROPOSED, actor="github_safety_guard")
            except ValueError as err:
                logger.debug("Rollback transition to FIX_PROPOSED failed: %s", err)
            task.evidence.append(
                f"Approval invalidated: Worktree patch modified after approval "
                f"(expected {approved_fp}, got {fp}). Re-verification and approval required."
            )
            raise PatchTamperedError(
                f"Gate E Failed: Worktree patch fingerprint '{fp}' does not match "
                f"approved fingerprint '{approved_fp}'. Approval has been invalidated."
            )

        # Gate G: No protected paths, traversal paths, or secrets
        changed_files_res = run_command(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=wt_resolved,
        )
        changed_files = (
            changed_files_res.stdout.strip().splitlines()
            if changed_files_res.passed and changed_files_res.stdout.strip()
            else []
        )
        for cf in changed_files:
            cf_norm = cf.strip().replace("\\", "/")
            parts = Path(cf_norm).parts
            if any(part in PROTECTED_ROOT_DIRS for part in parts):
                raise SafetyGateViolationError(
                    f"Gate G Failed: Modification of protected directory detected: '{cf}'."
                )
            if Path(cf_norm).name in PROTECTED_EXACT_FILES:
                raise SafetyGateViolationError(
                    f"Gate G Failed: Modification of protected file detected: '{cf}'."
                )

        secrets = _check_secret_patterns(diff_text)
        if secrets:
            raise SafetyGateViolationError(
                f"Gate G Failed: Detected sensitive credentials in patch: {', '.join(secrets)}."
            )

        # Gate H: Git repository and configuration validated
        config_errors = self.config.validate()
        if config_errors:
            raise SafetyGateViolationError(
                f"Gate H Failed: Configuration validation errors: {'; '.join(config_errors)}"
            )

        # Gate K: Multi-Repository Isolation
        is_demo = is_demo_repository(wt_resolved) or self.config.is_demo
        wt_origin = detect_repository_origin(wt_resolved)
        orchestrator_repo = (
            self.config.orchestrator_repository
            or get_orchestrator_repository(wt_resolved)
        )

        if is_demo:
            if (
                orchestrator_repo
                and self.config.repository
                and self.config.repository.lower() == orchestrator_repo.lower()
            ):
                raise SafetyGateViolationError(
                    f"Gate K Failed: Repository Isolation Violation: Demo repository patches cannot be published "
                    f"to the RepoRescue orchestrator repository ('{self.config.repository}'). "
                    f"The demo repository must target an independent repository."
                )
            if (
                not wt_origin
                and not self.config.dry_run
                and not isinstance(self.client, MockGitHubClient)
            ):
                import os

                if not os.getenv("DEMO_GITHUB_REPOSITORY"):
                    raise SafetyGateViolationError(
                        "Gate K Failed: Repository Isolation Violation: Demo repository (.reporescue/demo_repo) has "
                        "no configured GitHub remote origin. The demo coupon patch cannot be published until an "
                        "independent GitHub repository is configured as the demo repository's remote origin."
                    )

        if (
            wt_origin
            and self.config.repository
            and wt_origin.lower() != self.config.repository.lower()
        ):
            raise SafetyGateViolationError(
                f"Gate K Failed: Repository Isolation Violation: Worktree origin remote '{wt_origin}' "
                f"does not match configured target repository '{self.config.repository}'."
            )

        return fp, diff_text, commit_sha

    def push_branch(self, worktree_path: Path, branch_name: str) -> str:
        """Pushes the isolated task branch to GitHub without force push.

        Never exposes credentials in command-line arguments, remote URLs, logs, or exceptions.
        Authenticates via standard Git Smart HTTP Basic Auth or Windows Git Credential Manager.
        """
        # Dry-run or Mock client: simulate push without external network
        if self.config.dry_run or isinstance(self.client, MockGitHubClient):
            logger.info(
                "Dry-run / Mock: Simulating safe push of branch '%s'", branch_name
            )
            return "simulated-push"

        if not self.config.repository:
            raise SafetyGateViolationError(
                "Cannot push branch: repository is not configured."
            )

        wt_origin = detect_repository_origin(worktree_path)
        if (
            wt_origin
            and self.config.repository
            and wt_origin.lower() != self.config.repository.lower()
        ):
            raise SafetyGateViolationError(
                f"Repository Identity Mismatch: Worktree remote origin '{wt_origin}' "
                f"does not match target repository '{self.config.repository}'."
            )

        if not branch_name.startswith("reporescue/task-") or branch_name in (
            "main",
            "master",
        ):
            raise SafetyGateViolationError(
                f"Safety Invariant Violation: Refusing to push non-task branch '{branch_name}'. "
                f"Branch must start with 'reporescue/task-' and must not be main."
            )

        remote_url = f"https://github.com/{self.config.repository}.git"
        push_cmd = ["git", "push", "-u", remote_url, f"{branch_name}:{branch_name}"]

        # Configure secure in-memory environment for Git
        push_env: dict[str, str] = {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }

        if self.config.token:
            user = self.config.token_username or "x-access-token"
            auth_bytes = f"{user}:{self.config.token}".encode()
            basic_auth = base64.b64encode(auth_bytes).decode("ascii")
            push_env["GIT_CONFIG_PARAMETERS"] = (
                f"'credential.helper=' 'http.extraHeader=Authorization: Basic {basic_auth}'"
            )
            push_env["GITHUB_TOKEN"] = self.config.token
            push_env["GH_TOKEN"] = self.config.token
            push_env["GCM_TOKEN"] = self.config.token
        else:
            # Fallback to local Git Credential Manager only with explicit user warning
            logger.warning(
                "No explicit GITHUB_TOKEN configured. Falling back to local Git Credential Manager (GCM). "
                "Set GITHUB_TOKEN for deterministic headless CI/CD authentication."
            )

        # Execute push with strict timeout
        res = run_command(
            push_cmd,
            cwd=worktree_path,
            timeout_seconds=self.config.push_timeout_seconds,
            env=push_env,
        )

        sanitized_stdout = _sanitize_message(res.stdout, self.config.token)
        sanitized_stderr = _sanitize_message(res.stderr, self.config.token)

        if not res.passed:
            # If already up to date, treat as clean idempotency
            if (
                "Everything up-to-date" in sanitized_stdout
                or "Everything up-to-date" in sanitized_stderr
            ):
                logger.info("Branch '%s' is already up to date on remote.", branch_name)
                return "up-to-date"
            raise GitHubManagerError(
                f"Failed to push branch '{branch_name}' to GitHub: {sanitized_stderr or sanitized_stdout}"
            )

        logger.info(
            "Successfully pushed branch '%s' to %s", branch_name, self.config.repository
        )
        return "pushed"

    def publish_draft_pr(
        self,
        task: BugTask,
        worktree_path: Path,
        *,
        stage_results: dict[str, Any] | None = None,
        runtime_name: str = "mock",
    ) -> GitHubPRResult:
        """Publishes the approved bug fix as a GitHub DRAFT Pull Request.

        Enforces all 10 Safety Gates, pushes the branch, and creates the draft PR.
        """
        # Gate J: Idempotency check - if already published, return existing record
        if getattr(task, "pr_url", None) and getattr(task, "pr_number", None):
            logger.info("Task %s is already published at %s", task.task_id, task.pr_url)
            return GitHubPRResult(
                pr_url=task.pr_url,
                pr_number=task.pr_number,
                html_url=task.pr_url,
                branch_name=task.proposed_branch
                or f"reporescue/task-{task.task_id[:8]}",
                base_branch=self.config.base_branch,
                commit_sha=getattr(task, "commit_sha", "") or "",
                title=f"[RepoRescue] {task.title}",
                patch_fingerprint=getattr(task, "approved_patch_fingerprint", "") or "",
                draft=True,
                is_existing=True,
            )

        # 1. Validate all 10 strict safety gates
        fp, _diff_text, commit_sha = self.validate_safety_gates(
            task, worktree_path, stage_results=stage_results
        )

        branch_name = task.proposed_branch or f"reporescue/task-{task.task_id[:8]}"

        # 2. Push dedicated branch to remote
        self.push_branch(worktree_path, branch_name)

        # 3. Extract stage artifacts for PR body
        sr = stage_results or {}
        triage_res = sr.get("triage").result if sr.get("triage") else None
        invest_res = sr.get("investigation").result if sr.get("investigation") else None
        repro_res = sr.get("reproduction").result if sr.get("reproduction") else None
        fix_res = sr.get("fix").result if sr.get("fix") else None
        verif_res = sr.get("verification").result if sr.get("verification") else None

        title = build_pr_title(task, triage_res)
        body = build_pr_body(
            task,
            triage=triage_res,
            investigation=invest_res,
            reproduction=repro_res,
            fix=fix_res,
            verification=verif_res,
            commit_sha=commit_sha,
            patch_fingerprint=fp,
            runtime_name=runtime_name,
            branch_name=branch_name,
        )

        # 4. Create DRAFT Pull Request via client
        pr_req = GitHubPRRequest(
            task_id=task.task_id,
            title=title,
            body=body,
            head_branch=branch_name,
            base_branch=self.config.base_branch,
            draft=True,  # Safety invariant: strictly True
        )
        pr_result = self.client.create_draft_pr(pr_req)

        # 5. Update task record with PR metadata
        task.pr_url = pr_result.html_url
        task.pr_number = pr_result.pr_number
        task.commit_sha = commit_sha
        task.approved_patch_fingerprint = fp

        try:
            task.transition(TaskStatus.PR_CREATED, actor=task.approval_by or "system")
        except ValueError as err:
            logger.debug("Task status transition to PR_CREATED skipped: %s", err)

        task.evidence.append(
            f"GitHub Draft PR #{pr_result.pr_number} created: {pr_result.html_url} (draft={pr_result.draft})"
        )

        logger.info(
            "Draft PR successfully created for task %s: %s",
            task.task_id,
            pr_result.html_url,
        )
        return pr_result
