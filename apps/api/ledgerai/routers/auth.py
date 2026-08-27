"""Authentication endpoints.

Next.js (Auth.js) owns the browser session and calls POST /api/auth/login to
verify credentials. It then mints short-lived HS256 bearer tokens from that
session using the shared AUTH_SECRET, which this API verifies in deps.py.

Phase 1 uses a seeded demo user; Phase 3 adds OAuth providers on the Next.js
side without any change to this contract.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ..config import settings
from ..deps import CurrentUser, DbSession
from ..models import User
from ..schemas.common import LoginRequest, LoginResponse, UserOut
from ..security.jwt import create_access_token
from ..security.passwords import verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, session: DbSession) -> LoginResponse:
    user = (
        await session.execute(select(User).where(User.email == payload.email.lower().strip()))
    ).scalar_one_or_none()

    # Identical response for unknown user and wrong password — no account
    # enumeration through timing or message differences.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    token = create_access_token(user.id, user.email)
    return LoginResponse(
        user=UserOut.model_validate(user),
        access_token=token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/token", response_model=LoginResponse)
async def refresh(user: CurrentUser) -> LoginResponse:
    """Exchange a still-valid token for a fresh one."""
    token = create_access_token(user.id, user.email)
    return LoginResponse(
        user=UserOut.model_validate(user),
        access_token=token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )
