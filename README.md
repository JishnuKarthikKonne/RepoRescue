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
```

## Current status
Starter scaffold only. Switch room/agent integration and Git execution are not yet implemented.
