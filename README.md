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

### 1. Switch Room & Event Bridge (`reporescue.switch`)
Connects to the Switch Agent Bridge (Switch Core 0.31.0+), parses room events, and publishes workflow updates:
```python
from reporescue.switch import SwitchClient, RepoRescueDispatcher, SwitchConfig

# Load credentials from .env or pass explicitly
client = SwitchClient(SwitchConfig.from_env())
dispatcher = RepoRescueDispatcher(client)

# Ingest an incoming bug report event from the Switch room
task = dispatcher.handle_message_event(event)

# Publish progress updates as agents complete stages
dispatcher.publish_triage(task.task_id, room_id, severity="P1", acceptance_criteria="...")
dispatcher.publish_reproduction(task.task_id, room_id, test_file="tests/test_bug.py", exit_code=1, output_snippet="...")
dispatcher.publish_fix_proposed(task.task_id, room_id, branch_name="reporescue/issue-1", diff_snippet="...", rationale="...")
dispatcher.publish_verification(task.task_id, room_id, passed_count=5, failed_count=0, duration_s=0.8, evidence_text="...")

# Human in the Switch room responds with `/approve`
# Dispatcher verifies the human approval gate before advancing to APPROVED
```

### 2. Git Worktree Isolation (`reporescue.git_ops`)
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

### 3. Isolated Test Execution Runner (`reporescue.runner`)
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
2. In the connected Mattermost/Switch channel (e.g. `#reporescue`), post a bug report:
   ```text
   Bug: Checkout returns HTTP 500 when coupon code is expired
   Steps to reproduce: POST /checkout with code='EXPIRED'
   ```
3. RepoRescue receives the event, posts an acknowledgment with the Task ID, and runs through triage, reproduction, patch generation, and verification.
4. When the verification card appears, review the diff and reply in the room:
   ```text
   /approve
   ```
5. RepoRescue verifies the human approval gate and transitions the task to `approved`.
