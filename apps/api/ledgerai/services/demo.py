"""Ephemeral per-visitor demo accounts.

A portfolio reviewer should be able to click one button and land in a populated
Ledger AI, without an account, without a password, and without being able to
see or disturb anyone else who is doing the same thing.

The design in one line: **a demo visitor is a real user row**, so every
`user_id` predicate that already protects real accounts protects demo visitors
too. Nothing about isolation is special-cased for the demo, which is precisely
why it can be trusted — there is no second, weaker code path to get wrong.

Three properties this module has to guarantee:

* **Idempotent provisioning.** The caller supplies a request key. It is UNIQUE
  on `users`, so a retried request returns the account the first attempt
  created rather than building a second 250-row dataset, and two concurrent
  requests with the same key cannot both win.

* **All-or-nothing.** The user, their accounts and every transaction are
  written in ONE transaction. A failure half-way leaves no user at all, rather
  than an empty shell someone then signs into. Retrying with the same key
  starts cleanly, because the failed attempt left no row to collide with.

* **Bounded lifetime.** `demo_expires_at` lives on the row, not in the token.
  Refreshing the page mints a new token but cannot move that column, so a demo
  session cannot be extended by staying logged in.

`demo_expires_at IS NOT NULL` is the single marker of an ephemeral account and
the only thing the cleanup sweep selects on. The permanent local development
demo user has `is_demo=True` with that column NULL, and a real account has
neither — so neither can be reached by the sweep. See
`tests/test_demo.py::TestCleanupSafety`.
"""

from __future__ import annotations

import logging
import random
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..models import Account, Category, ProcessingJob, Transaction, User
from .alerts import analyze_user
from .analysis.cache import purge_user_cache_sync
from .categorize import (
    CategorizationContext,
    RuleCategorizer,
    TransactionCandidate,
    build_merchant_rule_index,
)
from .demo_data import (
    ACCOUNTS,
    DEMO_DENSITY,
    DEMO_MONTHS_OF_HISTORY,
    SYNTHETIC_MARKER,
    build_transactions,
)
from .ingest import load_merchant_rule_definitions
from .normalize import (
    compute_dedupe_hash,
    extract_merchant,
    merchant_key,
    normalize_description,
)
from .storage import StorageError, get_storage

logger = logging.getLogger(__name__)

# How long a demo account works for. Long enough to come back to after lunch,
# short enough that abandoned accounts do not accumulate.
DEMO_LIFETIME_HOURS = 24

# Ephemeral accounts get an address in a domain that cannot receive mail, so a
# demo account can never collide with, or be mistaken for, a real sign-up.
DEMO_EMAIL_DOMAIN = "demo.ledgerai.invalid"

DEMO_DISPLAY_NAME = "Demo Visitor (Synthetic Data)"

# Shown wherever the demo account's nature needs stating in the UI.
DEMO_DATA_NOTICE = (
    "This is a temporary demo account. Every transaction in it is synthetic and "
    "was generated for demonstration — it does not describe any real person, "
    "account or payment."
)


@dataclass(slots=True)
class DemoAccountInfo:
    """Plain values describing a provisioned demo account.

    Deliberately not an ORM object: provisioning runs in its own session on a
    worker thread, and handing a detached instance back to the request would
    invite a lazy load against a closed session.
    """

    user_id: uuid.UUID
    email: str
    display_name: str
    expires_at: datetime
    transaction_count: int
    account_count: int
    alert_count: int
    reused: bool = False

    @property
    def expires_in_seconds(self) -> int:
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))


@dataclass(slots=True)
class DemoCleanupReport:
    """What one cleanup pass removed."""

    users_removed: int = 0
    storage_objects_removed: int = 0
    cache_keys_removed: int = 0
    queued_jobs_cancelled: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def is_ephemeral_demo(user: User) -> bool:
    """Whether this account is a per-visitor demo that expires."""
    return bool(user.is_demo and user.demo_expires_at is not None)


def demo_has_expired(user: User, now: datetime | None = None) -> bool:
    """Whether an ephemeral demo account has passed its deadline.

    A permanent account always answers False, so callers can ask
    unconditionally without first checking what kind of account it is.
    """
    if not is_ephemeral_demo(user):
        return False
    deadline = user.demo_expires_at
    assert deadline is not None  # narrowed by is_ephemeral_demo  # noqa: S101
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline <= (now or datetime.now(UTC))


def new_request_key() -> str:
    """A provisioning idempotency key, when the caller did not supply one."""
    return secrets.token_urlsafe(24)


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------


def _existing_for_key(session: Session, request_key: str) -> User | None:
    return session.execute(
        select(User).where(User.demo_request_key == request_key)
    ).scalar_one_or_none()


def _describe(session: Session, user: User, *, reused: bool) -> DemoAccountInfo:
    from sqlalchemy import func

    from ..models import Alert

    counts = session.execute(
        select(
            select(func.count(Transaction.id))
            .where(Transaction.user_id == user.id)
            .scalar_subquery(),
            select(func.count(Account.id))
            .where(Account.user_id == user.id)
            .scalar_subquery(),
            select(func.count(Alert.id)).where(Alert.user_id == user.id).scalar_subquery(),
        )
    ).one()

    expires = user.demo_expires_at or datetime.now(UTC)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)

    return DemoAccountInfo(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        expires_at=expires,
        transaction_count=int(counts[0]),
        account_count=int(counts[1]),
        alert_count=int(counts[2]),
        reused=reused,
    )


def _seed_dataset(session: Session, user: User, rng: random.Random, today) -> None:
    """Write the synthetic accounts and transactions for one demo user.

    Runs inside the caller's transaction. Mirrors the ingestion pipeline's own
    ordering — normalize, categorize, dedupe-hash — so the demo dataset is the
    same shape as data that arrived through a real CSV upload, rather than a
    privileged shortcut that behaves differently.
    """
    accounts: list[Account] = []
    for name, institution, account_type, mask in ACCOUNTS:
        account = Account(
            user_id=user.id,
            name=name,
            institution=institution,
            account_type=account_type,
            mask=mask,
            currency="USD",
        )
        session.add(account)
        accounts.append(account)
    session.flush()

    categorizer = RuleCategorizer()
    context = CategorizationContext(
        correction_memory={},
        merchant_rules=build_merchant_rule_index(load_merchant_rule_definitions()),
    )
    category_ids = {
        row.slug: row.id
        for row in session.execute(
            select(Category.slug, Category.id).where(Category.is_system.is_(True))
        ).all()
    }

    raw_rows = build_transactions(rng, today, DEMO_MONTHS_OF_HISTORY, DEMO_DENSITY)
    raw_rows.sort(key=lambda row: row["date"])

    payloads = []
    for index, row in enumerate(raw_rows):
        # The marker makes it impossible to mistake this for real data.
        description = f"{row['description']} {SYNTHETIC_MARKER}"
        merchant = extract_merchant(row["description"])
        normalized = normalize_description(description)
        account = accounts[row["account"]]

        suggestion = categorizer.categorize(
            TransactionCandidate(
                merchant=merchant,
                merchant_key=merchant_key(merchant),
                normalized_description=normalized,
                amount_cents=row["cents"],
                posted_date=row["date"],
            ),
            context,
        )

        payloads.append({
            "id": uuid.uuid4(),
            "user_id": user.id,
            "account_id": account.id,
            "upload_id": None,
            "posted_date": row["date"],
            "amount_cents": row["cents"],
            "currency": row.get("currency", "USD"),
            "raw_description": description,
            "normalized_description": normalized,
            "merchant": merchant,
            "merchant_key": merchant_key(merchant),
            "category_id": category_ids.get(suggestion.category_slug),
            "confidence": suggestion.confidence,
            "categorized_by": suggestion.source,
            "needs_review": suggestion.needs_review,
            "is_corrected": False,
            # Includes the user id, so two demo visitors generating identical
            # rows never collide on the global dedupe_hash unique index.
            "dedupe_hash": compute_dedupe_hash(
                user.id, account.id, row["date"], row["cents"], normalized, index
            ),
            "source_row_index": index,
        })

    if payloads:
        session.execute(
            pg_insert(Transaction)
            .values(payloads)
            .on_conflict_do_nothing(index_elements=["dedupe_hash"])
        )
    session.flush()

    # Populate the alerts surface, so the dashboard is not empty on first load.
    analyze_user(session, user.id)


def provision_demo_user(
    factory: sessionmaker[Session],
    *,
    request_key: str,
    now: datetime | None = None,
    seed: int | None = None,
) -> DemoAccountInfo:
    """Create (or return) the demo account for `request_key`.

    Synchronous on purpose. Seeding runs the real categorizer and the real
    alert detectors over ~250 rows, both of which are sync services; the HTTP
    layer calls this on a worker thread rather than duplicating them in async
    form. See `routers/auth.py`.
    """
    now = now or datetime.now(UTC)
    rng = random.Random(seed if seed is not None else secrets.randbits(64))

    with factory() as session:
        existing = _existing_for_key(session, request_key)
        if existing is not None:
            # A retry of a request that already succeeded.
            logger.info("Demo provisioning reused an existing account for this request")
            return _describe(session, existing, reused=True)

        user = User(
            # Unguessable, so one visitor cannot address another's account even
            # by knowing the scheme.
            email=f"demo-{uuid.uuid4().hex}@{DEMO_EMAIL_DOMAIN}",
            # A random hash no one holds the pre-image of. A demo account has no
            # password to sign in with, and the seeded development account's
            # credentials are never reused or exposed here.
            password_hash=secrets.token_urlsafe(48),
            display_name=DEMO_DISPLAY_NAME,
            is_demo=True,
            demo_expires_at=now + timedelta(hours=DEMO_LIFETIME_HOURS),
            demo_request_key=request_key,
        )
        session.add(user)

        try:
            # Claims the unique request key before the expensive work, so a
            # concurrent duplicate blocks here rather than seeding in parallel.
            session.flush()
        except IntegrityError:
            session.rollback()
            winner = _existing_for_key(session, request_key)
            if winner is None:
                raise
            logger.info("Demo provisioning lost a race for this request; reusing the winner")
            return _describe(session, winner, reused=True)

        _seed_dataset(session, user, rng, now.date())

        info = _describe(session, user, reused=False)
        # One commit: the user, the accounts, every transaction and every alert
        # become visible together or not at all.
        session.commit()

    logger.info(
        "Provisioned demo account %s (%d transactions, %d alerts, expires %s)",
        info.user_id,
        info.transaction_count,
        info.alert_count,
        info.expires_at.isoformat(),
    )
    return info


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------


def expired_demo_user_ids(session: Session, now: datetime | None = None) -> list[uuid.UUID]:
    """Ephemeral demo accounts whose deadline has passed.

    The predicate is the safety property. `demo_expires_at IS NOT NULL` is what
    keeps the permanent development demo user and every real account out of
    this list; `is_demo` is belt-and-braces on top of it.
    """
    now = now or datetime.now(UTC)
    return list(
        session.execute(
            select(User.id).where(
                User.is_demo.is_(True),
                User.demo_expires_at.is_not(None),
                User.demo_expires_at < now,
            )
        ).scalars().all()
    )


def _cancel_queued_jobs(session: Session, user_id: uuid.UUID) -> int:
    """Cancel RQ jobs still pending, so a worker does not wake to a dead user."""
    from ..models import JobStage

    pending = session.execute(
        select(ProcessingJob.rq_job_id).where(
            ProcessingJob.user_id == user_id,
            ProcessingJob.rq_job_id.is_not(None),
            ProcessingJob.stage.notin_([JobStage.COMPLETE, JobStage.FAILED]),
        )
    ).scalars().all()

    job_ids = [job_id for job_id in pending if job_id]
    if not job_ids:
        return 0

    cancelled = 0
    try:
        from rq.job import Job

        from ..jobs.queue import get_redis

        connection = get_redis()
        for job_id in job_ids:
            try:
                Job.fetch(job_id, connection=connection).cancel()
                cancelled += 1
            except Exception:  # noqa: BLE001, S112 - an already-gone job is fine
                logger.debug("Queued job %s was already gone", job_id)
    except Exception:  # noqa: BLE001 - a broker outage must not block cleanup
        logger.warning("Could not reach the queue while cancelling demo jobs")
    return cancelled


def delete_demo_user(session: Session, user_id: uuid.UUID) -> DemoCleanupReport:
    """Remove one ephemeral demo account and everything it owns.

    Reaches the same four places account deletion does — the database, object
    storage, the analysis cache and the queue — and refuses to touch an account
    that is not an expired ephemeral demo, so a mistaken call cannot delete a
    real user.

    Idempotent: a second call finds no row and reports zero.
    """
    report = DemoCleanupReport()

    user = session.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if user is None:
        return report

    if not is_ephemeral_demo(user):
        # Not an assertion to be caught: reaching here means a caller passed an
        # id the sweep predicate could never have produced.
        raise ValueError(
            "delete_demo_user refuses a user that is not an ephemeral demo account"
        )

    report.queued_jobs_cancelled = _cancel_queued_jobs(session, user_id)

    try:
        report.storage_objects_removed = get_storage().delete_prefix(f"users/{user_id}/")
    except StorageError:
        report.errors.append("Some stored files for a demo account could not be removed.")
        logger.warning("Storage cleanup failed for demo user %s", user_id)

    report.cache_keys_removed = purge_user_cache_sync(user_id)

    # Every users.id foreign key is ON DELETE CASCADE, so this one statement
    # removes accounts, transactions, categories, uploads, jobs, receipts,
    # corrections, alerts, analysis runs and their steps.
    session.execute(delete(User).where(User.id == user_id))
    session.flush()

    report.users_removed = 1
    return report


def cleanup_expired_demo_users(
    session: Session, now: datetime | None = None, limit: int = 200
) -> DemoCleanupReport:
    """Delete every expired ephemeral demo account.

    Safe to run repeatedly and concurrently with live traffic: each account is
    removed independently, and one failure does not abandon the rest. `limit`
    bounds a single pass so a large backlog cannot hold a transaction open
    indefinitely — the next pass picks up the remainder.
    """
    total = DemoCleanupReport()
    expired = expired_demo_user_ids(session, now)[:limit]

    for user_id in expired:
        try:
            one = delete_demo_user(session, user_id)
        except Exception:  # noqa: BLE001 - one bad account must not stop the sweep
            logger.exception("Could not remove expired demo user %s", user_id)
            total.errors.append("One expired demo account could not be removed.")
            session.rollback()
            continue
        total.users_removed += one.users_removed
        total.storage_objects_removed += one.storage_objects_removed
        total.cache_keys_removed += one.cache_keys_removed
        total.queued_jobs_cancelled += one.queued_jobs_cancelled
        total.errors.extend(one.errors)

    if expired:
        logger.info(
            "Demo cleanup: removed %d expired account(s), %d file(s), %d cache key(s)",
            total.users_removed,
            total.storage_objects_removed,
            total.cache_keys_removed,
        )
    return total
