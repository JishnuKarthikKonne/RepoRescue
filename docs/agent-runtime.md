# RepoRescue Agent Runtime Architecture

This document details the provider-neutral agent runtime architecture for RepoRescue, covering execution protocols, prompt integration, simulation modes, and real LLM provider connectivity.

---

## 1. Provider-Neutral Protocol Design

RepoRescue decouples agent logic from specific AI models and vendors via `AgentRuntimeProtocol` in `reporescue.agent_runtime`:

```python
class AgentRuntimeProtocol(Protocol):
    """Protocol for provider-neutral agent runtime execution."""

    def invoke(self, request: AgentRequest) -> AgentResult:
        """Executes an agent task synchronously and returns structured output."""
        ...
```

### Request & Result Data Contracts

```python
@dataclass
class AgentRequest:
    agent_name: (
        str  # e.g., "triager", "investigator", "reproducer", "fixer", "verifier"
    )
    prompt: str  # System instructions loaded from agents/<name>.md
    user_context: str  # Bug title, description, code snippets, triage results
    repository_context: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    agent_name: str
    success: bool
    output_text: str
    structured_data: dict[str, Any] = field(default_factory=dict)
    raw_response: Any = None
    execution_time_seconds: float = 0.0
    error: str | None = None
    is_simulation: bool = False
    runtime_name: str = "mock-deterministic"
```

---

## 2. Agent Prompts

Role prompts are located in the `agents/` directory:
- `agents/triager.md`: Classifies severity, identifies affected components, and defines acceptance criteria.
- `agents/investigator.md`: Analyzes codebase structure, diagnoses root cause, and outlines reproduction strategies.
- `agents/reproducer.md`: Designs minimal reproducer tests demonstrating failure before patching.
- `agents/fixer.md`: Formulates minimal, surgical fixes avoiding collateral side effects.
- `agents/verifier.md`: Inspects test runner output, verifies regression freedom, and formats approval evidence.

Prompts are dynamically loaded at runtime using `load_agent_prompt(role_name)` with graceful fallbacks.

---

## 3. Runtimes Available

### A. Deterministic Mock Runtime (`MockAgentRuntime`)
- **Runtime Name**: `mock-deterministic`
- **Simulation Flag**: `is_simulation = True`
- **Purpose**: Offline development, unit testing, CI pipelines, and reliable hackathon demonstrations without live LLM costs or network dependencies.
- **Behavior**: Returns validated, reproducible JSON payloads matching the seeded checkout coupon bug scenario in `demo_repo/`.

### B. Factory Creation
```python
from reporescue.agent_runtime import AgentRuntimeFactory

# Explicit mock runtime
runtime = AgentRuntimeFactory.create("mock")

# Auto-detection (checks environment variables for OpenAI, Anthropic, or Gemini)
runtime = AgentRuntimeFactory.create()
```

---

## 4. Connecting Real LLM Providers

To connect a live provider, implement `AgentRuntimeProtocol` or extend `AgentRuntimeFactory`:

### OpenAI / Compatible Endpoint
```python
import os
import json
from openai import OpenAI
from reporescue.agent_runtime import AgentRequest, AgentResult


class OpenAIAgentRuntime:
    def __init__(self, model: str = "gpt-4o"):
        self.client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.model = model

    def invoke(self, request: AgentRequest) -> AgentResult:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": request.prompt},
                {"role": "user", "content": request.user_context},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
        return AgentResult(
            agent_name=request.agent_name,
            success=True,
            output_text=content,
            structured_data=data,
            is_simulation=False,
            runtime_name=f"openai/{self.model}",
        )
```

---

## 5. Security & Safety Boundaries

1. **No Direct Shell Execution by LLMs**: Agents never construct or execute raw shell strings. Code generation produces file modifications and test definitions that are safely written to isolated worktrees and invoked through `runner.run_command`.
2. **Credential Sanitization**: Agent prompts and context never ingest raw API keys, Switch tokens, or system environment secrets.
3. **Structured Validation**: All agent responses are parsed into strongly typed dataclasses (`TriageResult`, `InvestigationResult`, `ReproductionResult`, `FixResult`, `VerificationResult`). Invalid schemas fail the stage immediately before execution.
