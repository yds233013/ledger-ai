"""Verification of Clerk-issued session tokens.

The browser sends a Clerk session JWT straight to this API, so the API must
establish the identity itself. Nothing the browser asserts is used as
authenticated data before the signature and every claim below have been
checked.

**Two token families, never interchangeable.** The demo flow uses HS256 tokens
this service mints (`security/jwt.py`); Clerk uses RS256 tokens signed by keys
we only ever see through JWKS. Dispatch happens on the *unverified* header —
which is safe, because the header selects a verifier and is never itself
trusted — and each verifier pins `algorithms` to exactly one value. That
pinning is what forecloses algorithm confusion: an RS256 token can never be
checked against the HS256 secret, and an HS256 token can never be checked
against a JWKS key.

Claims validated, per Clerk's documented session-token behaviour:

* signature, RS256 only, key selected by `kid` from the issuer's JWKS
* `iss` — must equal the configured Frontend API URL exactly
* `azp` — must be one of the configured origins. Clerk's documentation is
  explicit that skipping this "can open your application to CSRF attacks", so
  a token without `azp` is rejected rather than waved through
* `exp` and `nbf`, with a small leeway for clock skew
* `sub` — must be present and shaped like a Clerk user id
* `aud` — Clerk session tokens carry none by default. If a custom template adds
  one, configuring `CLERK_AUDIENCE` makes it mandatory; unconfigured, an
  unexpected `aud` is ignored rather than trusted

An unverifiable token is 401, never 500 — including when JWKS cannot be
fetched. A verifier that cannot verify has not authenticated anybody, and
failing open on a network error would be the whole vulnerability.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

from ..config import settings

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"

# Clerk user ids look like `user_2abc...`. Checked so a malformed subject is
# rejected here rather than becoming a database lookup for a nonsense key.
_SUBJECT_PATTERN = re.compile(r"^user_[A-Za-z0-9]{10,64}$")

# JWKS fetches are network calls; PyJWKClient caches signing keys and only
# refetches on an unknown `kid`. One client per process, guarded because
# FastAPI serves requests concurrently.
_jwks_client: PyJWKClient | None = None
_jwks_lock = threading.Lock()


class ClerkTokenError(Exception):
    """Raised for any token that cannot be trusted, for any reason.

    Deliberately one type with a short message. Telling a caller whether the
    signature, the issuer or the expiry was wrong helps an attacker far more
    than it helps a user, and the remedy is the same either way: sign in again.
    """


def reset_jwks_client(client: PyJWKClient | None = None) -> None:
    """Inject or clear the cached JWKS client. Used by tests."""
    global _jwks_client
    with _jwks_lock:
        _jwks_client = client


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    with _jwks_lock:
        if _jwks_client is None:
            _jwks_client = PyJWKClient(
                settings.clerk_jwks_url,
                cache_keys=True,
                max_cached_keys=8,
                # A hung JWKS fetch would otherwise hold a request worker open.
                timeout=5,
            )
        return _jwks_client


def looks_like_clerk_token(token: str) -> bool:
    """Whether this token should be routed to the Clerk verifier.

    Reads the UNVERIFIED header, which is only ever used to choose a verifier —
    never as a claim about who the caller is. A token that does not clearly
    belong to one family is routed nowhere and rejected, so an ambiguous token
    cannot try both paths.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return False
    return header.get("alg") == ALGORITHM and bool(header.get("kid"))


@dataclass(frozen=True, slots=True)
class ClerkIdentity:
    """The verified subject. The only thing a caller may act on."""

    subject: str
    session_id: str | None
    email: str | None
    issued_at: int | None


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ClerkTokenError(reason)


def verify_clerk_token(token: str) -> ClerkIdentity:
    """Verify a Clerk session token and return its subject.

    Raises ClerkTokenError for every failure, including configuration that
    would make verification meaningless.
    """
    # Enabled-but-unconfigured must not mean "accept anything". Without an
    # issuer and an authorized party there is nothing to check against.
    if not settings.clerk_configured:
        raise ClerkTokenError("clerk_not_configured")

    _require(looks_like_clerk_token(token), "not_a_clerk_token")

    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    except Exception as exc:  # noqa: BLE001 - unreachable JWKS is not authentication
        # No key means no verification, which means no identity. 401, not 500:
        # the caller is not authenticated, and saying "server error" would
        # invite a retry that cannot succeed.
        logger.warning("clerk.jwks_unavailable error=%s", type(exc).__name__)
        raise ClerkTokenError("signing_key_unavailable") from exc

    expected_audience = settings.clerk_audience.strip() or None
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            # Pinned. This single-element list is what makes algorithm
            # confusion impossible on this path.
            algorithms=[ALGORITHM],
            issuer=settings.clerk_issuer.rstrip("/"),
            audience=expected_audience,
            leeway=settings.clerk_leeway_seconds,
            options={
                "require": ["exp", "sub", "iss"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iss": True,
                # Only meaningful when an audience is configured; PyJWT skips
                # the check when `audience` is None.
                "verify_aud": expected_audience is not None,
            },
        )
    except jwt.PyJWTError as exc:
        raise ClerkTokenError("invalid_token") from exc

    # --- claims PyJWT does not know about ---------------------------------
    #
    # azp is the origin that obtained the token. Clerk's docs call skipping it
    # a CSRF exposure, so an absent azp is a rejection and not a default-allow.
    azp = claims.get("azp")
    _require(isinstance(azp, str) and bool(azp), "missing_azp")
    _require(azp in settings.clerk_authorized_party_list, "unauthorized_party")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not _SUBJECT_PATTERN.match(subject):
        raise ClerkTokenError("bad_subject")

    email = claims.get("email")
    return ClerkIdentity(
        subject=subject,
        session_id=claims.get("sid") if isinstance(claims.get("sid"), str) else None,
        email=email if isinstance(email, str) and email else None,
        issued_at=claims.get("iat") if isinstance(claims.get("iat"), int) else None,
    )
