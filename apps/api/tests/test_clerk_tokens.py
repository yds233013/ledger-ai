"""Clerk token verification.

Every test here signs its own tokens with a keypair generated in-process and
serves them through a fake JWKS, so nothing touches the network and every
failure mode can be constructed deliberately rather than waited for.

The property that matters most is the one that is easiest to lose in a
refactor: **the two token families must never be interchangeable.** A demo
HS256 token must not authenticate as a Clerk user, and a Clerk RS256 token must
not be validated against the HS256 secret. Both directions are tested, because
algorithm confusion is a real class of bug and "we pinned the algorithm" is a
claim that should be checked rather than believed.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient
from jwt.utils import base64url_encode

from ledgerai.config import settings
from ledgerai.security import clerk
from ledgerai.security.clerk import ClerkTokenError, looks_like_clerk_token, verify_clerk_token
from ledgerai.security.jwt import create_access_token

ISSUER = "https://example.clerk.accounts.dev"
ORIGIN = "https://web-production-test.up.railway.app"
KID = "test-key-1"
SUBJECT = "user_2abcdefghijklmnop"


@pytest.fixture(scope="module")
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def _jwk(public_key) -> dict[str, Any]:
    numbers = public_key.public_numbers()

    def b64(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64url_encode(raw).decode()

    return {
        "kty": "RSA",
        "kid": KID,
        "alg": "RS256",
        "use": "sig",
        "n": b64(numbers.n),
        "e": b64(numbers.e),
    }


class _FakeJWKClient(PyJWKClient):
    """A JWKS client backed by an in-memory key set."""

    def __init__(self, jwks: dict[str, Any], fail: bool = False) -> None:
        self._jwks = jwks
        self._fail = fail

    def get_signing_key_from_jwt(self, token: str):  # type: ignore[override]
        if self._fail:
            raise ConnectionError("jwks unreachable")
        from jwt import PyJWKSet

        header = jwt.get_unverified_header(token)
        key_set = PyJWKSet.from_dict(self._jwks)
        for key in key_set.keys:
            if key.key_id == header.get("kid"):
                return key
        raise jwt.PyJWKClientError("no matching key")


@pytest.fixture(autouse=True)
def clerk_configured(monkeypatch, keypair):
    private, public = keypair
    monkeypatch.setattr(settings, "clerk_enabled", True, raising=False)
    monkeypatch.setattr(settings, "clerk_issuer", ISSUER, raising=False)
    monkeypatch.setattr(settings, "clerk_authorized_parties", ORIGIN, raising=False)
    monkeypatch.setattr(settings, "clerk_audience", "", raising=False)
    clerk.reset_jwks_client(_FakeJWKClient({"keys": [_jwk(public)]}))
    yield
    clerk.reset_jwks_client(None)


def make_token(keypair, **overrides: Any) -> str:
    private, _ = keypair
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": SUBJECT,
        "iss": ISSUER,
        "azp": ORIGIN,
        "sid": "sess_123",
        "iat": now,
        "nbf": now - 5,
        "exp": now + 600,
        "email": "beta@example.com",
        "v": 2,
    }
    claims.update({k: v for k, v in overrides.items() if v is not ...})
    for key, value in overrides.items():
        if value is ...:
            claims.pop(key, None)
    headers = {"kid": KID}
    return jwt.encode(claims, private, algorithm="RS256", headers=headers)


class TestAValidToken:
    def test_it_verifies_and_returns_the_subject(self, keypair) -> None:
        identity = verify_clerk_token(make_token(keypair))
        assert identity.subject == SUBJECT
        assert identity.email == "beta@example.com"
        assert identity.session_id == "sess_123"

    def test_clock_skew_within_the_leeway_is_tolerated(self, keypair) -> None:
        """A few seconds of drift between Clerk and this host is normal."""
        now = int(time.time())
        identity = verify_clerk_token(make_token(keypair, nbf=now + 5, exp=now + 600))
        assert identity.subject == SUBJECT

    def test_the_leeway_does_not_become_an_extension(self, keypair) -> None:
        """Tolerating skew must not mean tolerating expiry.

        A token just past the leeway is refused, so the allowance stays a
        clock-drift accommodation rather than extra token lifetime.
        """
        now = int(time.time())
        just_inside = settings.clerk_leeway_seconds - 5
        just_outside = settings.clerk_leeway_seconds + 30

        assert verify_clerk_token(make_token(keypair, exp=now - just_inside)).subject == SUBJECT
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(make_token(keypair, exp=now - just_outside))


class TestRejections:
    @pytest.mark.parametrize(
        ("overrides", "why"),
        [
            ({"iss": "https://evil.clerk.accounts.dev"}, "issuer must match exactly"),
            ({"azp": "https://evil.example.com"}, "azp must be a known origin"),
            ({"azp": ...}, "a token with no azp is a CSRF exposure"),
            ({"exp": int(time.time()) - 3600}, "expired well beyond the leeway"),
            ({"nbf": int(time.time()) + 3600}, "not valid yet"),
            ({"sub": ...}, "no subject"),
            ({"sub": "not-a-clerk-id"}, "malformed subject"),
            ({"sub": ""}, "empty subject"),
        ],
    )
    def test_bad_claims_are_refused(self, keypair, overrides, why) -> None:
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(make_token(keypair, **overrides))

    def test_a_token_signed_by_another_key_is_refused(self, keypair) -> None:
        """The signature is the whole point; a valid-looking payload is not."""
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        forged = jwt.encode(
            {"sub": SUBJECT, "iss": ISSUER, "azp": ORIGIN, "exp": now + 600},
            other,
            algorithm="RS256",
            headers={"kid": KID},
        )
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(forged)

    def test_an_unknown_kid_is_refused(self, keypair) -> None:
        private, _ = keypair
        now = int(time.time())
        token = jwt.encode(
            {"sub": SUBJECT, "iss": ISSUER, "azp": ORIGIN, "exp": now + 600},
            private,
            algorithm="RS256",
            headers={"kid": "some-other-key"},
        )
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(token)

    def test_garbage_is_refused(self) -> None:
        for junk in ("", "abc", "a.b.c", "..", json.dumps({"sub": "x"})):
            with pytest.raises(ClerkTokenError):
                verify_clerk_token(junk)


class TestTheTwoFamiliesNeverMix:
    """Algorithm confusion, tested in both directions."""

    def test_a_demo_hs256_token_is_not_a_clerk_token(self) -> None:
        demo = create_access_token(uuid.uuid4(), "demo@ledgerai.local")
        assert looks_like_clerk_token(demo) is False
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(demo)

    def test_an_hs256_token_forged_to_look_like_clerk_is_refused(self, keypair) -> None:
        """Claims copied wholesale, signed HS256 with a `kid` header.

        The verifier must not accept it just because the header says `kid`, and
        must not fall back to the HS256 path when RS256 verification fails.
        """
        now = int(time.time())
        forged = jwt.encode(
            {"sub": SUBJECT, "iss": ISSUER, "azp": ORIGIN, "exp": now + 600},
            settings.auth_secret,
            algorithm="HS256",
            headers={"kid": KID},
        )
        assert looks_like_clerk_token(forged) is False  # alg is HS256
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(forged)

    def test_a_clerk_token_is_not_accepted_by_the_demo_verifier(self, keypair) -> None:
        from ledgerai.security.jwt import TokenError, decode_access_token

        with pytest.raises(TokenError):
            decode_access_token(make_token(keypair))

    def test_the_none_algorithm_is_refused(self) -> None:
        """The classic: a token asserting it needs no signature."""
        header = base64url_encode(json.dumps({"alg": "none", "kid": KID}).encode()).decode()
        body = base64url_encode(
            json.dumps({"sub": SUBJECT, "iss": ISSUER, "azp": ORIGIN}).encode()
        ).decode()
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(f"{header}.{body}.")


class TestConfigurationIsPartOfSecurity:
    def test_unconfigured_means_closed_not_open(self, monkeypatch, keypair) -> None:
        """Enabled-but-unconfigured must not degrade into accept-anything."""
        monkeypatch.setattr(settings, "clerk_issuer", "", raising=False)
        assert settings.clerk_configured is False
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(make_token(keypair))

    def test_no_authorized_parties_means_closed(self, monkeypatch, keypair) -> None:
        monkeypatch.setattr(settings, "clerk_authorized_parties", "", raising=False)
        assert settings.clerk_configured is False
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(make_token(keypair))

    def test_an_unreachable_jwks_denies_rather_than_erroring(self, keypair) -> None:
        """A verifier that cannot verify has not authenticated anybody.

        Failing open on a network blip would be the entire vulnerability, and
        raising a 500 would invite a retry that cannot succeed.
        """
        clerk.reset_jwks_client(_FakeJWKClient({"keys": []}, fail=True))
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(make_token(keypair))

    def test_the_jwks_url_is_derived_from_the_issuer(self) -> None:
        """Two variables could drift; a JWKS fetched from somewhere other than
        the issuer is the whole attack."""
        assert settings.clerk_jwks_url == f"{ISSUER}/.well-known/jwks.json"

    def test_an_audience_is_enforced_only_when_configured(self, monkeypatch, keypair) -> None:
        """Clerk session tokens carry no `aud` by default."""
        verify_clerk_token(make_token(keypair))  # no aud, none configured — fine

        monkeypatch.setattr(settings, "clerk_audience", "ledgerai-api", raising=False)
        with pytest.raises(ClerkTokenError):
            verify_clerk_token(make_token(keypair))  # configured, token has none
        identity = verify_clerk_token(make_token(keypair, aud="ledgerai-api"))
        assert identity.subject == SUBJECT
