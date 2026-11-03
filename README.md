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

## Core Modules

### 1. Git Worktree Isolation (`reporescue.git_ops`)
Safely creates, inspects, and cleans up isolated worktrees without ever touching the main working tree:
```python
from reporescue.git_ops import WorktreeManager

# Manage isolated worktrees strictly within a designated directory (.reporescue/worktrees/)
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

### 2. Isolated Test Execution Runner (`reporescue.runner`)
Executes subprocess commands without `shell=True`, capturing duration, stdout, stderr, timeout, and truncation:
```python
from reporescue.runner import run_command, run_pytest

# Run pytest on an isolated worktree
result = run_pytest(cwd=wt.path, timeout_seconds=30.0)
print(f"Tests passed: {result.passed}, Exit Code: {result.exit_code}")
print(f"Execution Output:\n{result.stdout}")
```

## Current status
- State machine & data models implemented.
- Git worktree isolation and secure runner implemented.
- Switch room/agent integration and multi-agent pipeline are in progress.
