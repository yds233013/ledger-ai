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

import hashlib
import logging
import secrets
from dataclasses import dataclass
from ipaddress import ip_address, ip_network

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis

from ..config import settings

logger = logging.getLogger(__name__)

# The only ranges an address may be logged verbatim from: machines we or the
# platform run. Listed explicitly rather than using `ipaddress.is_private`,
# which also covers RFC 5737 documentation space (203.0.113.0/24 and friends),
# reserved space and 0.0.0.0/8 — none of which are our infrastructure, and all
# of which would then be written out in full. Anything not listed here is
# treated as a visitor address and only ever hashed, so the failure mode of an
# unfamiliar range is to over-redact.
_INFRASTRUCTURE_NETWORKS = (
    ip_network("100.64.0.0/10"),   # RFC 6598 CGNAT — Railway's inbound hops
    ip_network("10.0.0.0/8"),      # RFC 1918
    ip_network("172.16.0.0/12"),   # RFC 1918
    ip_network("192.168.0.0/16"),  # RFC 1918
    ip_network("fc00::/7"),        # IPv6 unique local
)

_INFRASTRUCTURE_CLASS = {
    ip_network("100.64.0.0/10"): "cgnat",
    ip_network("10.0.0.0/8"): "private",
    ip_network("172.16.0.0/12"): "private",
    ip_network("192.168.0.0/16"): "private",
    ip_network("fc00::/7"): "private",
}

# Per-process, never persisted. Public addresses are only ever logged as a
# digest under this salt, so the digests cannot be correlated across restarts
# or reversed to an address.
_HASH_SALT = secrets.token_bytes(16)

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


# Logged at most once per process. A per-request warning would be a log flood
# on exactly the deployment that is already misconfigured.
_warned_about_untrusted_proxy = False


def classify_address(candidate: str) -> str:
    """Which kind of address this is, without saying which address it is.

    The classes that matter here are the ones that tell you whether a hop is
    infrastructure or a person. Anything in the first four is a machine we or
    the platform runs; `public` is somebody's actual internet address.
    """
    try:
        parsed = ip_address(candidate)
    except ValueError:
        return "invalid"
    if parsed.is_loopback:
        return "loopback"
    if parsed.is_link_local:
        return "link_local"
    for network in _INFRASTRUCTURE_NETWORKS:
        if parsed.version == network.version and parsed in network:
            return _INFRASTRUCTURE_CLASS[network]
    # Everything else — including documentation and reserved space — is treated
    # as a visitor address and hashed. Over-redacting an odd range is the safe
    # direction; writing out somebody's address is not.
    return "public"


def sanitize_address(candidate: str) -> str:
    """An address safe to write to a log.

    Infrastructure addresses are recorded exactly — they are the values
    TRUSTED_PROXY_IPS needs, and they identify a machine rather than a person.
    A public address is a visitor's, so only its class and a short salted digest
    are recorded: enough to tell two visitors apart within one log, useless for
    identifying either. The salt is per-process and never persisted, so the
    digests cannot be correlated across restarts or back to an address.
    """
    kind = classify_address(candidate)
    if kind in {"loopback", "link_local", "cgnat", "private"}:
        return f"{kind}:{candidate}"
    if kind == "invalid":
        return "invalid"
    digest = hashlib.sha256(_HASH_SALT + candidate.encode()).hexdigest()[:8]
    return f"public:sha256-{digest}"


def describe_chain(peer: str, forwarded_header: str) -> dict[str, object]:
    """A sanitized account of one forwarded chain and how trust reads it.

    Everything here is either an infrastructure address, a class name, a
    boolean or a hash. No header, token, cookie or full public address is
    included, and the chain's own contents are only ever rendered through
    `sanitize_address`.
    """
    hops = [part.strip() for part in forwarded_header.split(",") if part.strip()]
    return {
        "peer": sanitize_address(peer),
        "peer_class": classify_address(peer),
        "peer_trusted": _is_trusted_proxy(peer),
        "hop_count": len(hops),
        # Position 1 is leftmost, as the header is written.
        "hops": [
            {
                "position": index,
                "class": classify_address(hop),
                "value": sanitize_address(hop),
                "trusted": _is_trusted_proxy(hop),
            }
            for index, hop in enumerate(hops, start=1)
        ],
        "resolved_identity": sanitize_address(resolve_client_ip(peer, forwarded_header)),
        "trust_active": settings.proxy_trust_active,
    }


def _warn_once_about_untrusted_proxy(peer: str, forwarded_header: str) -> None:
    """Report a proxy in front of us that we are not configured to trust.

    Existence of a forwarded chain we are ignoring means every caller behind
    that proxy shares one rate-limit bucket — the limits still hold, but they
    hold collectively, which for the public demo endpoint means the whole
    internet shares one budget.

    This exists because no hosting provider used by this project publishes a
    stable, authoritative CIDR for its inbound edge, and guessing one is worse
    than not setting it. The addresses logged here are infrastructure, observed
    from the deployment itself, and they are what TRUSTED_PROXY_IPS wants.

    Emitted once per process: the condition is a property of the deployment,
    not of a request, and repeating it per request would flood the log of the
    very deployment that is already misconfigured.
    """
    global _warned_about_untrusted_proxy
    if _warned_about_untrusted_proxy:
        return
    _warned_about_untrusted_proxy = True

    chain = describe_chain(peer, forwarded_header)
    logger.warning(
        "proxy.untrusted_chain peer=%s peer_class=%s hop_count=%s hops=%s "
        "resolved_identity=%s trust_active=%s — rate limits are keyed by the "
        "peer, so every caller behind it shares one budget. Set "
        "TRUST_PROXY_HEADERS=true and TRUSTED_PROXY_IPS from the infrastructure "
        "addresses above. Do not guess the range.",
        chain["peer"],
        chain["peer_class"],
        chain["hop_count"],
        chain["hops"],
        chain["resolved_identity"],
        chain["trust_active"],
    )


def reset_proxy_warning() -> None:
    """Re-arm the once-per-process warning. Used by tests."""
    global _warned_about_untrusted_proxy
    _warned_about_untrusted_proxy = False


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

    forwarded_header = request.headers.get("x-forwarded-for", "")
    # A forwarded chain we are not configured to look at is the signature of a
    # deployment whose limits have silently collapsed to a single bucket. Say
    # so once, with the address needed to fix it.
    if forwarded_header and not _is_trusted_proxy(peer):
        _warn_once_about_untrusted_proxy(peer, forwarded_header)

    return resolve_client_ip(peer, forwarded_header)


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
