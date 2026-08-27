"""Run the detectors over a user's transactions and persist what they find.

Idempotent by construction: `alerts` carries UNIQUE(transaction_id, alert_type),
so re-running detection over the same data inserts nothing new. That means the
pipeline can safely re-analyze after a retry, and a backfill can be run as often
as you like.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ...models import Alert, AlertStatus, Category, Transaction
from .detectors import DetectionContext, HistoricalCharge, detect_all

logger = logging.getLogger(__name__)

# Trailing window for the per-category distribution.
CATEGORY_WINDOW_DAYS = 90


def _load_history(
    session: Session, user_id: uuid.UUID, transaction: Transaction
) -> tuple[list[HistoricalCharge], list[HistoricalCharge]]:
    """Category history (trailing window) and merchant history (all time).

    Both are scoped to the user and exclude the transaction being examined.
    """
    window_start = transaction.posted_date - timedelta(days=CATEGORY_WINDOW_DAYS)

    category_rows = (
        session.execute(
            select(
                Transaction.id,
                Transaction.posted_date,
                Transaction.amount_cents,
                Transaction.merchant_key,
                Category.slug,
                Transaction.upload_id,
            )
            .outerjoin(Category, Transaction.category_id == Category.id)
            .where(
                Transaction.user_id == user_id,
                Transaction.id != transaction.id,
                Transaction.amount_cents < 0,
                Transaction.category_id == transaction.category_id,
                Transaction.posted_date >= window_start,
                Transaction.posted_date <= transaction.posted_date,
                Transaction.currency == transaction.currency,
            )
        ).all()
        if transaction.category_id is not None
        else []
    )

    merchant_rows = session.execute(
        select(
            Transaction.id,
            Transaction.posted_date,
            Transaction.amount_cents,
            Transaction.merchant_key,
            Category.slug,
            Transaction.upload_id,
        )
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.id != transaction.id,
            Transaction.amount_cents < 0,
            Transaction.merchant_key == transaction.merchant_key,
            Transaction.posted_date <= transaction.posted_date,
            Transaction.currency == transaction.currency,
        )
    ).all()

    def to_charges(rows) -> list[HistoricalCharge]:  # noqa: ANN001
        return [
            HistoricalCharge(
                transaction_id=row[0],
                posted_date=row[1],
                amount_cents=row[2],
                merchant_key=row[3],
                category_slug=row[4],
                upload_id=row[5],
            )
            for row in rows
        ]

    return to_charges(category_rows), to_charges(merchant_rows)


def analyze_transaction(
    session: Session, user_id: uuid.UUID, transaction: Transaction
) -> int:
    """Detect and persist alerts for one transaction. Returns how many were new."""
    # Income and transfers are not "charges"; alerting on them is noise.
    if transaction.amount_cents >= 0:
        return 0

    category_name = None
    category_slug = None
    if transaction.category_id is not None:
        row = session.execute(
            select(Category.slug, Category.name).where(
                Category.id == transaction.category_id
            )
        ).first()
        if row is not None:
            category_slug, category_name = row[0], row[1]

    if category_slug == "transfers":
        return 0

    category_history, merchant_history = _load_history(session, user_id, transaction)

    context = DetectionContext(
        transaction_id=transaction.id,
        posted_date=transaction.posted_date,
        amount_cents=transaction.amount_cents,
        merchant=transaction.merchant,
        merchant_key=transaction.merchant_key,
        category_slug=category_slug,
        category_name=category_name,
        upload_id=transaction.upload_id,
        category_history=category_history,
        merchant_history=merchant_history,
    )

    created = 0
    for candidate in detect_all(context):
        statement = (
            pg_insert(Alert)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                transaction_id=transaction.id,
                alert_type=candidate.alert_type,
                severity=candidate.severity,
                message=candidate.message,
                evidence=candidate.evidence,
                status=AlertStatus.OPEN,
            )
            .on_conflict_do_nothing(index_elements=["transaction_id", "alert_type"])
            .returning(Alert.id)
        )
        if session.execute(statement).scalar_one_or_none() is not None:
            created += 1
    return created


def analyze_upload(session: Session, user_id: uuid.UUID, upload_id: uuid.UUID) -> int:
    """Analyze every transaction that came from one upload."""
    transactions = (
        session.execute(
            select(Transaction).where(
                Transaction.user_id == user_id, Transaction.upload_id == upload_id
            )
        )
        .scalars()
        .all()
    )
    total = sum(analyze_transaction(session, user_id, tx) for tx in transactions)
    logger.info(
        "Alert detection for upload %s: %d transaction(s), %d new alert(s)",
        upload_id,
        len(transactions),
        total,
    )
    return total


def analyze_user(
    session: Session, user_id: uuid.UUID, since: date | None = None
) -> int:
    """Backfill detection across a user's history.

    Used by the seed script so the demo dataset has alerts to show, and safe to
    re-run because inserts are ON CONFLICT DO NOTHING.
    """
    query = select(Transaction).where(
        Transaction.user_id == user_id, Transaction.amount_cents < 0
    )
    if since is not None:
        query = query.where(Transaction.posted_date >= since)

    transactions = session.execute(query.order_by(Transaction.posted_date)).scalars().all()
    total = sum(analyze_transaction(session, user_id, tx) for tx in transactions)
    logger.info("Alert backfill: %d transaction(s), %d new alert(s)", len(transactions), total)
    return total
