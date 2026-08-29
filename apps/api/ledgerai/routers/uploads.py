"""Upload intake and job status.

Flow: validate -> hash -> store -> record -> enqueue. The content hash is
checked against a UNIQUE constraint, so re-uploading a file the user has
already ingested returns the original upload rather than creating a second one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..deps import CurrentUser, DbSession
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
from ..services.consent import missing_consents
from ..services.normalize import compute_content_hash
from ..services.scoping import user_jobs, user_uploads
from ..services.storage import StorageError, get_storage

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
    file: UploadFile = File(...),
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

    try:
        get_storage().put(storage_key, data, content_type)
    except StorageError as exc:
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That file is already being processed.",
        ) from exc

    job = ProcessingJob(
        upload_id=upload.id, user_id=user.id, stage=JobStage.QUEUED, progress=0
    )
    session.add(job)
    await session.flush()

    rq_job = get_queue().enqueue(
        process_upload,
        str(upload.id),
        str(job.id),
        on_failure=mark_job_failed,
    )
    job.rq_job_id = rq_job.id
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
