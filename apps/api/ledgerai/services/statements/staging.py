"""Persisting a parsed statement, and committing it once a person agrees.

Nothing here writes a transaction on its own. A parsed statement is an
inference about a document; the ledger only changes when somebody has looked at
the rows and said yes. That is the same shape receipts already use, for the
same reason.

Commit is atomic and idempotent: one transaction, guarded by the existing
`transactions.dedupe_hash` unique constraint, and a second confirmation of the
same import is a no-op rather than a second month of duplicates.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import (
    StatementImport,
    StatementImportRow,
    StatementImportStatus,
    Upload,
    UploadStatus,
)
from ..normalize import compute_dedupe_hash, merchant_key, normalize_description
from ..storage import StorageError, get_storage
from .parse import ParsedStatement
from .verify import VerificationResult

logger = logging.getLogger(__name__)


def expiry_from(now: datetime | None = None) -> datetime:
    """When an unconfirmed import is purged, original file and all."""
    return (now or datetime.now(UTC)) + timedelta(hours=settings.statement_review_hours)


def stage(
    session: Session,
    *,
    user_id: uuid.UUID,
    upload_id: uuid.UUID,
    parsed: ParsedStatement,
    verification: VerificationResult,
    now: datetime | None = None,
) -> StatementImport:
    """Write the parse result as rows awaiting review.

    Idempotent on `upload_id`, which is unique: a retried job replaces the rows
    it wrote last time rather than doubling them.
    """
    moment = now or datetime.now(UTC)

    record = session.execute(
        select(StatementImport).where(StatementImport.upload_id == upload_id)
    ).scalar_one_or_none()
    if record is None:
        record = StatementImport(user_id=user_id, upload_id=upload_id)
        session.add(record)
    else:
        for existing in session.execute(
            select(StatementImportRow).where(StatementImportRow.import_id == record.id)
        ).scalars():
            session.delete(existing)

    record.status = StatementImportStatus.NEEDS_REVIEW
    record.page_count = parsed.page_count
    record.table_pages = parsed.table_pages
    record.skipped_lines = parsed.skipped_lines
    record.period_start = parsed.period_start
    record.period_end = parsed.period_end
    record.currency = parsed.currency
    record.balance_chain_checked = parsed.balance_chain_checked
    record.balance_chain_ok = parsed.balance_chain_ok
    record.verified_pages = verification.checked_pages
    record.verified_mismatches = verification.mismatched_pages
    record.notes = {"parse": list(parsed.notes), "verify": list(verification.notes)}
    record.expires_at = expiry_from(moment)
    session.flush()

    for row in parsed.rows:
        session.add(
            StatementImportRow(
                import_id=record.id,
                user_id=user_id,
                source_page=row.source_page,
                source_line=row.source_line,
                posted_date=row.posted_date,
                description=row.description[:500],
                amount_cents=row.amount_cents,
                balance_cents=row.balance_cents,
                confidence=Decimal(str(row.confidence)),
                notes={"flags": list(row.notes)},
                excluded=False,
                edited=False,
            )
        )
    session.flush()

    logger.info(
        "statement.staged rows=%d pages=%d chain=%s verified=%d/%d",
        len(parsed.rows),
        parsed.page_count,
        "ok" if parsed.balance_chain_ok else "absent_or_broken",
        verification.checked_pages - verification.mismatched_pages,
        verification.checked_pages,
    )
    return record


def purge_original(session: Session, record: StatementImport) -> bool:
    """Delete the stored PDF, keeping the upload row so history still reads.

    Called on commit and on expiry. The bytes are the sensitive part; the row
    is what lets somebody see that an import happened at all.
    """
    upload = session.get(Upload, record.upload_id)
    if upload is None or not upload.storage_key:
        return False
    try:
        get_storage().delete(upload.storage_key)
    except StorageError:
        logger.warning("statement.purge_failed", exc_info=True)
        return False
    upload.storage_key = ""
    session.flush()
    logger.info("statement.original_purged")
    return True


def rows_for_commit(session: Session, record: StatementImport) -> list[StatementImportRow]:
    """The rows a confirmation would import, in document order."""
    return list(
        session.execute(
            select(StatementImportRow)
            .where(
                StatementImportRow.import_id == record.id,
                StatementImportRow.excluded.is_(False),
            )
            .order_by(StatementImportRow.source_page, StatementImportRow.source_line)
        ).scalars()
    )


def dedupe_hash_for(
    row: StatementImportRow, *, user_id: uuid.UUID, account_id: uuid.UUID
) -> str:
    """The row's idempotency key, from the same function the CSV path uses.

    `source_line` carries the position within the document, so re-importing the
    same statement produces the same keys while two genuinely identical charges
    on one day stay distinct.
    """
    return compute_dedupe_hash(
        user_id,
        account_id,
        row.posted_date,
        row.amount_cents,
        normalize_description(row.description),
        row.source_line,
    )


def logical_key(row: StatementImportRow) -> str:
    """An index-free key, for spotting the same charge arriving twice by different routes.

    The row hash deliberately includes document position, which is what keeps a
    retry idempotent — but it also means the same August transaction imported
    once from CSV and once from PDF hashes differently. This key ignores
    position so those can be surfaced. It flags; it never drops. Two identical
    coffees on one day are real, and silently discarding one would be worse than
    showing a duplicate to confirm.
    """
    return "|".join(
        [
            row.posted_date.isoformat(),
            str(row.amount_cents),
            merchant_key(normalize_description(row.description)),
        ]
    )


def mark_committed(
    session: Session,
    record: StatementImport,
    *,
    account_id: uuid.UUID,
    now: datetime | None = None,
) -> None:
    """Close the import and stop it being swept."""
    record.status = StatementImportStatus.COMMITTED
    record.account_id = account_id
    record.committed_at = now or datetime.now(UTC)
    upload = session.get(Upload, record.upload_id)
    if upload is not None:
        upload.status = UploadStatus.COMPLETE
    session.flush()
