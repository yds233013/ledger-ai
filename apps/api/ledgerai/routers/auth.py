"""Authentication endpoints.

Next.js (Auth.js) owns the browser session and calls POST /api/auth/login to
verify credentials. It then mints short-lived HS256 bearer tokens from that
session using the shared AUTH_SECRET, which this API verifies in deps.py.

Phase 1 uses a seeded demo user; Phase 3 adds OAuth providers on the Next.js
side without any change to this contract.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from functools import partial

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..deps import CurrentUser, DbSession, SyncSessionFactory
from ..models import User
from ..schemas.common import LoginRequest, LoginResponse, UserOut
from ..security.jwt import create_access_token
from ..security.passwords import verify_password
from ..security.ratelimit import DEMO_SESSION_LIMIT, LOGIN_LIMIT, enforce
from ..services.demo import (
    DEMO_DATA_NOTICE,
    new_request_key,
    provision_demo_user,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class DemoSessionRequest(BaseModel):
    """Optional idempotency key for demo provisioning.

    A caller that retries a timed-out request should reuse its key and get the
    account the first attempt created, not a second 250-row dataset. Omitting
    the key means "always give me a new account", which is the right default
    for a visitor clicking the button a second time on purpose.
    """

    request_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Idempotency key. Retrying with the same key returns the same account.",
    )


class DemoSessionResponse(BaseModel):
    user: UserOut
    access_token: str
    expires_in: int
    demo_expires_at: datetime
    demo_expires_in_seconds: int
    transaction_count: int
    account_count: int
    alert_count: int
    reused: bool
    notice: str = DEMO_DATA_NOTICE


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest, request: Request, session: DbSession
) -> LoginResponse:
    # Per IP, not per account: the thing being throttled is credential
    # guessing, and the guesser chooses the account name.
    await enforce(request, LOGIN_LIMIT)

    user = (
        await session.execute(select(User).where(User.email == payload.email.lower().strip()))
    ).scalar_one_or_none()

    # Identical response for unknown user and wrong password — no account
    # enumeration through timing or message differences.
    # A Clerk-backed account has no password of ours. Treated exactly like a
    # wrong password — same branch, same message — so this endpoint cannot be
    # used to discover which accounts are Clerk-backed.
    if user is None or not user.password_hash or not verify_password(
        payload.password, user.password_hash
    ):
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


@router.post("/demo-session", response_model=DemoSessionResponse)
async def demo_session(
    payload: DemoSessionRequest,
    request: Request,
    factory: SyncSessionFactory,
) -> DemoSessionResponse:
    """Provision a fresh, isolated demo account and return a token for it.

    Unauthenticated by definition — this endpoint IS the way in — so it is
    rate limited as a public surface: fails closed in production, because an
    unmetered endpoint that writes 250 rows per call is a resource-exhaustion
    lever, and open in development so the demo still works with Redis down.

    The account it creates is an ordinary user row. Every user-scoped query in
    the rest of the API therefore isolates demo visitors from each other and
    from real accounts with no additional logic.
    """
    await enforce(request, DEMO_SESSION_LIMIT)

    request_key = payload.request_key or new_request_key()

    # Seeding runs the sync categorizer and the sync alert detectors over ~250
    # rows. Off the event loop, so one visitor provisioning does not stall
    # everyone else's in-flight requests, including open SSE streams.
    info = await to_thread.run_sync(
        partial(provision_demo_user, factory, request_key=request_key)
    )

    # The API token never outlives the demo account itself, so an expired demo
    # cannot keep working until the token's own clock runs out.
    ttl_minutes = min(
        settings.access_token_ttl_minutes,
        max(1, info.expires_in_seconds // 60),
    )
    token = create_access_token(info.user_id, info.email, ttl_minutes=ttl_minutes)

    return DemoSessionResponse(
        user=UserOut(
            id=info.user_id,
            email=info.email,
            display_name=info.display_name,
            is_demo=True,
            demo_expires_at=info.expires_at,
        ),
        access_token=token,
        expires_in=ttl_minutes * 60,
        demo_expires_at=info.expires_at,
        demo_expires_in_seconds=info.expires_in_seconds,
        transaction_count=info.transaction_count,
        account_count=info.account_count,
        alert_count=info.alert_count,
        reused=info.reused,
    )


class GitHubIdentityRequest(BaseModel):
    """A GitHub identity, as reported by the Next.js OAuth callback.

    `provider_account_id` is GitHub's immutable numeric account id and is the
    only field this endpoint resolves an account by. The rest is display data.
    """

    provider_account_id: str = Field(min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=320)
    email_verified: bool = False
    display_name: str | None = Field(default=None, max_length=120)


class GitHubIdentityResponse(BaseModel):
    user: UserOut
    created: bool


@router.post("/oauth/github", response_model=GitHubIdentityResponse)
async def resolve_github_identity(
    payload: GitHubIdentityRequest, session: DbSession
) -> GitHubIdentityResponse:
    """Resolve a GitHub identity to a Ledger AI account, creating one if new.

    **Linking policy — the security-relevant part.**

    An account is found by `github_id` and by nothing else. In particular an
    existing account is NEVER adopted because its email matches the one GitHub
    reported, even when GitHub says that address is verified. "Verified by the
    provider" means the provider believes the person controls that mailbox; it
    says nothing about who owns the Ledger AI account already using it. Merging
    on it would mean anyone who can set their GitHub address to a known user's
    address inherits that user's financial data, which is exactly the takeover
    this refuses to enable.

    So a GitHub identity that has not been seen before always gets its own new
    account. Linking an existing password account to GitHub is a deliberate,
    authenticated action and is not implemented in this build — see
    docs/security.md.

    The reported address is stored only when GitHub verified it AND no other
    account holds it. Otherwise a non-routable placeholder is used, so an
    unverified or contested address never becomes an account identifier.
    """
    provider_id = payload.provider_account_id.strip()

    existing = (
        await session.execute(select(User).where(User.github_id == provider_id))
    ).scalar_one_or_none()
    if existing is not None:
        return GitHubIdentityResponse(user=UserOut.model_validate(existing), created=False)

    email = (payload.email or "").strip().lower()
    usable_email = ""
    if payload.email_verified and email:
        taken = (
            await session.execute(select(User.id).where(User.email == email))
        ).scalar_one_or_none()
        # Refuse to adopt, and refuse to collide.
        if taken is None:
            usable_email = email

    user = User(
        email=usable_email or f"github-{provider_id}@github.ledgerai.invalid",
        # No password: this account signs in through GitHub. An unguessable
        # value rather than an empty string, so the credentials path cannot be
        # tricked into matching it.
        password_hash=secrets.token_urlsafe(48),
        display_name=(payload.display_name or "").strip()[:120] or "GitHub user",
        is_demo=False,
        github_id=provider_id,
    )
    session.add(user)

    try:
        await session.commit()
    except IntegrityError:
        # Two callbacks for the same brand-new identity raced. The unique index
        # on github_id decided it; re-read the winner rather than failing.
        await session.rollback()
        winner = (
            await session.execute(select(User).where(User.github_id == provider_id))
        ).scalar_one_or_none()
        if winner is None:
            raise
        return GitHubIdentityResponse(user=UserOut.model_validate(winner), created=False)

    await session.refresh(user)
    logger.info("Created a Ledger AI account for a new GitHub identity")
    return GitHubIdentityResponse(user=UserOut.model_validate(user), created=True)


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
