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
from ipaddress import ip_address

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


def _is_trusted_proxy(candidate: str) -> bool:
    """Whether an address belongs to a configured reverse proxy."""
    if not settings.proxy_trust_active:
        return False
    try:
        parsed = ip_address(candidate)
    except ValueError:
        return False
    return any(parsed in network for network in settings.trusted_proxy_networks)


def resolve_client_ip(peer: str, forwarded_header: str) -> str:
    """The caller's address, given the socket peer and X-Forwarded-For.

    The header is a chain, appended to left-to-right, so the rightmost entries
    are the ones added by infrastructure closest to us and the leftmost is
    whatever the original client claimed. Walking from the right and stopping
    at the first hop that is NOT a configured proxy yields the address of the
    last party we can actually vouch for.

    Taking the *leftmost* entry instead — the obvious reading, and the one the
    previous implementation used — is the whole vulnerability: that value is
    supplied by the caller, so rotating it hands out a fresh rate-limit bucket
    per request.

    Returns `peer` unchanged whenever the header cannot be believed: no trusted
    proxy configured, the peer is not one of them, or every hop in the chain is
    a proxy (leaving no client address to attribute the request to).
    """
    if not _is_trusted_proxy(peer):
        return peer

    for hop in reversed([part.strip() for part in forwarded_header.split(",")]):
        if not hop or _is_trusted_proxy(hop):
            continue
        try:
            # A hop that is not a valid address is not evidence of anything.
            return str(ip_address(hop))
        except ValueError:
            return peer
    return peer


def client_identifier(request: Request) -> str:
    """Best-effort caller identity.

    Behind a proxy the socket address is the proxy, so the forwarded chain is
    consulted — but only when the socket peer is itself a configured proxy.
    Untrusted by default: uvicorn is started WITHOUT --proxy-headers so
    `request.client.host` is always the real TCP peer, and this function is the
    single place that decides whether to look past it.
    """
    peer = request.client.host if request.client else ""
    if not peer:
        return "unknown"
    return resolve_client_ip(peer, request.headers.get("x-forwarded-for", ""))


# INCR and EXPIRE as two round-trips is the classic broken limiter: anything
# that interrupts the process between them (a client disconnect cancelling the
# task, SIGTERM during a rolling deploy, a connection reset) leaves a counter
# with no TTL. Nothing re-arms it afterwards, because the "first request"
# branch never runs again, so the count climbs forever and that identifier is
# locked out permanently — a Redis blip turned into a durable denial of service
# against a legitimate user.
#
# One EVAL is genuinely atomic: Redis runs the whole script without
# interleaving another command, so the counter and its expiry are created
# together or not at all. The PTTL branch additionally repairs a key that
# already lost its TTL, so a counter stranded by an older build heals on its
# next use instead of needing an operator to DEL it.
_INCR_WITH_TTL = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
else
  if redis.call('PTTL', KEYS[1]) < 0 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
  end
end
return current
"""


async def check_rate_limit(key: str, limit: RateLimit) -> tuple[bool, int, bool]:
    """Consume one unit. Returns (allowed, remaining, store_available).

    Fixed window: the counter is created with its TTL in a single atomic step
    and incremented afterwards, the window ending when that TTL expires. Less
    precise than a sliding window at the boundary, and entirely adequate for
    abuse control.

    The third element is what lets the caller apply the right failure policy.
    This function does not decide it: it reports whether the count is real.
    """
    redis_key = f"ratelimit:{limit.name}:{key}"
    try:
        client = get_limiter_redis()
        # eval() ships the script each call rather than EVALSHA + NOSCRIPT
        # recovery. The script is a few hundred bytes against a local Redis,
        # and one code path is worth more here than the saved bandwidth.
        current = int(await client.eval(_INCR_WITH_TTL, 1, redis_key, limit.seconds * 1000))
        remaining = max(0, limit.times - current)
        return current <= limit.times, remaining, True
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
