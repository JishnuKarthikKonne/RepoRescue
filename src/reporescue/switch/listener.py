from __future__ import annotations

import logging
import threading
from typing import Self

from .client import SwitchAuthError, SwitchClientProtocol, SwitchError
from .dispatcher import RepoRescueDispatcher

logger = logging.getLogger("reporescue.switch.listener")


class SwitchListener:
    """Runnable listener that ingests Switch room events, deduplicates, and dispatches them.

    Features:
    - Event deduplication by message_id.
    - Safe exponential backoff reconnection on network interruptions.
    - Interruptible and graceful shutdown via threading.Event.
    - Credential safety: never logs or leaks tokens/secrets in error messages.
    """

    def __init__(
        self,
        client: SwitchClientProtocol,
        dispatcher: RepoRescueDispatcher,
        *,
        room_id: str | None = None,
        poll_interval_seconds: float = 2.0,
        backoff_multiplier: float = 1.5,
        max_backoff_seconds: float = 30.0,
        max_seen_cache: int = 5_000,
    ) -> None:
        self.client = client
        self.dispatcher = dispatcher
        self.room_id = room_id
        self.poll_interval = max(0.01, poll_interval_seconds)
        self.backoff_multiplier = max(1.1, backoff_multiplier)
        self.max_backoff = max(self.poll_interval, max_backoff_seconds)
        self.max_seen_cache = max_seen_cache

        self._stop_event = threading.Event()
        self._processed_ids: set[str] = set()
        self._id_history: list[str] = []
        self._consecutive_errors = 0
        self._current_delay = self.poll_interval

    @property
    def is_running(self) -> bool:
        """Returns True if the listener loop is active."""
        return not self._stop_event.is_set()

    def mark_processed(self, message_id: str) -> bool:
        """Records a message_id as processed. Returns False if it was already seen."""
        if not message_id:
            return True
        if message_id in self._processed_ids:
            return False

        if len(self._id_history) >= self.max_seen_cache:
            evict_id = self._id_history.pop(0)
            self._processed_ids.discard(evict_id)

        self._processed_ids.add(message_id)
        self._id_history.append(message_id)
        return True

    def poll_once(self) -> int:
        """Performs a single poll and dispatch cycle.

        Returns:
            The number of newly processed events.
        """
        events = self.client.poll_events(room_id=self.room_id)
        if not events:
            return 0

        new_count = 0
        for event in events:
            # Deduplicate by unique message_id
            if not self.mark_processed(event.message_id):
                logger.debug("Skipping duplicate message event: %s", event.message_id)
                continue

            self.dispatcher.handle_message_event(event)
            new_count += 1

        return new_count

    def run_loop(self) -> None:
        """Runs the main listening loop until stop() is requested."""
        logger.info(
            "RepoRescue Switch listener started (room=%s)", self.room_id or "default"
        )
        self._stop_event.clear()
        self._consecutive_errors = 0
        self._current_delay = self.poll_interval

        while not self._stop_event.is_set():
            try:
                self.poll_once()
                # On success, reset backoff delay
                if self._consecutive_errors > 0:
                    logger.info("Reconnection successful. Normal polling resumed.")
                    self._consecutive_errors = 0
                    self._current_delay = self.poll_interval

            except SwitchAuthError as err:
                self._consecutive_errors += 1
                logger.error("Authentication error contacting Switch server: %s", err)
                self._apply_backoff()

            except SwitchError as err:
                self._consecutive_errors += 1
                logger.warning(
                    "Network error connecting to Switch (attempt %d): %s. Retrying in %.1fs...",
                    self._consecutive_errors,
                    err,
                    self._current_delay,
                )
                self._apply_backoff()

            except Exception:
                self._consecutive_errors += 1
                logger.exception("Unexpected error in Switch poll loop")
                self._apply_backoff()

            # Interruptible sleep
            self._stop_event.wait(self._current_delay)

        logger.info("RepoRescue Switch listener stopped gracefully.")

    def _apply_backoff(self) -> None:
        """Calculates exponential backoff delay capped at max_backoff."""
        self._current_delay = min(
            self.max_backoff,
            self._current_delay * self.backoff_multiplier,
        )

    def stop(self) -> None:
        """Signals the listener loop to stop."""
        self._stop_event.set()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.stop()
