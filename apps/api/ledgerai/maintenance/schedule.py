"""When each maintenance sweep runs, and which worker gets to run it.

Railway's Hobby plan caps a project at five services. Postgres, Redis, the API,
the worker and the web front end fill it exactly, which leaves no room for the
two cron services the deployment originally used. Rather than drop the sweeps
or pay for a larger plan, they are scheduled from inside the worker — the one
process that is already long-running, already holds a Redis connection, and is
already the place background work happens. See docs/architecture.md.

Three properties are what make that safe:

**Only one runner.** Every worker replica evaluates the same schedule, so the
decision to run has to be arbitrated somewhere shared. A Redis lock, taken with
`SET NX PX` and released only by the holder, does that. The due-check is
repeated *after* the lock is held, because two workers can both find a sweep due
before either has acquired anything.

**State outside the process.** "Last run" lives in Redis, not in memory, so a
restart or a redeploy does not re-run a sweep that just ran, and does not wait a
full interval before the next one either. Scheduling is a function of the clock
and one Redis key — never of how long this particular process has been up.

**Failure is contained.** A sweep that raises is logged and its lock released;
the next tick re-evaluates from scratch. Nothing propagates to the caller,
because the caller is the worker that is also processing uploads.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis import Redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "ledgerai:maintenance"

# How long a holder may keep the lock. Comfortably longer than a sweep takes,
# short enough that a worker killed mid-sweep does not block the next tick for
# long. The lock is a mutex, not a deadline: it is released in `finally`, and
# this TTL only matters when the process dies without unwinding.
LOCK_TTL_MS = 15 * 60 * 1000

# Release only if we still hold it. A plain DEL would let a worker whose lock
# had already expired delete a lock a *different* worker has since taken.
_RELEASE_IF_OWNED = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


@dataclass(frozen=True, slots=True)
class ScheduledSweep:
    """One periodic job: what to call, how often, and what to call it."""

    name: str
    interval_seconds: int
    run: Callable[[], Mapping[str, object]]

    @property
    def lock_key(self) -> str:
        return f"{KEY_PREFIX}:{self.name}:lock"

    @property
    def last_run_key(self) -> str:
        return f"{KEY_PREFIX}:{self.name}:last_run"


@dataclass(slots=True)
class SweepOutcome:
    """What happened on one evaluation of one sweep.

    `ran` is the only field callers need; the rest exist so tests and logs can
    distinguish "not due yet" from "another worker has it" without inspecting
    Redis themselves.
    """

    name: str
    ran: bool
    reason: str
    report: Mapping[str, object] | None = None
    duration_seconds: float | None = None
    error: str | None = None
    counts: dict[str, int] = field(default_factory=dict)


def _acquire(redis: Redis, key: str, token: str, ttl_ms: int) -> bool:
    return bool(redis.set(key, token, nx=True, px=ttl_ms))


def _release(redis: Redis, key: str, token: str) -> None:
    try:
        redis.eval(_RELEASE_IF_OWNED, 1, key, token)
    except Exception:  # noqa: BLE001 - releasing is best-effort; the TTL backstops it
        logger.warning("maintenance.lock_release_failed job=%s", key)


def _seconds_since_last_run(redis: Redis, sweep: ScheduledSweep, now: float) -> float | None:
    """Age of the recorded last run, or None if it has never run."""
    raw = redis.get(sweep.last_run_key)
    if raw is None:
        return None
    try:
        return now - float(raw)
    except (TypeError, ValueError):
        # A corrupt marker must not wedge the schedule forever. Treat it as
        # never-run: the sweeps are idempotent, so an extra run is harmless.
        logger.warning("maintenance.bad_last_run_marker job=%s", sweep.name)
        return None


def _numeric_counts(report: Mapping[str, object] | None) -> dict[str, int]:
    """Integer fields of a report, and nothing else.

    Reports carry counts plus, for demo cleanup, an `errors` list whose entries
    can name specific records. Only the integers are safe to log, so only the
    integers are extracted — the shape of the log line cannot drift into
    carrying user data as the reports evolve.
    """
    if not report:
        return {}
    counts = {k: v for k, v in report.items() if isinstance(v, int) and not isinstance(v, bool)}
    errors = report.get("errors")
    if isinstance(errors, (list, tuple)):
        counts["errors"] = len(errors)
    return counts


def run_sweep_if_due(
    redis: Redis,
    sweep: ScheduledSweep,
    now: float | None = None,
) -> SweepOutcome:
    """Run `sweep` if it is due and this process wins the lock.

    Never raises. Every exit path is reported through the returned outcome so a
    caller in a worker thread has nothing to catch.
    """
    now = time.time() if now is None else now

    try:
        age = _seconds_since_last_run(redis, sweep, now)
    except Exception as exc:  # noqa: BLE001 - an unreachable store is not this thread's problem
        logger.warning("maintenance.store_unavailable job=%s", sweep.name)
        return SweepOutcome(
            sweep.name, ran=False, reason="store_unavailable", error=type(exc).__name__
        )
    if age is not None and age < sweep.interval_seconds:
        return SweepOutcome(sweep.name, ran=False, reason="not_due")

    token = secrets.token_hex(16)
    try:
        acquired = _acquire(redis, sweep.lock_key, token, LOCK_TTL_MS)
    except Exception as exc:  # noqa: BLE001 - an unreachable Redis is not this thread's problem
        logger.warning("maintenance.lock_unavailable job=%s", sweep.name)
        return SweepOutcome(
            sweep.name, ran=False, reason="lock_unavailable", error=type(exc).__name__
        )

    if not acquired:
        # Another worker is running it right now.
        return SweepOutcome(sweep.name, ran=False, reason="locked")

    try:
        # Re-check under the lock. Two workers can both pass the check above
        # before either acquires; without this the loser would run a second
        # copy the moment the winner released. The marker is read again — that
        # fresh read is the point — but against the same `now` as the first
        # check, so the decision cannot flip on the clock alone.
        try:
            age = _seconds_since_last_run(redis, sweep, now)
        except Exception as exc:  # noqa: BLE001 - same reasoning as the first read
            logger.warning("maintenance.store_unavailable job=%s", sweep.name)
            return SweepOutcome(
                sweep.name, ran=False, reason="store_unavailable", error=type(exc).__name__
            )
        if age is not None and age < sweep.interval_seconds:
            return SweepOutcome(sweep.name, ran=False, reason="not_due")

        started = time.time()
        logger.info("maintenance.sweep_started job=%s started_at=%.0f", sweep.name, started)
        try:
            report = sweep.run()
        except Exception as exc:  # noqa: BLE001 - a failed sweep must not stop the worker
            duration = time.time() - started
            logger.exception(
                "maintenance.sweep_failed job=%s duration_seconds=%.3f error=%s",
                sweep.name,
                duration,
                type(exc).__name__,
            )
            # The marker is deliberately NOT written: a failed sweep stays due,
            # so the next tick retries rather than waiting a full interval.
            return SweepOutcome(
                sweep.name,
                ran=False,
                reason="failed",
                duration_seconds=duration,
                error=type(exc).__name__,
            )

        duration = time.time() - started
        finished = time.time()
        try:
            redis.set(sweep.last_run_key, str(finished))
        except Exception:  # noqa: BLE001 - the run happened; losing the marker only risks a repeat
            logger.warning("maintenance.marker_write_failed job=%s", sweep.name)

        counts = _numeric_counts(report)
        logger.info(
            "maintenance.sweep_completed job=%s duration_seconds=%.3f finished_at=%.0f counts=%s",
            sweep.name,
            duration,
            finished,
            counts,
        )
        return SweepOutcome(
            sweep.name,
            ran=True,
            reason="ran",
            report=report,
            duration_seconds=duration,
            counts=counts,
        )
    finally:
        _release(redis, sweep.lock_key, token)


def run_due_sweeps(
    redis: Redis,
    sweeps: list[ScheduledSweep],
    now: float | None = None,
) -> list[SweepOutcome]:
    """Evaluate every sweep once. One failure never skips the others."""
    outcomes: list[SweepOutcome] = []
    for sweep in sweeps:
        try:
            outcomes.append(run_sweep_if_due(redis, sweep, now=now))
        except Exception as exc:  # noqa: BLE001 - defence in depth; run_sweep_if_due already catches
            logger.exception("maintenance.evaluation_failed job=%s", sweep.name)
            outcomes.append(
                SweepOutcome(sweep.name, ran=False, reason="failed", error=type(exc).__name__)
            )
    return outcomes


def _demo_cleanup() -> Mapping[str, object]:
    from ..jobs.demo_cleanup import run_demo_cleanup

    return run_demo_cleanup()


def _retention_sweep() -> Mapping[str, object]:
    from ..jobs.retention import run_retention_sweep

    return run_retention_sweep()


def _account_reconcile() -> Mapping[str, object]:
    from ..jobs.account_reconcile import run_account_reconcile

    return run_account_reconcile()


# The two schedules the cron services used to hold, unchanged in cadence.
# Demo accounts expire on a 24-hour clock, so an hourly sweep bounds how long an
# expired one lingers; retention works on 7- and 30-day windows, where daily is
# ample.
DEMO_CLEANUP = ScheduledSweep("demo-cleanup", interval_seconds=3600, run=_demo_cleanup)
RETENTION = ScheduledSweep("retention", interval_seconds=86_400, run=_retention_sweep)

# Deletion is the one sweep a user is actively waiting on, and an interrupted
# one leaves their data in place. Five minutes, so a failed step is retried
# soon rather than at the next daily tick.
ACCOUNT_RECONCILE = ScheduledSweep(
    "account-reconcile", interval_seconds=300, run=_account_reconcile
)


def default_sweeps() -> list[ScheduledSweep]:
    return [DEMO_CLEANUP, RETENTION, ACCOUNT_RECONCILE]
