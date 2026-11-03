from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from reporescue.agent_runtime import (
    AgentNetworkError,
    AgentRequest,
    AgentResponseValidationError,
    AgentRuntimeFactory,
    MockAgentRuntime,
    OpenAICompatibleRuntime,
    RuntimeConfigurationError,
    extract_json_payload,
    validate_agent_payload,
)
from reporescue.orchestrator import FixerSafetyError, validate_safe_worktree_file

# -----------------------------------------------------------------------------
# 1. Structured Response Parsing & Validation Tests
# -----------------------------------------------------------------------------


def test_extract_json_payload_direct():
    raw = '{"summary": "Test issue", "severity": "P1"}'
    res = extract_json_payload(raw)
    assert res["summary"] == "Test issue"
    assert res["severity"] == "P1"


def test_extract_json_payload_markdown_fences():
    raw = 'Here is your JSON response:\n```json\n{\n  "summary": "Test issue",\n  "severity": "P2"\n}\n```\nHope this helps!'
    res = extract_json_payload(raw)
    assert res["summary"] == "Test issue"
    assert res["severity"] == "P2"


def test_extract_json_payload_outer_braces():
    raw = 'Explanation: {"probable_root_cause": "Null check missing"} trailing notes'
    res = extract_json_payload(raw)
    assert res["probable_root_cause"] == "Null check missing"


def test_extract_json_payload_empty_fails():
    with pytest.raises(AgentResponseValidationError, match="Empty response received"):
        extract_json_payload("")


def test_extract_json_payload_malformed_fails():
    with pytest.raises(
        AgentResponseValidationError, match="Failed to parse valid JSON"
    ):
        extract_json_payload("Just a plain text response without JSON.")


def test_validate_agent_payload_triager():
    valid_data = {
        "summary": "Checkout 500 error",
        "severity": "p0",
        "affected_components": ["checkout"],
        "acceptance_criteria": "HTTP 200 returned",
    }
    validated = validate_agent_payload("triager", valid_data)
    assert validated["severity"] == "P0"
    assert validated["affected_components"] == ["checkout"]

    # Missing summary
    with pytest.raises(AgentResponseValidationError, match="missing valid 'summary'"):
        validate_agent_payload("triager", {"summary": ""})


def test_validate_agent_payload_investigator():
    valid_data = {
        "probable_root_cause": "KeyError in lookup",
        "affected_files": ["src/checkout.py"],
        "evidence": ["line 42 raises KeyError"],
        "confidence": "HIGH",
    }
    validated = validate_agent_payload("investigator", valid_data)
    assert validated["probable_root_cause"] == "KeyError in lookup"
    assert validated["confidence"] == "high"

    with pytest.raises(
        AgentResponseValidationError, match="missing valid 'probable_root_cause'"
    ):
        validate_agent_payload("investigator", {"probable_root_cause": "   "})


def test_validate_agent_payload_reproducer():
    valid_data = {
        "test_file": "tests/test_repro.py",
        "test_code": "def test_repro(): assert False",
    }
    validated = validate_agent_payload("reproducer", valid_data)
    assert validated["test_file"] == "tests/test_repro.py"

    with pytest.raises(AgentResponseValidationError, match="missing valid 'test_file'"):
        validate_agent_payload("reproducer", {"test_code": "assert True"})


def test_validate_agent_payload_fixer():
    valid_data = {
        "changed_files": ["src/checkout.py"],
        "file_contents": {"src/checkout.py": "def checkout(): pass\n"},
        "rationale": "Handle empty cart",
    }
    validated = validate_agent_payload("fixer", valid_data)
    assert "src/checkout.py" in validated["file_contents"]

    with pytest.raises(
        AgentResponseValidationError,
        match="missing valid 'file_contents' dictionary or 'diff'",
    ):
        validate_agent_payload("fixer", {"changed_files": []})


# -----------------------------------------------------------------------------
# 2. OpenAICompatibleRuntime Tests (with mock HTTP handler)
# -----------------------------------------------------------------------------


def test_openai_compatible_runtime_success():
    recorded_requests: list[urllib.request.Request] = []

    def mock_handler(req: urllib.request.Request, timeout: float) -> dict[str, Any]:
        recorded_requests.append(req)
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"summary": "Live AI summary", "severity": "P1", "affected_components": ["core"], "acceptance_criteria": "Pass tests"}'
                    }
                }
            ]
        }

    runtime = OpenAICompatibleRuntime(
        api_key="test-secret-key",
        base_url="https://api.example.com/v1",
        model="test-model",
        http_handler=mock_handler,
    )

    request = AgentRequest(
        role="triager",
        context={"title": "Bug: crash on checkout"},
    )
    result = runtime.run_agent(request)

    assert result.role == "triager"
    assert result.runtime_name == "openai/test-model"
    assert result.is_simulation is False
    assert result.structured_data["summary"] == "Live AI summary"
    assert result.structured_data["severity"] == "P1"

    # Verify authorization header and endpoint
    assert len(recorded_requests) == 1
    sent_req = recorded_requests[0]
    assert sent_req.full_url == "https://api.example.com/v1/chat/completions"
    assert sent_req.headers.get("Authorization") == "Bearer test-secret-key"


def test_openai_compatible_runtime_http_401():
    def mock_handler_401(req: urllib.request.Request, timeout: float) -> dict[str, Any]:
        raise urllib.error.HTTPError(
            url="https://api.openai.com/v1",
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,  # type: ignore[arg-type]
        )

    runtime = OpenAICompatibleRuntime(
        api_key="invalid-key",
        http_handler=mock_handler_401,
    )
    request = AgentRequest(role="triager", context={})

    with pytest.raises(AgentNetworkError, match="HTTP 401"):
        runtime.run_agent(request)


def test_openai_compatible_runtime_timeout():
    def mock_handler_timeout(
        req: urllib.request.Request, timeout: float
    ) -> dict[str, Any]:
        raise TimeoutError("Socket timed out")

    runtime = OpenAICompatibleRuntime(
        api_key="valid-key",
        timeout_seconds=5.0,
        http_handler=mock_handler_timeout,
    )
    request = AgentRequest(role="triager", context={})

    with pytest.raises(AgentNetworkError, match="timed out after 5.0s"):
        runtime.run_agent(request)


def test_openai_compatible_runtime_connection_error():
    def mock_handler_conn_err(
        req: urllib.request.Request, timeout: float
    ) -> dict[str, Any]:
        raise urllib.error.URLError("Connection refused")

    runtime = OpenAICompatibleRuntime(
        base_url="http://localhost:99999",
        http_handler=mock_handler_conn_err,
    )
    request = AgentRequest(role="triager", context={})

    with pytest.raises(AgentNetworkError, match="Connection error"):
        runtime.run_agent(request)


# -----------------------------------------------------------------------------
# 3. Fixer Safety Validation Tests
# -----------------------------------------------------------------------------


def test_validate_safe_worktree_file_valid(tmp_path: Path):
    wt_path = tmp_path / "worktree"
    wt_path.mkdir()

    target = validate_safe_worktree_file(wt_path, "src/shop/checkout.py")
    assert target == (wt_path / "src" / "shop" / "checkout.py").resolve()


def test_validate_safe_worktree_file_path_traversal(tmp_path: Path):
    wt_path = tmp_path / "worktree"
    wt_path.mkdir()

    with pytest.raises(FixerSafetyError, match="Path traversal detected"):
        validate_safe_worktree_file(wt_path, "../../etc/passwd")

    with pytest.raises(FixerSafetyError, match="Path traversal detected"):
        validate_safe_worktree_file(wt_path, "src/../../outside.py")


def test_validate_safe_worktree_file_symlink_escape(tmp_path: Path):
    wt_path = tmp_path / "worktree"
    wt_path.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    symlink_dir = wt_path / "leak_link"
    try:
        symlink_dir.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation requires elevated permissions on this OS")

    with pytest.raises(FixerSafetyError, match="escapes worktree boundary"):
        validate_safe_worktree_file(wt_path, "leak_link/secret.txt")


def test_validate_safe_worktree_file_absolute_path(tmp_path: Path):
    wt_path = tmp_path / "worktree"
    wt_path.mkdir()

    with pytest.raises(FixerSafetyError):
        validate_safe_worktree_file(wt_path, "/etc/shadow")


def test_validate_safe_worktree_file_protected_paths(tmp_path: Path):
    wt_path = tmp_path / "worktree"
    wt_path.mkdir()

    with pytest.raises(FixerSafetyError, match="Modification of protected directory"):
        validate_safe_worktree_file(wt_path, ".git/config")

    with pytest.raises(FixerSafetyError, match="Modification of protected directory"):
        validate_safe_worktree_file(wt_path, ".reporescue/secrets.json")

    with pytest.raises(FixerSafetyError, match="Modification of protected file"):
        validate_safe_worktree_file(wt_path, ".env")


# -----------------------------------------------------------------------------
# 4. Runtime Selection & Factory Tests
# -----------------------------------------------------------------------------


def test_runtime_factory_explicit_mock():
    runtime = AgentRuntimeFactory.get_runtime("mock")
    assert isinstance(runtime, MockAgentRuntime)
    assert runtime.runtime_name == "mock-deterministic"


def test_runtime_factory_explicit_real_missing_config(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    with pytest.raises(RuntimeConfigurationError, match="Real AI runtime requested"):
        AgentRuntimeFactory.get_runtime("real")


def test_runtime_factory_explicit_real_with_api_key():
    runtime = AgentRuntimeFactory.get_runtime(
        "real", api_key="test-key", model="gpt-4o"
    )
    assert isinstance(runtime, OpenAICompatibleRuntime)
    assert runtime.model == "gpt-4o"


def test_runtime_factory_auto_detect_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    runtime = AgentRuntimeFactory.get_runtime()
    assert isinstance(runtime, MockAgentRuntime)


def test_runtime_factory_auto_detect_with_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-mock-env-key")
    runtime = AgentRuntimeFactory.get_runtime()
    assert isinstance(runtime, OpenAICompatibleRuntime)


# -----------------------------------------------------------------------------
# 5. Mock Mode Regression Tests
# -----------------------------------------------------------------------------


def test_mock_agent_runtime_all_roles():
    runtime = MockAgentRuntime()

    for role in ["triager", "investigator", "reproducer", "fixer", "verifier"]:
        req = AgentRequest(role=role, context={"title": "Test Bug"})
        result = runtime.run_agent(req)
        assert result.role == role
        assert result.is_simulation is True
        assert isinstance(result.structured_data, dict)
        assert len(result.structured_data) > 0


# -----------------------------------------------------------------------------
# 6. Provider Selection Regression Tests
# -----------------------------------------------------------------------------


def test_runtime_factory_explicit_ollama_host_success(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://mock-ollama:11434")

    with patch(
        "reporescue.agent_runtime.check_ollama_alive", return_value=True
    ) as mock_alive:
        runtime = AgentRuntimeFactory.get_runtime("real")
        assert isinstance(runtime, OpenAICompatibleRuntime)
        assert runtime.base_url == "http://mock-ollama:11434/v1"
        assert "ollama" in runtime.runtime_name
        mock_alive.assert_called_once_with("http://mock-ollama:11434")


def test_runtime_factory_explicit_ollama_host_unreachable_real(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://mock-ollama:11434")

    with (
        patch("reporescue.agent_runtime.check_ollama_alive", return_value=False),
        pytest.raises(
            RuntimeConfigurationError, match="Ollama is explicitly configured"
        ),
    ):
        AgentRuntimeFactory.get_runtime("real")


def test_runtime_factory_explicit_ollama_host_unreachable_auto_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://mock-ollama:11434")

    with patch("reporescue.agent_runtime.check_ollama_alive", return_value=False):
        runtime = AgentRuntimeFactory.get_runtime()
        assert isinstance(runtime, MockAgentRuntime)


def test_runtime_factory_explicit_ollama_model(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "mistral:latest")

    with patch("reporescue.agent_runtime.check_ollama_alive", return_value=True):
        runtime = AgentRuntimeFactory.get_runtime()
        assert isinstance(runtime, OpenAICompatibleRuntime)
        assert runtime.model == "mistral:latest"
        assert runtime.runtime_name == "ollama/mistral:latest"


def test_runtime_factory_real_without_config_never_silently_mocks(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    with patch(
        "reporescue.agent_runtime.check_ollama_alive", return_value=True
    ) as mock_alive:
        with pytest.raises(
            RuntimeConfigurationError, match="Real AI runtime requested"
        ):
            AgentRuntimeFactory.get_runtime("real")
        mock_alive.assert_not_called()
