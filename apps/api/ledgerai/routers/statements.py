"""Reviewing and confirming a parsed bank statement.

Every route is scoped to the caller through the shared selectable, and a row
belonging to someone else is a 404 rather than a 403 — a 403 would confirm the
row exists.

Confirmation is the only thing here that changes the ledger. Until it happens
the parsed rows are inert, and they expire on their own if nobody comes back.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from ..deps import CurrentUser, DbSession, SyncSessionFactory
from ..models import (
    Account,
    StatementImport,
    StatementImportRow,
    StatementImportStatus,
    Transaction,
)
from ..security.ratelimit import UPLOAD_LIMIT, enforce
from ..services import statements
from ..services.ingest import resolve_account
from ..services.normalize import extract_merchant, merchant_key, normalize_description

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/statements", tags=["statements"])


class RowOut(BaseModel):
    id: uuid.UUID
    source_page: int
    posted_date: date
    description: str
    amount_cents: int
    balance_cents: int | None
    direction: str
    confidence: float
    flags: list[str]
    excluded: bool
    edited: bool
    duplicate_of_existing: bool = False


class ImportOut(BaseModel):
    id: uuid.UUID
    status: str
    page_count: int
    table_pages: int
    skipped_lines: int
    period_start: date | None
    period_end: date | None
    currency: str | None
    balance_chain_checked: bool
    balance_chain_ok: bool
    verified_pages: int
    verified_mismatches: int
    notes: list[str]
    expires_at: datetime
    committed_at: datetime | None
    row_count: int
    rows: list[RowOut] | None = None
    message: str | None = None


class RowPatch(BaseModel):
    """A correction made during review. Every field optional; absent means unchanged."""

    posted_date: date | None = None
    description: str | None = Field(default=None, max_length=500)
    amount_cents: int | None = None
    excluded: bool | None = None


class ConfirmRequest(BaseModel):
    account_id: uuid.UUID | None = None


def _owned(user_id: uuid.UUID):
    return select(StatementImport).where(StatementImport.user_id == user_id)


def _notes(record: StatementImport) -> list[str]:
    raw = record.notes or {}
    return sorted({*raw.get("parse", []), *raw.get("verify", [])})


async def _load(session: DbSession, user_id: uuid.UUID, import_id: uuid.UUID) -> StatementImport:
    record = (
        await session.execute(_owned(user_id).where(StatementImport.id == import_id))
    ).scalar_one_or_none()
    if record is None:
        # 404 rather than 403 for another user's import — no existence leak.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import not found")
    return record


def _row_out(row: StatementImportRow, *, duplicate: bool = False) -> RowOut:
    return RowOut(
        id=row.id,
        source_page=row.source_page,
        posted_date=row.posted_date,
        description=row.description,
        amount_cents=row.amount_cents,
        balance_cents=row.balance_cents,
        direction="credit" if row.amount_cents > 0 else "debit",
        confidence=float(row.confidence),
        flags=list((row.notes or {}).get("flags", [])),
        excluded=row.excluded,
        edited=row.edited,
        duplicate_of_existing=duplicate,
    )


def _summary(record: StatementImport, row_count: int) -> ImportOut:
    return ImportOut(
        id=record.id,
        status=record.status,
        page_count=record.page_count,
        table_pages=record.table_pages,
        skipped_lines=record.skipped_lines,
        period_start=record.period_start,
        period_end=record.period_end,
        currency=record.currency,
        balance_chain_checked=record.balance_chain_checked,
        balance_chain_ok=record.balance_chain_ok,
        verified_pages=record.verified_pages,
        verified_mismatches=record.verified_mismatches,
        notes=_notes(record),
        expires_at=record.expires_at,
        committed_at=record.committed_at,
        row_count=row_count,
    )


@router.get("", response_model=list[ImportOut])
async def list_imports(user: CurrentUser, session: DbSession) -> list[ImportOut]:
    records = (
        await session.execute(
            _owned(user.id).order_by(desc(StatementImport.created_at)).limit(50)
        )
    ).scalars().all()

    out: list[ImportOut] = []
    for record in records:
        count = (
            await session.execute(
                select(StatementImportRow).where(StatementImportRow.import_id == record.id)
            )
        ).scalars().all()
        out.append(_summary(record, len(count)))
    return out


@router.get("/{import_id}", response_model=ImportOut)
async def get_import(
    import_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> ImportOut:
    """The parsed rows, with anything already in the ledger marked as a repeat."""
    record = await _load(session, user.id, import_id)

    rows = (
        await session.execute(
            select(StatementImportRow)
            .where(StatementImportRow.import_id == record.id)
            .order_by(StatementImportRow.source_page, StatementImportRow.source_line)
        )
    ).scalars().all()

    # The same charge can arrive once by CSV and once by PDF. The row hash
    # includes document position so those look distinct — which is right for
    # idempotency and wrong for the user, who wants to be told. Flag, never drop.
    existing = {
        (txn.posted_date, txn.amount_cents, txn.normalized_description)
        for txn in (
            await session.execute(
                select(Transaction).where(Transaction.user_id == user.id)
            )
        ).scalars()
    }

    summary = _summary(record, len(rows))
    summary.rows = [
        _row_out(
            row,
            duplicate=(
                row.posted_date,
                row.amount_cents,
                normalize_description(row.description),
            )
            in existing,
        )
        for row in rows
    ]
    return summary


@router.patch("/{import_id}/rows/{row_id}", response_model=RowOut)
async def update_row(
    import_id: uuid.UUID,
    row_id: uuid.UUID,
    payload: RowPatch,
    user: CurrentUser,
    session: DbSession,
) -> RowOut:
    """Correct or exclude one row before confirming."""
    record = await _load(session, user.id, import_id)
    if record.status != StatementImportStatus.NEEDS_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This import has already been confirmed.",
        )

    row = (
        await session.execute(
            select(StatementImportRow).where(
                StatementImportRow.id == row_id,
                StatementImportRow.import_id == record.id,
                StatementImportRow.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Row not found")

    changed = False
    if payload.posted_date is not None and payload.posted_date != row.posted_date:
        row.posted_date, changed = payload.posted_date, True
    if payload.description is not None and payload.description.strip() != row.description:
        row.description, changed = payload.description.strip()[:500], True
    if payload.amount_cents is not None and payload.amount_cents != row.amount_cents:
        row.amount_cents, changed = payload.amount_cents, True
    if payload.excluded is not None:
        row.excluded = payload.excluded

    if changed:
        # A corrected row is the user's assertion, not the parser's inference.
        row.edited = True
        row.confidence = Decimal("1.000")

    await session.commit()
    await session.refresh(row)
    return _row_out(row)


@router.post("/{import_id}/confirm", response_model=ImportOut)
async def confirm_import(
    import_id: uuid.UUID,
    payload: ConfirmRequest,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    factory: SyncSessionFactory,
) -> ImportOut:
    """Create transactions from the accepted rows, once.

    Atomic and idempotent: one transaction, guarded by the existing unique
    dedupe hash, and confirming twice imports nothing the second time. The
    stored PDF and every rendering are purged in the same commit — the file has
    done its job by this point, and a statement kept "just in case" is the
    largest avoidable risk in the feature.
    """
    await enforce(request, UPLOAD_LIMIT, key=str(user.id))
    record = await _load(session, user.id, import_id)

    if record.status == StatementImportStatus.COMMITTED:
        rows = (
            await session.execute(
                select(StatementImportRow).where(StatementImportRow.import_id == record.id)
            )
        ).scalars().all()
        summary = _summary(record, len(rows))
        summary.message = "This statement was already imported."
        return summary

    if record.status != StatementImportStatus.NEEDS_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This import is not ready to confirm."
        )

    user_id = user.id
    account_id = payload.account_id
    record_id = record.id

    def _commit() -> tuple[int, int, int, datetime]:
        with factory() as sync_session:
            local = sync_session.get(StatementImport, record_id)
            if local is None or local.user_id != user_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Import not found"
                )

            if account_id is not None:
                account = sync_session.get(Account, account_id)
                # Another user's account id must read as "not found", never as
                # "not yours" — the second answer confirms it exists.
                if account is None or account.user_id != user_id:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail="Account not found"
                    )
            else:
                account = resolve_account(sync_session, user_id, None)

            rows = statements.rows_for_commit(sync_session, local)
            imported = 0
            skipped = 0
            for row in rows:
                digest = statements.dedupe_hash_for(
                    row, user_id=user_id, account_id=account.id
                )
                clash = sync_session.execute(
                    select(Transaction).where(Transaction.dedupe_hash == digest)
                ).scalar_one_or_none()
                if clash is not None:
                    skipped += 1
                    continue
                merchant = extract_merchant(row.description)
                sync_session.add(
                    Transaction(
                        user_id=user_id,
                        account_id=account.id,
                        upload_id=local.upload_id,
                        posted_date=row.posted_date,
                        amount_cents=row.amount_cents,
                        currency=local.currency or "USD",
                        raw_description=row.description[:512],
                        normalized_description=normalize_description(row.description)[:512],
                        merchant=merchant[:200],
                        merchant_key=merchant_key(merchant)[:200],
                        # A row the parser was unsure about stays flagged after
                        # import, so it surfaces in the review filter rather than
                        # blending into the ledger as though it were certain.
                        needs_review=float(row.confidence) < 0.8 and not row.edited,
                        dedupe_hash=digest,
                    )
                )
                imported += 1

            statements.mark_committed(sync_session, local, account_id=account.id)
            statements.purge_original(sync_session, local)
            committed_at = local.committed_at or datetime.now(UTC)
            total_rows = len(
                sync_session.execute(
                    select(StatementImportRow).where(
                        StatementImportRow.import_id == local.id
                    )
                ).scalars().all()
            )
            sync_session.commit()
            return imported, skipped, total_rows, committed_at

    imported, skipped, total_rows, committed_at = await to_thread.run_sync(_commit)
    await session.commit()

    # Built from what the commit reported rather than re-read: the write
    # happened on another connection, so anything this session hands back would
    # be its pre-commit copy. Only three fields changed, and the commit knows
    # all three.
    summary = _summary(record, total_rows)
    summary.status = StatementImportStatus.COMMITTED.value
    summary.committed_at = committed_at
    summary.message = (
        f"{imported} transaction(s) imported"
        + (f", {skipped} already present." if skipped else ".")
        + " The original PDF has been deleted."
    )
    logger.info("statement.committed imported=%d skipped=%d", imported, skipped)
    return summary


@router.delete("/{import_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_import(
    import_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    factory: SyncSessionFactory,
) -> None:
    """Throw the whole thing away now rather than waiting for it to expire."""
    record = await _load(session, user.id, import_id)
    record_id = record.id

    def _discard() -> None:
        with factory() as sync_session:
            local = sync_session.get(StatementImport, record_id)
            if local is None:
                return
            statements.purge_original(sync_session, local)
            sync_session.delete(local)
            sync_session.commit()

    await to_thread.run_sync(_discard)
    logger.info("statement.discarded")
