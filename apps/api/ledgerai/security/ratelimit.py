"""Redis fixed-window rate limiting.

Hand-rolled rather than pulled from a library: the whole mechanism is a counter
with an expiry, Redis is already a dependency, and a limiter is small enough
that a package would cost more in supply chain than it saves in code.

Failure behaviour is deliberately asymmetric, because "fail open" and "fail
closed" are each wrong in one of the two situations this limiter runs in.

* **Development and tests, and authenticated non-public endpoints anywhere:**
  fail **open**. Rate limiting is abuse control, not authorization. Locking a
  user out of their own data because Redis blipped is a worse outcome than
  briefly not counting their requests, and a developer whose Redis is down
  should still be able to work.

* **Public, abuse-sensitive endpoints in production:** fail **closed**. Login,
  demo-session provisioning, uploads and analysis are the surfaces an anonymous
  attacker reaches, and they are exactly the ones where an unmetered request is
  the thing being defended against. If the counter cannot run, the request is
  refused. An attacker who can knock Redis over must not thereby win unlimited
  credential-stuffing attempts.

Refusals are a generic 503. The caller is told to try again shortly and nothing
else — which dependency failed is an internal detail and a hint about what to
attack next.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis

from ..config import settings

logger = logging.getLogger(__name__)

_client: Redis | None = None


def get_limiter_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def reset_limiter(client: Redis | None = None) -> None:
    """Inject a client, or clear the cached one. Used by tests."""
    global _client
    _client = client


@dataclass(slots=True, frozen=True)
class RateLimit:
    """A budget of `times` requests per `seconds`.

    `public` marks the abuse-sensitive surfaces an unauthenticated or
    cheaply-obtained caller can reach. Those fail closed in production when the
    store is unavailable; everything else fails open. See the module docstring.
    """

    name: str
    times: int
    seconds: int
    public: bool = False

    @property
    def retry_after(self) -> int:
        return self.seconds


# Named so a test can assert the boundary rather than a magic number, and so
# the values are reviewable in one place.
LOGIN_LIMIT = RateLimit("login", times=10, seconds=300, public=True)
UPLOAD_LIMIT = RateLimit("upload", times=30, seconds=3600, public=True)
ANALYSIS_LIMIT = RateLimit("analysis", times=60, seconds=3600, public=True)

# Demo-session provisioning is unauthenticated by definition, so its budget is
# declared with the policy already attached. The endpoint itself is Checkpoint B
# work; this constant exists so that endpoint cannot be added without it.
DEMO_SESSION_LIMIT = RateLimit("demo-session", times=5, seconds=3600, public=True)

# Authenticated, self-directed operations on the caller's own data. Failing
# these closed would deny a user their own export or deletion during an outage,
# which protects nobody.
EXPORT_LIMIT = RateLimit("export", times=5, seconds=3600)
DESTRUCTIVE_LIMIT = RateLimit("destructive", times=5, seconds=3600)


def client_identifier(request: Request) -> str:
    """Best-effort caller identity.

    Behind a proxy the socket address is the proxy, so the first hop of
    X-Forwarded-For is used when the deployment says it is trusted. Left
    untrusted by default: an attacker can otherwise forge the header and
    sidestep the limit entirely.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def check_rate_limit(key: str, limit: RateLimit) -> tuple[bool, int, bool]:
    """Consume one unit. Returns (allowed, remaining, store_available).

    Fixed window: the counter is created with a TTL on first use and simply
    incremented afterwards. Less precise than a sliding window at the boundary,
    and entirely adequate for abuse control.

    The third element is what lets the caller apply the right failure policy.
    This function does not decide it: it reports whether the count is real.
    """
    redis_key = f"ratelimit:{limit.name}:{key}"
    try:
        client = get_limiter_redis()
        current = await client.incr(redis_key)
        if current == 1:
            await client.expire(redis_key, limit.seconds)
        remaining = max(0, limit.times - int(current))
        return int(current) <= limit.times, remaining, True
    except Exception:  # noqa: BLE001 - the policy lives in enforce()
        # No exception text: it carries connection strings and host names.
        logger.warning("Rate limiter store unavailable for limit '%s'", limit.name)
        return True, limit.times, False


def fails_closed(limit: RateLimit) -> bool:
    """Whether an unavailable store should refuse this limit's requests."""
    return limit.public and settings.is_production


async def probe_limiter_store() -> bool:
    """Whether the rate-limit store answers right now. Used by /health."""
    try:
        return bool(await get_limiter_redis().ping())
    except Exception:  # noqa: BLE001 - an unreachable store is the answer
        return False


async def enforce(request: Request, limit: RateLimit, key: str | None = None) -> None:
    """Apply the budget, raising 429 over it and 503 when it cannot be applied."""
    identifier = key or client_identifier(request)
    allowed, _remaining, store_available = await check_rate_limit(identifier, limit)

    if not store_available:
        if fails_closed(limit):
            logger.error(
                "Refusing '%s' request: rate-limit store unavailable and the "
                "limit is enforced in production",
                limit.name,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service temporarily unavailable. Please try again shortly.",
                headers={"Retry-After": "30"},
            )
        return

    if allowed:
        return

    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Please wait a moment and try again.",
        headers={"Retry-After": str(limit.retry_after)},
    )
