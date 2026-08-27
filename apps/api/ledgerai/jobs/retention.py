"""Scheduled cleanup of what processing leaves behind.

Runs as an RQ job in production (cron-triggered) and via `make sweep` locally.
Every step is idempotent, so running it twice, or concurrently with normal
traffic, is safe.
"""

from __future__ import annotations

import logging

from ..db import sync_session
from ..services.lifecycle import retention_sweep

logger = logging.getLogger(__name__)


def run_retention_sweep() -> dict[str, int]:
    """RQ entry point. Returns counts, never raises into the queue."""
    with sync_session() as session:
        report = retention_sweep(session)
    return report.as_dict()
