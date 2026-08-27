"""Receipt deletion and retention — the worker-facing, synchronous surface.

Export and account deletion run inside an HTTP request on the async session, so
they are exercised end-to-end in test_lifecycle_api.py rather than being driven
here with a second session that would not match how they actually run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ledgerai.models import (
    JobStage,
    ProcessingJob,
    Receipt,
    ReceiptStatus,
    Transaction,
    Upload,
    UploadKind,
    UploadStatus,
    User,
)
from ledgerai.security.filenames import build_storage_key
from ledgerai.services.lifecycle import (
    FAILED_UPLOAD_FILE_DAYS,
    STUCK_JOB_HOURS,
    UNCONFIRMED_RECEIPT_DAYS,
    delete_receipt,
    retention_sweep,
)
from ledgerai.services.storage import get_storage


def make_upload(
    session: Session,
    user: User,
    *,
    status: UploadStatus = UploadStatus.COMPLETE,
    created_at: datetime | None = None,
    with_file: bool = True,
) -> Upload:
    key = build_storage_key(user.id, "receipt.png")
    if with_file:
        get_storage().put(key, b"synthetic-receipt-bytes", "image/png")

    upload = Upload(
        user_id=user.id,
        filename="receipt.png",
        original_filename="receipt_synthetic.png",
        content_hash=uuid.uuid4().hex,
        kind=UploadKind.IMAGE,
        content_type="image/png",
        size_bytes=23,
        storage_key=key,
        status=status,
    )
    session.add(upload)
    session.flush()
    if created_at is not None:
        upload.created_at = created_at
        session.flush()
    return upload


def make_receipt(
    session: Session,
    user: User,
    upload: Upload,
    *,
    status: ReceiptStatus = ReceiptStatus.NEEDS_REVIEW,
    transaction_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> Receipt:
    receipt = Receipt(
        user_id=user.id,
        upload_id=upload.id,
        status=status,
        transaction_id=transaction_id,
        page_count=1,
        ocr_confidence=0.9,
        raw_text="SANDBOX GROCERS TOTAL 30.36",
        merchant="Sandbox Grocers",
        total_cents=3036,
        currency="USD",
    )
    session.add(receipt)
    session.flush()
    if created_at is not None:
        receipt.created_at = created_at
        session.flush()
    return receipt


class TestReceiptDeletion:
    def test_removes_the_row_and_the_file(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        upload = make_upload(sync_db, demo_data["user"])
        receipt = make_receipt(sync_db, demo_data["user"], upload)
        sync_db.commit()
        key = upload.storage_key

        result = delete_receipt(sync_db, receipt)
        sync_db.commit()

        assert result["storage_removed"] is True
        assert sync_db.get(Receipt, receipt.id) is None
        from ledgerai.services.storage import StorageError

        with pytest.raises(StorageError):
            get_storage().get(key)

    def test_a_confirmed_receipt_keeps_its_transaction(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        """Deleting the evidence must not silently change what was spent."""
        transaction = sync_db.execute(
            select(Transaction).where(Transaction.user_id == demo_data["user"].id)
        ).scalars().first()
        upload = make_upload(sync_db, demo_data["user"])
        receipt = make_receipt(
            sync_db, demo_data["user"], upload,
            status=ReceiptStatus.CONFIRMED, transaction_id=transaction.id,
        )
        sync_db.commit()

        result = delete_receipt(sync_db, receipt)
        sync_db.commit()

        assert result["detached_transaction_id"] == str(transaction.id)
        assert sync_db.get(Transaction, transaction.id) is not None


class TestRetention:
    def test_a_job_stuck_mid_pipeline_is_failed(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        """A worker killed outright never runs its own failure handler."""
        user = demo_data["user"]
        upload = make_upload(sync_db, user)
        job = ProcessingJob(
            upload_id=upload.id, user_id=user.id, stage=JobStage.EXTRACTING
        )
        sync_db.add(job)
        sync_db.flush()
        job.created_at = datetime.now(UTC) - timedelta(hours=STUCK_JOB_HOURS + 1)
        sync_db.commit()

        report = retention_sweep(sync_db)
        sync_db.commit()

        assert report.stuck_jobs_failed == 1
        assert sync_db.get(ProcessingJob, job.id).stage == JobStage.FAILED

    def test_a_recent_job_is_left_alone(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        user = demo_data["user"]
        upload = make_upload(sync_db, user)
        job = ProcessingJob(
            upload_id=upload.id, user_id=user.id, stage=JobStage.EXTRACTING
        )
        sync_db.add(job)
        sync_db.commit()

        assert retention_sweep(sync_db).stuck_jobs_failed == 0
        assert sync_db.get(ProcessingJob, job.id).stage == JobStage.EXTRACTING

    def test_failed_upload_files_are_removed_but_the_row_stays(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=FAILED_UPLOAD_FILE_DAYS + 1)
        upload = make_upload(
            sync_db, demo_data["user"], status=UploadStatus.FAILED, created_at=old
        )
        sync_db.commit()

        report = retention_sweep(sync_db)
        sync_db.commit()

        assert report.failed_upload_files_removed == 1
        # The row survives so the user can still see what happened.
        assert sync_db.get(Upload, upload.id) is not None
        assert sync_db.get(Upload, upload.id).storage_key == ""

    def test_a_recently_failed_upload_keeps_its_file(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        upload = make_upload(sync_db, demo_data["user"], status=UploadStatus.FAILED)
        sync_db.commit()

        assert retention_sweep(sync_db).failed_upload_files_removed == 0
        assert sync_db.get(Upload, upload.id).storage_key != ""

    def test_receipts_never_confirmed_expire(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=UNCONFIRMED_RECEIPT_DAYS + 1)
        upload = make_upload(sync_db, demo_data["user"])
        receipt = make_receipt(sync_db, demo_data["user"], upload, created_at=old)
        sync_db.commit()

        report = retention_sweep(sync_db)
        sync_db.commit()

        assert report.unconfirmed_receipts_removed == 1
        assert sync_db.get(Receipt, receipt.id) is None

    def test_a_confirmed_receipt_never_expires(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=UNCONFIRMED_RECEIPT_DAYS + 1)
        transaction = sync_db.execute(
            select(Transaction).where(Transaction.user_id == demo_data["user"].id)
        ).scalars().first()
        upload = make_upload(sync_db, demo_data["user"])
        receipt = make_receipt(
            sync_db, demo_data["user"], upload,
            status=ReceiptStatus.CONFIRMED, transaction_id=transaction.id,
            created_at=old,
        )
        sync_db.commit()

        assert retention_sweep(sync_db).unconfirmed_receipts_removed == 0
        assert sync_db.get(Receipt, receipt.id) is not None

    def test_the_sweep_is_idempotent(self, sync_db: Session, demo_data: dict) -> None:
        old = datetime.now(UTC) - timedelta(hours=STUCK_JOB_HOURS + 1)
        upload = make_upload(sync_db, demo_data["user"])
        job = ProcessingJob(
            upload_id=upload.id, user_id=demo_data["user"].id, stage=JobStage.EXTRACTING
        )
        sync_db.add(job)
        sync_db.flush()
        job.created_at = old
        sync_db.commit()

        assert retention_sweep(sync_db).stuck_jobs_failed == 1
        sync_db.commit()
        assert retention_sweep(sync_db).stuck_jobs_failed == 0
