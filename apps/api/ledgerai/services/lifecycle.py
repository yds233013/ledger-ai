"""Data lifecycle: export, deletion, and retention.

Deleting an account has to reach four places, and the last two are the ones
usually forgotten:

  1. PostgreSQL — one DELETE; every users.id foreign key is ON DELETE CASCADE.
  2. Object storage — the user's whole key prefix.
  3. Redis — cached analysis entries, findable only because store_run_id keeps
     a per-user index of them.
  4. The queue — any RQ job still pending, cancelled by its stored id so a
     worker does not wake up to an upload that no longer exists.

Export exists alongside deletion for the obvious reason: a user should be able
to take their data with them before removing it.

Export and deletion are async because they are driven by HTTP requests and must
run inside the caller's transaction. The retention sweep is sync because it is
driven by the RQ worker, which is a plain process.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ..models import (
    Account,
    Alert,
    AnalysisRun,
    Category,
    JobStage,
    ProcessingJob,
    Receipt,
    ReceiptStatus,
    Transaction,
    TransactionCorrection,
    Upload,
    UploadStatus,
    User,
)
from .storage import StorageError, get_storage

logger = logging.getLogger(__name__)

# Retention windows. Named so a test can assert the boundary rather than a
# magic number buried in a query.
FAILED_UPLOAD_FILE_DAYS = 7
STUCK_JOB_HOURS = 1
UNCONFIRMED_RECEIPT_DAYS = 30

# An export is streamed, but a runaway account should not be able to build a
# gigabyte in memory.
EXPORT_ROW_CAP = 100_000

EXPORT_README = """Ledger AI — data export
=======================

This archive contains the data held for a single Ledger AI account.

IMPORTANT: All data in the Ledger AI demo is SYNTHETIC. It is generated for
demonstration and does not describe any real person, account, or transaction.

Files
-----
  transactions.csv    every transaction, amounts in whole currency units
  receipts.csv        receipts and what OCR extracted from them
  alerts.csv          detected duplicate and unusual-charge alerts
  corrections.csv     manual category and merchant corrections
  accounts.csv        the accounts transactions belong to
  analysis_runs.json  Ask Ledger questions, plans, and computed results
  profile.json        account profile

Amounts are exported as decimal values. Internally Ledger AI stores integer
cents and never uses floating point for money.

Negative amounts are outflows (spending); positive amounts are inflows.
"""


@dataclass(slots=True)
class DeletionReport:
    """What a deletion did — or, in dry-run mode, what it would do."""

    user_id: str
    dry_run: bool = False
    account_removed: bool = False
    rows_by_table: dict[str, int] = field(default_factory=dict)
    storage_objects_removed: int = 0
    cache_keys_removed: int = 0
    queued_jobs_cancelled: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.rows_by_table.values())

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["total_rows"] = self.total_rows
        return payload


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def _write_csv(archive: zipfile.ZipFile, name: str, header: list[str], rows: list[list]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    archive.writestr(name, buffer.getvalue())


async def build_export(session: AsyncSession, user: User) -> bytes:
    """Build a ZIP of everything belonging to one user.

    Every query filters on user_id. A test asserts a second user's rows never
    appear in the archive.
    """
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", EXPORT_README)

        archive.writestr(
            "profile.json",
            json.dumps(
                {
                    "email": user.email,
                    "display_name": user.display_name,
                    "base_currency": user.base_currency,
                    "is_demo_account": user.is_demo,
                    "exported_at": datetime.now(UTC).isoformat(),
                    "note": "All Ledger AI demo data is synthetic.",
                },
                indent=2,
            ),
        )

        accounts = (await session.execute(
            select(Account).where(Account.user_id == user.id).order_by(Account.name)
        )).scalars().all()
        _write_csv(
            archive,
            "accounts.csv",
            ["id", "name", "institution", "type", "mask", "currency", "is_synthetic"],
            [
                [str(a.id), a.name, a.institution, a.account_type, a.mask, a.currency,
                 a.is_synthetic]
                for a in accounts
            ],
        )

        transaction_rows = (await session.execute(
            select(Transaction, Account.name, Category.name)
            .join(Account, Transaction.account_id == Account.id)
            .outerjoin(Category, Transaction.category_id == Category.id)
            .where(Transaction.user_id == user.id)
            .order_by(Transaction.posted_date)
            .limit(EXPORT_ROW_CAP)
        )).all()
        _write_csv(
            archive,
            "transactions.csv",
            ["id", "date", "merchant", "description", "amount", "currency", "category",
             "account", "confidence", "categorized_by", "needs_review", "is_corrected"],
            [
                [str(t.id), t.posted_date.isoformat(), t.merchant, t.raw_description,
                 f"{t.amount_cents / 100:.2f}", t.currency, category or "", account,
                 f"{float(t.confidence):.2f}", t.categorized_by, t.needs_review,
                 t.is_corrected]
                for t, account, category in transaction_rows
            ],
        )

        receipts = (await session.execute(
            select(Receipt).where(Receipt.user_id == user.id).order_by(Receipt.created_at)
        )).scalars().all()
        _write_csv(
            archive,
            "receipts.csv",
            ["id", "status", "merchant", "date", "subtotal", "tax", "tip", "total",
             "currency", "ocr_confidence", "link_mode", "transaction_id"],
            [
                [str(r.id), str(r.status), r.merchant or "",
                 r.posted_date.isoformat() if r.posted_date else "",
                 _money(r.subtotal_cents), _money(r.tax_cents), _money(r.tip_cents),
                 _money(r.total_cents), r.currency, f"{float(r.ocr_confidence):.3f}",
                 str(r.link_mode or ""), str(r.transaction_id or "")]
                for r in receipts
            ],
        )

        alert_rows = (await session.execute(
            select(Alert, Transaction.merchant, Transaction.posted_date)
            .join(Transaction, Alert.transaction_id == Transaction.id)
            .where(Alert.user_id == user.id)
            .order_by(Alert.created_at)
        )).all()
        _write_csv(
            archive,
            "alerts.csv",
            ["id", "type", "severity", "status", "merchant", "date", "message"],
            [
                [str(a.id), str(a.alert_type), str(a.severity), str(a.status), merchant,
                 posted.isoformat(), a.message]
                for a, merchant, posted in alert_rows
            ],
        )

        corrections = (await session.execute(
            select(TransactionCorrection)
            .where(TransactionCorrection.user_id == user.id)
            .order_by(TransactionCorrection.created_at)
        )).scalars().all()
        _write_csv(
            archive,
            "corrections.csv",
            ["id", "transaction_id", "field", "old_value", "new_value", "scope",
             "merchant_key", "created_at"],
            [
                [str(c.id), str(c.transaction_id), str(c.field), c.old_value or "",
                 c.new_value, str(c.scope), c.merchant_key, c.created_at.isoformat()]
                for c in corrections
            ],
        )

        runs = (await session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.user_id == user.id)
            .order_by(AnalysisRun.created_at)
        )).scalars().all()
        archive.writestr(
            "analysis_runs.json",
            json.dumps(
                [
                    {
                        "id": str(r.id),
                        "question": r.question,
                        "status": str(r.status),
                        "planner": str(r.planner),
                        "narrator": str(r.narrator),
                        "plan": r.plan,
                        "result": r.result,
                        "narration": r.narration,
                        "duration_ms": r.duration_ms,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in runs
                ],
                indent=2,
                default=str,
            ),
        )

    return buffer.getvalue()


def _money(cents: int | None) -> str:
    return "" if cents is None else f"{cents / 100:.2f}"


# --------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------


async def cancel_queued_jobs(
    session: AsyncSession, user_id: uuid.UUID, dry_run: bool = False
) -> int:
    """Cancel RQ jobs still pending for this user.

    Without this a worker later picks up a job whose upload has been deleted
    and fails noisily for no reason.
    """
    pending = (await session.execute(
        select(ProcessingJob.rq_job_id).where(
            ProcessingJob.user_id == user_id,
            ProcessingJob.rq_job_id.is_not(None),
            ProcessingJob.stage.notin_([JobStage.COMPLETE, JobStage.FAILED]),
        )
    )).scalars().all()

    job_ids = [job_id for job_id in pending if job_id]
    if dry_run or not job_ids:
        return len(job_ids)

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
    except Exception:  # noqa: BLE001 - a broker outage must not block deletion
        logger.warning("Could not reach the queue while cancelling jobs")
    return cancelled


async def _count_user_rows(session: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in (
        Transaction,
        TransactionCorrection,
        Receipt,
        Alert,
        AnalysisRun,
        Upload,
        ProcessingJob,
        Account,
    ):
        counts[model.__tablename__] = int(
            (await session.execute(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            )).scalar_one()
        )
    return counts


async def delete_user_data(
    session: AsyncSession,
    user: User,
    *,
    delete_account: bool = False,
    dry_run: bool = False,
) -> DeletionReport:
    """Remove a user's data from the database, storage and the queue.

    Redis cache purging is async and therefore done by the caller; the report
    field is filled in there.
    """
    report = DeletionReport(user_id=str(user.id), dry_run=dry_run)
    report.rows_by_table = await _count_user_rows(session, user.id)
    report.queued_jobs_cancelled = await cancel_queued_jobs(session, user.id, dry_run=dry_run)

    if dry_run:
        report.account_removed = delete_account
        return report

    # --- object storage -------------------------------------------------
    try:
        report.storage_objects_removed = get_storage().delete_prefix(f"users/{user.id}/")
    except StorageError as exc:
        # Storage failing must not leave the database half-deleted, but the
        # user must be told their files may linger.
        report.errors.append(f"Some stored files could not be removed: {exc}")
        logger.warning("Storage cleanup failed for user %s", user.id)

    # --- database --------------------------------------------------------
    if delete_account:
        # Every users.id foreign key cascades, so this removes everything.
        await session.execute(delete(User).where(User.id == user.id))
        report.account_removed = True
    else:
        # Keep the user and their accounts; remove the data they hold.
        for model in (
            Alert,
            AnalysisRun,
            TransactionCorrection,
            Receipt,
            Transaction,
            ProcessingJob,
            Upload,
        ):
            await session.execute(delete(model).where(model.user_id == user.id))
        # User-created categories go too; system ones (user_id IS NULL) stay.
        await session.execute(delete(Category).where(Category.user_id == user.id))

    await session.flush()
    logger.info(
        "Deleted data for user %s (account_removed=%s, rows=%d, files=%d, jobs=%d)",
        user.id,
        report.account_removed,
        report.total_rows,
        report.storage_objects_removed,
        report.queued_jobs_cancelled,
    )
    return report


def delete_receipt(session: Session, receipt: Receipt) -> dict[str, object]:
    """Remove one receipt and its stored original.

    A confirmed receipt detaches from its transaction rather than taking it
    down: the money was still spent, and deleting the evidence should not
    silently change someone's balance.
    """
    detached_transaction = receipt.transaction_id
    storage_removed = False

    upload = session.get(Upload, receipt.upload_id)
    if upload is not None:
        try:
            get_storage().delete(upload.storage_key)
            storage_removed = True
        except StorageError:
            logger.warning("Could not remove the stored file for receipt %s", receipt.id)

    session.execute(delete(Receipt).where(Receipt.id == receipt.id))
    if upload is not None:
        session.execute(delete(Upload).where(Upload.id == upload.id))
    session.flush()

    return {
        "receipt_id": str(receipt.id),
        "storage_removed": storage_removed,
        "detached_transaction_id": str(detached_transaction) if detached_transaction else None,
    }


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RetentionReport:
    stuck_jobs_failed: int = 0
    failed_upload_files_removed: int = 0
    unconfirmed_receipts_removed: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def retention_sweep(session: Session, now: datetime | None = None) -> RetentionReport:
    """Clean up what processing leaves behind.

    Safe to run repeatedly and safe to run concurrently with normal traffic:
    every step is idempotent and scoped to rows that are already terminal or
    demonstrably abandoned.
    """
    now = now or datetime.now(UTC)
    report = RetentionReport()

    # --- jobs abandoned mid-pipeline -------------------------------------
    # A worker killed outright (OOM, SIGKILL) never runs its own failure
    # handler, so its job would otherwise sit "extracting" forever.
    stuck_before = now - timedelta(hours=STUCK_JOB_HOURS)
    stuck = session.execute(
        select(ProcessingJob).where(
            ProcessingJob.stage.notin_([JobStage.COMPLETE, JobStage.FAILED]),
            ProcessingJob.created_at < stuck_before,
        )
    ).scalars().all()
    for job in stuck:
        job.stage = JobStage.FAILED
        job.progress = 100
        job.finished_at = now
        job.error_message = (
            "Processing did not finish and was timed out. Please upload the file again."
        )
        upload = session.get(Upload, job.upload_id)
        if upload is not None:
            upload.status = UploadStatus.FAILED
    report.stuck_jobs_failed = len(stuck)

    # --- stored files for failed uploads ---------------------------------
    # The row stays so the user can see what happened; the bytes do not.
    file_cutoff = now - timedelta(days=FAILED_UPLOAD_FILE_DAYS)
    failed_uploads = session.execute(
        select(Upload).where(
            Upload.status == UploadStatus.FAILED,
            Upload.created_at < file_cutoff,
            Upload.storage_key != "",
        )
    ).scalars().all()
    storage = get_storage()
    for upload in failed_uploads:
        try:
            storage.delete(upload.storage_key)
            upload.storage_key = ""
            report.failed_upload_files_removed += 1
        except StorageError:
            continue

    # --- receipts never confirmed ----------------------------------------
    receipt_cutoff = now - timedelta(days=UNCONFIRMED_RECEIPT_DAYS)
    abandoned = session.execute(
        select(Receipt).where(
            Receipt.transaction_id.is_(None),
            Receipt.status.in_([ReceiptStatus.PENDING, ReceiptStatus.NEEDS_REVIEW]),
            Receipt.created_at < receipt_cutoff,
        )
    ).scalars().all()
    for receipt in abandoned:
        delete_receipt(session, receipt)
        report.unconfirmed_receipts_removed += 1

    session.flush()
    logger.info(
        "Retention sweep: %d stuck job(s) failed, %d file(s) removed, %d receipt(s) removed",
        report.stuck_jobs_failed,
        report.failed_upload_files_removed,
        report.unconfirmed_receipts_removed,
    )
    return report
