"""Upload intake and job status.

Flow: validate -> hash -> store -> record -> enqueue. The content hash is
checked against a UNIQUE constraint, so re-uploading a file the user has
already ingested returns the original upload rather than creating a second one.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from rq import Retry
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..deps import CurrentUser, DbSession, SyncSessionFactory
from ..jobs.process_upload import mark_job_failed, process_upload
from ..jobs.queue import get_queue
from ..models import JobStage, ProcessingJob, Upload, UploadKind, UploadStatus
from ..security.filenames import build_storage_key, sanitize_filename
from ..security.ratelimit import UPLOAD_LIMIT, enforce
from ..security.validators import (
    ValidationError,
    detect_kind,
    validate_csv_structure,
    validate_size,
)
from ..services import quota, sensitive
from ..services.consent import missing_consents
from ..services.normalize import compute_content_hash
from ..services.scoping import user_jobs, user_uploads
from ..services.statements import extract_pages
from ..services.statements.extract import EncryptedPdfError, NoTextLayerError
from ..services.storage import StorageError, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

# Read in bounded chunks and count bytes as they arrive: Content-Length is
# client-supplied and must never be the thing that enforces the limit.
CHUNK_SIZE = 64 * 1024


class JobOut(BaseModel):
    id: uuid.UUID
    upload_id: uuid.UUID
    stage: str
    progress: int
    rows_total: int
    rows_imported: int
    rows_skipped: int
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None


#: What the uploader said the file is. A PDF is a statement or a receipt
#: depending only on this — never on a heuristic. Guessing wrong in either
#: direction is silently destructive: a statement read as a receipt collapses a
#: month into one row, and a receipt read as a statement finds no table at all.
STATEMENT = "statement"
RECEIPT = "receipt"


class UploadOut(BaseModel):
    id: uuid.UUID
    original_filename: str
    kind: str
    size_bytes: int
    status: str
    created_at: datetime
    job: JobOut | None = None
    duplicate_of_existing: bool = False
    message: str | None = None


def _discard_object(storage_key: str) -> None:
    """Remove an object whose upload row did not survive.

    Best effort. A failure here leaves an orphan for the retention sweep rather
    than turning a recoverable race into an error the user sees.
    """
    try:
        get_storage().delete(storage_key)
    except StorageError:
        logger.warning("upload.orphan_object_left", exc_info=True)


async def _read_bounded(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(CHUNK_SIZE):
        total += len(chunk)
        if total > settings.max_upload_bytes:
            limit_mb = settings.max_upload_bytes / 1024 / 1024
            raise ValidationError(f"File exceeds the {limit_mb:.0f} MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def create_upload(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    factory: SyncSessionFactory,
    file: UploadFile = File(...),
    kind_hint: str | None = Form(default=None, alias='kind'),
) -> UploadOut:
    # Uploads are the widest attack surface here: they consume storage, worker
    # time and OCR cycles. Budgeted per user, not per IP.
    await enforce(request, UPLOAD_LIMIT, key=str(user.id))

    # Upload is the moment new financial data enters the system, so it is the
    # one action gated on consent. Reading, exporting and deleting your own
    # data never are — withholding somebody's records until they accept a new
    # document would be leverage, not consent. Demo accounts are exempt: the
    # data is synthetic and the account deletes itself within a day.
    outstanding = await missing_consents(session, user)
    if outstanding:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Please review and accept the current terms before uploading: "
                + ", ".join(outstanding)
            ),
        )
    try:
        data = await _read_bounded(file)
        validate_size(len(data))
        original_name = file.filename or "upload"
        kind, content_type = detect_kind(original_name, data)
        if kind == UploadKind.CSV:
            validate_csv_structure(data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    # A PDF is a statement or a receipt because the uploader said so. Asked
    # rather than inferred: both misroutes lose data quietly.
    statement_pages = 0
    if content_type == "application/pdf":
        if kind_hint not in (STATEMENT, RECEIPT):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Please say whether this PDF is a bank statement or a receipt "
                    "before uploading it."
                ),
            )
        if kind_hint == STATEMENT:
            kind = UploadKind.STATEMENT_PDF

    # Refuse unmasked account numbers, SSNs, routing numbers and IBANs before a
    # single byte reaches storage. Nothing has been stored or reserved yet, so
    # rejection here needs no cleanup — which is precisely why the check lives
    # at this point in the flow rather than after.
    #
    # The whole file is refused, and the response carries only category names,
    # counts and remediation. Naming the row, column or page would tell whoever
    # holds the response exactly where the sensitive data sits.
    findings: sensitive.Findings | None = None
    if kind == UploadKind.CSV:
        findings = sensitive.scan_csv(data)
    elif kind == UploadKind.STATEMENT_PDF:
        # Text extraction is fast enough to run here — under half a second for a
        # maximal statement — so a statement carrying a full account number is
        # refused without ever being written down.
        try:
            pages = extract_pages(data)
        except (EncryptedPdfError, NoTextLayerError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        statement_pages = len(pages)
        findings = sensitive.scan_free_text(
            "\n".join(" ".join(w.text for w in page) for page in pages)
        )

    if findings is not None and findings.rejected:
        logger.info(
            "upload.rejected_sensitive categories=%s", ",".join(findings.categories)
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=findings.guidance(),
            headers={"X-Rejected-Categories": ",".join(findings.categories)},
        )

    content_hash = compute_content_hash(data)

    # Idempotency: identical bytes for this user are the same upload.
    existing = (
        await session.execute(
            user_uploads(user.id).where(Upload.content_hash == content_hash)
        )
    ).scalar_one_or_none()
    if existing is not None:
        job = (
            await session.execute(
                user_jobs(user.id)
                .where(ProcessingJob.upload_id == existing.id)
                .order_by(desc(ProcessingJob.created_at))
            )
        ).scalars().first()
        return UploadOut(
            id=existing.id,
            original_filename=existing.original_filename,
            kind=existing.kind,
            size_bytes=existing.size_bytes,
            status=existing.status,
            created_at=existing.created_at,
            job=JobOut.model_validate(job, from_attributes=True) if job else None,
            duplicate_of_existing=True,
            message=(
                "This exact file was already processed, so no duplicate transactions "
                "were created."
            ),
        )

    safe_name = sanitize_filename(original_name)
    storage_key = build_storage_key(user.id, safe_name)

    # Claim budget before storing anything. The reservation commits in its own
    # transaction, so a second request arriving now sees the claim and is
    # refused rather than racing this one to the same last slot. Everything
    # after this point releases it on failure.
    try:
        reservation = await quota.reserve_for_request(
            factory, user, len(data), pages=statement_pages
        )
    except quota.QuotaExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.detail,
            headers=exc.headers(),
        ) from exc

    try:
        get_storage().put(storage_key, data, content_type)
    except StorageError as exc:
        await quota.release_for_request(factory, reservation)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File storage is unavailable. Please try again.",
        ) from exc

    upload = Upload(
        user_id=user.id,
        filename=safe_name,
        original_filename=original_name[:255],
        content_hash=content_hash,
        kind=kind,
        content_type=content_type,
        size_bytes=len(data),
        storage_key=storage_key,
        status=UploadStatus.PROCESSING,
    )
    session.add(upload)

    try:
        await session.flush()
    except IntegrityError as exc:
        # Lost a race against a concurrent identical upload — the constraint
        # did its job; report the existing one rather than failing.
        await session.rollback()
        await quota.release_for_request(factory, reservation)
        _discard_object(storage_key)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That file is already being processed.",
        ) from exc

    # Bind the claim to the upload in this same transaction, so the row and
    # the binding become visible together and the worker can always find it.
    await quota.attach_in_transaction(session, reservation, upload.id)

    job = ProcessingJob(
        upload_id=upload.id, user_id=user.id, stage=JobStage.QUEUED, progress=0
    )
    session.add(job)
    await session.flush()

    try:
        rq_job = get_queue().enqueue(
            process_upload,
            str(upload.id),
            str(job.id),
            # Transient failures — a storage blip, a worker restarted mid-job —
            # are worth one more go. `on_failure` fires only once the retries
            # are exhausted, which is what makes it the terminal point where the
            # held budget is handed back. A rejected file never gets here: that
            # path returns normally so it is not retried.
            retry=Retry(max=max(0, settings.quota_max_job_attempts - 1)),
            on_failure=mark_job_failed,
        )
    except Exception as exc:  # noqa: BLE001 - the queue is a dependency, not a bug
        # Nothing will ever process this file, so undo the whole attempt rather
        # than leaving a row that says "processing" forever and a claim that
        # holds a slot until it expires.
        await session.rollback()
        await quota.release_for_request(factory, reservation)
        _discard_object(storage_key)
        logger.warning("upload.enqueue_failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Processing is unavailable right now. Please try again.",
        ) from exc

    job.rq_job_id = rq_job.id
    # The claim stays held past this commit. It is the concurrent-job limit
    # while the job runs, and the worker converts it to committed usage on
    # completion or releases it on terminal failure.
    await session.commit()

    return UploadOut(
        id=upload.id,
        original_filename=upload.original_filename,
        kind=upload.kind,
        size_bytes=upload.size_bytes,
        status=upload.status,
        created_at=upload.created_at,
        job=JobOut.model_validate(job, from_attributes=True),
    )


@router.get("", response_model=list[UploadOut])
async def list_uploads(user: CurrentUser, session: DbSession) -> list[UploadOut]:
    uploads = (
        await session.execute(user_uploads(user.id).order_by(desc(Upload.created_at)).limit(50))
    ).scalars().all()

    jobs = (
        await session.execute(user_jobs(user.id).order_by(desc(ProcessingJob.created_at)))
    ).scalars().all()
    latest: dict[uuid.UUID, ProcessingJob] = {}
    for job in jobs:
        latest.setdefault(job.upload_id, job)

    return [
        UploadOut(
            id=upload.id,
            original_filename=upload.original_filename,
            kind=upload.kind,
            size_bytes=upload.size_bytes,
            status=upload.status,
            created_at=upload.created_at,
            job=(
                JobOut.model_validate(latest[upload.id], from_attributes=True)
                if upload.id in latest
                else None
            ),
        )
        for upload in uploads
    ]


@router.get("/{upload_id}/job", response_model=JobOut)
async def get_job(upload_id: uuid.UUID, user: CurrentUser, session: DbSession) -> JobOut:
    """Polled by the upload UI while a file is processing."""
    job = (
        await session.execute(
            user_jobs(user.id)
            .where(ProcessingJob.upload_id == upload_id)
            .order_by(desc(ProcessingJob.created_at))
        )
    ).scalars().first()

    if job is None:
        # 404 rather than 403 for another user's upload — no existence leak.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return JobOut.model_validate(job, from_attributes=True)
