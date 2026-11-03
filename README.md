# RepoRescue
Collaborative AI bug triage, reproduction, fixing, verification, and human approval through Switch.

## MVP flow
1. A developer reports a bug in a Switch room.
2. Triage agent classifies and assigns it.
3. Reproducer agent creates a minimal failing test.
4. Fix agent proposes a patch on an isolated branch.
5. Verifier runs tests and reports evidence.
6. Human reviews and approves before a PR is opened/merged.

> Never auto-merge or deploy. Human approval is mandatory.

## Local setup
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[dev]"
pytest
ruff check .
```

## Running the Application

### 1. Live Switch Room Listener
Starts listening for inbound bug reports in the configured Switch/Mattermost room:
```powershell
python -m reporescue.cli --listen
# Or specify room override:
python -m reporescue.cli --listen --room-id "!reporescue:localhost"
```

### 2. Offline Mock Mode (No Switch Server Required)
For offline local development and demonstration:
```powershell
python -m reporescue.cli --mock
```

### 3. Interactive Demo Simulation
Runs the mock listener and automatically injects a sample bug report:
```powershell
python -m reporescue.cli --mock --demo
```

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

## Core Modules

### 1. Switch Room Listener & Bridge (`reporescue.switch`)
Connects to the Switch Agent Bridge (Switch Core 0.31.0+), deduplicates events, and publishes workflow updates:
```python
from reporescue.switch import (
    SwitchClient,
    RepoRescueDispatcher,
    SwitchListener,
    SwitchConfig,
)

# Initialize client and listener
client = SwitchClient(SwitchConfig.from_env())
dispatcher = RepoRescueDispatcher(client)
listener = SwitchListener(client, dispatcher, room_id="!reporescue:localhost")

# Run listener loop (handles exponential backoff and graceful shutdown)
listener.run_loop()
```

### 2. Human Approval Hardening
Human approval is a hard security gate. RepoRescue enforces:
- **Verified Human Identity**: Automated agents, bots, or unverified automated messages cannot grant approval. Senders with `sender_kind == "agent"`, `sender_kind == "bot"`, or bot-like identifiers are rejected.
- **Timestamp & Attribution**: When a verified human sends `/approve`, the approver's name and ISO timestamp are immutably recorded in `task.approval_by` and `task.approved_at`.
- **State Invariant**: Approvals submitted when a task is not in `awaiting_approval` (e.g. before verification passes) are rejected.

### 3. Git Worktree Isolation (`reporescue.git_ops`)
Safely creates, inspects, and cleans up isolated worktrees without ever touching the main working tree:
```python
from reporescue.git_ops import WorktreeManager

mgr = WorktreeManager(repo_path=".")

# Create an isolated worktree for an issue
wt = mgr.create_worktree("reporescue/issue-42")
print(f"Isolated worktree created at: {wt.path} on branch {wt.branch}")

# Check for uncommitted modifications
if not mgr.has_uncommitted_changes(wt.path):
    mgr.remove_worktree(wt.path)
else:
    # Requires explicit confirmation / force to prevent data loss
    mgr.remove_worktree(wt.path, force=True)
```

### 4. Isolated Test Execution Runner (`reporescue.runner`)
Executes subprocess commands without `shell=True`, capturing duration, stdout, stderr, timeout, and truncation:
```python
from reporescue.runner import run_command, run_pytest

# Run pytest on an isolated worktree
result = run_pytest(cwd=wt.path, timeout_seconds=30.0)
print(f"Tests passed: {result.passed}, Exit Code: {result.exit_code}")
print(f"Execution Output:\n{result.stdout}")
```

## Testing in a Switch Room

1. Ensure the Switch Console is active (`http://localhost:65008`).
2. Start the listener:
   ```powershell
   python -m reporescue.cli --listen
   ```
3. In the connected Mattermost/Switch channel (e.g. `#reporescue`), post a bug report:
   ```text
   Bug: Checkout returns HTTP 500 when coupon is expired
   Steps to reproduce: POST /checkout with code='EXPIRED'
   ```
4. Observe RepoRescue post automated progress cards (Triage, Reproduction, Fix Proposal, Verification).
5. When the verification card requests approval, reply in the room:
   ```text
   /approve
   ```
6. RepoRescue verifies the human approval gate and transitions the task to `approved`.
