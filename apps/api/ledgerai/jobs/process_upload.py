"""The upload processing pipeline.

Stages are the contract with the UI:
    queued -> extracting -> normalizing -> categorizing -> complete | failed

Each transition is written to processing_jobs (polled by the frontend) and to
the RQ job's meta. The whole function is safe to run twice: parsing is pure,
and the insert relies on the unique dedupe_hash, so a retry converges rather
than duplicating spend.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from rq import get_current_job
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import sync_session
from ..models import (
    JobStage,
    ProcessingJob,
    Receipt,
    ReceiptStatus,
    Upload,
    UploadKind,
    UploadStatus,
)
from ..security.validators import ValidationError
from ..services.alerts import analyze_upload
from ..services.categorize import build_categorizer
from ..services.csv_parser import parse_statement_csv
from ..services.ingest import build_context, ingest_rows, resolve_account, resolve_category_ids
from ..services.ocr import build_engine, parse_receipt
from ..services.ocr.preprocess import load_pages, prepare_for_ocr
from ..services.storage import get_storage

logger = logging.getLogger(__name__)

# Progress is reported per stage so the bar advances even on a small file.
STAGE_PROGRESS: dict[JobStage, int] = {
    JobStage.QUEUED: 0,
    JobStage.EXTRACTING: 20,
    JobStage.NORMALIZING: 45,
    JobStage.CATEGORIZING: 65,
    JobStage.ANALYZING: 85,
    JobStage.COMPLETE: 100,
}


def _set_stage(
    session: Session,
    job: ProcessingJob,
    stage: JobStage,
    *,
    progress: int | None = None,
    **fields: object,
) -> None:
    job.stage = stage
    job.progress = progress if progress is not None else STAGE_PROGRESS.get(stage, job.progress)
    for key, value in fields.items():
        setattr(job, key, value)
    session.flush()
    session.commit()

    rq_job = get_current_job()
    if rq_job is not None:
        rq_job.meta["stage"] = stage.value
        rq_job.meta["progress"] = job.progress
        rq_job.save_meta()


def mark_job_failed(job, connection, exc_type, exc_value, traceback) -> None:  # noqa: ANN001
    """RQ failure callback.

    Runs in the worker process, not the work horse, so it still fires when the
    horse is killed outright (OOM, SIGKILL, a fork-safety abort). Without this
    a hard crash would leave the job stuck mid-stage in the UI forever.
    """
    try:
        _, job_row_id = job.args
        with sync_session() as session:
            row = session.get(ProcessingJob, uuid.UUID(job_row_id))
            if row is None or row.stage in {JobStage.COMPLETE, JobStage.FAILED}:
                return
            row.stage = JobStage.FAILED
            row.progress = 100
            row.finished_at = datetime.now(UTC)
            row.error_message = (
                "Processing stopped unexpectedly. Please try uploading the file again."
            )
            upload = session.get(Upload, uuid.UUID(job.args[0]))
            if upload is not None:
                upload.status = UploadStatus.FAILED
    except Exception:  # noqa: BLE001 - a failing failure-handler must stay quiet
        logger.exception("Could not record job failure for %s", job.id)


def process_upload(upload_id: str, job_id: str) -> dict[str, int | str]:
    """RQ entry point. Arguments are strings because RQ serializes them."""
    with sync_session() as session:
        job = session.get(ProcessingJob, uuid.UUID(job_id))
        upload = session.get(Upload, uuid.UUID(upload_id))
        if job is None or upload is None:
            raise RuntimeError(f"Upload {upload_id} or job {job_id} no longer exists")

        _set_stage(session, job, JobStage.EXTRACTING, started_at=datetime.now(UTC))

        try:
            # --- extract -----------------------------------------------------
            data = get_storage().get(upload.storage_key)

            if upload.kind == UploadKind.IMAGE:
                return _process_receipt(session, job, upload, data)

            parsed = parse_statement_csv(data)
            _set_stage(session, job, JobStage.NORMALIZING, rows_total=parsed.total_rows)

            # --- normalize ---------------------------------------------------
            # Parsing already produced normalized descriptions, merchants,
            # integer-cent amounts and parsed dates. This stage resolves the
            # destination account and the categorization lookup tables.
            account = resolve_account(session, upload.user_id, _dominant_hint(parsed))
            context = build_context(session, upload.user_id)
            category_ids = resolve_category_ids(session)

            _set_stage(session, job, JobStage.CATEGORIZING)

            # --- categorize + insert -----------------------------------------
            result = ingest_rows(
                session,
                user_id=upload.user_id,
                account_id=account.id,
                upload_id=upload.id,
                rows=parsed.rows,
                categorizer=build_categorizer(list(category_ids)),
                context=context,
                category_ids=category_ids,
            )

            # --- detect duplicates and unusual charges -----------------------
            _set_stage(session, job, JobStage.ANALYZING)
            alerts_created = analyze_upload(session, upload.user_id, upload.id)

            upload.status = UploadStatus.COMPLETE
            _set_stage(
                session,
                job,
                JobStage.COMPLETE,
                rows_imported=result.imported,
                rows_skipped=result.skipped_duplicates + len(parsed.errors),
                finished_at=datetime.now(UTC),
                error_message=_row_error_summary(parsed.errors),
            )

            logger.info(
                "Upload %s: imported=%d duplicates=%d unparseable=%d review=%d alerts=%d",
                upload_id,
                result.imported,
                result.skipped_duplicates,
                len(parsed.errors),
                result.needs_review,
                alerts_created,
            )
            return {
                "imported": result.imported,
                "duplicates": result.skipped_duplicates,
                "unparseable": len(parsed.errors),
                "needs_review": result.needs_review,
                "alerts": alerts_created,
                "account": account.name,
            }

        except Exception as exc:  # noqa: BLE001 - every failure must reach the UI
            session.rollback()
            message = str(exc) if isinstance(exc, ValidationError) else _safe_error(exc)
            failed = session.get(ProcessingJob, uuid.UUID(job_id))
            failed_upload = session.get(Upload, uuid.UUID(upload_id))
            if failed_upload is not None:
                failed_upload.status = UploadStatus.FAILED
            if failed is not None:
                _set_stage(
                    session,
                    failed,
                    JobStage.FAILED,
                    progress=100,
                    error_message=message,
                    finished_at=datetime.now(UTC),
                )
            logger.exception("Upload %s failed", upload_id)
            raise


def _process_receipt(
    session: Session, job: ProcessingJob, upload: Upload, data: bytes
) -> dict[str, int | str]:
    """OCR a receipt into structured fields.

    Deliberately creates no transaction: a receipt is inert until the user
    confirms it on the review page. Logging here carries only ids, page counts
    and status — never OCR text, merchant names or amounts.
    """
    pages = load_pages(data, upload.content_type)
    prepared = [prepare_for_ocr(page) for page in pages]

    result = build_engine().extract(prepared)
    _set_stage(session, job, JobStage.NORMALIZING, rows_total=1)

    parsed = parse_receipt(result)
    _set_stage(session, job, JobStage.CATEGORIZING)

    status = ReceiptStatus.NEEDS_REVIEW if parsed.needs_review else ReceiptStatus.PENDING

    # upload_id is UNIQUE, so a retried job updates the existing row rather
    # than creating a second receipt for the same file.
    receipt = session.execute(
        select(Receipt).where(Receipt.upload_id == upload.id)
    ).scalar_one_or_none()
    if receipt is None:
        receipt = Receipt(user_id=upload.user_id, upload_id=upload.id)
        session.add(receipt)

    receipt.status = status
    receipt.page_count = result.page_count
    receipt.ocr_engine = result.engine
    receipt.ocr_confidence = Decimal(str(round(result.mean_confidence, 3)))
    receipt.raw_text = result.text[:20_000]
    receipt.merchant = parsed.merchant
    receipt.posted_date = parsed.posted_date
    receipt.subtotal_cents = parsed.subtotal_cents
    receipt.tax_cents = parsed.tax_cents
    receipt.tip_cents = parsed.tip_cents
    receipt.total_cents = parsed.total_cents
    receipt.currency = parsed.currency
    receipt.field_confidence = parsed.field_confidence
    receipt.parse_notes = parsed.notes
    session.flush()

    _set_stage(session, job, JobStage.ANALYZING)

    upload.status = UploadStatus.COMPLETE
    _set_stage(
        session,
        job,
        JobStage.COMPLETE,
        rows_imported=0,
        rows_skipped=0,
        finished_at=datetime.now(UTC),
    )

    logger.info(
        "Receipt %s processed: pages=%d status=%s",
        receipt.id,
        result.page_count,
        status.value,
    )
    return {
        "receipt_id": str(receipt.id),
        "pages": result.page_count,
        "status": status.value,
        "needs_review": int(parsed.needs_review),
    }


def _dominant_hint(parsed) -> str | None:  # noqa: ANN001 - ParseResult, avoids a cycle
    """Most common account label in the file, if it has an account column."""
    hints = [row.account_hint for row in parsed.rows if row.account_hint]
    if not hints:
        return None
    return max(set(hints), key=hints.count)


def _row_error_summary(errors: list) -> str | None:
    if not errors:
        return None
    sample = "; ".join(f"row {e.row_index + 2}: {e.message}" for e in errors[:3])
    suffix = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
    return f"{len(errors)} row(s) skipped — {sample}{suffix}"


def _safe_error(exc: Exception) -> str:
    """Never leak stack traces, file paths or connection strings to the UI."""
    return f"Processing failed ({type(exc).__name__}). Check the worker logs for details."
