"""Scheduled removal of expired demo accounts.

Runs as an RQ job in production (cron-triggered, alongside the retention
sweep) and via `make demo-sweep` locally. Idempotent, and safe to run
concurrently with live traffic: each account is removed independently and the
selection predicate cannot reach a real account.
"""

from __future__ import annotations

import logging

from ..db import sync_session
from ..services.demo import cleanup_expired_demo_users

logger = logging.getLogger(__name__)


def run_demo_cleanup() -> dict[str, object]:
    """RQ entry point. Returns counts, never raises into the queue."""
    with sync_session() as session:
        report = cleanup_expired_demo_users(session)
    return report.as_dict()
