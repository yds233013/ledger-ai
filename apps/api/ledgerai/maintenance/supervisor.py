"""The thread that ticks the maintenance schedule inside the worker process.

Deliberately a thread rather than a second process: the worker already has a
Redis connection and a lifecycle, and Railway's Hobby plan has no spare service
slot to put a scheduler in. The thread is a daemon and every tick is wrapped, so
the only thing it can do to the worker is nothing.

It is started by `ledgerai.worker`, never by the API. The API serves requests;
giving a request-handling process periodic side effects would mean the sweeps
ran once per replica and once per restart, which is exactly what the lock in
`schedule.py` exists to prevent.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .schedule import ScheduledSweep, default_sweeps, run_due_sweeps

logger = logging.getLogger(__name__)

# How often the schedule is evaluated. Well below the shortest interval (one
# hour), so a sweep starts within a minute of becoming due, and cheap: a tick
# that finds nothing due is one Redis GET per sweep.
DEFAULT_TICK_SECONDS = 60.0


class MaintenanceScheduler:
    """Evaluates the sweep schedule on a fixed tick until asked to stop."""

    def __init__(
        self,
        redis_factory: Callable[[], object],
        sweeps: list[ScheduledSweep] | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._redis_factory = redis_factory
        self._sweeps = default_sweeps() if sweeps is None else sweeps
        self._tick_seconds = tick_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("MaintenanceScheduler already started")
        self._thread = threading.Thread(
            target=self.run,
            name="ledgerai-maintenance",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "maintenance.scheduler_started jobs=%s tick_seconds=%.0f",
            [s.name for s in self._sweeps],
            self._tick_seconds,
        )

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        logger.info("maintenance.scheduler_stopped")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the loop ----------------------------------------------------------

    def tick(self) -> None:
        """One evaluation. Never raises — the worker must survive any outcome."""
        try:
            redis = self._redis_factory()
            run_due_sweeps(redis, self._sweeps)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - including an unreachable Redis
            logger.exception("maintenance.tick_failed")

    def run(self) -> None:
        # The first evaluation happens after one tick, not immediately: a
        # redeploy restarts every replica at once, and a sweep that ran a minute
        # ago under the old deployment should not run again just because a new
        # process started. The Redis marker would refuse it anyway; waiting
        # keeps the common case quiet rather than relying on that refusal.
        while not self._stop.wait(self._tick_seconds):
            self.tick()
