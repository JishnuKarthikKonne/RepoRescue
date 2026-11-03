from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("reporescue.agent_runtime")


class AgentRuntimeError(Exception):
    """Base exception for agent runtime errors."""


class RuntimeConfigurationError(AgentRuntimeError):
    """Raised when real AI runtime is requested but configuration or credentials are missing."""


class AgentNetworkError(AgentRuntimeError):
    """Raised when a network request to an AI provider fails or times out."""


class AgentResponseValidationError(AgentRuntimeError):
    """Raised when model returns non-conforming or invalid structured JSON."""


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


# Schema specifications injected into system prompts for real AI providers
ROLE_SCHEMA_INSTRUCTIONS: dict[str, str] = {
    "triager": (
        "Respond with a single JSON object containing:\n"
        '{\n  "summary": "one-sentence restatement of the problem",\n'
        '  "severity": "P0" | "P1" | "P2" | "P3",\n'
        '  "affected_components": ["component1", ...],\n'
        '  "acceptance_criteria": "what must pass to consider resolved",\n'
        '  "missing_information": []\n}'
    ),
    "investigator": (
        "Respond with a single JSON object containing:\n"
        '{\n  "probable_root_cause": "detailed explanation of bug cause",\n'
        '  "affected_files": ["relative/path/to/file.py", ...],\n'
        '  "evidence": ["evidence line 1", ...],\n'
        '  "reproduction_strategy": "proposed strategy to reproduce",\n'
        '  "confidence": "low" | "medium" | "high"\n}'
    ),
    "reproducer": (
        "Carefully inspect `repository_files` in the context! Write a minimal, deterministic pytest test that imports from the actual package shown in repository_files (e.g. `from shop.checkout import calculate_order_total`) and reproduces the reported bug failure when executed.\n"
        "Respond with a single JSON object containing:\n"
        '{\n  "test_file": "tests/test_reproduce_coupon_500.py",\n'
        '  "test_code": "from shop.checkout import calculate_order_total\\n\\ndef test_reproduce_expired_coupon_500():\\n    items = [{\\"price\\": 100.0, \\"quantity\\": 1}]\\n    total = calculate_order_total(items, coupon_code=\\"EXPIRED\\")\\n    assert total == 100.0\\n",\n'
        '  "failure_evidence": "description of expected failure output"\n}'
    ),
    "fixer": (
        "Carefully inspect `repository_files` and `reproduction_evidence` in the context! Provide the complete updated file content of the actual module in repository_files (e.g. `src/shop/checkout.py`) to fix the bug without breaking existing tests.\n"
        "CRITICAL FIX RULES:\n"
        "1. Fix the problem directly and defensively so tests pass and no unhandled exceptions are raised.\n"
        "2. Preserve existing function signatures, return types, and import statements.\n"
        "3. Keep 'from __future__ import annotations' at the top of the file; never import builtin types like 'list', 'dict', 'float', 'str' from typing.\n"
        "Respond with a single JSON object containing:\n"
        '{\n  "changed_files": ["src/shop/checkout.py"],\n'
        '  "file_contents": {\n'
        '    "src/shop/checkout.py": "... complete fixed file content ..."\n'
        "  },\n"
        '  "rationale": "one-sentence rationale for the fix"\n}'
    ),
    "verifier": (
        "Respond with a single JSON object containing:\n"
        '{\n  "passed_count": 0,\n'
        '  "failed_count": 0,\n'
        '  "duration_seconds": 0.0,\n'
        '  "exit_code": 0,\n'
        '  "test_logs": "test runner output",\n'
        '  "verified": true\n}'
    ),
}


def extract_json_payload(raw_text: str) -> dict[str, Any]:
    """Extracts and parses JSON object from model response text.

    Supports:
    - Pure JSON string
    - Markdown code fences ```json ... ``` or ``` ... ```
    - Substrings bounded by outer curly braces
    """
    text = raw_text.strip()
    if not text:
        raise AgentResponseValidationError("Empty response received from model.")

    # 1. Direct JSON parse
    try:
        data = json.loads(text, strict=False)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # 2. Markdown code block extraction
    code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if code_block_match:
        try:
            data = json.loads(code_block_match.group(1), strict=False)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # 3. Outer curly braces extraction
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            data = json.loads(candidate, strict=False)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    raise AgentResponseValidationError(
        f"Failed to parse valid JSON object from model response: {text[:150]}..."
    )


def validate_agent_payload(role: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validates structured JSON payload conforming to the role's schema."""
    role = role.lower()
    if role == "triager":
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise AgentResponseValidationError(
                "Triager output missing valid 'summary' string"
            )
        sev = str(data.get("severity", "P2")).upper()
        if sev not in ("P0", "P1", "P2", "P3"):
            sev = "P2"
        data["severity"] = sev
        comps = data.get("affected_components", ["core"])
        if not isinstance(comps, list):
            comps = [str(comps)]
        data["affected_components"] = comps
        if not isinstance(data.get("acceptance_criteria"), str):
            data["acceptance_criteria"] = "Issue resolved and verified by test suite."

    elif role == "investigator":
        cause = data.get("probable_root_cause")
        if not isinstance(cause, str) or not cause.strip():
            raise AgentResponseValidationError(
                "Investigator output missing valid 'probable_root_cause' string"
            )
        files = data.get("affected_files", [])
        if not isinstance(files, list):
            files = [str(files)]
        data["affected_files"] = files
        evidence = data.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        data["evidence"] = evidence
        conf = str(data.get("confidence", "medium")).lower()
        if conf not in ("low", "medium", "high"):
            conf = "medium"
        data["confidence"] = conf

    elif role == "reproducer":
        test_file = data.get("test_file")
        if not isinstance(test_file, str) or not test_file.strip():
            raise AgentResponseValidationError(
                "Reproducer output missing valid 'test_file'"
            )
        test_code = data.get("test_code")
        if not isinstance(test_code, str) or not test_code.strip():
            raise AgentResponseValidationError(
                "Reproducer output missing valid 'test_code'"
            )

    elif role == "fixer":
        file_contents = data.get("file_contents")
        if not isinstance(file_contents, dict) or not file_contents:
            if not data.get("diff"):
                raise AgentResponseValidationError(
                    "Fixer output missing valid 'file_contents' dictionary or 'diff'"
                )
            file_contents = {}
        data["file_contents"] = file_contents
        changed_files = data.get("changed_files", list(file_contents.keys()))
        if not isinstance(changed_files, list):
            changed_files = list(file_contents.keys())
        data["changed_files"] = changed_files
        if not isinstance(data.get("rationale"), str):
            data["rationale"] = "Apply minimal bug fix"

    return data


class MockAgentRuntime:
    """Deterministic agent runtime for unit tests, offline demos, and reproducible execution.

    Invariants:
    - Always flags output as `is_simulation=True`.
    - Never fabricates real execution when external calls are absent.
    - Generates coherent, valid structured data conforming to RepoRescue schemas.
    """

    def __init__(self, runtime_name: str = "mock-deterministic") -> None:
        self.runtime_name = runtime_name
        self.is_simulation = True
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
            data = {
                "summary": f"Mock output for {role}",
                "details": request.context,
            }

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


class OpenAICompatibleRuntime:
    """Real AI Agent Runtime connecting to any OpenAI-compatible HTTP endpoint.

    Supported Providers:
    - OpenAI API (https://api.openai.com/v1)
    - Local or remote Ollama (http://localhost:11434/v1)
    - LiteLLM / vLLM / OpenRouter / Azure OpenAI / Groq
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 30.0,
        runtime_name: str | None = None,
        http_handler: (
            Callable[[urllib.request.Request, float], dict[str, Any]] | None
        ) = None,
    ) -> None:
        self.api_key = (
            api_key or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        )
        self.base_url = (
            base_url
            or os.getenv("OPENAI_BASE_URL")
            or os.getenv("LLM_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = (
            model
            or os.getenv("OPENAI_MODEL")
            or os.getenv("LLM_MODEL")
            or "gpt-4o-mini"
        )
        self.timeout_seconds = timeout_seconds
        self.runtime_name = runtime_name or f"openai/{self.model}"
        self.is_simulation = False
        self.http_handler = http_handler

    def run_agent(self, request: AgentRequest) -> AgentResult:
        start_time = time.perf_counter()
        role = request.role.lower()

        base_prompt = request.prompt_instruction or load_agent_prompt(role)
        schema_spec = ROLE_SCHEMA_INSTRUCTIONS.get(role, "")

        system_instruction = (
            f"{base_prompt}\n\n"
            f"STRICT OUTPUT REQUIREMENT:\n"
            f"You MUST respond ONLY with a single valid JSON object strictly conforming to:\n"
            f"{schema_spec}\n"
            f"Do NOT include markdown formatting, explanations, or text outside the JSON object."
        )

        user_content = json.dumps(request.context, indent=2)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
        }

        endpoint_url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "RepoRescue/0.1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        body_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint_url, data=body_bytes, headers=headers, method="POST"
        )

        try:
            if self.http_handler:
                raw_response = self.http_handler(req, self.timeout_seconds)
            else:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    resp_bytes = resp.read()
                    raw_response = json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as err:
            err_body = ""
            try:
                err_body = err.read().decode("utf-8", errors="replace")[:200]
            except OSError as read_err:
                logger.debug("Could not read HTTP error body: %s", read_err)
            msg = f"HTTP {err.code} from {self.base_url}: {err.reason} ({err_body})"
            logger.error("AI Provider error: %s", msg)
            raise AgentNetworkError(msg) from err
        except urllib.error.URLError as err:
            msg = f"Connection error to {self.base_url}: {err.reason}"
            logger.error("AI Provider connection failed: %s", msg)
            raise AgentNetworkError(msg) from err
        except TimeoutError as err:
            msg = f"Request to {self.base_url} timed out after {self.timeout_seconds}s"
            logger.error("AI Provider timeout: %s", msg)
            raise AgentNetworkError(msg) from err

        choices = raw_response.get("choices", [])
        if not choices:
            raise AgentResponseValidationError("Model response contains no choices.")
        message_data = choices[0].get("message", {})
        raw_text = message_data.get("content", "")

        parsed_data = extract_json_payload(raw_text)
        validated_data = validate_agent_payload(role, parsed_data)
        duration = time.perf_counter() - start_time

        return AgentResult(
            role=role,
            runtime_name=self.runtime_name,
            structured_data=validated_data,
            raw_text=raw_text,
            execution_duration=duration,
            is_simulation=False,
        )


def check_ollama_alive(url: str = "http://127.0.0.1:11434") -> bool:
    """Checks whether local Ollama daemon is reachable."""
    try:
        req = urllib.request.Request(f"{url}/api/version", method="GET")
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            return resp.status == 200
    except OSError:
        return False


class AgentRuntimeFactory:
    """Factory to provision agent runtimes depending on environment configuration."""

    @staticmethod
    def get_runtime(
        preference: str | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> AgentRuntimeProtocol:
        """Returns the appropriate runtime.

        Preference options:
        - "mock": Deterministic MockAgentRuntime (is_simulation=True)
        - "real": OpenAICompatibleRuntime (is_simulation=False); connects to OpenAI, cloud LLM, or local Ollama
        - None: Auto-detect. Uses real provider if OPENAI_API_KEY / LLM_API_KEY / OPENAI_BASE_URL is set, or local Ollama is active.
        """
        force_mock = os.getenv("REPORESCUE_FORCE_MOCK", "").lower() in (
            "1",
            "true",
        )
        if preference == "mock" or force_mock:
            logger.info("Using MockAgentRuntime (explicit mock mode)")
            return MockAgentRuntime()

        key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        custom_url = (
            base_url or os.getenv("OPENAI_BASE_URL") or os.getenv("LLM_BASE_URL")
        )

        # Check if local Ollama daemon is active if no custom url was specified
        ollama_active = False
        target_model = model
        if not key and not custom_url:
            ollama_url = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
            if check_ollama_alive(ollama_url):
                ollama_active = True
                custom_url = f"{ollama_url}/v1"
                target_model = (
                    model
                    or os.getenv("OLLAMA_MODEL")
                    or os.getenv("OPENAI_MODEL")
                    or "qwen3:8b"
                )

        if preference == "real":
            if not key and not custom_url and not ollama_active:
                raise RuntimeConfigurationError(
                    "Real AI runtime requested, but neither OPENAI_API_KEY (or LLM_API_KEY) "
                    "nor OPENAI_BASE_URL (or LLM_BASE_URL) is configured, and no local Ollama "
                    "instance was detected on http://127.0.0.1:11434."
                )
            rt_label = (
                f"ollama/{target_model}"
                if ollama_active
                else f"openai/{target_model or 'gpt-4o-mini'}"
            )
            logger.info("Initializing real AI agent runtime: %s", rt_label)
            return OpenAICompatibleRuntime(
                api_key=key,
                base_url=custom_url,
                model=target_model,
                timeout_seconds=timeout_seconds,
                runtime_name=rt_label,
            )

        # Auto-detect: if credentials or active local Ollama are detected, use real provider
        if key or custom_url or ollama_active:
            rt_label = (
                f"ollama/{target_model}"
                if ollama_active
                else f"openai/{target_model or 'gpt-4o-mini'}"
            )
            logger.info(
                "Active AI provider detected (%s). Using real AI runtime.",
                rt_label,
            )
            return OpenAICompatibleRuntime(
                api_key=key,
                base_url=custom_url,
                model=target_model,
                timeout_seconds=timeout_seconds,
                runtime_name=rt_label,
            )

        logger.info(
            "No external AI provider keys or endpoints detected. Operating with deterministic MockAgentRuntime."
        )
        return MockAgentRuntime()
