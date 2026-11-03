from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal


class WorkflowStage(StrEnum):
    TRIAGE = "triage"
    INVESTIGATION = "investigation"
    REPRODUCTION = "reproduction"
    FIX = "fix"
    VERIFICATION = "verification"
    APPROVAL = "approval"
    PULL_REQUEST = "pull_request"


@dataclass(frozen=True)
class TriageResult:
    """Structured output from the Triager agent."""

    summary: str
    severity: Literal["P0", "P1", "P2", "P3"]
    affected_components: list[str]
    acceptance_criteria: str
    missing_information: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InvestigationResult:
    """Structured output from the Investigator agent."""

    probable_root_cause: str
    affected_files: list[str]
    evidence: list[str]
    reproduction_strategy: str
    confidence: Literal["low", "medium", "high"] = "medium"


@dataclass(frozen=True)
class ReproductionResult:
    """Structured output from the Reproducer agent."""

    test_file: str
    test_code: str
    exit_code: int
    failure_evidence: str
    reproduced: bool


@dataclass(frozen=True)
class FixResult:
    """Structured output from the Fixer agent."""

    changed_files: list[str]
    diff: str
    rationale: str
    branch_name: str


@dataclass(frozen=True)
class VerificationResult:
    """Structured output from the Verifier agent."""

    passed_count: int
    failed_count: int
    duration_seconds: float
    exit_code: int
    test_logs: str
    verified: bool


@dataclass(frozen=True)
class WorkflowEvidence:
    """Audit evidence record captured during workflow progression."""

    stage: WorkflowStage
    runtime_name: str
    summary: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, Any] = field(default_factory=dict)
    command_output: str | None = None


@dataclass(frozen=True)
class StageExecutionResult:
    """Encapsulates the execution result of an individual workflow stage."""

    stage: WorkflowStage
    status: Literal["success", "failure", "skipped"]
    result: Any
    evidence: list[str]
    duration_seconds: float = 0.0
    error: str | None = None
    runtime_name: str = "mock"
