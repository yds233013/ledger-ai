"""Deleting a persistent account across five systems that cannot be atomic.

Deletion has to reach Postgres, Redis, the RQ queue, R2 and Clerk. There is no
transaction spanning those, so the design assumes it will be interrupted and
makes every step resumable instead of pretending it won't be.

**The tombstone is written first.** Before anything is removed, a row in
`deleted_identities` records the intent. That single fact does the work of
several guarantees:

* access is denied immediately — the request that follows a deletion is
  rejected by the tombstone, not by the absence of a profile
* lazy provisioning cannot rebuild the account. A token minted before deletion
  stays cryptographically valid until it expires, and without the tombstone the
  next request would recreate exactly what the user asked us to erase
* a repeat request is a no-op rather than a second deletion
* a crash leaves a record of unfinished work for the reconciler

**State machine.** `pending → storage_purged → complete`, advancing only when a
step has actually succeeded:

    pending          nothing removed yet, or our data still being removed
    storage_purged   Postgres/Redis/queue/R2 done; Clerk identity still exists
    complete         Clerk identity revoked too; `user_id` cleared

The two partial-failure cases the ordering is chosen for:

* **our cleanup fails, Clerk succeeded** — the tombstone stays `pending` and the
  reconciler retries. The identity is already gone at the provider, so nothing
  can sign in meanwhile.
* **our cleanup succeeded, Clerk failed** — the tombstone sits at
  `storage_purged`, which still denies provisioning, so the identity remains
  blocked at our end while the reconciler retries the revocation.

**Webhooks are not the guarantee.** Clerk's own documentation says delivery is
not guaranteed. The webhook only records intent; a sweep on the existing worker
finishes the job, so a dropped or delayed delivery costs time and not
correctness.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models import DeletedIdentity, User

logger = logging.getLogger(__name__)

STATE_PENDING = "pending"
STATE_STORAGE_PURGED = "storage_purged"
STATE_COMPLETE = "complete"

UNFINISHED_STATES = (STATE_PENDING, STATE_STORAGE_PURGED)

# Bounded so a permanently failing identity cannot be retried forever. It stays
# in the table denying provisioning either way — the retry stops, the block
# does not.
MAX_ATTEMPTS = 20


def record_deletion_intent(
    session: Session,
    *,
    clerk_user_id: str,
    now: datetime | None = None,
) -> DeletedIdentity:
    """Write the tombstone and mark the profile. Idempotent.

    Called by both the in-app deletion route and the webhook. Running it twice —
    or running both at once — converges on one row.
    """
    now = now or datetime.now(UTC)

    user = session.execute(
        select(User).where(User.clerk_user_id == clerk_user_id)
    ).scalar_one_or_none()

    session.execute(
        pg_insert(DeletedIdentity)
        .values(
            clerk_user_id=clerk_user_id,
            state=STATE_PENDING,
            requested_at=now,
            user_id=user.id if user else None,
        )
        # A second request must not reset progress already made, so only the
        # profile id is refreshed and only while it is still unknown.
        .on_conflict_do_update(
            index_elements=["clerk_user_id"],
            set_={"user_id": user.id if user else None},
            where=DeletedIdentity.user_id.is_(None),
        )
    )

    if user is not None:
        # Denies every authenticated route immediately, without waiting for the
        # rows to actually go.
        user.status = "pending_deletion"

    session.flush()
    logger.info("account_deletion.intent_recorded state=%s", STATE_PENDING)
    return session.get(DeletedIdentity, clerk_user_id)  # type: ignore[return-value]


def is_revoked(session: Session, clerk_user_id: str) -> bool:
    """Whether this subject is tombstoned, in any state."""
    return session.get(DeletedIdentity, clerk_user_id) is not None


def mark_storage_purged(session: Session, clerk_user_id: str) -> None:
    tombstone = session.get(DeletedIdentity, clerk_user_id)
    if tombstone is None or tombstone.state == STATE_COMPLETE:
        return
    tombstone.state = STATE_STORAGE_PURGED
    session.flush()


def mark_complete(session: Session, clerk_user_id: str, now: datetime | None = None) -> None:
    tombstone = session.get(DeletedIdentity, clerk_user_id)
    if tombstone is None:
        return
    tombstone.state = STATE_COMPLETE
    tombstone.completed_at = now or datetime.now(UTC)
    tombstone.last_error = ""
    # The profile is gone; keeping its id would be data the tombstone does not
    # need in order to refuse a subject.
    tombstone.user_id = None
    session.flush()


def record_attempt(
    session: Session,
    clerk_user_id: str,
    *,
    error: BaseException | None = None,
    now: datetime | None = None,
) -> None:
    """Count an attempt and record only an exception CLASS name.

    Never the message: an exception string can quote a row, a filename or a
    connection string, and this table is not allowed to hold any of that.
    """
    tombstone = session.get(DeletedIdentity, clerk_user_id)
    if tombstone is None:
        return
    tombstone.attempts += 1
    tombstone.last_attempt_at = now or datetime.now(UTC)
    tombstone.last_error = type(error).__name__[:80] if error else ""
    session.flush()


def unfinished(session: Session, limit: int = 50) -> list[DeletedIdentity]:
    """Tombstones the reconciler should pick up."""
    return list(
        session.execute(
            select(DeletedIdentity)
            .where(
                DeletedIdentity.state.in_(UNFINISHED_STATES),
                DeletedIdentity.attempts < MAX_ATTEMPTS,
            )
            .order_by(DeletedIdentity.requested_at)
            .limit(limit)
        ).scalars()
    )


def summarize(report: Mapping[str, Any] | None) -> dict[str, int]:
    """Integer counts only, for logs and audit rows."""
    if not report:
        return {}
    return {k: v for k, v in report.items() if isinstance(v, int) and not isinstance(v, bool)}
