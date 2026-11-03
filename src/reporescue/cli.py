from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from .models import BugTask
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
    room_id: str | None = None,
    poll_interval: float = 2.0,
) -> None:
    """Runs the RepoRescue Switch listener in either live or mock mode."""
    cfg = SwitchConfig.from_env()
    target_room = room_id or cfg.default_room_id or "!reporescue:localhost"

    if mock:
        logger.info("Initializing RepoRescue in MOCK mode (offline demonstration)")
        client = MockSwitchClient(cfg)
    else:
        logger.info(
            "Connecting to Switch Agent Bridge at %s (agent=%s)",
            cfg.server_url,
            cfg.agent_id,
        )
        client = SwitchClient(cfg)

    dispatcher = RepoRescueDispatcher(client)
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
        # Simulate an incoming bug report in mock demo mode
        logger.info("Injecting simulated bug report into mock room...")
        mock_client = client  # type: ignore[assignment]
        mock_client.queue_event(
            SwitchMessageEvent.from_agent_event(
                {
                    "type": "message",
                    "room_id": target_room,
                    "payload": {
                        "message_id": "demo_msg_001",
                        "sender": "@developer:localhost",
                        "sender_name": "Developer Alex",
                        "sender_kind": "user",
                        "body": "Bug: Checkout returns HTTP 500 when coupon is applied\nSteps to reproduce: POST /checkout with code='EXPIRED'",
                        "timestamp": int(time.time() * 1000),
                    },
                }
            )
        )

    # Run listener loop
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
        "--demo", action="store_true", help="Inject a sample bug report in mock mode"
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

    # Preserve standalone CLI creation options
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

    if args.listen or args.mock or args.demo:
        run_listener(
            mock=args.mock,
            demo=args.demo,
            room_id=args.room_id,
            poll_interval=args.poll_interval,
        )
    else:
        # Standalone task creation mode (preserves original behavior)
        task = BugTask(args.title, args.description, args.reporter)
        print(f"Created task {task.task_id}: {task.title} [{task.status}]")


if __name__ == "__main__":
    main()
