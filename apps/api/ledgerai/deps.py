"""Shared FastAPI dependencies."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from .db import SyncSessionLocal, get_db
from .models import User
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


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the bearer token to a real user row.

    Every user-scoped route depends on this. The returned User.id is the only
    identity the rest of the request may use — no route reads a user id from
    the path, query string or body.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise CREDENTIALS_ERROR

    token = authorization.split(" ", 1)[1].strip()
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

    # Demo expiry is enforced HERE, against the row, on every single request.
    #
    # Putting it in the token instead would not hold: the browser session mints
    # a fresh short-lived token whenever the old one nears expiry, so a visitor
    # who simply keeps the tab open would renew their way past the deadline
    # forever. The column cannot be renewed by refreshing.
    if demo_has_expired(user):
        raise DEMO_EXPIRED_ERROR
    return user


def get_sync_sessionmaker() -> sessionmaker[Session]:
    """The synchronous session factory, as a dependency.

    Exposed this way purely so tests can point it at the test database the same
    way they override `get_db`. Demo provisioning runs the sync categorizer and
    alert detectors on a worker thread and needs a real sync session there.
    """
    return SyncSessionLocal


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
SyncSessionFactory = Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)]
