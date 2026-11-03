# RepoRescue Switch Integration Guide

This guide documents the architecture, protocol endpoints, authentication model, and live demonstration workflow for connecting RepoRescue with **Switch Core 0.31.0**.

---

## 1. Switch Architecture Overview

RepoRescue operates as a **Switch-connected autonomous engineering teammate**. It interacts with the Switch ecosystem through official HTTP REST APIs exposed by Switch Core:

```mermaid
flowchart TD
    subgraph HumanClient["Switch Gateway / Client"]
        User["Human Developer (Chat / Console)"]
        GatewayAuth["POST /gateway/auth/login"]
        MintKey["POST /gateway/api-keys (mint registration token)"]
        CreateChat["POST /gateway/chats"]
        SendMsg["POST /gateway/chats/{roomId}/messages"]
    end

    subgraph SwitchCore["Switch Core (Port 65008)"]
        AgentBridge["Agent Bridge API (/agents)"]
        ModerationRooms["Moderation & Chat API"]
        EventBus["Long-Polling Event Bus (/events)"]
        Database[("PostgreSQL 16 (Port 65010)")]
    end

    subgraph RepoRescueAgent["RepoRescue Service"]
        Client["SwitchClient (HTTP Bearer Auth)"]
        Listener["SwitchListener (Deduplication & Polling)"]
        Dispatcher["RepoRescueDispatcher (State Machine)"]
        Orchestrator["WorkflowOrchestrator (Multi-Agent Engine)"]
    end

    User --> GatewayAuth
    GatewayAuth --> MintKey
    MintKey --> CreateChat
    CreateChat --> SendMsg
    SendMsg --> EventBus

    RepoRescueAgent -->|Register: POST /agents| AgentBridge
    RepoRescueAgent -->|Poll: GET /agents/{id}/events| EventBus
    RepoRescueAgent -->|Publish: POST /agents/{id}/message| ModerationRooms
    RepoRescueAgent -->|History: GET /agents/{id}/rooms/{roomId}/history| ModerationRooms

    EventBus --> Database
    ModerationRooms --> Database
```

---

## 2. Official Switch Core 0.31.0 Endpoints & Protocol

RepoRescue connects to Switch Core 0.31.0 using standard documented REST endpoints:

### Agent Bridge Endpoints (`switch_core.bridges.agent`)

1. **Agent Registration**:
   - `POST /agents`
   - **Header**: `Authorization: Bearer <registration_token>`
   - **Body**:
     ```json
     {
       "name": "reporescue",
       "description": "RepoRescue Autonomous Engineering Teammate",
       "display_name": "RepoRescue",
       "connector_type": "hosted",
       "integration_profile": {
         "connection_model": "always_on",
         "message_exchange": true,
         "pre_invocation_mediation": [],
         "post_invocation_mediation": [],
         "event_reporting": []
       },
       "overwrite": true
     }
     ```
   - **Response**: `200 OK` with `{"id": "<agent_id>", "api_key": "<bearer_token>"}`.

2. **Publish Room Message**:
   - `POST /agents/{agent_id}/message`
   - **Header**: `Authorization: Bearer <api_key>`
   - **Body**: `{"room_id": "<room_id>", "content": "<markdown_card>", "metadata": {"task_id": "..."}}`
   - **Response**: `200 OK` with `{"event_id": "<id>"}`.

3. **Poll Inbound Events**:
   - `GET /agents/{agent_id}/events?timeout=2.0` (or `GET /agents/{agent_id}/rooms/{room_id}/events`)
   - **Header**: `Authorization: Bearer <api_key>`
   - **Response**: `200 OK` with `{"events": [...]}` or `204 No Content`.

4. **Retrieve Room History**:
   - `GET /agents/{agent_id}/rooms/{room_id}/history`
   - **Header**: `Authorization: Bearer <api_key>`
   - **Response**: `200 OK` with `{"events": [...]}`.

### Switch Gateway Endpoints (Human User Surface)

1. **User Authentication**:
   - `POST /gateway/auth/login` with `{"email": "...", "password": "..."}`
   - Sets secure session cookie (`switch_auth`).

2. **Mint User-Owned Registration Key**:
   - `POST /gateway/api-keys` with `{"label": "..."}`
   - Returns plaintext registration key.

3. **Create Chat Room**:
   - `POST /gateway/chats` with `{"agentId": "<agent_id>", "name": "...", "requestId": "..."}`
   - Returns chat metadata including `roomId`.

4. **Send Message**:
   - `POST /gateway/chats/{room_id}/messages` with `{"requestId": "...", "body": "..."}`

---

## 3. Addressing Policy & Security Guarantee

Switch Core enforces an agent addressing policy (`owner_and_owner_agents_policy`):
- By default, human users can only converse with agents owned by the same user account.
- When RepoRescue registers with a user-owned registration token (minted via `POST /gateway/api-keys`), ownership is bound directly to the human developer.
- This ensures full interactive chat capabilities while preventing unauthorized cross-tenant agent invocation.

### Anti-Bot Human Approval Gate

RepoRescue enforces strict human-in-the-loop governance:
- **Identity Verification**: When `/approve` or `/reject` is received, RepoRescue inspects `event.sender_kind`, `event.sender`, and `event.sender_name`.
- **Bot Rejection**: Senders with `sender_kind == "agent"`, `sender_kind == "bot"`, or `@switch-agent-` URIs are rejected with a warning card.
- **State Machine Protection**: Only tasks in status `AWAITING_APPROVAL` can be approved.
- **Audit Logging**: The verified approver's name and an immutable timestamp are recorded in the task's audit log.
- **Zero Auto-Merge / Auto-Deploy**: Approving a task marks it `APPROVED`. Pull requests must be reviewed and merged manually by humans.

---

## 4. Running the Live Switch Demonstration

RepoRescue includes a built-in live test runner:

```powershell
# 1. Start local Switch Core service if not running
# (Requires PostgreSQL 16 on port 65010 and Switch Core on port 65008)

# 2. Run live demonstration with deterministic mock runtime
python -m reporescue.cli --live-demo --runtime mock

# 3. Run live demonstration with real GPU-accelerated local Ollama AI
python -m reporescue.cli --live-demo --real
```

### Demonstration Steps Performed Live:
1. Connects to `http://127.0.0.1:65008` and verifies health check (`/health`).
2. Authenticates human developer (`Admin <admin@switch.local>`) and mints registration key.
3. Registers `reporescue` agent with Switch Core.
4. Creates a live Switch chat room (`RepoRescue Live Bug Triage`).
5. Posts bug report into the Switch room:
   > `"Bug: Checkout returns HTTP 500 when coupon is applied\nSteps to reproduce: calculate_order_total(items, coupon_code='EXPIRED') raises ValueError\nExpected: Invalid or expired coupons should be handled gracefully without raising ValueError, applying 0.0 discount."`
6. RepoRescue listener polls Switch Core, triages the bug, diagnoses root cause, reproduces failure in an isolated worktree (exit code 1), applies fix, verifies with pytest (3 passed, exit code 0), and posts stage status cards to Switch.
7. Human user posts `/approve <task_id>` into the Switch room.
8. RepoRescue verifies human sender identity, marks task `APPROVED`, and posts confirmation.
9. Room history is fetched directly from Switch Core's PostgreSQL database to prove all 15 messages were exchanged through the live system.
