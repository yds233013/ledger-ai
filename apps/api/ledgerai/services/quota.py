"""Durable per-user budgets for persistent beta accounts.

Postgres is the authority. Redis already does burst rate limiting and keeps
doing exactly that — a Redis restart must never hand somebody a fresh daily
allowance, so nothing durable is kept there.

**Why reservations exist.** Counting committed rows is not enough. Between
"you are under your limit" and the insert that puts you over it, a concurrent
request passes the same check. So the check and the claim happen in one
statement: the daily row is locked, the reservation is written, and the
transaction commits or it does not. Two simultaneous uploads competing for the
last slot are resolved by the database rather than by timing.

**Lifecycle.** reserve → (the job runs) → commit, or release. The reservation
is taken in the request that accepts the upload and is held for as long as the
job is in flight — that hold *is* the concurrent-job limit, so converting it at
the end of the request would leave that limit enforcing nothing. The worker
converts it to committed usage once the upload completed, or releases it on
terminal failure, rejection and cancellation. A process that dies in between
leaves a row with an expiry; the sweep clears it, so the cost of a crash is a
little headroom for one sweep interval rather than a permanently lost slot.

**Deleting a file does not refund the day's upload count.** Storage, rows and
receipts all fall as soon as the data is gone, because those are counted rather
than kept. The daily counters are a record of what was sent today, and
refunding them would make the daily limit bypassable by uploading, deleting and
uploading again.

**Lifetime totals are derived, never accumulated.** Stored bytes, transaction
rows and receipts are counted from the authoritative tables at check time.
Accumulating them would drift the moment anything was deleted outside this
module, and a drifted counter locks somebody out of their own account with no
way to see why. Counting is slightly more work and cannot drift.

**Persistent accounts only.** Demo accounts keep the existing rate limits and
their 24-hour expiry, and are never charged against any of this.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from anyio import to_thread
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings
from ..models import (
    Receipt,
    Transaction,
    Upload,
    UsageReservation,
    User,
    UserUsage,
)

logger = logging.getLogger(__name__)

# How long a reservation may outlive the request that made it. Long enough that
# a slow OCR job is never cut off, short enough that a crash does not strand a
# slot for the rest of the day.
RESERVATION_TTL = timedelta(minutes=30)


class QuotaExceededError(Exception):
    """One budget is exhausted. Carries what the caller may safely tell the user."""

    def __init__(self, quota: str, limit: int, remaining: int, resets_at: datetime | None):
        self.quota = quota
        self.limit = limit
        self.remaining = max(0, remaining)
        self.resets_at = resets_at
        super().__init__(f"quota exceeded: {quota}")

    @property
    def detail(self) -> str:
        return _MESSAGES.get(self.quota, "You have reached a private-beta limit.")

    def headers(self) -> dict[str, str]:
        """Remaining and reset, in the conventional headers.

        Safe to expose: they describe the caller's own budget and nothing else.
        """
        out = {"X-Quota": self.quota, "X-Quota-Limit": str(self.limit),
               "X-Quota-Remaining": str(self.remaining)}
        if self.resets_at is not None:
            out["X-Quota-Reset"] = self.resets_at.isoformat()
        return out


_MESSAGES = {
    "uploads_per_day": (
        "You have reached the private-beta limit for uploads today. "
        "It resets at midnight UTC."
    ),
    "upload_bytes_per_day": (
        "You have reached the private-beta limit for data uploaded today. "
        "It resets at midnight UTC."
    ),
    "stored_bytes": (
        "You have reached the private-beta storage limit. Delete a file or two "
        "from Settings to free space."
    ),
    "transaction_rows": (
        "You have reached the private-beta limit for stored transactions."
    ),
    "receipts": "You have reached the private-beta limit for stored receipts.",
    "concurrent_jobs": (
        "You already have files being processed. Please wait for them to finish."
    ),
}


@dataclass(frozen=True)
class Reservation:
    """A held claim. Commit it or release it; never both."""

    id: uuid.UUID
    bytes_reserved: int


def utc_today(now: datetime | None = None) -> date:
    """The UTC day a usage row belongs to.

    UTC rather than a local zone: the reset must not move when somebody
    travels, and the server has no business guessing where they are.
    """
    return (now or datetime.now(UTC)).astimezone(UTC).date()


def next_utc_midnight(now: datetime | None = None) -> datetime:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return datetime.combine(moment.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)


def applies_to(user: User) -> bool:
    """Persistent accounts only."""
    return not user.is_demo and user.clerk_user_id is not None


def _lock_day_row(session: Session, user_id: uuid.UUID, day: date) -> UserUsage:
    """Get or create today's row and hold it for the rest of the transaction.

    The lock is what makes two concurrent requests serialise. Insert-then-lock
    rather than lock-then-insert, because the row may not exist yet and two
    requests may try to create it at once.
    """
    session.execute(
        pg_insert(UserUsage)
        .values(id=uuid.uuid4(), user_id=user_id, usage_date=day, uploads_today=0, bytes_today=0)
        .on_conflict_do_nothing(index_elements=["user_id", "usage_date"])
    )
    session.flush()
    row = session.execute(
        select(UserUsage)
        .where(UserUsage.user_id == user_id, UserUsage.usage_date == day)
        .with_for_update()
    ).scalar_one()
    return row


def _reserved_totals(session: Session, user_id: uuid.UUID, day: date) -> tuple[int, int, int]:
    """(count, bytes, live) currently held but not yet committed."""
    now = datetime.now(UTC)
    rows = session.execute(
        select(UsageReservation).where(
            UsageReservation.user_id == user_id, UsageReservation.expires_at > now
        )
    ).scalars().all()
    today = [r for r in rows if r.usage_date == day]
    return len(today), sum(r.bytes_reserved for r in today), len(rows)


def _committed_counts(session: Session, user_id: uuid.UUID) -> tuple[int, int, int]:
    """Lifetime totals, counted from the authoritative tables rather than kept."""
    # Only uploads whose object still exists. A file whose bytes were purged —
    # by the retention sweep or by a rejection — occupies no storage, and
    # charging for it would be charging for something that is gone.
    stored = session.scalar(
        select(func.coalesce(func.sum(Upload.size_bytes), 0)).where(
            Upload.user_id == user_id, Upload.storage_key != ""
        )
    ) or 0
    txns = session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.user_id == user_id)
    ) or 0
    receipts = session.scalar(
        select(func.count()).select_from(Receipt).where(Receipt.user_id == user_id)
    ) or 0
    return int(stored), int(txns), int(receipts)


def reserve_upload(
    session: Session,
    user_id: uuid.UUID,
    size_bytes: int,
    *,
    now: datetime | None = None,
) -> Reservation:
    """Claim budget for one upload, or raise QuotaExceededError.

    Runs inside a transaction the caller commits — and the caller must commit
    promptly, because an uncommitted reservation is invisible to the concurrent
    request it exists to block.
    """
    moment = now or datetime.now(UTC)
    day = utc_today(moment)
    reset = next_utc_midnight(moment)

    row = _lock_day_row(session, user_id, day)
    held_count, held_bytes, held_live = _reserved_totals(session, user_id, day)
    stored, txns, receipts = _committed_counts(session, user_id)

    def refuse(quota: str, limit: int, used: int, resets: datetime | None) -> None:
        raise QuotaExceededError(quota, limit, limit - used, resets)

    if held_live >= settings.quota_concurrent_jobs:
        refuse("concurrent_jobs", settings.quota_concurrent_jobs, held_live, None)
    if row.uploads_today + held_count >= settings.quota_uploads_per_day:
        refuse("uploads_per_day", settings.quota_uploads_per_day,
               row.uploads_today + held_count, reset)
    if row.bytes_today + held_bytes + size_bytes > settings.quota_upload_bytes_per_day:
        refuse("upload_bytes_per_day", settings.quota_upload_bytes_per_day,
               row.bytes_today + held_bytes, reset)
    if stored + held_bytes + size_bytes > settings.quota_stored_bytes:
        refuse("stored_bytes", settings.quota_stored_bytes, stored + held_bytes, None)
    if txns >= settings.quota_transaction_rows:
        refuse("transaction_rows", settings.quota_transaction_rows, txns, None)
    if receipts >= settings.quota_receipts:
        refuse("receipts", settings.quota_receipts, receipts, None)

    reservation = UsageReservation(
        id=uuid.uuid4(),
        user_id=user_id,
        upload_id=None,
        bytes_reserved=size_bytes,
        usage_date=day,
        expires_at=moment + RESERVATION_TTL,
    )
    session.add(reservation)
    session.flush()
    logger.info("quota.reserved bytes=%d", size_bytes)
    return Reservation(id=reservation.id, bytes_reserved=size_bytes)


def attach_upload(session: Session, reservation: Reservation, upload_id: uuid.UUID) -> None:
    """Bind a held reservation to the upload it turned into."""
    row = session.get(UsageReservation, reservation.id)
    if row is not None:
        row.upload_id = upload_id
        session.flush()


def release(session: Session, reservation: Reservation) -> None:
    """Give the claim back. Safe to call twice."""
    session.execute(delete(UsageReservation).where(UsageReservation.id == reservation.id))
    session.flush()
    logger.info("quota.released")


def commit_by_upload(session: Session, upload_id: uuid.UUID) -> None:
    """Convert the claim held for one upload — called when its job completes.

    Silent when no reservation is found: the account may be exempt, or the
    reservation may have already been converted by an earlier attempt of the
    same job. Both are fine, and neither should fail a completed upload.
    """
    row = session.execute(
        select(UsageReservation).where(UsageReservation.upload_id == upload_id)
    ).scalars().first()
    if row is None:
        return
    usage = _lock_day_row(session, row.user_id, row.usage_date)
    usage.uploads_today += 1
    usage.bytes_today += row.bytes_reserved
    session.delete(row)
    session.flush()
    logger.info("quota.committed bytes=%d", row.bytes_reserved)


def release_for_upload(session: Session, upload_id: uuid.UUID) -> None:
    """Release whatever is held against an upload — terminal failure, deletion.

    Deleting the upload row would cascade this away anyway; calling it directly
    is for the cases where the row survives its reservation.
    """
    session.execute(delete(UsageReservation).where(UsageReservation.upload_id == upload_id))
    session.flush()


def sweep_expired(session: Session, *, now: datetime | None = None) -> int:
    """Clear reservations whose work never finished.

    Idempotent. This is what makes a crash between reserve and commit cost one
    sweep interval of headroom rather than a slot lost until midnight.
    """
    moment = now or datetime.now(UTC)
    stale = list(
        session.execute(
            select(UsageReservation.id).where(UsageReservation.expires_at <= moment)
        ).scalars()
    )
    if stale:
        session.execute(delete(UsageReservation).where(UsageReservation.id.in_(stale)))
        session.flush()
    return len(stale)


def snapshot(
    session: Session, user_id: uuid.UUID, *, now: datetime | None = None
) -> dict[str, object]:
    """Current usage against each budget, for the caller's own account.

    Everything here describes the requesting user's own consumption, so it is
    safe to return to them.
    """
    moment = now or datetime.now(UTC)
    day = utc_today(moment)
    row = session.execute(
        select(UserUsage).where(UserUsage.user_id == user_id, UserUsage.usage_date == day)
    ).scalar_one_or_none()
    stored, txns, receipts = _committed_counts(session, user_id)
    held_count, held_bytes, held_live = _reserved_totals(session, user_id, day)

    return {
        "resets_at": next_utc_midnight(moment).isoformat(),
        "uploads_today": (row.uploads_today if row else 0) + held_count,
        "uploads_per_day": settings.quota_uploads_per_day,
        "bytes_today": (row.bytes_today if row else 0) + held_bytes,
        "upload_bytes_per_day": settings.quota_upload_bytes_per_day,
        "stored_bytes": stored,
        "stored_bytes_limit": settings.quota_stored_bytes,
        "transaction_rows": txns,
        "transaction_rows_limit": settings.quota_transaction_rows,
        "receipts": receipts,
        "receipts_limit": settings.quota_receipts,
        "jobs_in_flight": held_live,
        "concurrent_jobs_limit": settings.quota_concurrent_jobs,
    }


def reconcile(
    session: Session, user_id: uuid.UUID, *, now: datetime | None = None
) -> dict[str, int]:
    """Repair a user's counters from authoritative state.

    Rebuilds today's committed daily counters from the uploads actually stored,
    and clears expired reservations. Trusts only the database — never anything
    a user sent — so it is safe to run at any time, and running it twice
    changes nothing the second time.
    """
    moment = now or datetime.now(UTC)
    day = utc_today(moment)
    swept = sweep_expired(session, now=moment)

    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    rows = session.execute(
        select(Upload).where(Upload.user_id == user_id, Upload.created_at >= start)
    ).scalars().all()

    usage = _lock_day_row(session, user_id, day)
    usage.uploads_today = len(rows)
    usage.bytes_today = sum(int(u.size_bytes or 0) for u in rows)
    session.flush()

    logger.info(
        "quota.reconciled uploads=%d reservations_swept=%d", len(rows), swept
    )
    return {"uploads_today": len(rows), "reservations_swept": swept}


# ---------------------------------------------------------------------------
# Bridges for the async request path
# ---------------------------------------------------------------------------
#
# Each of these runs its own short sync transaction and commits it. That is
# deliberate rather than incidental: a reservation held inside the request's
# open transaction is invisible to the concurrent request it exists to block,
# so it would enforce nothing. Committing immediately is what makes the claim
# real to everybody else.


async def reserve_for_request(
    factory: sessionmaker[Session], user: User, size_bytes: int
) -> Reservation | None:
    """Reserve budget for an upload. None when quotas do not apply."""
    if not applies_to(user):
        return None
    user_id = user.id

    def _run() -> Reservation:
        with factory() as sync_session:
            reservation = reserve_upload(sync_session, user_id, size_bytes)
            sync_session.commit()
            return reservation

    return await to_thread.run_sync(_run)


async def release_for_request(
    factory: sessionmaker[Session], reservation: Reservation | None
) -> None:
    """Hand a claim back. Never raises: a failed release must not mask the
    failure that prompted it, and the sweep collects anything left behind."""
    if reservation is None:
        return

    def _run() -> None:
        with factory() as sync_session:
            release(sync_session, reservation)
            sync_session.commit()

    try:
        await to_thread.run_sync(_run)
    except Exception:  # noqa: BLE001 - best effort; the sweep is the backstop
        logger.warning("quota.release_failed", exc_info=True)


async def attach_in_transaction(
    session: AsyncSession, reservation: Reservation | None, upload_id: uuid.UUID
) -> None:
    """Bind a held claim to its upload, inside the caller's own transaction.

    Deliberately not one of the self-committing bridges above. The upload row
    does not exist outside this transaction yet, so a separate connection
    writing the foreign key would violate it; and binding after the commit
    would race a fast worker to the reservation. Joining the transaction that
    creates the upload settles both: the row and the binding become visible in
    the same instant.
    """
    if reservation is None:
        return
    await session.execute(
        update(UsageReservation)
        .where(UsageReservation.id == reservation.id)
        .values(upload_id=upload_id)
    )


async def snapshot_for_request(
    factory: sessionmaker[Session], user: User
) -> dict[str, object]:
    if not applies_to(user):
        return {"applies": False}
    user_id = user.id

    def _run() -> dict[str, object]:
        with factory() as sync_session:
            return snapshot(sync_session, user_id)

    result = await to_thread.run_sync(_run)
    return {"applies": True, **result}
