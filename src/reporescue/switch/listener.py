from __future__ import annotations

import logging
import threading
from typing import Self

from .client import (
    SwitchAuthError,
    SwitchClientProtocol,
    SwitchError,
    SwitchMembershipError,
    SwitchRoomNotFoundError,
)
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
        self._last_heartbeat_time = 0.0

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

        # Report connector ONLINE and send initial heartbeat to Switch Core
        try:
            self.client.update_status(
                room_id=self.room_id, presence="online", status="RepoRescue Active"
            )
            self.client.send_heartbeat()
        except Exception as err:  # noqa: BLE001
            logger.debug("Initial status update failed: %s", err)

        import time

        self._last_heartbeat_time = time.time()

        try:
            while not self._stop_event.is_set():
                try:
                    self.poll_once()
                    # On success, reset backoff delay
                    if self._consecutive_errors > 0:
                        logger.info("Reconnection successful. Normal polling resumed.")
                        self._consecutive_errors = 0
                        self._current_delay = self.poll_interval

                    # Periodically refresh heartbeat every 15 seconds
                    now = time.time()
                    if now - self._last_heartbeat_time >= 15.0:
                        self.client.send_heartbeat()
                        self._last_heartbeat_time = now

                except SwitchAuthError as err:
                    logger.error(
                        "Permanent authentication error contacting Switch server: %s. Stopping listener.",
                        err,
                    )
                    self.stop()
                    break

                except SwitchRoomNotFoundError as err:
                    logger.error(
                        "Permanent room error: Room '%s' not found on Switch server: %s. Stopping listener.",
                        self.room_id,
                        err,
                    )
                    self.stop()
                    break

                except SwitchMembershipError as err:
                    logger.error(
                        "Permanent membership error: Agent lacks access to room '%s': %s. Stopping listener.",
                        self.room_id,
                        err,
                    )
                    self.stop()
                    break

                except SwitchError as err:
                    self._consecutive_errors += 1
                    if self._consecutive_errors >= 10:
                        logger.error(
                            "Exceeded maximum consecutive network retry attempts (10). Stopping listener."
                        )
                        self.stop()
                        break
                    logger.warning(
                        "Transient network error connecting to Switch (attempt %d/10): %s. Retrying in %.1fs...",
                        self._consecutive_errors,
                        err,
                        self._current_delay,
                    )
                    self._apply_backoff()

                except Exception:
                    self._consecutive_errors += 1
                    if self._consecutive_errors >= 10:
                        logger.exception(
                            "Exceeded maximum consecutive error threshold (10). Stopping listener."
                        )
                        self.stop()
                        break
                    logger.exception(
                        "Unexpected error in Switch poll loop (attempt %d/10)",
                        self._consecutive_errors,
                    )
                    self._apply_backoff()

                # Interruptible sleep
                self._stop_event.wait(self._current_delay)
        finally:
            try:
                self.client.update_status(
                    room_id=self.room_id,
                    presence="offline",
                    status="RepoRescue Stopped",
                )
            except Exception as err:  # noqa: BLE001
                logger.debug("Shutdown status update failed: %s", err)
            logger.info("RepoRescue Switch listener stopped gracefully.")

    def _apply_backoff(self) -> None:
        """Calculates exponential backoff delay with jitter capped at max_backoff."""
        import random

        jitter = random.uniform(0.85, 1.15)
        self._current_delay = min(
            self.max_backoff,
            self._current_delay * self.backoff_multiplier * jitter,
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
