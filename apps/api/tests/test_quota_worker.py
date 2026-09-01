"""What the worker owes a reservation.

The claim taken when an upload is accepted is held for as long as the job runs
— that hold is the concurrent-job limit. So the worker owns both terminal
transitions: converting the claim when the upload completes, and handing it
back when the job fails for good. Getting either wrong costs somebody a slot
they never used, or gives away one they did.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ledgerai.config import settings
from ledgerai.jobs.process_upload import (
    SensitiveContentError,
    TerminalValidationError,
    _fail_terminally,
    _reject_upload,
    mark_job_failed,
)
from ledgerai.models import (
    JobStage,
    ProcessingJob,
    Upload,
    UploadStatus,
    UsageReservation,
    UserUsage,
)
from ledgerai.services import quota, sensitive
from ledgerai.services.lifecycle import STUCK_JOB_HOURS, retention_sweep
from ledgerai.services.storage import StorageError, get_storage

from .test_quota import make_persistent_user, make_upload


def _reserved_upload(session: Session, size_bytes: int = 2048) -> tuple[Upload, ProcessingJob]:
    """An upload mid-flight: stored, recorded, with its claim held."""
    user = make_persistent_user(session)
    reservation = quota.reserve_upload(session, user.id, size_bytes)
    upload = make_upload(session, user, size_bytes)
    upload.status = UploadStatus.PROCESSING
    quota.attach_upload(session, reservation, upload.id)
    job = ProcessingJob(
        upload_id=upload.id, user_id=user.id, stage=JobStage.EXTRACTING, progress=20
    )
    session.add(job)
    session.flush()
    return upload, job


class _FakeRqJob:
    """Stands in for the RQ job object the failure callback receives."""

    def __init__(self, upload_id: uuid.UUID, job_id: uuid.UUID) -> None:
        self.args = (str(upload_id), str(job_id))
        self.id = "rq-test-job"


class TestTerminalFailure:
    def test_a_terminal_failure_hands_the_claim_back(
        self, sync_db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        upload, job = _reserved_upload(sync_db)
        sync_db.commit()
        monkeypatch.setattr(
            "ledgerai.jobs.process_upload.sync_session", lambda: _NoCloseSession(sync_db)
        )

        mark_job_failed(_FakeRqJob(upload.id, job.id), None, None, None, None)

        sync_db.expire_all()
        assert sync_db.execute(select(UsageReservation)).scalars().all() == []

    def test_a_terminal_failure_charges_nothing_to_the_day(
        self, sync_db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        upload, job = _reserved_upload(sync_db)
        sync_db.commit()
        monkeypatch.setattr(
            "ledgerai.jobs.process_upload.sync_session", lambda: _NoCloseSession(sync_db)
        )

        mark_job_failed(_FakeRqJob(upload.id, job.id), None, None, None, None)

        sync_db.expire_all()
        row = sync_db.execute(select(UserUsage)).scalar_one()
        assert row.uploads_today == 0

    def test_a_freed_slot_can_be_used_again(
        self, sync_db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        upload, job = _reserved_upload(sync_db)
        user_id = upload.user_id
        for _ in range(settings.quota_concurrent_jobs - 1):
            quota.reserve_upload(sync_db, user_id, 1024)
        sync_db.commit()
        monkeypatch.setattr(
            "ledgerai.jobs.process_upload.sync_session", lambda: _NoCloseSession(sync_db)
        )

        mark_job_failed(_FakeRqJob(upload.id, job.id), None, None, None, None)

        sync_db.expire_all()
        assert quota.reserve_upload(sync_db, user_id, 1024) is not None

    def test_an_already_terminal_job_is_left_alone(
        self, sync_db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        upload, job = _reserved_upload(sync_db)
        job.stage = JobStage.COMPLETE
        sync_db.commit()
        monkeypatch.setattr(
            "ledgerai.jobs.process_upload.sync_session", lambda: _NoCloseSession(sync_db)
        )

        mark_job_failed(_FakeRqJob(upload.id, job.id), None, None, None, None)

        sync_db.expire_all()
        # The claim stays: the job already succeeded, and a late failure
        # callback must not refund an upload that was actually processed.
        assert len(sync_db.execute(select(UsageReservation)).scalars().all()) == 1


class TestDeterministicFailure:
    """A verdict that cannot change is not worth three attempts.

    The text-layer cross-check renders and OCRs every page. Letting a refusal
    ride the ordinary retry path repeats that work twice more to reach an answer
    already known, and keeps the page budget held while it does.
    """

    def test_a_deterministic_refusal_hands_the_claim_back_at_once(
        self, sync_db: Session
    ) -> None:
        upload, job = _reserved_upload(sync_db)
        held = sync_db.execute(
            select(UsageReservation).where(UsageReservation.upload_id == upload.id)
        ).scalars().all()
        assert held, "the claim is held while the job runs"

        _fail_terminally(sync_db, upload.id, job.id, "does not match what the pages show")

        assert sync_db.execute(
            select(UsageReservation).where(UsageReservation.upload_id == upload.id)
        ).scalars().all() == []

    def test_a_deterministic_refusal_ends_the_job_and_says_why(
        self, sync_db: Session
    ) -> None:
        upload, job = _reserved_upload(sync_db)
        _fail_terminally(sync_db, upload.id, job.id, "does not match what the pages show")

        sync_db.refresh(job)
        sync_db.refresh(upload)
        assert job.stage == JobStage.FAILED
        assert job.finished_at is not None
        assert "does not match" in (job.error_message or "")
        # The row stays so the reason is readable; only the bytes are collected,
        # by the same sweep that collects any failed upload's file.
        assert upload.status == UploadStatus.FAILED

    def test_it_returns_rather_than_raises_so_rq_does_not_retry(
        self, sync_db: Session
    ) -> None:
        upload, job = _reserved_upload(sync_db)
        result = _fail_terminally(sync_db, upload.id, job.id, "refused")
        assert result["imported"] == 0
        assert "refused" in str(result["error"])

    def test_the_terminal_error_is_still_a_validation_error(self) -> None:
        """So existing handling that reports ValidationError to the UI still applies."""
        from ledgerai.security.validators import ValidationError

        assert issubclass(TerminalValidationError, ValidationError)


class TestRejectedUpload:
    def test_a_rejected_receipts_contents_are_purged(self, sync_db: Session) -> None:
        upload, job = _reserved_upload(sync_db)
        get_storage().put(upload.storage_key, b"receipt bytes", "image/png")
        key = upload.storage_key
        sync_db.commit()

        findings = sensitive.scan_text("VISA 4111111111111111 APPROVED")
        _reject_upload(sync_db, upload.id, job.id, SensitiveContentError(findings))

        sync_db.expire_all()
        rejected = sync_db.get(Upload, upload.id)
        assert rejected.storage_key == ""
        assert rejected.status == UploadStatus.FAILED
        with pytest.raises(StorageError):
            get_storage().get(key)
        assert sync_db.execute(select(UsageReservation)).scalars().all() == []

    def test_a_rejected_upload_is_still_visible_to_its_owner(self, sync_db: Session) -> None:
        # Otherwise the file appears to vanish with no explanation of what to
        # change before trying again.
        upload, job = _reserved_upload(sync_db)
        sync_db.commit()

        findings = sensitive.scan_text("VISA 4111111111111111 APPROVED")
        _reject_upload(sync_db, upload.id, job.id, SensitiveContentError(findings))

        sync_db.expire_all()
        assert sync_db.get(Upload, upload.id) is not None
        assert sync_db.get(ProcessingJob, job.id) is not None

    def test_a_purged_upload_no_longer_counts_toward_stored_bytes(
        self, sync_db: Session
    ) -> None:
        upload, job = _reserved_upload(sync_db, 4096)
        sync_db.commit()
        before = quota.snapshot(sync_db, upload.user_id)["stored_bytes"]

        findings = sensitive.scan_text("VISA 4111111111111111 APPROVED")
        _reject_upload(sync_db, upload.id, job.id, SensitiveContentError(findings))

        sync_db.expire_all()
        assert before == 4096
        assert quota.snapshot(sync_db, upload.user_id)["stored_bytes"] == 0

    def test_the_job_records_remediation_and_nothing_else(self, sync_db: Session) -> None:
        upload, job = _reserved_upload(sync_db)
        sync_db.commit()

        findings = sensitive.scan_text("VISA 4111111111111111 APPROVED")
        _reject_upload(sync_db, upload.id, job.id, SensitiveContentError(findings))

        sync_db.expire_all()
        failed = sync_db.get(ProcessingJob, job.id)
        assert failed.stage == JobStage.FAILED
        assert "mask" in failed.error_message
        assert "4111111111111111" not in failed.error_message

    def test_a_rejection_returns_only_category_names(self, sync_db: Session) -> None:
        upload, job = _reserved_upload(sync_db)
        sync_db.commit()

        findings = sensitive.scan_text("VISA 4111111111111111 APPROVED")
        result = _reject_upload(sync_db, upload.id, job.id, SensitiveContentError(findings))

        assert result == {"rejected": "payment_card"}

    def test_the_exception_itself_cannot_carry_the_matched_text(self) -> None:
        findings = sensitive.scan_text("VISA 4111111111111111 APPROVED")
        exc = SensitiveContentError(findings)
        assert "4111111111111111" not in repr(exc.findings)
        assert "4111111111111111" not in str(exc)


class TestSuccess:
    def test_completing_an_upload_converts_its_claim(self, sync_db: Session) -> None:
        upload, _ = _reserved_upload(sync_db, 2048)

        quota.commit_by_upload(sync_db, upload.id)

        row = sync_db.execute(select(UserUsage)).scalar_one()
        assert row.uploads_today == 1
        assert row.bytes_today == 2048
        assert sync_db.execute(select(UsageReservation)).scalars().all() == []


class TestRetentionSweep:
    def test_an_abandoned_job_releases_its_claim(self, sync_db: Session) -> None:
        upload, job = _reserved_upload(sync_db)
        sync_db.flush()
        job.created_at = datetime.now(UTC) - timedelta(hours=STUCK_JOB_HOURS + 1)
        sync_db.commit()

        report = retention_sweep(sync_db)
        sync_db.commit()

        assert report.stuck_jobs_failed == 1
        assert sync_db.execute(select(UsageReservation)).scalars().all() == []

    def test_expired_claims_are_swept(self, sync_db: Session) -> None:
        user = make_persistent_user(sync_db)
        quota.reserve_upload(
            sync_db, user.id, 1024, now=datetime.now(UTC) - timedelta(hours=2)
        )
        sync_db.commit()

        report = retention_sweep(sync_db)
        sync_db.commit()

        assert report.reservations_swept == 1

    def test_a_live_claim_survives_the_sweep(self, sync_db: Session) -> None:
        upload, _ = _reserved_upload(sync_db)
        sync_db.commit()

        report = retention_sweep(sync_db)
        sync_db.commit()

        assert report.reservations_swept == 0
        assert len(sync_db.execute(select(UsageReservation)).scalars().all()) == 1

    def test_the_sweep_is_idempotent(self, sync_db: Session) -> None:
        user = make_persistent_user(sync_db)
        quota.reserve_upload(
            sync_db, user.id, 1024, now=datetime.now(UTC) - timedelta(hours=2)
        )
        sync_db.commit()

        assert retention_sweep(sync_db).reservations_swept == 1
        sync_db.commit()
        assert retention_sweep(sync_db).reservations_swept == 0


class _NoCloseSession:
    """Lends the test's session to code that owns its own `with sync_session()`.

    The worker opens and closes its own session; the test needs to inspect the
    same rows afterwards. Suppressing only the close keeps the code under test
    unmodified.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, *exc: object) -> bool:
        return False
