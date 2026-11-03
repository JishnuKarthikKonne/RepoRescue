# RepoRescue Agent Runtime Architecture

This document details the provider-neutral agent runtime architecture for RepoRescue, covering execution protocols, prompt integration, simulation modes, and real LLM provider connectivity.

---

## 1. Provider-Neutral Protocol Design

RepoRescue decouples agent workflow logic from specific AI models and vendors via `AgentRuntimeProtocol` in `reporescue.agent_runtime`:

```python
class AgentRuntimeProtocol(Protocol):
    """Protocol for provider-neutral agent runtime execution."""

    def run_agent(self, request: AgentRequest) -> AgentResult:
        """Executes an agent task synchronously and returns structured output."""
        ...
```

### Request & Result Data Contracts

```python
@dataclass(frozen=True)
class AgentRequest:
    role: str  # "triager", "investigator", "reproducer", "fixer", "verifier"
    context: dict[str, Any]
    prompt_instruction: str = ""


@dataclass(frozen=True)
class AgentResult:
    role: str
    runtime_name: str
    structured_data: dict[str, Any]
    raw_text: str
    execution_duration: float
    is_simulation: bool = False
```

---

## 2. Agent Prompts & Schema Injection

Role instructions are located in the `agents/` directory:
- `agents/triager.md`: Classifies severity, identifies affected components, and defines acceptance criteria.
- `agents/investigator.md`: Analyzes codebase structure, diagnoses root cause, and outlines reproduction strategies.
- `agents/reproducer.md`: Designs minimal reproducer tests demonstrating failure before patching.
- `agents/fixer.md`: Formulates minimal, surgical fixes avoiding collateral side effects.
- `agents/verifier.md`: Inspects test runner output, verifies regression freedom, and formats approval evidence.

Prompts are dynamically loaded at runtime using `load_agent_prompt(role_name)`. For real LLM providers, strict JSON schema instructions are appended to enforce structured response parsing.

---

## 3. Runtimes Available

### A. Deterministic Mock Runtime (`MockAgentRuntime`)
- **Runtime Name**: `mock-deterministic`
- **Simulation Flag**: `is_simulation = True`
- **Purpose**: Offline development, unit testing, CI pipelines, and reproducible hackathon demonstrations without live LLM costs or network dependencies.
- **Behavior**: Returns validated, reproducible JSON payloads matching the seeded checkout coupon bug scenario in `demo_repo/`.

### B. Real AI Provider Runtime (`OpenAICompatibleRuntime`)
- **Runtime Name**: `openai/<model>` (e.g. `openai/gpt-4o-mini`)
- **Simulation Flag**: `is_simulation = False`
- **Supported Providers**:
  - OpenAI API (`https://api.openai.com/v1`)
  - Local or remote Ollama (`http://localhost:11434/v1`)
  - LiteLLM / vLLM / OpenRouter / Azure OpenAI / Groq
- **Implementation**: Built entirely with Python's standard library `urllib.request` (zero third-party dependency overhead).
- **Features**:
  - Injects role-specific JSON schema contracts into model system prompts.
  - Robust JSON extraction from raw strings, markdown code blocks, or substring braces.
  - Validates all responses against typed schema rules before passing to the orchestrator.
  - Enforces configurable HTTP timeouts (default: 30.0s).

---

## 4. Factory Configuration & Usage

Use `AgentRuntimeFactory.get_runtime()` to instantiate the appropriate adapter:

```python
from reporescue.agent_runtime import AgentRuntimeFactory

# 1. Force deterministic mock mode (for offline tests/demos)
mock_runtime = AgentRuntimeFactory.get_runtime("mock")

# 2. Force real AI provider (raises RuntimeConfigurationError if unconfigured)
real_runtime = AgentRuntimeFactory.get_runtime("real")

# 3. Auto-detection (uses real provider if OPENAI_API_KEY/LLM_BASE_URL is present, else mock)
runtime = AgentRuntimeFactory.get_runtime()
```

### CLI Execution
```powershell
# Run with deterministic mock simulation (default for demos)
python -m reporescue.cli --mock --demo

# Run with real AI provider (requires OPENAI_API_KEY or LLM_BASE_URL)
python -m reporescue.cli --listen --real
# Or:
python -m reporescue.cli --mock --demo --real
```

---

## 5. Fixer Safety Invariants

AI-generated patches undergo rigorous containment validation via `validate_safe_worktree_file` before application:

1. **Worktree Confinement**: All target files must strictly resolve inside the isolated worktree directory (`wt.path`). Absolute paths or directory traversals (`../`) are immediately rejected with `FixerSafetyError`.
2. **Protected Target Shielding**: AI patches cannot modify `.git/`, `.reporescue/`, `.venv/`, `.env`, or build cache directories.
3. **No Shell Execution by Models**: AI output never constructs or executes shell commands. Only predetermined test runners (`run_pytest`) and diff inspections (`git diff`) are executed.
4. **Independent Verification**: The orchestrator never trusts model claims regarding test results. An independent execution of `run_pytest` must exit with code 0, 0 failures, and at least 1 passing test before the task can advance to human approval.
