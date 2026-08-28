"""The maintenance schedule that replaced the two cron services.

Railway's Hobby plan caps a project at five services, which Postgres, Redis, the
API, the worker and the web front end fill exactly. The sweeps moved into the
worker rather than being dropped, and everything that used to be guaranteed by
"one cron service, one run" now has to be guaranteed by code:

  * only one worker runs a sweep, even with several replicas ticking together
  * a restart or redeploy does not re-run something that just ran
  * a sweep that fails does not stop the worker, and does not wait a whole
    interval before being retried

These run against the real Redis the rest of the suite uses. A fake would prove
the code calls the methods it calls; the point here is that SET NX and the
compare-and-delete release behave the way the design assumes.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from redis import Redis

from ledgerai.config import settings
from ledgerai.maintenance import schedule as sched
from ledgerai.maintenance.schedule import (
    ScheduledSweep,
    run_due_sweeps,
    run_sweep_if_due,
)
from ledgerai.maintenance.supervisor import MaintenanceScheduler


@pytest.fixture
def redis() -> Redis:
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    client.ping()
    return client


@pytest.fixture
def sweep_name() -> str:
    """A unique name per test, so concurrent runs never share Redis keys."""
    return f"test-sweep-{uuid.uuid4().hex[:10]}"


def make_sweep(name: str, interval: int = 3600, run=None) -> ScheduledSweep:
    return ScheduledSweep(name, interval_seconds=interval, run=run or (lambda: {"removed": 0}))


@pytest.fixture(autouse=True)
def _cleanup(redis: Redis, sweep_name: str):
    yield
    redis.delete(
        f"{sched.KEY_PREFIX}:{sweep_name}:lock",
        f"{sched.KEY_PREFIX}:{sweep_name}:last_run",
    )


class TestScheduling:
    def test_a_sweep_that_has_never_run_is_due(self, redis, sweep_name) -> None:
        calls = []
        sweep = make_sweep(sweep_name, run=lambda: calls.append(1) or {"removed": 3})

        outcome = run_sweep_if_due(redis, sweep)

        assert outcome.ran is True
        assert outcome.reason == "ran"
        assert calls == [1]

    def test_a_sweep_inside_its_interval_is_skipped(self, redis, sweep_name) -> None:
        calls = []
        sweep = make_sweep(sweep_name, interval=3600, run=lambda: calls.append(1) or {})

        run_sweep_if_due(redis, sweep)
        second = run_sweep_if_due(redis, sweep)

        assert second.ran is False
        assert second.reason == "not_due"
        assert calls == [1], "the sweep ran twice inside one interval"

    def test_a_sweep_past_its_interval_runs_again(self, redis, sweep_name) -> None:
        calls = []
        sweep = make_sweep(sweep_name, interval=60, run=lambda: calls.append(1) or {})

        run_sweep_if_due(redis, sweep)
        # Evaluate as if an hour has passed, rather than sleeping.
        later = run_sweep_if_due(redis, sweep, now=time.time() + 3601)

        assert later.ran is True
        assert calls == [1, 1]

    def test_the_shipped_schedule_is_hourly_and_daily(self) -> None:
        """The cadence the cron services had, preserved."""
        assert sched.DEMO_CLEANUP.interval_seconds == 3600
        assert sched.RETENTION.interval_seconds == 86_400
        assert {s.name for s in sched.default_sweeps()} == {"demo-cleanup", "retention"}

    def test_the_shipped_sweeps_call_the_existing_jobs(self) -> None:
        """The business logic must be the same code the cron services ran."""
        import inspect

        assert "run_demo_cleanup" in inspect.getsource(sched._demo_cleanup)
        assert "run_retention_sweep" in inspect.getsource(sched._retention_sweep)


class TestOnlyOneRunnerAtATime:
    def test_a_held_lock_blocks_a_second_worker(self, redis, sweep_name) -> None:
        calls = []
        sweep = make_sweep(sweep_name, run=lambda: calls.append(1) or {})

        # Stand in for another worker mid-sweep.
        redis.set(sweep.lock_key, "another-worker", px=60_000)
        outcome = run_sweep_if_due(redis, sweep)

        assert outcome.ran is False
        assert outcome.reason == "locked"
        assert calls == []

    def test_the_lock_is_released_after_a_successful_run(self, redis, sweep_name) -> None:
        sweep = make_sweep(sweep_name)
        run_sweep_if_due(redis, sweep)
        assert redis.get(sweep.lock_key) is None

    def test_the_lock_is_released_after_a_failing_run(self, redis, sweep_name) -> None:
        """A crashed sweep must not wedge the schedule until the TTL expires."""

        def explode():
            raise RuntimeError("sweep blew up")

        sweep = make_sweep(sweep_name, run=explode)
        run_sweep_if_due(redis, sweep)
        assert redis.get(sweep.lock_key) is None

    def test_a_worker_cannot_release_a_lock_it_does_not_hold(self, redis, sweep_name) -> None:
        """The compare-and-delete guard.

        A plain DEL would let a worker whose own lock had expired delete the
        lock a different worker has since taken, and two sweeps would overlap.
        """
        sweep = make_sweep(sweep_name)
        redis.set(sweep.lock_key, "owned-by-someone-else", px=60_000)

        sched._release(redis, sweep.lock_key, "a-different-token")

        assert redis.get(sweep.lock_key) == "owned-by-someone-else"

    def test_concurrent_workers_run_it_exactly_once(self, redis, sweep_name) -> None:
        """Eight threads evaluate the same due sweep simultaneously."""
        calls: list[int] = []
        barrier = threading.Barrier(8)
        lock = threading.Lock()

        def run_body():
            time.sleep(0.05)  # widen the window a duplicate could slip through
            with lock:
                calls.append(1)
            return {"removed": 1}

        sweep = make_sweep(sweep_name, run=run_body)
        outcomes: list = []

        def worker():
            barrier.wait()
            outcomes.append(run_sweep_if_due(redis, sweep))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(calls) == 1, f"sweep ran {len(calls)} times across 8 workers"
        assert sum(1 for o in outcomes if o.ran) == 1
        assert {o.reason for o in outcomes if not o.ran} <= {"locked", "not_due"}


class TestFailureRecovery:
    def test_a_failing_sweep_does_not_raise(self, redis, sweep_name) -> None:
        """The caller is the worker thread. Nothing may propagate to it."""

        def explode():
            raise RuntimeError("database on fire")

        outcome = run_sweep_if_due(redis, make_sweep(sweep_name, run=explode))

        assert outcome.ran is False
        assert outcome.reason == "failed"
        assert outcome.error == "RuntimeError"

    def test_a_failure_stays_due_so_the_next_tick_retries(self, redis, sweep_name) -> None:
        """No last-run marker is written on failure.

        Writing one would mean a transient database blip cost a full interval —
        a whole day, for retention.
        """
        state = {"fail": True}

        def flaky():
            if state["fail"]:
                raise RuntimeError("transient")
            return {"removed": 2}

        sweep = make_sweep(sweep_name, run=flaky)

        assert run_sweep_if_due(redis, sweep).reason == "failed"
        assert redis.get(sweep.last_run_key) is None

        state["fail"] = False
        second = run_sweep_if_due(redis, sweep)
        assert second.ran is True
        assert redis.get(sweep.last_run_key) is not None

    def test_one_failing_sweep_does_not_skip_the_others(self, redis) -> None:
        ran: list[str] = []
        def boom():
            raise RuntimeError("boom")

        a = make_sweep(f"a-{uuid.uuid4().hex[:8]}", run=boom)
        b = make_sweep(f"b-{uuid.uuid4().hex[:8]}", run=lambda: ran.append("b") or {})

        outcomes = run_due_sweeps(redis, [a, b])

        assert ran == ["b"]
        assert [o.reason for o in outcomes] == ["failed", "ran"]
        for s in (a, b):
            redis.delete(s.lock_key, s.last_run_key)

    def test_an_unreachable_redis_is_reported_not_raised(self, sweep_name) -> None:
        dead = Redis.from_url("redis://127.0.0.1:6389/0", socket_connect_timeout=0.2)
        outcome = run_sweep_if_due(dead, make_sweep(sweep_name))
        assert outcome.ran is False
        assert outcome.reason in {"store_unavailable", "lock_unavailable", "failed"}
        assert outcome.error is not None

    def test_a_corrupt_last_run_marker_does_not_wedge_the_schedule(
        self, redis, sweep_name
    ) -> None:
        calls = []
        sweep = make_sweep(sweep_name, run=lambda: calls.append(1) or {})
        redis.set(sweep.last_run_key, "not-a-timestamp")

        assert run_sweep_if_due(redis, sweep).ran is True
        assert calls == [1]


class TestRestartBehaviour:
    def test_state_survives_a_restart_so_a_sweep_is_not_repeated(
        self, redis, sweep_name
    ) -> None:
        """Scheduling is a function of Redis and the clock, not process uptime.

        A redeploy restarts every replica at once. If "last run" lived in
        memory, every one of them would sweep on boot.
        """
        calls = []
        sweep = make_sweep(sweep_name, interval=3600, run=lambda: calls.append(1) or {})

        run_sweep_if_due(redis, sweep)  # "old process"
        # A brand-new process: fresh objects, fresh connection, same Redis.
        fresh = Redis.from_url(settings.redis_url, decode_responses=True)
        after_restart = run_sweep_if_due(fresh, make_sweep(sweep_name, interval=3600,
                                                           run=lambda: calls.append(1) or {}))

        assert after_restart.ran is False
        assert after_restart.reason == "not_due"
        assert calls == [1]

    def test_a_restart_does_not_reset_the_interval_clock(self, redis, sweep_name) -> None:
        """The next run is due relative to the last run, not to boot time."""
        sweep = make_sweep(sweep_name, interval=600)
        run_sweep_if_due(redis, sweep)
        recorded = float(redis.get(sweep.last_run_key))

        # A new process evaluating 11 minutes after the LAST RUN finds it due.
        assert run_sweep_if_due(redis, sweep, now=recorded + 601).ran is True


class TestSupervisor:
    def test_a_tick_never_raises_even_with_a_dead_redis(self) -> None:
        def dead_factory():
            raise ConnectionError("no redis")

        MaintenanceScheduler(redis_factory=dead_factory).tick()  # must not raise

    def test_a_tick_never_raises_when_a_sweep_explodes(self, redis, sweep_name) -> None:
        sweep = make_sweep(sweep_name, run=lambda: (_ for _ in ()).throw(RuntimeError()))
        MaintenanceScheduler(redis_factory=lambda: redis, sweeps=[sweep]).tick()

    def test_start_and_stop_are_clean(self, redis, sweep_name) -> None:
        scheduler = MaintenanceScheduler(
            redis_factory=lambda: redis,
            sweeps=[make_sweep(sweep_name)],
            tick_seconds=0.05,
        )
        scheduler.start()
        assert scheduler.running is True
        time.sleep(0.2)
        scheduler.stop(timeout=2)
        assert scheduler.running is False

    def test_the_thread_is_a_daemon_so_it_cannot_block_shutdown(
        self, redis, sweep_name
    ) -> None:
        scheduler = MaintenanceScheduler(
            redis_factory=lambda: redis,
            sweeps=[make_sweep(sweep_name)],
            tick_seconds=0.05,
        )
        scheduler.start()
        try:
            assert scheduler._thread is not None
            assert scheduler._thread.daemon is True
        finally:
            scheduler.stop(timeout=2)

    def test_starting_twice_is_refused(self, redis, sweep_name) -> None:
        scheduler = MaintenanceScheduler(
            redis_factory=lambda: redis,
            sweeps=[make_sweep(sweep_name)],
            tick_seconds=0.05,
        )
        scheduler.start()
        try:
            with pytest.raises(RuntimeError):
                scheduler.start()
        finally:
            scheduler.stop(timeout=2)

    def test_the_loop_actually_runs_the_sweep(self, redis, sweep_name) -> None:
        calls = []
        sweep = make_sweep(sweep_name, run=lambda: calls.append(1) or {"removed": 0})
        scheduler = MaintenanceScheduler(
            redis_factory=lambda: redis, sweeps=[sweep], tick_seconds=0.05
        )
        scheduler.start()
        try:
            deadline = time.time() + 5
            while not calls and time.time() < deadline:
                time.sleep(0.05)
        finally:
            scheduler.stop(timeout=2)
        assert calls == [1]


class TestLogHygiene:
    def test_only_integer_counts_are_extracted_for_logging(self) -> None:
        """Reports carry an `errors` list whose entries can name records.

        Only its length may be logged, and only integer fields alongside it —
        so the log line cannot start carrying user data as reports evolve.
        """
        report = {
            "users_removed": 2,
            "storage_objects_removed": 7,
            "errors": ["user 41f2 failed: merchant Blue Bottle Coffee"],
            "note": "some free text",
            "flag": True,
        }
        counts = sched._numeric_counts(report)

        assert counts == {"users_removed": 2, "storage_objects_removed": 7, "errors": 1}
        rendered = str(counts)
        assert "Blue Bottle" not in rendered
        assert "some free text" not in rendered
        assert "flag" not in counts, "booleans are not counts"

    def test_a_completed_sweep_logs_counts_and_no_payload(
        self, redis, sweep_name, caplog
    ) -> None:
        sweep = make_sweep(
            sweep_name,
            run=lambda: {"users_removed": 1, "errors": ["merchant Whole Foods exploded"]},
        )
        with caplog.at_level("INFO"):
            run_sweep_if_due(redis, sweep)

        assert "maintenance.sweep_completed" in caplog.text
        assert f"job={sweep_name}" in caplog.text
        assert "users_removed" in caplog.text
        assert "Whole Foods" not in caplog.text
