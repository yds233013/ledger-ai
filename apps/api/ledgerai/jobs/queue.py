"""RQ wiring.

RQ over Celery, deliberately: Redis is already a dependency for the analysis
cache, the pipeline is a single linear function rather than a routing graph,
and `job.meta` maps one-to-one onto the stage/progress model the upload UI
renders. Celery's strengths (multi-broker, chords, complex routing) buy us
nothing here and cost a large configuration surface.
"""

from __future__ import annotations

from functools import lru_cache

from redis import Redis
from rq import Queue

from ..config import settings

QUEUE_NAME = "ledgerai"
JOB_TIMEOUT = 600  # seconds; a 20k-row CSV finishes in well under a minute
RESULT_TTL = 86_400


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(settings.redis_url)


@lru_cache
def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=get_redis(), default_timeout=JOB_TIMEOUT)
