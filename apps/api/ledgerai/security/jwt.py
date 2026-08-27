"""HS256 access tokens shared with the Next.js Auth.js layer.

Next.js owns the browser session; its /api/auth/token route mints a short-lived
token from the session and the browser sends it to FastAPI as a bearer token.
Both sides sign/verify with AUTH_SECRET.

If this API is ever exposed to third-party clients, move to RS256/JWKS so the
verifier no longer needs the signing key. HS256 is appropriate here only
because both services are ours and co-deployed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from ..config import settings

ALGORITHM = "HS256"
ISSUER = "ledgerai"
AUDIENCE = "ledgerai-api"


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or mis-signed."""


def create_access_token(
    user_id: uuid.UUID | str,
    email: str,
    ttl_minutes: int | None = None,
) -> str:
    now = datetime.now(UTC)
    ttl = ttl_minutes if ttl_minutes is not None else settings.access_token_ttl_minutes
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()),
    }
    return jwt.encode(payload, settings.auth_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.auth_secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
