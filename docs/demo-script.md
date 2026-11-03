# RepoRescue 3-Minute Hackathon Demo Script

This script provides an exact, step-by-step walkthrough for demonstrating RepoRescue during the Flip the Switch Hackathon 2026.

---

## Demo Overview
- **Scenario**: E-commerce shopping service (`demo_repo`) where applying an expired coupon code causes an unhandled `ValueError` (HTTP 500 in production).
- **Goal**: Show an autonomous multi-agent engineering workflow inside a Switch room moving from bug report to verified fix with isolated worktrees and mandatory human approval.
- **Duration**: ~3 minutes.

---

## Pre-Demo Checklist
1. Ensure the Python 3.12 virtual environment is activated:
   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```
2. Verify all tests pass:
   ```powershell
   pytest
   ```
3. Run the demonstration:
   ```powershell
   # Mode A: Live Switch Core Integration (Live HTTP REST + Switch Room)
   python -m reporescue.cli --live-demo --runtime mock
   # Or with real local GPU AI:
   python -m reporescue.cli --live-demo --real

   # Mode B: Offline Deterministic Simulation
   python -m reporescue.cli --mock --demo
   ```

---

## 3-Minute Presentation Walkthrough

### 0:00 - 0:30 | The Problem & Setup
- **Presenter**: "Developers and on-call engineers spend hours triaging reports, writing reproduction tests, and testing bug fixes. RepoRescue is an autonomous AI teammate embedded in our Switch rooms that does the heavy lifting while keeping humans strictly in control."
- **Visual**: Show the Switch room `#reporescue` or terminal demo runner.
- **Action**: Launch the RepoRescue demo:
  ```powershell
  python -m reporescue.cli --mock --demo
  ```

### 0:30 - 1:00 | Bug Report & Triage
- **Inbound Message**: Developer reports:
  > `"Bug: Checkout returns HTTP 500 when coupon is applied. Invoking calculate_order_total with coupon_code='EXPIRED' crashes with ValueError."`
- **RepoRescue Reaction**:
  - Automatically acknowledges receipt (`Bug accepted`).
  - **Triager Agent** classifies the bug:
    - **Severity**: `HIGH`
    - **Components**: `shop.checkout`
    - **Acceptance Criteria**: Gracefully handle expired or invalid coupon codes without crashing.

### 1:00 - 1:45 | Autonomous Investigation & Reproduction
- **Investigator Agent**:
  - Pinpoints line 31 in `src/shop/checkout.py`.
  - Explains root cause: `valid_coupons[coupon_code]` triggers `KeyError` when code is not in dictionary.
- **Reproducer Agent**:
  - Automatically creates a temporary worktree `reporescue/task-xxxxxx`.
  - Generates minimal reproducer test `test_reproduce_expired_coupon_500`.
  - Executes `pytest` in the isolated worktree via the safe runner.
  - **Evidence Published**: Confirms reproduction failure with exit code 1 and captured traceback.

### 1:45 - 2:15 | Isolated Fix & Independent Verification
- **Fixer Agent**:
  - Applies a surgical patch to `src/shop/checkout.py`:
    ```diff
    - discount = valid_coupons[coupon_code]
    + discount = valid_coupons.get(coupon_code, 0.0)
    ```
  - Isolates the commit on branch `reporescue/task-xxxxxx`.
  - Main working tree remains 100% clean.
- **Verifier Agent**:
  - Re-runs the test suite in the isolated worktree.
  - **Evidence Published**:
    - Tests passed: 2
    - Tests failed: 0
    - Verification: `COMPLETE ✅`

### 2:15 - 3:00 | Human Approval Gate & Secure GitHub Draft PR
- **RepoRescue Message**:
  > `[IMPORTANT] Human Approval Required`
  > `Reply: /approve or /reject <reason>`
- **Safety Enforcement**:
  - Bot approvals are rejected by identity verification rules.
  - Patch fingerprint (`sha256:...`) cryptographically seals worktree diff.
- **Action**: Lead Developer replies:
  > `/approve`
- **RepoRescue Safety Validation**:
  - Validates all 10 Strict Safety Gates (A-J).
  - Pushes dedicated branch `reporescue/task-xxxxxx` (no force push).
  - Creates a GitHub **DRAFT** Pull Request (`draft=True`).
- **Final Card in Switch Room**:
  > `🚀 RepoRescue | Draft Pull Request Created`
  > `Draft PR: #1 (https://github.com/switch-hackathon/RepoRescue/pull/1)`
  > `Branch: reporescue/task-xxxxxx`
  > `State: DRAFT (Awaiting human code review)`
  > `Approved By: @lead_dev`
  > `Zero Auto-Merge / Zero Auto-Deploy Guarantee: This PR was created in DRAFT mode and cannot be merged automatically.`
- **Presenter**: "The fix is reproduced, surgically patched, independently verified with pytest, human-approved in Switch, and published as a GitHub Draft PR without ever modifying main or risking auto-deployments. That is RepoRescue."

---

## Offline Fallback Notice
When live Switch servers or external AI API keys are unavailable, `--mock --demo` provides a 100% deterministic, offline execution with genuine subprocess pytest runs, real Git worktree operations, and offline mock Draft PR creation.
