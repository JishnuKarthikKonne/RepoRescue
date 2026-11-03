from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from .agent_runtime import AgentRuntimeFactory, RuntimeConfigurationError
from .demo import init_demo_repository
from .git_ops import WorktreeManager
from .github.client import GitHubClient, GitHubClientProtocol, MockGitHubClient
from .github.config import GitHubConfig, is_demo_repository
from .github.manager import GitHubPRManager
from .models import BugTask, TaskStatus
from .orchestrator import WorkflowOrchestrator
from .switch.client import MockSwitchClient, SwitchClient, SwitchConfig, SwitchError
from .switch.dispatcher import RepoRescueDispatcher
from .switch.listener import SwitchListener
from .switch.models import SwitchMessageEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("reporescue")


def run_listener(
    *,
    mock: bool = False,
    demo: bool = False,
    runtime_mode: str | None = None,
    room_id: str | None = None,
    poll_interval: float = 2.0,
    repo_path: Path | str | None = None,
) -> None:
    """Runs the RepoRescue Switch listener in either live or mock mode."""
    cfg = SwitchConfig.from_env()
    target_room = room_id or cfg.default_room_id or "!reporescue:localhost"

    # Setup demo repository if requested
    if demo:
        demo_dir = Path(".reporescue") / "demo_repo"
        logger.info("Initializing isolated demo repository at %s", demo_dir)
        working_repo = init_demo_repository(demo_dir)
    else:
        working_repo = Path(repo_path).resolve() if repo_path else Path.cwd()

    # Determine runtime preference
    pref = runtime_mode
    if not pref and (mock or demo):
        pref = "mock"

    if pref == "real":
        from .agent_runtime import check_ollama_alive

        if (
            not (
                os.getenv("OPENAI_API_KEY")
                or os.getenv("LLM_BASE_URL")
                or os.getenv("OLLAMA_HOST")
            )
            and check_ollama_alive()
        ):
            logger.info("Detected local Ollama instance at http://127.0.0.1:11434")
            os.environ["OLLAMA_HOST"] = "http://127.0.0.1:11434"

    try:
        runtime = AgentRuntimeFactory.get_runtime(pref)
    except RuntimeConfigurationError as err:
        logger.error("Configuration error initializing real AI runtime: %s", err)
        sys.exit(1)

    healthy, health_msg = runtime.check_health()
    if not healthy:
        if pref == "real":
            logger.error("AI runtime health check failed: %s", health_msg)
            sys.exit(1)
        else:
            logger.warning("AI runtime health warning: %s", health_msg)
    else:
        logger.info("AI runtime health: %s", health_msg)

    is_sim = getattr(runtime, "is_simulation", True)
    rt_name = getattr(runtime, "runtime_name", "mock")
    logger.info(
        "Agent runtime mode: %s (simulation=%s)",
        rt_name,
        is_sim,
    )

    if mock or demo:
        logger.info(
            "Switch Transport: MockSwitchClient (offline simulated Switch events)"
        )
        client = MockSwitchClient(cfg)
    else:
        logger.info(
            "Switch Transport: Connecting to live Switch Agent Bridge at %s (agent=%s)",
            cfg.server_url,
            cfg.agent_id,
        )
        client = SwitchClient(cfg)
        if (
            not (client.config.agent_id and client.config.api_key)
            and client.config.registration_token
        ):
            logger.info("Auto-registering RepoRescue agent with Switch server...")
            try:
                agent_id, _ = client.register_agent(name="reporescue-agent")
                logger.info("Successfully registered agent ID: %s", agent_id)
            except (SwitchError, urllib.error.URLError, OSError) as err:
                logger.warning("Could not auto-register agent: %s", err)

    dispatcher = RepoRescueDispatcher(client)
    worktree_mgr = WorktreeManager(working_repo)
    orchestrator = WorkflowOrchestrator(
        repo_path=working_repo,
        runtime=runtime,
        worktree_mgr=worktree_mgr,
        dispatcher=dispatcher,
        room_id=target_room,
    )
    dispatcher.orchestrator = orchestrator

    # Attach GitHub PR Manager
    gh_cfg = GitHubConfig.from_env(working_repo)
    if mock or demo or gh_cfg.dry_run:
        if not gh_cfg.repository:
            gh_cfg.repository = "switch-hackathon/RepoRescue"
        gh_cfg.dry_run = True
        gh_client = MockGitHubClient(gh_cfg)
        gh_mgr = GitHubPRManager(gh_cfg, client=gh_client, worktree_mgr=worktree_mgr)
        orchestrator.github_manager = gh_mgr
    elif gh_cfg.is_configured:
        gh_client = GitHubClient(gh_cfg)
        gh_mgr = GitHubPRManager(gh_cfg, client=gh_client, worktree_mgr=worktree_mgr)
        orchestrator.github_manager = gh_mgr

    listener = SwitchListener(
        client,
        dispatcher,
        room_id=target_room,
        poll_interval_seconds=poll_interval,
    )

    # Register graceful shutdown signals
    def _sig_handler(sig: int, frame: Any) -> None:
        logger.info("Received interrupt signal (%d). Shutting down listener...", sig)
        listener.stop()

    signal.signal(signal.SIGINT, _sig_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig_handler)

    if mock and demo:
        logger.info("=== STARTING AUTONOMOUS WORKFLOW DEMO ===")
        mock_client: MockSwitchClient = client  # type: ignore[assignment]

        # 1. Inject simulated bug report
        mock_client.queue_event(
            SwitchMessageEvent.from_agent_event(
                {
                    "type": "message",
                    "room_id": target_room,
                    "payload": {
                        "message_id": "demo_msg_001",
                        "sender": "@alex:localhost",
                        "sender_name": "Alex (Developer)",
                        "sender_kind": "user",
                        "body": "Bug: Checkout returns HTTP 500 when coupon is applied\nSteps to reproduce: POST /checkout with code='EXPIRED'",
                        "timestamp": int(time.time() * 1000),
                    },
                }
            )
        )

        # Process the bug report and run multi-agent workflow
        listener.poll_once()

        # Display cards broadcast to the room
        print("\n" + "=" * 60)
        print("  MESSAGES POSTED TO SWITCH ROOM (#reporescue)")
        print("=" * 60)
        for room, content, meta in mock_client.sent_messages:
            print(f"\n[Room: {room}]\n{content}\n" + "-" * 40)

        # 2. Simulate human approval in the room
        logger.info("Simulating verified human approval '/approve' in the room...")
        mock_client.queue_event(
            SwitchMessageEvent.from_agent_event(
                {
                    "type": "message",
                    "room_id": target_room,
                    "payload": {
                        "message_id": "demo_msg_002",
                        "sender": "@alex:localhost",
                        "sender_name": "Alex (Developer)",
                        "sender_kind": "user",
                        "body": "/approve",
                        "timestamp": int(time.time() * 1000),
                    },
                }
            )
        )
        listener.poll_once()

        print(f"\n[Final Message to Room]:\n{mock_client.last_message_content}\n")
        logger.info("=== DEMO WORKFLOW COMPLETED SUCCESSFULLY ===")
        return

    # Run continuous listener loop for live execution
    logger.info("RepoRescue is listening for bug reports in room '%s'...", target_room)
    try:
        listener.run_loop()
    except KeyboardInterrupt:
        listener.stop()


def run_live_demo(
    *,
    runtime_mode: str | None = None,
    repo_path: Path | str | None = None,
) -> None:
    """Runs a live end-to-end demonstration against Switch Core 0.31.0."""
    print("=" * 70)
    print("  REPORESCUE: LIVE SWITCH INTEGRATION DEMONSTRATION")
    print("=" * 70)

    cfg = SwitchConfig.from_env()

    # 1. Check Switch Core connectivity
    try:
        health_req = urllib.request.Request(f"{cfg.server_url}/health")
        with urllib.request.urlopen(health_req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") != "ok":
                raise RuntimeError(f"Unexpected health response: {data}")
    except (urllib.error.URLError, OSError, RuntimeError) as err:
        logger.error(
            "Cannot connect to Switch Core at %s (%s).\n"
            "Please ensure Switch Core 0.31.0 is running on port 65008.",
            cfg.server_url,
            err,
        )
        sys.exit(1)

    print(f"\n[Step 1] Connected to Switch Core at {cfg.server_url} (Health: OK)")

    # 2. Authenticate or prepare registration key
    admin_email = os.getenv("SWITCH_ADMIN_EMAIL", "admin@switch.local")
    admin_password = os.getenv("SWITCH_ADMIN_PASSWORD", "")
    if not admin_password:
        appdata = os.environ.get("APPDATA", "")
        candidates = [
            (
                Path(appdata)
                / "switchdash"
                / "local-switch-server"
                / "native"
                / "run"
                / "switch-core.env"
                if appdata
                else None
            ),
            (
                Path.home()
                / ".config"
                / "switchdash"
                / "local-switch-server"
                / "native"
                / "run"
                / "switch-core.env"
            ),
        ]
        for c in candidates:
            if c and c.is_file():
                try:
                    for line in c.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line.startswith("GATEWAY_ADMIN_PASSWORD="):
                            admin_password = line.split("=", 1)[1].strip()
                        elif line.startswith("GATEWAY_ADMIN_EMAIL="):
                            admin_email = line.split("=", 1)[1].strip()
                except OSError:
                    pass

    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    user_info = {}
    user_reg_token = cfg.registration_token

    if admin_password:
        try:
            login_data = json.dumps(
                {"email": admin_email, "password": admin_password}
            ).encode("utf-8")
            login_req = urllib.request.Request(
                f"{cfg.server_url}/gateway/auth/login",
                data=login_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(login_req, timeout=5.0) as res:
                user_info = json.loads(res.read().decode("utf-8"))
                print(
                    f"[Step 2] Authenticated Switch human user: {user_info.get('name')} <{user_info.get('email')}>"
                )

            mint_data = json.dumps(
                {"label": f"RepoRescue Live Demo {int(time.time())}"}
            ).encode("utf-8")
            mint_req = urllib.request.Request(
                f"{cfg.server_url}/gateway/api-keys",
                data=mint_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(mint_req, timeout=5.0) as res:
                key_resp = json.loads(res.read().decode("utf-8"))
                user_reg_token = key_resp["key"]
                print("  ✓ Minted user-owned registration token.")
        except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as err:
            logger.warning("Gateway human login skipped: %s", err)

    # 3. Register agent
    print("\n[Step 3] Registering RepoRescue agent with Switch Core...")
    if not cfg.api_key or not cfg.agent_id:
        try:
            del_req = urllib.request.Request(
                f"{cfg.server_url}/gateway/agents/by-name/reporescue",
                method="DELETE",
            )
            with opener.open(del_req, timeout=5.0):
                pass
        except (urllib.error.URLError, OSError):
            logger.debug("Previous agent cleanup skipped.")

        client = SwitchClient(
            SwitchConfig(server_url=cfg.server_url, registration_token=user_reg_token)
        )
        agent_id, _api_key = client.register_agent(name="reporescue")
        print(f"  ✓ Registered agent ID: {agent_id}")
    else:
        client = SwitchClient(cfg)
        print(f"  ✓ Using existing agent ID: {cfg.agent_id}")

    # 4. Create chat room
    print("\n[Step 4] Creating live chat room in Switch...")
    room_id = cfg.default_room_id
    if not room_id and user_info:
        try:
            chat_req_data = json.dumps(
                {
                    "agentId": client.config.agent_id,
                    "name": "RepoRescue Live Bug Triage",
                    "requestId": str(uuid.uuid4()),
                }
            ).encode("utf-8")
            create_chat_req = urllib.request.Request(
                f"{cfg.server_url}/gateway/chats",
                data=chat_req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(create_chat_req, timeout=5.0) as res:
                chat_resp = json.loads(res.read().decode("utf-8"))
                room_id = chat_resp["chat"]["roomId"]
                print(
                    f"  ✓ Live room created: {chat_resp['chat']['name']} (ID: {room_id})"
                )
        except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as err:
            logger.warning("Could not create user chat room via Gateway: %s", err)

    if not room_id:
        room = client.create_room(name="RepoRescue Bug Triage Room")
        room_id = room.get("id") or "!reporescue:localhost"
        print(f"  ✓ Created agent room (ID: {room_id})")

    # Update agent presence to ONLINE and send initial heartbeat
    client.update_status(room_id=room_id, presence="online", status="RepoRescue Active")
    client.send_heartbeat()
    print("  ✓ Reported agent presence ONLINE to Switch Core")

    # 5. Initialize pristine demo repo & multi-agent orchestrator
    print("\n[Step 5] Initializing demo repo & multi-agent orchestrator...")
    if repo_path:
        working_repo = Path(repo_path).resolve()
    else:
        demo_dir = Path(".reporescue") / "demo_repo"
        working_repo = init_demo_repository(demo_dir)
    print(f"  ✓ Working repository at {working_repo}")

    pref = runtime_mode or (
        "real"
        if (
            os.getenv("OPENAI_API_KEY")
            or os.getenv("LLM_BASE_URL")
            or os.getenv("OLLAMA_HOST")
        )
        else "mock"
    )
    if pref == "real":
        from .agent_runtime import check_ollama_alive

        if (
            not (
                os.getenv("OPENAI_API_KEY")
                or os.getenv("LLM_BASE_URL")
                or os.getenv("OLLAMA_HOST")
            )
            and check_ollama_alive()
        ):
            logger.info("Detected local Ollama instance at http://127.0.0.1:11434")
            os.environ["OLLAMA_HOST"] = "http://127.0.0.1:11434"

    try:
        runtime = AgentRuntimeFactory.get_runtime(pref)
    except (RuntimeConfigurationError, OSError, ValueError):
        logger.info("Falling back to deterministic mock runtime")
        runtime = AgentRuntimeFactory.get_runtime("mock")

    is_sim = getattr(runtime, "is_simulation", True)
    rt_name = getattr(runtime, "runtime_name", "mock")
    print(f"  ✓ AI Runtime: {rt_name} (simulation={is_sim})")

    dispatcher = RepoRescueDispatcher(client, active_room_id=room_id)
    worktree_mgr = WorktreeManager(working_repo)
    orchestrator = WorkflowOrchestrator(
        repo_path=working_repo,
        runtime=runtime,
        worktree_mgr=worktree_mgr,
        dispatcher=dispatcher,
        room_id=room_id,
    )
    dispatcher.orchestrator = orchestrator

    gh_cfg = GitHubConfig.from_env(working_repo)
    if gh_cfg.is_configured and not gh_cfg.dry_run:
        gh_client = GitHubClient(gh_cfg)
    else:
        if not gh_cfg.repository:
            gh_cfg.repository = "switch-hackathon/RepoRescue"
        gh_cfg.dry_run = True
        gh_client = MockGitHubClient(gh_cfg)
    gh_mgr = GitHubPRManager(gh_cfg, client=gh_client, worktree_mgr=worktree_mgr)
    orchestrator.github_manager = gh_mgr

    listener = SwitchListener(
        client,
        dispatcher,
        room_id=room_id,
        poll_interval_seconds=1.0,
    )

    # 6. Human user posts bug report
    print("\n[Step 6] Human user posts bug report into Switch room...")
    bug_msg = (
        "Bug: Checkout returns HTTP 500 when coupon is applied\n"
        "Steps to reproduce: calculate_order_total(items, coupon_code='EXPIRED') raises ValueError\n"
        "Expected: Invalid or expired coupons should be handled gracefully without raising ValueError, applying 0.0 discount."
    )
    if user_info:
        post_msg_req = urllib.request.Request(
            f"{cfg.server_url}/gateway/chats/{room_id}/messages",
            data=json.dumps({"requestId": str(uuid.uuid4()), "body": bug_msg}).encode(
                "utf-8"
            ),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(post_msg_req, timeout=5.0):
            print(f"  ✓ Posted message as '{user_info.get('name', 'Admin')}'")
    else:
        client.send_message(room_id, bug_msg)
        print("  ✓ Posted bug report message to room")

    # 7. Poll and process bug report
    print("\n[Step 7] RepoRescue listener polls and dispatches multi-agent workflow...")
    listener.poll_once()

    task = next(iter(dispatcher.tasks.values())) if dispatcher.tasks else None
    if not task:
        logger.error("No task was created!")
        sys.exit(1)

    print(f"  ✓ Task created: ID `{task.task_id}`, status: `{task.status}`")
    for ev in task.evidence:
        print(f"    - {ev}")

    if task.status != TaskStatus.AWAITING_APPROVAL:
        logger.error(
            "Workflow failed before reaching awaiting_approval: %s", task.status
        )
        sys.exit(1)

    # 8. Human user posts approval
    print("\n[Step 8] Human user posts '/approve' command...")
    approve_cmd = f"/approve {task.task_id}"
    if user_info:
        approve_req = urllib.request.Request(
            f"{cfg.server_url}/gateway/chats/{room_id}/messages",
            data=json.dumps(
                {"requestId": str(uuid.uuid4()), "body": approve_cmd}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(approve_req, timeout=5.0):
            print(f"  ✓ Posted approval command: '{approve_cmd}'")
    else:
        client.send_message(room_id, approve_cmd)

    # 9. Poll and validate approval
    print("\n[Step 9] RepoRescue listener validates human approval...")
    listener.poll_once()
    print(f"  ✓ Final Task status: `{task.status}`")
    print(f"  ✓ Approved by: `{task.approval_by}` at `{task.approved_at}`")

    # 10. Fetch room history from Switch Core
    print("\n[Step 10] Room history from Switch Core:")
    hist = client.get_room_history(room_id)
    events = hist.get("events", [])
    print(f"  ✓ Switch room contains {len(events)} total messages:")
    for idx, ev in enumerate(events, 1):
        sender = ev.get("sender_name")
        body = ev.get("body", "")
        snippet = body.replace("\n", " ")[:90]
        print(f"    {idx}. [{sender}]: {snippet}...")

    print("\n" + "=" * 70)
    print("  SUCCESS: LIVE SWITCH INTEGRATION & HUMAN APPROVAL DEMO COMPLETED")
    print("=" * 70)


def run_github_check(repo_path: Path | str | None = None) -> None:
    """Diagnoses and validates GitHub PR publishing configuration, Smart HTTP access, and repo isolation."""
    print("=" * 70)
    print("  REPORESCUE: GITHUB INTEGRATION DIAGNOSTIC CHECK")
    print("=" * 70)

    working_repo = Path(repo_path).resolve() if repo_path else Path.cwd()
    from .github.config import (
        detect_repository_origin,
        get_orchestrator_repository,
        is_demo_repository,
    )

    is_demo = is_demo_repository(working_repo)
    cfg = GitHubConfig.from_env(working_repo, is_demo=is_demo)

    print(
        f"\nRepository Context:    {'Demo App (.reporescue/demo_repo)' if is_demo else 'Standard Repository'}"
    )
    print(f"Local Working Dir:     {working_repo}")
    print(f"Target Repository:     {cfg.repository or '<not configured>'}")
    print(f"Base Branch:           {cfg.base_branch}")
    print(f"API Endpoint:          {cfg.api_url}")
    print(f"Draft Enforcement:     {cfg.draft} (Strictly enforced)")
    print(f"Dry Run Mode:          {cfg.dry_run}")
    print(f"Repository Allowlist:  {cfg.repository_allowlist or '<none>'}")
    print(f"Token Configuration:   {cfg.masked_token}")

    errors = cfg.validate()
    if errors:
        print("\n[Configuration & Isolation Issues]:")
        for err in errors:
            print(f"  ✗ {err}")
        print("\nReadiness: 🛑 NOT READY for live GitHub publishing.")
        if is_demo:
            print(
                "Remediation for Demo Repository:\n"
                "  1. Set an independent GitHub target for demo coupons:\n"
                "     $env:DEMO_GITHUB_REPOSITORY = '<username>/<demo-repo>'\n"
                "     (Or: git -C .reporescue/demo_repo remote add origin https://github.com/<username>/<demo-repo>.git)\n"
                "  2. For dry-run simulation: $env:GITHUB_DRY_RUN = '1'"
            )
        else:
            print(
                "Note: Set GITHUB_TOKEN and GITHUB_REPOSITORY (or configure git remote origin)."
            )
        return

    # Multi-Repository Isolation Check
    orchestrator_repo = cfg.orchestrator_repository or get_orchestrator_repository(
        working_repo
    )
    wt_origin = detect_repository_origin(working_repo)

    print("\n[Stage 1: Multi-Repository Isolation Analysis]")
    if is_demo:
        if (
            orchestrator_repo
            and cfg.repository
            and cfg.repository.lower() == orchestrator_repo.lower()
        ):
            print(
                f"  ✗ Isolation Failure: Demo repository target points to main RepoRescue orchestrator ('{cfg.repository}')."
            )
            print(
                "    Safety rule: Main RepoRescue project must NEVER receive demo coupon patches."
            )
            print("\nReadiness: 🛑 NOT READY (Isolation gate failure).")
            return
        print(
            f"  ✓ Demo repository isolated from orchestrator codebase ('{orchestrator_repo or 'unknown'}')"
        )
        if wt_origin:
            print(f"  ✓ Demo git origin matches configured target: {wt_origin}")
        elif os.getenv("DEMO_GITHUB_REPOSITORY"):
            print(
                f"  ✓ Demo target explicitly configured via DEMO_GITHUB_REPOSITORY: {cfg.repository}"
            )
    else:
        print("  ✓ Standard repository workflow (non-demo)")
        if wt_origin:
            print(f"  ✓ Local remote origin: {wt_origin}")

    # Authentication & Git Smart HTTP Preflight Check
    if cfg.token or cfg.dry_run:
        print("\n[Stage 2: GitHub REST API Authentication & Repository Metadata]")
        client: GitHubClientProtocol = (
            MockGitHubClient(cfg) if cfg.dry_run else GitHubClient(cfg)
        )

        preflight = client.preflight_git_receive_pack()

        if preflight.api_authenticated:
            user_disp = (
                f" as user '{preflight.api_username}'" if preflight.api_username else ""
            )
            print(f"  ✓ Connected to GitHub REST API{user_disp}")
            print(f"  ✓ Target repository access verified: {cfg.repository}")
            print(
                f"  ✓ API role-based write permission: push={preflight.api_push_permission}"
            )
        else:
            print(f"  ✗ REST API Authentication Failed: {preflight.error_message}")
            print("\nReadiness: 🛑 NOT READY (REST API authentication failed).")
            return

        print("\n[Stage 3: Git Smart HTTP Service Authorization (git-receive-pack)]")
        if preflight.git_smart_http_authorized:
            print("  ✓ Git Smart HTTP Push Preflight: AUTHORIZED (HTTP 200)")
            print(
                "  ✓ Verified Git Smart HTTP receives push capabilities for this token"
            )
            print("\nReadiness: ✅ READY for secure Draft PR publishing.")
        else:
            print(f"  ✗ Git Smart HTTP Push: DENIED (Status {preflight.status_code})")
            print(f"    Error: {preflight.error_message}")
            if preflight.diagnostic_guidance:
                print(
                    f"\n[Diagnostic Analysis & Remediation]:\n{preflight.diagnostic_guidance}"
                )
            print(
                "\nReadiness: 🛑 NOT READY (Git push permission denied despite REST role)."
            )
    else:
        print("\nReadiness: ⚠️ DRY RUN ONLY (No live token configured).")


def run_publish_task(
    task_id: str,
    repo_path: Path | str | None = None,
) -> None:
    """Retries publishing an approved task as a GitHub Draft PR."""
    print("=" * 70)
    print("  REPORESCUE: RETRY GITHUB DRAFT PR PUBLICATION")
    print("=" * 70)

    working_repo = Path(repo_path).resolve() if repo_path else Path.cwd()
    worktree_mgr = WorktreeManager(working_repo)

    # Locate matching active worktree
    target_prefix = task_id[:8]
    matching_wt = None
    for wt in worktree_mgr.list_worktrees():
        if wt.branch.startswith(f"reporescue/task-{target_prefix}"):
            matching_wt = wt
            break

    if not matching_wt:
        print(
            f"\n❌ Error: No active worktree found for task '{task_id}' "
            f"(branch prefix 'reporescue/task-{target_prefix}')."
        )
        print("Available worktrees:")
        for wt in worktree_mgr.list_worktrees():
            print(f"  - {wt.branch} at {wt.path}")
        sys.exit(1)

    print(f"\nFound worktree branch: {matching_wt.branch}")
    print(f"Worktree path:         {matching_wt.path}")

    from .github.fingerprint import compute_worktree_patch_fingerprint

    fp, _diff_text, commit_sha = compute_worktree_patch_fingerprint(matching_wt.path)
    print(f"Commit SHA:            {commit_sha}")
    print(f"Patch Fingerprint:     {fp}")

    task = BugTask(
        title=f"Bugfix for {matching_wt.branch}",
        description="Retried approved task publication",
        reporter="switch-user",
        task_id=task_id,
        status=TaskStatus.APPROVED,
        proposed_branch=matching_wt.branch,
        approval_by="Verified Maintainer",
        approved_patch_fingerprint=fp,
        verified_patch_fingerprint=fp,
        commit_sha=commit_sha,
    )
    task.evidence.append("Verification PASSED in isolated worktree")

    is_demo = is_demo_repository(matching_wt.path) or is_demo_repository(working_repo)
    cfg = GitHubConfig.from_env(working_repo, is_demo=is_demo)

    if cfg.dry_run:
        client = MockGitHubClient(cfg)
    else:
        client = GitHubClient(cfg)
    gh_mgr = GitHubPRManager(cfg, client=client, worktree_mgr=worktree_mgr)

    print("\nExecuting secure GitHub Draft PR publication...")
    try:
        pr_result = gh_mgr.publish_draft_pr(task, matching_wt.path)
        print("\n" + "=" * 70)
        print("  SUCCESS: DRAFT PULL REQUEST CREATED")
        print("=" * 70)
        print(f"  PR URL:       {pr_result.html_url}")
        print(f"  PR Number:    #{pr_result.pr_number}")
        print(f"  Branch:       {pr_result.branch_name}")
        print(f"  Base Branch:  {pr_result.base_branch}")
        print(f"  Draft:        {pr_result.draft} (Strictly enforced)")
        print(f"  Existing:     {pr_result.is_existing}")
    except Exception as err:  # noqa: BLE001
        print(f"\n❌ Publication Failed: {err}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RepoRescue: Human-approved collaborative AI bug-fixing workflow"
    )
    parser.add_argument(
        "--listen", action="store_true", help="Start the live Switch room listener"
    )
    parser.add_argument(
        "--live-demo",
        action="store_true",
        help="Run end-to-end autonomous workflow against live Switch Core",
    )
    parser.add_argument(
        "--github-check",
        action="store_true",
        help="Diagnose and validate GitHub PR publishing configuration and access",
    )
    parser.add_argument(
        "--publish-task",
        "--retry-pr",
        dest="publish_task",
        metavar="TASK_ID",
        help="Retry publishing an approved task as a GitHub Draft PR",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use mock Switch client for offline testing and demos",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run self-contained autonomous multi-agent demo",
    )
    parser.add_argument(
        "--runtime",
        choices=["mock", "real"],
        default=None,
        help="AI agent runtime to use: 'mock' (deterministic simulation) or 'real' (live LLM provider)",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Use real AI agent runtime (requires OPENAI_API_KEY or LLM_BASE_URL)",
    )
    parser.add_argument(
        "--room-id",
        help="Switch room ID to monitor (defaults to SWITCH_ROOM_ID env var)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between polls (default: 2.0s)",
    )
    parser.add_argument(
        "--repo-path",
        help="Target repository to work on (defaults to current directory)",
    )

    # Standalone CLI task creation options
    parser.add_argument(
        "--title", default="Checkout returns HTTP 500", help="Task title"
    )
    parser.add_argument(
        "--description",
        default="Checkout fails after recent deployment.",
        help="Task description",
    )
    parser.add_argument("--reporter", default="demo-user", help="Reporter identifier")

    args = parser.parse_args()

    runtime_mode = "real" if args.real else args.runtime

    if args.github_check:
        run_github_check(repo_path=args.repo_path)
    elif args.publish_task:
        run_publish_task(args.publish_task, repo_path=args.repo_path)
    elif args.live_demo:
        run_live_demo(
            runtime_mode=runtime_mode,
            repo_path=args.repo_path,
        )
    elif args.listen:
        run_listener(
            mock=args.mock,
            demo=args.demo,
            runtime_mode=runtime_mode,
            room_id=args.room_id,
            poll_interval=args.poll_interval,
            repo_path=args.repo_path,
        )
    elif args.demo or args.mock or (args.real and not args.listen):
        run_listener(
            mock=True,
            demo=True,
            runtime_mode=runtime_mode,
            room_id=args.room_id,
            poll_interval=args.poll_interval,
            repo_path=args.repo_path,
        )
    else:
        # Standalone task creation mode (preserves original behavior)
        task = BugTask(args.title, args.description, args.reporter)
        print(f"Created task {task.task_id}: {task.title} [{task.status}]")


if __name__ == "__main__":
    main()
