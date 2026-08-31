"""Shared FastAPI dependencies."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .db import SyncSessionLocal, get_db
from .models import User
from .security.clerk import ClerkTokenError, looks_like_clerk_token, verify_clerk_token
from .security.jwt import TokenError, decode_access_token
from .services.demo import demo_has_expired

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)

# A distinct message, because the cause and the remedy are both different: the
# session was fine and simply ran out, and the fix is to start a new demo
# rather than to check a password. 401 so the browser client's existing
# session-expiry handling redirects to sign-in without a new code path.
DEMO_EXPIRED_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail=(
        "This demo session has ended. Demo accounts last 24 hours and their data "
        "is then deleted. Start a new demo to continue."
    ),
    headers={"WWW-Authenticate": "Bearer"},
)


_EMAIL_LOOKUP_FALLBACK = (
    "Could not confirm your email address with Clerk. Please try again in a moment."
)

# Distinct causes, distinct remedies — but none of them names an address, an
# identifier, or anything about our configuration.
_EMAIL_LOOKUP_MESSAGES = {
    "no_verified_email": (
        "Your Clerk account has no verified email address. Verify your address "
        "with Clerk and try again."
    ),
    "ambiguous": (
        "Your Clerk account has several verified email addresses and no primary "
        "one. Set a primary address with Clerk and try again."
    ),
    "not_found": "This sign-in is no longer valid. Please sign in again.",
}

ACCOUNT_DELETED_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="This account has been deleted.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def _user_from_demo_token(token: str, session: AsyncSession) -> User:
    """The existing HS256 path, unchanged in behaviour."""
    try:
        claims = decode_access_token(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise CREDENTIALS_ERROR from exc

    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise CREDENTIALS_ERROR
    return user


async def _user_from_clerk_token(
    token: str,
    session: AsyncSession,
    factory: sessionmaker[Session],
) -> User:
    """Verify a Clerk token, then find or create the profile it belongs to.

    Provisioning is lazy — the verified token is the source of truth — and runs
    in a sync session because it needs `ON CONFLICT ... RETURNING` semantics and
    a transaction boundary it controls.
    """
    if not settings.clerk_enabled:
        # The kill switch. With Clerk off, an RS256 token is simply not a
        # credential here, and the demo flow is unaffected.
        raise CREDENTIALS_ERROR

    try:
        identity = verify_clerk_token(token)
    except ClerkTokenError as exc:
        # One generic message: which claim failed is a hint about what to try
        # next, and the remedy is the same in every case.
        raise CREDENTIALS_ERROR from exc

    from anyio import to_thread

    from .services.clerk_admin import EmailOutcome, fetch_verified_email
    from .services.identity import ProvisioningError, provision_profile

    def _resolve_email() -> str | None:
        """The verified address for this subject, from Clerk's Backend API.

        Called only when a profile is about to be created. Every non-success
        outcome raises rather than returning None, so the caller cannot mistake
        "Clerk was unreachable" for "this person has no address" — the first
        must be retried, the second must not.
        """
        outcome, email = fetch_verified_email(identity.subject)
        if outcome is EmailOutcome.RESOLVED and email:
            return email
        raise ProvisioningError(
            _EMAIL_LOOKUP_MESSAGES.get(outcome.value, _EMAIL_LOOKUP_FALLBACK)
        )

    def _provision() -> uuid.UUID:
        # The injected factory, not the module-level one. A code path that can
        # only ever reach the database DATABASE_URL names is a path that cannot
        # be tested against another — the same coupling that hid two earlier
        # bugs in this repository.
        with factory() as sync_session:
            user = provision_profile(
                sync_session,
                clerk_user_id=identity.subject,
                # A Clerk session token carries no address, and a templated
                # claim would not carry a verification status either. Resolved
                # from the Backend API, lazily, only when creating.
                resolve_email=_resolve_email,
            )
            user_id = user.id
            sync_session.commit()
            return user_id

    try:
        user_id = await to_thread.run_sync(_provision)
    except ProvisioningError as exc:
        # Deliberately 403, not 401: the caller IS authenticated, they are just
        # not allowed an account. A 401 would send the browser back to sign in,
        # which would succeed and loop.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise CREDENTIALS_ERROR
    return user


def get_sync_sessionmaker() -> sessionmaker[Session]:
    """The synchronous session factory, as a dependency.

    Exposed this way purely so tests can point it at the test database the same
    way they override `get_db`. Demo provisioning runs the sync categorizer and
    alert detectors on a worker thread and needs a real sync session there.
    """
    return SyncSessionLocal


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_db),
    factory: sessionmaker[Session] = Depends(get_sync_sessionmaker),
) -> User:
    """Resolve the bearer token to a real user row.

    Every user-scoped route depends on this. The returned User.id is the only
    identity the rest of the request may use — no route reads a user id from
    the path, query string or body.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise CREDENTIALS_ERROR

    token = authorization.split(" ", 1)[1].strip()

    # Two token families, dispatched on the UNVERIFIED header. That is safe
    # because the header only selects a verifier — it is never read as a claim
    # about who the caller is — and each verifier pins its algorithm to exactly
    # one value. There is no fallback between them: a token that fails the path
    # it was routed to is rejected, never retried against the other.
    if looks_like_clerk_token(token):
        user = await _user_from_clerk_token(token, session, factory)
    else:
        user = await _user_from_demo_token(token, session)

    if user.status == "pending_deletion":
        # The rows may still be there; the answer is already no.
        raise ACCOUNT_DELETED_ERROR

    # Demo expiry is enforced HERE, against the row, on every single request.
    #
    # Putting it in the token instead would not hold: the browser session mints
    # a fresh short-lived token whenever the old one nears expiry, so a visitor
    # who simply keeps the tab open would renew their way past the deadline
    # forever. The column cannot be renewed by refreshing.
    if demo_has_expired(user):
        raise DEMO_EXPIRED_ERROR
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
SyncSessionFactory = Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)]
