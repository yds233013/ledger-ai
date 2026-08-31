"""Email normalization, invitation matching, and profile provisioning.

Three things live together here because they are one flow: a verified Clerk
email is normalized, matched against a local invitation, and turned into a
Ledger profile — atomically, or not at all.

**Why the invitation is bound to an email at all.** The administrator creates
an invitation for an address and separately sends the Clerk invitation to that
same address. The user copies nothing. That means provisioning has to *find*
the invitation from the email Clerk verified, so the address must be matchable.

**Why it is a keyed HMAC and not a hash.** Matchable is not the same as
readable. A plain SHA-256 of an email offers almost no protection: the input
space is small and guessable, so anyone who read the table could confirm
whether a specific person was invited by hashing their address. An HMAC under a
key that is not in the table makes that impossible. The key is derived from
AUTH_SECRET with a domain separator rather than being a new secret to manage —
with the documented consequence that rotating AUTH_SECRET invalidates
unredeemed invitations, which is recoverable (reissue them) and far better than
another credential to lose.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import unicodedata
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..models import DeletedIdentity, Invitation, User

logger = logging.getLogger(__name__)

DEFAULT_INVITE_TTL_DAYS = 30

# Domain separator: the same secret is used for session tokens, and a digest
# from one context must never be usable in the other.
_HMAC_CONTEXT = b"ledgerai.invitation.email.v1"


class ProvisioningError(Exception):
    """Provisioning refused. The message is safe to show a signed-in user."""


class IdentityRevokedError(ProvisioningError):
    """This Clerk subject has been deleted and must not come back."""


def normalize_email(email: str) -> str:
    """One canonical form, used for both storing and matching.

    Case folding and NFKC come first so visually identical addresses agree.
    The local part is deliberately NOT stripped of dots or +tags: those are
    Gmail conventions, not standards, and treating `a.b@` as `ab@` would let an
    invitation for one address be redeemed by a different mailbox somewhere
    that treats them as distinct.
    """
    collapsed = unicodedata.normalize("NFKC", email or "").strip()
    if "@" not in collapsed:
        return collapsed.casefold()
    local, _, domain = collapsed.rpartition("@")
    return f"{local.casefold()}@{domain.casefold()}"


def email_hmac(email: str) -> str:
    """Keyed digest of a normalized address. Not reversible, not enumerable."""
    key = hashlib.sha256(_HMAC_CONTEXT + settings.auth_secret.encode()).digest()
    return hmac.new(key, normalize_email(email).encode(), hashlib.sha256).hexdigest()


def email_hint(email: str) -> str:
    """A lossy label so an administrator can tell invitations apart.

    Never enough to recover the address, which is the whole point of not
    storing it.
    """
    normalized = normalize_email(email)
    local, _, domain = normalized.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain[:1]}***.{domain.rpartition('.')[2]}"


# --------------------------------------------------------------------------
# Invitations
# --------------------------------------------------------------------------


def create_invitation(
    session: Session,
    email: str,
    *,
    ttl_days: int = DEFAULT_INVITE_TTL_DAYS,
    note: str = "",
) -> Invitation:
    """Create or refresh the invitation for an address.

    Idempotent by design: the unique `email_hmac` means inviting the same
    person twice refreshes the expiry rather than creating a second row an
    administrator would have to reconcile.
    """
    digest = email_hmac(email)
    expires = datetime.now(UTC) + timedelta(days=ttl_days)
    statement = (
        pg_insert(Invitation)
        .values(
            id=uuid.uuid4(),
            email_hmac=digest,
            email_hint=email_hint(email),
            expires_at=expires,
            note=note[:200],
        )
        .on_conflict_do_update(
            index_elements=["email_hmac"],
            set_={"expires_at": expires, "revoked_at": None, "note": note[:200]},
            # Never resurrect an invitation that has already been used.
            where=Invitation.redeemed_at.is_(None),
        )
    )
    session.execute(statement)
    session.flush()
    invitation = session.execute(
        select(Invitation).where(Invitation.email_hmac == digest)
    ).scalar_one()
    logger.info("invitation.created hint=%s expires_at=%s", invitation.email_hint, expires)
    return invitation


def revoke_invitation(session: Session, email: str) -> bool:
    digest = email_hmac(email)
    invitation = session.execute(
        select(Invitation).where(Invitation.email_hmac == digest)
    ).scalar_one_or_none()
    if invitation is None or invitation.redeemed_at is not None:
        return False
    invitation.revoked_at = datetime.now(UTC)
    session.flush()
    logger.info("invitation.revoked hint=%s", invitation.email_hint)
    return True


def _claim_invitation(session: Session, email: str, now: datetime) -> Invitation | None:
    """Atomically take an active invitation, or return None.

    `WHERE redeemed_at IS NULL` inside the UPDATE is what makes this safe under
    concurrency: two simultaneous first-requests both try, and exactly one row
    is affected. Checking first and updating second would let both through.
    """
    digest = email_hmac(email)
    claimed = session.execute(
        update(Invitation)
        .where(
            Invitation.email_hmac == digest,
            Invitation.redeemed_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > now,
        )
        .values(redeemed_at=now)
        .returning(Invitation.id)
    ).scalar_one_or_none()
    if claimed is None:
        return None
    return session.execute(select(Invitation).where(Invitation.id == claimed)).scalar_one()


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------


def provision_profile(
    session: Session,
    *,
    clerk_user_id: str,
    email: str | None = None,
    resolve_email: Callable[[], str | None] | None = None,
    display_name: str | None = None,
    now: datetime | None = None,
) -> User:
    """Find or create the Ledger profile for a verified Clerk subject.

    Lazy rather than webhook-driven, following Clerk's own guidance that
    webhook delivery is not guaranteed and introduces ordering races. The
    verified token is the source of truth, so there is no window in which a
    signed-in user has no profile.

    The address arrives through `resolve_email` rather than as a claim, because
    a Clerk session token does not carry one — and even a template that added
    one would say nothing about whether it had been verified. The callable is
    invoked only on the path that creates a profile, so a returning user costs
    no call to Clerk at all.
    """
    now = now or datetime.now(UTC)

    # 1. Tombstone first. A token minted before deletion is still
    #    cryptographically valid until it expires, and lazy provisioning would
    #    happily rebuild the account somebody asked us to erase.
    tombstone = session.get(DeletedIdentity, clerk_user_id)
    if tombstone is not None:
        raise IdentityRevokedError(
            "This account has been deleted. Sign up again with a new invitation "
            "if you would like to start over."
        )

    existing = session.execute(
        select(User).where(User.clerk_user_id == clerk_user_id)
    ).scalar_one_or_none()
    if existing is not None:
        if email and normalize_email(email) != normalize_email(existing.email):
            # Clerk lets a user change their address. The profile follows it;
            # the identity key does not move.
            existing.email = normalize_email(email)
        existing.last_seen_at = now
        return existing

    # Only now — with no profile to return — is the address worth fetching.
    if not email and resolve_email is not None:
        email = resolve_email()

    if not email:
        raise ProvisioningError("A verified email address is required to create an account.")

    if settings.beta_invite_only and _claim_invitation(session, email, now) is None:
        # No invitation, no profile — even though Clerk already let them in.
        # The second gate exists precisely so a misconfigured dashboard is not
        # the only thing standing between the internet and an account.
        raise ProvisioningError(
            "Ledger AI is in private beta. This email address has not been invited, "
            "or its invitation has already been used or expired."
        )

    normalized = normalize_email(email)
    statement = (
        pg_insert(User)
        .values(
            id=uuid.uuid4(),
            clerk_user_id=clerk_user_id,
            email=normalized,
            password_hash=None,
            display_name=(display_name or normalized.partition("@")[0])[:120],
            is_demo=False,
            created_via="invite",
            status="active",
            last_seen_at=now,
        )
        # Two concurrent first requests race here. The partial unique index on
        # clerk_user_id decides it; the loser inserts nothing and re-reads the
        # winner's row, so exactly one profile exists either way.
        #
        # `index_where` has to repeat the index's own predicate. Postgres
        # matches ON CONFLICT to an index by its definition, and without the
        # predicate there is no index matching the specification — the insert
        # fails outright rather than conflicting.
        .on_conflict_do_nothing(
            index_elements=["clerk_user_id"],
            index_where=User.clerk_user_id.isnot(None),
        )
    )
    session.execute(statement)
    session.flush()

    user = session.execute(
        select(User).where(User.clerk_user_id == clerk_user_id)
    ).scalar_one_or_none()
    if user is None:  # pragma: no cover - only reachable if the insert vanished
        raise ProvisioningError("Could not create your account. Please try again.")

    # Bind the claimed invitation to the profile it created.
    session.execute(
        update(Invitation)
        .where(Invitation.email_hmac == email_hmac(email), Invitation.redeemed_by.is_(None))
        .values(redeemed_by=user.id)
    )
    session.flush()
    logger.info("profile.provisioned created_via=invite")
    return user


def count_active_registrations_since(session: Session, since: datetime) -> int:
    """Input to the global registration circuit breaker (Phase 2)."""
    return int(
        session.execute(
            select(func.count())
            .select_from(User)
            .where(User.created_via == "invite", User.created_at >= since)
        ).scalar_one()
    )
