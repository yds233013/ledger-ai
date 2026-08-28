"""The worker entry point: RQ consumer plus the maintenance scheduler.

`rq worker ledgerai` remains a perfectly good way to consume the queue, and the
Dockerfile used it for a long time. What it cannot do is run the periodic
sweeps, which used to be two separate Railway cron services. Railway's Hobby
plan caps a project at five services — Postgres, Redis, api, worker, web — so
those two services had nowhere to live. This entry point puts the schedule in
the process that was already long-running.

The division of labour is deliberate:

* RQ owns the main thread. `Worker.work()` installs the signal handlers that
  give RQ its warm shutdown, so SIGTERM still finishes the job in flight rather
  than abandoning a half-processed receipt.
* The scheduler owns a daemon thread. It never touches the queue, never raises
  into the worker, and holds a Redis lock for the seconds it is actually
  sweeping.

Nothing about upload processing changes. If the scheduler thread died outright
the worker would carry on consuming jobs.
"""

from __future__ import annotations

import logging
import sys

from .jobs.queue import QUEUE_NAME, get_redis
from .maintenance import MaintenanceScheduler
from .security.logging import install_redaction

logger = logging.getLogger("ledgerai.worker")


def main() -> int:
    logging.basicConfig(level="INFO", format="%(levelname)-5s %(message)s")
    install_redaction()

    from rq import Queue, Worker

    connection = get_redis()
    scheduler = MaintenanceScheduler(redis_factory=get_redis)
    scheduler.start()

    try:
        worker = Worker(
            [Queue(QUEUE_NAME, connection=connection)],
            connection=connection,
        )
        logger.info("worker.starting queue=%s", QUEUE_NAME)
        # Blocks until SIGTERM/SIGINT, running RQ's own warm shutdown.
        worker.work(with_scheduler=False)
    finally:
        # Best-effort: the thread is a daemon, so a hard exit would drop it
        # anyway, but stopping cleanly keeps the shutdown logs honest.
        scheduler.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
