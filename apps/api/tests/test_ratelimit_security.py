"""Rate limiter security properties.

Two independent failures were found by review and are pinned here:

  1. A forged X-Forwarded-For rotated the rate-limit identity, so the login
     budget could be sidestepped entirely by varying one header.
  2. INCR and EXPIRE ran as separate round-trips, so an interruption between
     them could strand a counter with no TTL — permanently locking out whoever
     that key identified.

Both are properties of the limiter itself rather than of any one endpoint, so
they are tested directly against `resolve_client_ip` and `check_rate_limit`
rather than through a route.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import Request

from ledgerai.config import settings
from ledgerai.security import ratelimit
from ledgerai.security.ratelimit import (
    LOGIN_LIMIT,
    RateLimit,
    check_rate_limit,
    client_identifier,
    get_limiter_redis,
    resolve_client_ip,
)


@pytest.fixture(autouse=True)
def _clean_limiter():
    """Every test starts from a real, empty limiter."""
    ratelimit.reset_limiter(None)
    yield
    ratelimit.reset_limiter(None)


@pytest.fixture
def trust_proxy(monkeypatch):
    """Turn on proxy trust for a specific, narrow allow-list."""

    def _configure(cidrs: str = "10.0.0.0/8") -> None:
        monkeypatch.setattr(settings, "trust_proxy_headers", True, raising=False)
        monkeypatch.setattr(settings, "trusted_proxy_ips", cidrs, raising=False)

    return _configure


def make_request(peer: str, forwarded: str | None = None) -> Request:
    """A minimal ASGI scope — enough for request.client and request.headers."""
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": headers,
            "client": (peer, 51234),
            "query_string": b"",
        }
    )


# --------------------------------------------------------------------------
# Finding 1 — forged X-Forwarded-For must not rotate the identity
# --------------------------------------------------------------------------


class TestForwardedHeaderIsNotTrustedByDefault:
    def test_default_configuration_trusts_no_proxy(self) -> None:
        assert settings.trust_proxy_headers is False
        assert settings.proxy_trust_active is False

    def test_a_forged_header_cannot_change_the_identity(self) -> None:
        """The attack: one attacker, a different forged IP on every request.

        With trust off, all of them must collapse to the same socket peer, so
        they consume one shared budget instead of a fresh one each.
        """
        peer = "203.0.113.7"
        identities = {
            client_identifier(make_request(peer, f"1.1.1.{n}")) for n in range(1, 40)
        }
        assert identities == {peer}

    def test_a_missing_header_uses_the_socket_peer(self) -> None:
        assert client_identifier(make_request("203.0.113.7")) == "203.0.113.7"

    def test_direct_access_with_no_client_is_not_a_crash(self) -> None:
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"",
            }
        )
        assert client_identifier(request) == "unknown"

    async def test_the_forged_header_does_not_buy_extra_login_attempts(self) -> None:
        """End to end through check_rate_limit, the way login uses it."""
        peer = f"198.51.100.{uuid.uuid4().int % 200}"
        results = []
        for n in range(LOGIN_LIMIT.times + 3):
            identity = client_identifier(make_request(peer, f"9.9.9.{n}"))
            allowed, _remaining, available = await check_rate_limit(identity, LOGIN_LIMIT)
            assert available is True
            results.append(allowed)

        assert results.count(True) == LOGIN_LIMIT.times
        assert results[-1] is False, "the budget must actually run out"


class TestExplicitlyTrustedProxy:
    """The path that remains once trust is configured narrowly."""

    def test_a_trusted_proxy_peer_yields_the_forwarded_client(self, trust_proxy) -> None:
        trust_proxy("10.0.0.0/8")
        assert resolve_client_ip("10.0.0.5", "203.0.113.9") == "203.0.113.9"

    def test_an_untrusted_peer_is_ignored_even_with_trust_enabled(self, trust_proxy) -> None:
        trust_proxy("10.0.0.0/8")
        # The peer is not one of our proxies, so its header is just a claim.
        assert resolve_client_ip("198.51.100.4", "203.0.113.9") == "198.51.100.4"

    def test_the_rightmost_untrusted_hop_wins(self, trust_proxy) -> None:
        """A client may prepend anything; only hops we appended can be trusted.

        Here the client forged "1.2.3.4", a real client at 203.0.113.9 reached
        our proxy chain, and 10.0.0.2/10.0.0.5 are our own proxies. The answer
        must be 203.0.113.9 — the last address we can vouch for — not the
        attacker-supplied leftmost entry.
        """
        trust_proxy("10.0.0.0/8")
        chain = "1.2.3.4, 203.0.113.9, 10.0.0.2"
        assert resolve_client_ip("10.0.0.5", chain) == "203.0.113.9"

    def test_a_chain_of_only_proxies_falls_back_to_the_peer(self, trust_proxy) -> None:
        trust_proxy("10.0.0.0/8")
        assert resolve_client_ip("10.0.0.5", "10.0.0.2, 10.0.0.3") == "10.0.0.5"

    def test_a_garbage_hop_is_not_believed(self, trust_proxy) -> None:
        trust_proxy("10.0.0.0/8")
        assert resolve_client_ip("10.0.0.5", "not-an-ip") == "10.0.0.5"

    def test_an_empty_header_falls_back_to_the_peer(self, trust_proxy) -> None:
        trust_proxy("10.0.0.0/8")
        assert resolve_client_ip("10.0.0.5", "") == "10.0.0.5"

    def test_trust_without_an_allow_list_fails_safe(self, monkeypatch) -> None:
        """The flag alone must not re-open the hole it was added to close."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True, raising=False)
        monkeypatch.setattr(settings, "trusted_proxy_ips", "", raising=False)
        assert settings.proxy_trust_active is False
        assert resolve_client_ip("10.0.0.5", "203.0.113.9") == "10.0.0.5"

    def test_an_unparseable_allow_list_fails_safe(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "trust_proxy_headers", True, raising=False)
        monkeypatch.setattr(settings, "trusted_proxy_ips", "nonsense, ///", raising=False)
        assert settings.proxy_trust_active is False
        assert resolve_client_ip("10.0.0.5", "203.0.113.9") == "10.0.0.5"

    def test_a_single_address_is_accepted_without_a_prefix(self, trust_proxy) -> None:
        trust_proxy("10.1.2.3")
        assert resolve_client_ip("10.1.2.3", "203.0.113.9") == "203.0.113.9"
        assert resolve_client_ip("10.1.2.4", "203.0.113.9") == "10.1.2.4"


class TestUvicornIsNotConfiguredToRewriteTheClient:
    """The application-level guard above is worthless if uvicorn pre-empts it.

    `--forwarded-allow-ips="*"` makes uvicorn overwrite scope["client"] from
    the header for ANY peer, before a single line of this application runs.
    """

    def test_the_production_image_does_not_wildcard_forwarded_ips(self) -> None:
        from pathlib import Path

        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
        # Comments in that file explain why the flags are absent, so only the
        # instructions Docker actually executes are inspected.
        instructions = [
            line
            for line in dockerfile.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        executed = "\n".join(instructions)

        assert "--forwarded-allow-ips" not in executed
        assert "--proxy-headers" not in executed
        # Positive control: the scan is looking at the right lines.
        assert "uvicorn ledgerai.main:app" in executed


# --------------------------------------------------------------------------
# Finding 3 — the counter and its expiry must be set atomically
# --------------------------------------------------------------------------


class TestCounterAlwaysCarriesATtl:
    def _limit(self) -> RateLimit:
        # A unique name per test so counters never collide across the suite.
        return RateLimit(f"test-{uuid.uuid4().hex[:8]}", times=3, seconds=60)

    async def test_a_new_counter_gets_its_ttl_in_the_same_step(self) -> None:
        limit = self._limit()
        allowed, remaining, available = await check_rate_limit("someone", limit)

        assert (allowed, remaining, available) == (True, 2, True)
        ttl = await get_limiter_redis().ttl(f"ratelimit:{limit.name}:someone")
        assert 0 < ttl <= limit.seconds, "the very first write must leave a TTL"

    async def test_later_increments_keep_the_original_window(self) -> None:
        """Fixed window: the deadline must not slide forward on every request."""
        limit = self._limit()
        key = f"ratelimit:{limit.name}:someone"
        client = get_limiter_redis()

        await check_rate_limit("someone", limit)
        await client.pexpire(key, 20_000)  # pretend most of the window elapsed

        await check_rate_limit("someone", limit)
        ttl_ms = await client.pttl(key)
        assert ttl_ms <= 20_000, "a healthy counter must not have its window extended"

    async def test_a_counter_stranded_without_a_ttl_heals(self) -> None:
        """The exact state the old two-step code could leave behind.

        Without repair this key never expires, the count climbs forever, and
        that identifier is refused indefinitely.
        """
        limit = self._limit()
        key = f"ratelimit:{limit.name}:stranded"
        client = get_limiter_redis()

        await client.set(key, 1)  # value, but no expiry — the damaged state
        assert await client.ttl(key) == -1

        await check_rate_limit("stranded", limit)

        ttl = await client.ttl(key)
        assert ttl > 0, "a counter with no TTL must be re-armed, not left stranded"

    async def test_the_budget_is_still_enforced_exactly(self) -> None:
        limit = self._limit()
        outcomes = [
            (await check_rate_limit("someone", limit))[0]
            for _ in range(limit.times + 2)
        ]
        assert outcomes == [True] * limit.times + [False, False]

    async def test_concurrent_increments_do_not_lose_counts(self) -> None:
        """INCR is atomic; running the script concurrently must not double-spend."""
        limit = RateLimit(f"test-{uuid.uuid4().hex[:8]}", times=25, seconds=60)
        results = await asyncio.gather(
            *(check_rate_limit("racer", limit) for _ in range(25))
        )

        assert all(available for _allowed, _remaining, available in results)
        assert all(allowed for allowed, _r, _a in results), "25 requests fit in a 25 budget"
        # Exactly 25 increments happened, so the next one is the first refusal.
        assert (await check_rate_limit("racer", limit))[0] is False

        remaining = sorted(value for _a, value, _av in results)
        assert remaining == list(range(0, 25)), "each request consumed exactly one unit"

    async def test_remaining_never_goes_negative(self) -> None:
        limit = self._limit()
        for _ in range(limit.times + 5):
            _allowed, remaining, _available = await check_rate_limit("someone", limit)
            assert remaining >= 0
