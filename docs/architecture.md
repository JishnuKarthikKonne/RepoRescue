# RepoRescue Architecture

RepoRescue is an autonomous multi-agent engineering workflow designed for collaborative bug fixing inside Switch rooms. Developers report issues in chat; RepoRescue triages, investigates, reproduces, patches, verifies, and requests human approval.

---

## 1. System Architecture

```mermaid
flowchart TD
    subgraph Human["Developer Workspace"]
        Dev["Developer (Chat)"]
    end

    subgraph SwitchPlatform["Switch Agent Platform"]
        Room["Switch Room / Channel"]
        Core["Switch Core (Agent Protocol / Matrix)"]
    end

    subgraph RepoRescueRuntime["RepoRescue Orchestration Service"]
        Listener["SwitchListener (Deduplication & Polling)"]
        Dispatcher["RepoRescueDispatcher (State Machine)"]
        Orchestrator["WorkflowOrchestrator (Sequential Pipeline)"]
        Runtime["AgentRuntime (Provider-Neutral / Mock)"]
    end

    subgraph ExecutionLayer["Safe Execution & Isolation Layer"]
        WorktreeMgr["WorktreeManager (Git Worktrees)"]
        TestRunner["Safe Test Runner (subprocess, timeout, truncation)"]
    end

    Dev -->|Report Bug / Approve| Room
    Room --> Core
    Core -->|Agent Events| Listener
    Listener --> Dispatcher
    Dispatcher --> Orchestrator
    Orchestrator --> Runtime
    Orchestrator --> WorktreeMgr
    Orchestrator --> TestRunner
    Dispatcher -->|Publish Formatted Cards| Room
```

---

## 2. Multi-Agent Pipeline

RepoRescue sequences specialized autonomous stages. Each stage consumes structured context from previous stages and returns validated typed output with genuine execution evidence.

```mermaid
flowchart LR
    Report["Bug Report"] --> Triager["1. Triager"]
    Triager --> Investigator["2. Investigator"]
    Investigator --> Reproducer["3. Reproducer"]
    Reproducer --> Fixer["4. Fixer"]
    Fixer --> Verifier["5. Verifier"]
    Verifier --> ApprovalGate{"6. Human Approval Gate"}
    ApprovalGate -->|/approve (Verified Human)| Approved["Task APPROVED"]
    ApprovalGate -->|/reject (Human)| Rejected["Task REJECTED"]
    Approved --> SafetyGates{"10 Safety Gates Check"}
    SafetyGates -->|Gates Passed| DraftPR["7. GitHub Draft PR (draft=True)"]
    SafetyGates -->|Tamper Detected| Invalidated["Approval Invalidated"]
```

### Stage Responsibilities & Artifacts

| Stage | Input | Agent Action | Primary Evidence / Output |
| :--- | :--- | :--- | :--- |
| **Triager** | Raw bug report, repository context | Classifies severity, defines acceptance criteria, identifies components | `TriageResult` (severity, acceptance criteria) |
| **Investigator** | Bug report, triage summary, codebase files | Inspects code paths, diagnoses root cause, defines reproduction strategy | `InvestigationResult` (root cause, affected files, evidence) |
| **Reproducer** | Investigation, isolated worktree | Generates minimal reproducer test file (`test_reproduce_*.py`), runs pytest | `ReproductionResult` (failing test exit code 1, test output) |
| **Fixer** | Root cause, reproduction test, worktree branch | Implements minimal surgical fix in isolated branch, runs `git diff` | `FixResult` (patch diff, files changed, rationale) |
| **Verifier** | Patched worktree, full test suite | Executes pytest via safe test runner, validates 0 failures | `VerificationResult` (passed count, failed count, exit code 0) |
| **Approval Gate**| Verification evidence, diff card | Pauses in `AWAITING_APPROVAL`. Enforces verified human identity and patch binding | `TaskStatus.APPROVED` or `TaskStatus.REJECTED` |
| **Pull Request** | Approved task, verified worktree branch | Validates 10 Safety Gates, pushes dedicated branch, opens DRAFT PR | `TaskStatus.PR_CREATED` (Draft PR URL, PR card) |

---

## 3. Worktree Isolation Invariants

All automated code modifications and reproducer tests are strictly segregated from the developer's main branch:

1. **Pristine Main Working Tree**: RepoRescue never creates branches or modifies files in the repository's root working tree.
2. **Dedicated Worktree Directory**: All worktrees reside within `.reporescue/worktrees/` (or a configured directory).
3. **Repository Exclusion**: RepoRescue automatically registers `.reporescue/` in `.git/info/exclude` to guarantee `git status` on the main tree remains clean.
4. **Destructive Operation Protection**: Worktrees with uncommitted changes cannot be removed without explicit `force=True` confirmation.
5. **Path Boundary Validation**: Worktree paths must strictly resolve inside `worktrees_dir` and are prevented from targeting root or parent directories.

---

## 4. Safe Command Execution Engine (`runner.py`)

Execution of pytest and Git commands is protected by deterministic safety boundaries:

- **No Shell Execution**: All commands are invoked directly as argument vectors (`shell=False`) to eliminate shell injection vulnerabilities.
- **Configurable Timeouts**: Every subprocess execution enforces a timeout (default: 30.0 seconds).
- **Output Buffering & Truncation**: Standard output and error streams are captured and capped (default: 4000 characters) to prevent memory exhaustion or oversized Switch messages.
- **Timing & Exit Code Tracking**: Exact duration (seconds) and integer exit codes are recorded as immutable workflow evidence.

---

## 5. Switch Integration & Human-in-the-Loop Gate

Switch serves as the central communication surface between human developers and RepoRescue:

- **Official Protocol Compatibility**: Ingests `SwitchMessageEvent` structures compatible with Switch Core 0.31.0 Agent Protocol.
- **Event Deduplication**: Inbound message IDs are tracked in an LRU deduplication cache to prevent re-processing events across network retries.
- **Exponential Reconnection**: Network disconnects trigger progressive backoff up to 30 seconds before reconnecting.
- **Anti-Bot Approval Verification**:
  - Approvals (`/approve`) from senders with `sender_kind == "agent"`, `sender_kind == "bot"`, or bot-like identifiers are rejected.
  - Only verified human senders can advance a task past `AWAITING_APPROVAL`.
  - The approver's handle and an ISO UTC timestamp are immutably stored in the task record.
- **Zero Auto-Merge / Auto-Deploy**: Pull request creation, merging, and deployment remain strictly under human control.

---

## 6. Secure GitHub Draft Pull Request Integration (`github/`)

Following human approval in Switch, RepoRescue safely packages the verified fix into a GitHub Draft Pull Request:

1. **Strictly Draft Mode**: All PRs are created with `draft: True`. They cannot be merged until human maintainers inspect and mark them ready on GitHub.
2. **Cryptographic SHA-256 Patch Binding**: Human approval cryptographically seals the verified git diff. Modifying worktree files post-approval triggers tamper detection, immediately revoking approval and transitioning the task back to `fix_proposed`.
3. **The 10 Strict Safety Gates (A-J)**: Must pass prior to any Git push or PR creation:
   - *Gate A*: Task status is `approved`.
   - *Gate B*: Approver is a verified human (not an agent/bot).
   - *Gate C*: Real verified patch record exists.
   - *Gate D*: Independent verification test suite succeeded (exit code 0).
   - *Gate E*: Current worktree diff matches approved cryptographic SHA-256 fingerprint.
   - *Gate F*: Branch strictly matches dedicated `reporescue/task-<task-id>` format.
   - *Gate G*: No forbidden paths or secrets in diff or commits.
   - *Gate H*: Target repository matches configured target and repository allowlist.
   - *Gate I*: Worktree is completely clean with zero uncommitted changes.
   - *Gate J*: Remote repository and base branch are reachable.
4. **Complete Audit Trail**: Pull Request body includes full investigation evidence, reproduction logs, surgical patch diff, verification test duration, human approver identity, and patch fingerprint.
5. **No Force Pushes**: Pushes use `git push -u origin <branch>` without `--force` to protect remote Git history.
6. **Detailed Documentation**: See [docs/github-integration.md](github-integration.md) for full token permissions, environment variables, and protocol specifications.
