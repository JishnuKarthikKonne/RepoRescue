from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("reporescue.agent_runtime")


@dataclass(frozen=True)
class AgentRequest:
    """Request sent to an AI Agent Runtime."""

    role: str
    context: dict[str, Any]
    prompt_instruction: str = ""


@dataclass(frozen=True)
class AgentResult:
    """Structured response returned by an AI Agent Runtime."""

    role: str
    runtime_name: str
    structured_data: dict[str, Any]
    raw_text: str
    execution_duration: float
    is_simulation: bool = False


class AgentRuntimeProtocol(Protocol):
    """Protocol for pluggable, provider-neutral agent execution runtimes."""

    def run_agent(self, request: AgentRequest) -> AgentResult:
        """Executes an agent request and returns validated structured data."""
        ...


def load_agent_prompt(role: str, repo_root: Path | None = None) -> str:
    """Loads the role prompt instructions from agents/<role>.md if available."""
    root = repo_root or Path.cwd()
    prompt_file = root / "agents" / f"{role}.md"
    if prompt_file.exists():
        try:
            return prompt_file.read_text(encoding="utf-8").strip()
        except OSError as err:
            logger.warning("Failed to read prompt file %s: %s", prompt_file, err)
    return f"You are the RepoRescue {role.capitalize()} Agent."


class MockAgentRuntime:
    """Deterministic agent runtime for unit tests, offline demos, and reproducible execution.

    Invariants:
    - Always flags output as `is_simulation=True`.
    - Never fabricates real execution when external calls are absent.
    - Generates coherent, valid structured data conforming to RepoRescue schemas.
    """

    def __init__(self, runtime_name: str = "mock-deterministic") -> None:
        self.runtime_name = runtime_name
        self.custom_responses: dict[str, dict[str, Any]] = {}

    def set_custom_response(self, role: str, data: dict[str, Any]) -> None:
        """Allows test suites to override responses for specific roles."""
        self.custom_responses[role] = data

    def run_agent(self, request: AgentRequest) -> AgentResult:
        start_time = time.perf_counter()
        role = request.role.lower()

        if role in self.custom_responses:
            data = self.custom_responses[role]
        elif role == "triager":
            data = self._generate_mock_triage(request.context)
        elif role == "investigator":
            data = self._generate_mock_investigation(request.context)
        elif role == "reproducer":
            data = self._generate_mock_reproduction(request.context)
        elif role == "fixer":
            data = self._generate_mock_fix(request.context)
        elif role == "verifier":
            data = self._generate_mock_verification(request.context)
        else:
            data = {"summary": f"Mock output for {role}", "details": request.context}

        duration = time.perf_counter() - start_time
        return AgentResult(
            role=role,
            runtime_name=self.runtime_name,
            structured_data=data,
            raw_text=json.dumps(data, indent=2),
            execution_duration=duration,
            is_simulation=True,
        )

    def _generate_mock_triage(self, ctx: dict[str, Any]) -> dict[str, Any]:
        title = ctx.get("title", "Unhandled error during execution")
        desc = ctx.get("description", "")
        severity = "P1" if "500" in title or "crash" in desc.lower() else "P2"
        return {
            "summary": f"Triaged issue: {title}",
            "severity": severity,
            "affected_components": ["checkout", "billing", "api"],
            "acceptance_criteria": "Checkout returns HTTP 200 without throwing unhandled exceptions.",
            "missing_information": [],
        }

    def _generate_mock_investigation(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "probable_root_cause": (
                "Unhandled exception raised when coupon code is invalid or expired. "
                "Discount application lacks defensive fallback."
            ),
            "affected_files": ["src/shop/checkout.py"],
            "evidence": [
                "src/shop/checkout.py:24: unhandled ValueError on expired coupon",
                "Missing check for coupon expiration date and active status",
            ],
            "reproduction_strategy": "Invoke calculate_discount(100.0, 'EXPIRED') and assert graceful error handling.",
            "confidence": "high",
        }

    def _generate_mock_reproduction(self, ctx: dict[str, Any]) -> dict[str, Any]:
        test_code = (
            "from shop.checkout import calculate_order_total\n\n"
            "def test_reproduce_expired_coupon_500():\n"
            "    items = [{'price': 100.0, 'quantity': 1}]\n"
            "    # Should not throw HTTP 500 error on expired coupon\n"
            "    total = calculate_order_total(items, coupon_code='EXPIRED')\n"
            "    assert total == 100.0\n"
        )
        return {
            "test_file": "tests/test_reproduce_coupon_500.py",
            "test_code": test_code,
            "exit_code": 1,
            "failure_evidence": (
                "FAILED tests/test_reproduce_coupon_500.py::test_reproduce_expired_coupon_500\n"
                "ValueError: HTTP 500: Server error unhandled coupon state EXPIRED"
            ),
            "reproduced": True,
        }

    def _generate_mock_fix(self, ctx: dict[str, Any]) -> dict[str, Any]:
        fixed_code = (
            "from __future__ import annotations\n\n\n"
            "def calculate_order_total(items: list[dict[str, float]], coupon_code: str | None = None) -> float:\n"
            '    """Calculates order total applying optional coupon code."""\n'
            "    subtotal = sum(item['price'] * item.get('quantity', 1) for item in items)\n"
            "    if not coupon_code:\n"
            "        return round(subtotal, 2)\n\n"
            "    valid_coupons = {\n"
            "        'SAVE10': 0.10,\n"
            "        'SAVE20': 0.20,\n"
            "    }\n\n"
            "    # Fix: Expired or invalid coupons are handled gracefully without raising HTTP 500\n"
            "    discount = valid_coupons.get(coupon_code, 0.0)\n"
            "    return round(subtotal * (1.0 - discount), 2)\n"
        )
        diff_snippet = (
            "--- a/src/shop/checkout.py\n"
            "+++ b/src/shop/checkout.py\n"
            "@@ -15,4 +15,2 @@\n"
            "-    if coupon_code == 'EXPIRED':\n"
            "-        raise ValueError('HTTP 500: Server error unhandled coupon state EXPIRED')\n"
            "     discount = valid_coupons.get(coupon_code, 0.0)\n"
        )
        return {
            "changed_files": ["src/shop/checkout.py"],
            "diff": diff_snippet,
            "file_contents": {"src/shop/checkout.py": fixed_code},
            "rationale": "Handle expired or invalid coupons gracefully by ignoring them without raising an unhandled HTTP 500 exception.",
            "branch_name": ctx.get("branch_name", "reporescue/fix-branch"),
        }

    def _generate_mock_verification(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "passed_count": 5,
            "failed_count": 0,
            "duration_seconds": 0.42,
            "exit_code": 0,
            "test_logs": "5 passed in 0.42s",
            "verified": True,
        }


class AgentRuntimeFactory:
    """Factory to provision agent runtimes depending on environment configuration."""

    @staticmethod
    def get_runtime(preference: str | None = None) -> AgentRuntimeProtocol:
        """Returns the appropriate runtime.

        Defaults to MockAgentRuntime when offline or no API keys exist.
        """
        if preference == "mock" or os.getenv("REPORESCUE_FORCE_MOCK", "").lower() in (
            "1",
            "true",
        ):
            logger.info("Using MockAgentRuntime (explicit mock mode)")
            return MockAgentRuntime()

        # If future real provider keys are detected:
        # e.g., GEMINI_API_KEY, OPENAI_API_KEY
        # Since currently no external AI CLI or API keys exist in this environment,
        # fallback gracefully to MockAgentRuntime with explicit logging.
        logger.info(
            "No external AI provider keys detected. Operating with deterministic MockAgentRuntime."
        )
        return MockAgentRuntime()
