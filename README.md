# RepoRescue

Collaborative AI bug triage, reproduction, fixing, verification, and human approval through Switch.

RepoRescue embeds an autonomous multi-agent engineering workflow directly inside Switch and Mattermost rooms. Developers report bugs in chat; RepoRescue triages the issue, investigates the codebase, reproduces the failure in an isolated Git worktree, applies a surgical patch, runs verification tests, and requests human sign-off.

> **Safety Invariant**: RepoRescue never automatically merges, pushes to main, or deploys code. All patches are strictly isolated in Git worktrees and require verified human approval.

---

## Autonomous Workflow Stages

```text
[Switch Room Bug Report]
         │
         ▼
 1. TRIAGER       ──> Classifies severity, acceptance criteria, affected modules
         │
         ▼
 2. INVESTIGATOR  ──> Analyzes codebase, diagnoses root cause, plans reproduction
         │
         ▼
 3. REPRODUCER    ──> Creates isolated worktree, writes minimal test, confirms failure
         │
         ▼
 4. FIXER         ──> Applies minimal surgical patch, isolates commit on branch
         │
         ▼
 5. VERIFIER      ──> Executes independent pytest suite, captures genuine evidence
         │
         ▼
 6. APPROVAL GATE ──> Pauses workflow, rejects bots, requires verified human /approve
```

---

## Local Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[dev]"
pytest
ruff check .
```

---

## Running the Application

### 1. Interactive Demo Simulation (Recommended for Evaluation)
Runs the complete autonomous multi-agent pipeline on a seeded bug in `demo_repo/`, executing real worktree operations and real pytest subprocess commands:
```powershell
# Run with deterministic mock simulation (default for offline demos)
python -m reporescue.cli --mock --demo

# Run with real AI provider (requires OPENAI_API_KEY or LLM_BASE_URL)
python -m reporescue.cli --mock --demo --real
```

### 2. Offline Mock Listener
Starts the RepoRescue event loop using the offline mock Switch adapter:
```powershell
python -m reporescue.cli --mock
```

### 3. Live Switch Room Listener
Starts listening for inbound bug reports in the configured live Switch/Mattermost room:
```powershell
python -m reporescue.cli --listen
# Or specify room override:
python -m reporescue.cli --listen --room-id "!reporescue:localhost"
# Use real AI agent runtime:
python -m reporescue.cli --listen --real
```

---

## Switch Room Setup & Configuration

1. Copy `.env.example` to `.env`:
   ```powershell
   Copy-Item .env.example .env
   ```
2. Configure Switch environment variables:
   - `SWITCH_SERVER_URL`: Local Switch API server (default: `http://localhost:65008`).
   - `SWITCH_AGENT_ID`: Unique Switch agent identifier (default: `reporescue`).
   - `SWITCH_API_KEY`: Agent bearer token provisioned by Switch Console.
   - `SWITCH_ROOM_ID`: The target room/channel ID (e.g., Mattermost `#reporescue`).

---

## Core Architecture & Safety Guarantees

### 1. Multi-Agent Orchestrator (`reporescue.orchestrator`)
- Coordinates the 5 specialized agent stages in strict sequence.
- Ensures preceding stage prerequisites are met before advancing.
- Emits real-time progress cards into the Switch room at each transition.
- Halts at `AWAITING_APPROVAL` until a verified human issues `/approve`.

### 2. Provider-Neutral Agent Runtime (`reporescue.agent_runtime`)
- Decouples workflow logic from AI vendors via `AgentRuntimeProtocol`.
- **Deterministic Mock Runtime** (`MockAgentRuntime`, `is_simulation=True`): For reliable offline development, automated tests, and reproducible demos.
- **Real AI Provider Runtime** (`OpenAICompatibleRuntime`, `is_simulation=False`): Connects to OpenAI API, local/remote Ollama, LiteLLM, vLLM, or Azure endpoints via standard `urllib.request`.
- Injects strict JSON schema instructions, validates typed structured outputs, and catches network timeouts.
- Role-specific system instructions loaded directly from `agents/*.md`.

### 3. Git Worktree Isolation & Fixer Safety (`reporescue.git_ops`, `reporescue.orchestrator`)
- **Pristine Main Working Tree**: Never touches the main repository working tree.
- **Dedicated Worktrees**: All isolated worktrees are located under `.reporescue/worktrees/` and automatically excluded in `.git/info/exclude`.
- **Fixer Safety Guard**: Rejects directory traversals (`../`), absolute paths, and modifications to protected targets (`.git`, `.env`, `.reporescue`).
- **Data Loss Prevention**: Worktrees containing uncommitted modifications cannot be removed without explicit confirmation.

### 4. Safe Command Execution Engine (`reporescue.runner`)
- Subprocess execution without shell interpretation (`shell=False`).
- Configurable execution timeouts (default: 30.0s).
- Stdout and stderr buffering with truncation limits to prevent memory overflow.
- Captures genuine exit codes and timings as immutable verification evidence.

### 5. Switch Bridge & Anti-Bot Human Approval Gate (`reporescue.switch`)
- Connects to the Switch Agent Bridge (Switch Core 0.31.0+).
- Ingests and normalizes `SwitchMessageEvent` with LRU event deduplication.
- Exponential backoff reconnection handling for resilient live operations.
- **Strict Human Approval Enforcement**:
  - Rejects approval attempts originating from automated agents, bots, or unverified senders.
  - Immutably records the human approver's identity and timestamp.
  - Rejects approval if the task is not in `awaiting_approval`.

---

## Standalone Demo Repository (`demo_repo/`)

The repository includes a standalone Python package (`demo_repo/`) containing a seeded bug:
- **Scenario**: E-commerce shop checkout crashes with `ValueError` when an expired or invalid coupon code is applied.
- **Location**: `demo_repo/src/shop/checkout.py` and `demo_repo/tests/test_checkout.py`.
- **Initialization Helper**: `reporescue.demo.init_demo_repository(target_path)` creates an ephemeral Git repository for end-to-end testing without affecting project source code.

---

## Documentation

- [Architecture Blueprint](docs/architecture.md): Detailed system diagram, stage contracts, and safety boundaries.
- [Agent Runtime Guide](docs/agent-runtime.md): Provider protocol, prompt integration, and live LLM configuration.
- [3-Minute Demo Script](docs/demo-script.md): Step-by-step presentation script for the hackathon.
