"""Analysis result caching.

The cache key folds in the user's data watermark (latest transaction
updated_at + row count). Editing a category therefore invalidates every cached
answer for that user automatically — there is no manual cache-busting anywhere
in this codebase, and no way to serve a stale number after a correction.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from redis.asyncio import Redis

from ...config import settings

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")

_client: Redis | None = None


def get_async_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def normalize_question(question: str) -> str:
    """So "How much did I spend?" and "how much did i spend" share a cache slot."""
    text = _PUNCT.sub(" ", question.lower())
    return _WHITESPACE.sub(" ", text).strip()


def build_cache_key(user_id: uuid.UUID, normalized_question: str, watermark: str) -> str:
    digest = hashlib.sha256(
        f"{user_id}|{normalized_question}|{watermark}".encode()
    ).hexdigest()
    return digest[:64]


async def lookup_run_id(cache_key: str) -> str | None:
    try:
        value = await get_async_redis().get(f"analysis:{cache_key}")
        return value if isinstance(value, str) else None
    except Exception:  # noqa: BLE001 - a cache outage must never fail an analysis
        return None


async def store_run_id(
    cache_key: str, run_id: uuid.UUID | str, user_id: uuid.UUID | None = None
) -> None:
    """Cache the run id, and remember the key so it can be purged precisely.

    The cache key is a digest, so nothing about it identifies its owner. Without
    the index below, deleting an account could only wait for the TTL — this way
    deletion actually removes the entries.
    """
    try:
        client = get_async_redis()
        key = f"analysis:{cache_key}"
        await client.setex(key, settings.analysis_cache_ttl_seconds, str(run_id))
        if user_id is not None:
            index = user_index_key(user_id)
            await client.sadd(index, key)
            # The index must not outlive the entries it points at.
            await client.expire(index, settings.analysis_cache_ttl_seconds * 24)
    except Exception:  # noqa: BLE001
        return


def user_index_key(user_id: uuid.UUID | str) -> str:
    return f"analysis-keys:{user_id}"


async def purge_user_cache(user_id: uuid.UUID) -> int:
    """Remove every cached analysis belonging to one user.

    Returns how many keys were removed. A cache outage must not block a
    deletion, so failures are swallowed and reported as zero.
    """
    try:
        client = get_async_redis()
        index = user_index_key(user_id)
        keys = await client.smembers(index)
        removed = 0
        if keys:
            removed = await client.delete(*keys)
        await client.delete(index)
        return int(removed)
    except Exception:  # noqa: BLE001
        return 0
