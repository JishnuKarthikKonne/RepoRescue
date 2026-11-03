from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from .agent_runtime import AgentRuntimeFactory, RuntimeConfigurationError
from .demo import init_demo_repository
from .git_ops import WorktreeManager
from .models import BugTask
from .orchestrator import WorkflowOrchestrator
from .switch.client import MockSwitchClient, SwitchClient, SwitchConfig
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

    try:
        runtime = AgentRuntimeFactory.get_runtime(pref)
    except RuntimeConfigurationError as err:
        logger.error("Configuration error initializing real AI runtime: %s", err)
        sys.exit(1)

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RepoRescue: Human-approved collaborative AI bug-fixing workflow"
    )
    parser.add_argument(
        "--listen", action="store_true", help="Start the live Switch room listener"
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

    if args.listen or args.mock or args.demo or args.real:
        run_listener(
            mock=args.mock or (not args.listen),
            demo=args.demo or args.real,
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
