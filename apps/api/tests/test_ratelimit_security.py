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
# A proxy we are not configured to trust must be reported, not guessed at
# --------------------------------------------------------------------------


class TestAnUntrustedProxyIsReportedOnce:
    """Refusing to believe the header is safe, but it is not free.

    Behind an edge proxy with trust off, every caller resolves to the proxy's
    address and shares a single bucket — so the public demo endpoint's budget
    becomes the whole internet's budget rather than each visitor's. The limits
    still hold; they just stop being per-visitor.

    No hosting provider used here publishes an authoritative CIDR for its
    inbound edge, and a guessed range fails silently in the same direction. So
    the deployment reports the address it actually observes, once, and that
    observed value is what TRUSTED_PROXY_IPS is set from.
    """

    @pytest.fixture(autouse=True)
    def _rearm(self):
        ratelimit.reset_proxy_warning()
        yield
        ratelimit.reset_proxy_warning()

    def test_a_forwarded_chain_we_ignore_is_reported(self, caplog) -> None:
        with caplog.at_level("WARNING"):
            client_identifier(make_request("10.1.2.3", "203.0.113.9"))
        assert "10.1.2.3" in caplog.text
        assert "TRUSTED_PROXY_IPS" in caplog.text

    def test_the_report_names_the_proxy_and_not_the_caller(self, caplog) -> None:
        """The chain's contents are visitor addresses. The peer is
        infrastructure in this branch and is recorded exactly, because it is
        the value TRUSTED_PROXY_IPS needs; the hops are hashed."""
        with caplog.at_level("WARNING"):
            client_identifier(make_request("10.1.2.3", "203.0.113.9, 198.51.100.4"))
        assert "203.0.113.9" not in caplog.text
        assert "198.51.100.4" not in caplog.text
        assert "hop_count=2" in caplog.text
        assert "private:10.1.2.3" in caplog.text

    def test_it_is_logged_once_not_once_per_request(self, caplog) -> None:
        """A per-request warning would flood the log of the very deployment
        that is already misconfigured."""
        with caplog.at_level("WARNING"):
            for n in range(25):
                client_identifier(make_request("10.1.2.3", f"203.0.113.{n}"))
        assert caplog.text.count("TRUSTED_PROXY_IPS") == 1

    def test_a_direct_caller_is_not_reported(self, caplog) -> None:
        """No forwarded header means no proxy, so there is nothing to fix —
        and the peer would be a user's own address."""
        with caplog.at_level("WARNING"):
            client_identifier(make_request("203.0.113.7"))
        assert "TRUSTED_PROXY_IPS" not in caplog.text

    def test_a_correctly_configured_proxy_is_not_reported(
        self, trust_proxy, caplog
    ) -> None:
        trust_proxy("10.0.0.0/8")
        with caplog.at_level("WARNING"):
            client_identifier(make_request("10.1.2.3", "203.0.113.9"))
        assert "TRUSTED_PROXY_IPS" not in caplog.text

    def test_reporting_does_not_change_the_identity(self, caplog) -> None:
        """The diagnostic is observability only. It must not start trusting
        the header it is complaining about."""
        with caplog.at_level("WARNING"):
            identity = client_identifier(make_request("10.1.2.3", "203.0.113.9"))
        assert identity == "10.1.2.3"


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


# --------------------------------------------------------------------------
# The sanitized chain diagnostic
# --------------------------------------------------------------------------


class TestChainDiagnosticSanitization:
    """Enough detail to configure TRUSTED_PROXY_IPS, and nothing more.

    The question it answers — "which hop is infrastructure, and where does the
    right-to-left walk stop?" — needs real addresses for the infrastructure
    hops, because those are the values that go into the allow-list. It does not
    need the visitor's address, so that one never appears.
    """

    def test_infrastructure_addresses_are_recorded_exactly(self) -> None:
        """These are the values TRUSTED_PROXY_IPS is set from."""
        assert ratelimit.sanitize_address("100.64.0.7") == "cgnat:100.64.0.7"
        assert ratelimit.sanitize_address("10.1.2.3") == "private:10.1.2.3"
        assert ratelimit.sanitize_address("127.0.0.1") == "loopback:127.0.0.1"
        assert ratelimit.sanitize_address("169.254.1.1") == "link_local:169.254.1.1"

    def test_a_public_address_is_never_written_out(self) -> None:
        rendered = ratelimit.sanitize_address("203.0.113.9")
        assert "203.0.113.9" not in rendered
        assert rendered.startswith("public:sha256-")

    def test_two_public_addresses_are_distinguishable_but_opaque(self) -> None:
        a = ratelimit.sanitize_address("203.0.113.9")
        b = ratelimit.sanitize_address("198.51.100.4")
        assert a != b
        assert "203.0" not in a and "198.51" not in b

    def test_the_same_address_is_stable_within_a_process(self) -> None:
        """So two hops can be compared, without the value being recoverable."""
        assert ratelimit.sanitize_address("203.0.113.9") == ratelimit.sanitize_address(
            "203.0.113.9"
        )

    def test_classes_are_named_correctly(self) -> None:
        assert ratelimit.classify_address("100.64.0.7") == "cgnat"
        assert ratelimit.classify_address("203.0.113.9") == "public"
        assert ratelimit.classify_address("not-an-address") == "invalid"


class TestChainDiagnosticDescribesTheWalk:
    def test_it_reports_each_hop_position_class_and_trust(self) -> None:
        chain = ratelimit.describe_chain("100.64.0.7", "203.0.113.9, 100.64.0.3")

        assert chain["peer"] == "cgnat:100.64.0.7"
        assert chain["hop_count"] == 2
        hops = chain["hops"]
        assert hops[0]["position"] == 1 and hops[0]["class"] == "public"
        assert hops[1]["position"] == 2 and hops[1]["class"] == "cgnat"
        assert hops[1]["value"] == "cgnat:100.64.0.3"
        # Untrusted by default, which is the whole point of the diagnostic.
        assert chain["peer_trusted"] is False
        assert chain["trust_active"] is False

    def test_the_visitor_address_is_absent_from_the_whole_payload(self) -> None:
        chain = ratelimit.describe_chain("100.64.0.7", "203.0.113.9, 100.64.0.3")
        assert "203.0.113.9" not in str(chain)

    def test_with_trust_off_the_identity_is_the_peer(self) -> None:
        chain = ratelimit.describe_chain("100.64.0.7", "203.0.113.9, 100.64.0.3")
        assert chain["resolved_identity"] == "cgnat:100.64.0.7"

    def test_with_the_right_allow_list_the_walk_reaches_the_visitor(
        self, trust_proxy
    ) -> None:
        """Both infrastructure hops trusted, so right-to-left reaches hop 1 —
        reported as a hash, never as the address."""
        trust_proxy("100.64.0.0/10")
        chain = ratelimit.describe_chain("100.64.0.7", "203.0.113.9, 100.64.0.3")

        assert chain["peer_trusted"] is True
        assert chain["hops"][1]["trusted"] is True
        assert chain["resolved_identity"].startswith("public:sha256-")
        assert "203.0.113.9" not in str(chain)

    def test_a_slash32_on_the_peer_alone_stops_at_the_inner_hop(
        self, trust_proxy
    ) -> None:
        """Why a single-address allow-list is not enough for a two-hop chain.

        With only the peer trusted, the walk finds hop 2 untrusted and returns
        it — an infrastructure address that every visitor shares, so the limit
        stays collective while looking configured.
        """
        trust_proxy("100.64.0.7/32")
        chain = ratelimit.describe_chain("100.64.0.7", "203.0.113.9, 100.64.0.3")

        assert chain["peer_trusted"] is True
        assert chain["hops"][1]["trusted"] is False
        assert chain["resolved_identity"] == "cgnat:100.64.0.3"


class TestTheDiagnosticIsEmittedOncePerProcess:
    @pytest.fixture(autouse=True)
    def _rearm(self):
        ratelimit.reset_proxy_warning()
        yield
        ratelimit.reset_proxy_warning()

    def test_it_logs_the_structured_chain(self, caplog) -> None:
        with caplog.at_level("WARNING"):
            client_identifier(make_request("100.64.0.7", "203.0.113.9, 100.64.0.3"))
        assert "proxy.untrusted_chain" in caplog.text
        assert "cgnat:100.64.0.7" in caplog.text
        assert "203.0.113.9" not in caplog.text

    def test_it_logs_once_across_many_requests(self, caplog) -> None:
        with caplog.at_level("WARNING"):
            for n in range(20):
                client_identifier(make_request("100.64.0.7", f"203.0.113.{n}, 100.64.0.3"))
        assert caplog.text.count("proxy.untrusted_chain") == 1
