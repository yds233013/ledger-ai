"""Durable per-user budgets.

The interesting cases are the ones where a counter could be wrong rather than
merely large: two requests racing for the last slot, a crash between reserving
and committing, a job that fails after its claim was taken, and a counter that
has already drifted and must be repairable without trusting anything a user
sent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ledgerai.config import settings
from ledgerai.models import Upload, UploadKind, UploadStatus, UsageReservation, User, UserUsage
from ledgerai.security.filenames import build_storage_key
from ledgerai.services import quota
from ledgerai.services.quota import QuotaExceededError

from .conftest import make_user

MB = 1024 * 1024


def make_persistent_user(session: Session, email: str = "beta@test.local") -> User:
    """An invited Clerk account — the only kind quotas apply to."""
    user = User(
        email=email,
        password_hash=None,
        display_name="Beta User",
        is_demo=False,
        clerk_user_id=f"user_{uuid.uuid4().hex}",
    )
    session.add(user)
    session.flush()
    return user


def make_upload(session: Session, user: User, size_bytes: int = 1024) -> Upload:
    upload = Upload(
        user_id=user.id,
        filename="statement.csv",
        original_filename="statement.csv",
        content_hash=uuid.uuid4().hex,
        kind=UploadKind.CSV,
        content_type="text/csv",
        size_bytes=size_bytes,
        storage_key=build_storage_key(user.id, "statement.csv"),
        status=UploadStatus.COMPLETE,
    )
    session.add(upload)
    session.flush()
    return upload


class TestScope:
    def test_quotas_apply_to_invited_accounts(self, sync_db: Session):
        assert quota.applies_to(make_persistent_user(sync_db))

    def test_demo_accounts_are_exempt(self, sync_db: Session):
        # Demo accounts are bounded by their 24-hour expiry and the existing
        # rate limits. A durable per-day counter for an account that deletes
        # itself daily would be counting nothing.
        assert not quota.applies_to(make_user(sync_db))

    def test_an_account_without_a_clerk_identity_is_exempt(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        user.clerk_user_id = None
        sync_db.flush()
        assert not quota.applies_to(user)


class TestDailyWindow:
    def test_the_window_is_utc(self):
        # 23:30 in New York is already the next UTC day. The reset must follow
        # UTC, or it moves when somebody travels.
        late_utc = datetime(2026, 3, 2, 4, 30, tzinfo=UTC)
        assert quota.utc_today(late_utc) == datetime(2026, 3, 2, tzinfo=UTC).date()

    def test_the_reset_is_the_next_utc_midnight(self):
        moment = datetime(2026, 3, 2, 23, 59, tzinfo=UTC)
        assert quota.next_utc_midnight(moment) == datetime(2026, 3, 3, tzinfo=UTC)

    def test_yesterdays_usage_does_not_count_against_today(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        yesterday = quota.utc_today() - timedelta(days=1)
        sync_db.add(
            UserUsage(
                id=uuid.uuid4(),
                user_id=user.id,
                usage_date=yesterday,
                uploads_today=settings.quota_uploads_per_day,
                bytes_today=settings.quota_upload_bytes_per_day,
            )
        )
        sync_db.flush()

        reservation = quota.reserve_upload(sync_db, user.id, 1024)
        assert reservation is not None


class TestReservationLifecycle:
    def test_reserving_creates_a_held_claim(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 4096)

        held = sync_db.get(UsageReservation, reservation.id)
        assert held is not None
        assert held.bytes_reserved == 4096
        assert held.upload_id is None
        assert held.usage_date == quota.utc_today()

    def test_a_held_claim_is_not_yet_committed_usage(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        quota.reserve_upload(sync_db, user.id, 4096)

        row = sync_db.execute(
            select(UserUsage).where(UserUsage.user_id == user.id)
        ).scalar_one()
        assert row.uploads_today == 0
        assert row.bytes_today == 0

    def test_committing_moves_the_claim_into_the_daily_counters(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 4096)
        upload = make_upload(sync_db, user, 4096)
        quota.attach_upload(sync_db, reservation, upload.id)

        quota.commit_by_upload(sync_db, upload.id)

        row = sync_db.execute(
            select(UserUsage).where(UserUsage.user_id == user.id)
        ).scalar_one()
        assert row.uploads_today == 1
        assert row.bytes_today == 4096
        assert sync_db.get(UsageReservation, reservation.id) is None

    def test_releasing_gives_the_claim_back_without_charging_it(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 4096)

        quota.release(sync_db, reservation)

        assert sync_db.get(UsageReservation, reservation.id) is None
        row = sync_db.execute(
            select(UserUsage).where(UserUsage.user_id == user.id)
        ).scalar_one()
        assert row.uploads_today == 0

    def test_releasing_twice_is_harmless(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 4096)
        quota.release(sync_db, reservation)
        quota.release(sync_db, reservation)

    def test_committing_an_already_released_claim_charges_nothing(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 4096)
        upload = make_upload(sync_db, user, 4096)
        quota.attach_upload(sync_db, reservation, upload.id)
        quota.release(sync_db, reservation)

        quota.commit_by_upload(sync_db, upload.id)

        row = sync_db.execute(
            select(UserUsage).where(UserUsage.user_id == user.id)
        ).scalar_one()
        assert row.uploads_today == 0

    def test_committing_the_same_upload_twice_charges_once(self, sync_db: Session):
        # A retried job must not double-charge the day.
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 4096)
        upload = make_upload(sync_db, user, 4096)
        quota.attach_upload(sync_db, reservation, upload.id)

        quota.commit_by_upload(sync_db, upload.id)
        quota.commit_by_upload(sync_db, upload.id)

        row = sync_db.execute(
            select(UserUsage).where(UserUsage.user_id == user.id)
        ).scalar_one()
        assert row.uploads_today == 1

    def test_releasing_by_upload_finds_the_claim(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 4096)
        upload = make_upload(sync_db, user, 4096)
        quota.attach_upload(sync_db, reservation, upload.id)

        quota.release_for_upload(sync_db, upload.id)

        assert sync_db.get(UsageReservation, reservation.id) is None

    def test_deleting_the_upload_takes_its_claim_with_it(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 4096)
        upload = make_upload(sync_db, user, 4096)
        quota.attach_upload(sync_db, reservation, upload.id)

        sync_db.delete(upload)
        sync_db.flush()

        assert sync_db.get(UsageReservation, reservation.id) is None


class TestLimits:
    def test_the_daily_upload_count_is_enforced(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        row = quota._lock_day_row(sync_db, user.id, quota.utc_today())
        row.uploads_today = settings.quota_uploads_per_day
        sync_db.flush()

        with pytest.raises(QuotaExceededError) as exc:
            quota.reserve_upload(sync_db, user.id, 1024)
        assert exc.value.quota == "uploads_per_day"
        assert exc.value.remaining == 0

    def test_the_daily_byte_total_is_enforced(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        row = quota._lock_day_row(sync_db, user.id, quota.utc_today())
        row.bytes_today = settings.quota_upload_bytes_per_day - 1024
        sync_db.flush()

        with pytest.raises(QuotaExceededError) as exc:
            quota.reserve_upload(sync_db, user.id, 2048)
        assert exc.value.quota == "upload_bytes_per_day"

    def test_a_file_that_exactly_fits_the_remaining_daily_bytes_is_allowed(
        self, sync_db: Session
    ):
        user = make_persistent_user(sync_db)
        row = quota._lock_day_row(sync_db, user.id, quota.utc_today())
        row.bytes_today = settings.quota_upload_bytes_per_day - 2048
        sync_db.flush()

        assert quota.reserve_upload(sync_db, user.id, 2048) is not None

    def test_total_stored_bytes_are_enforced(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        make_upload(sync_db, user, settings.quota_stored_bytes - 1024)

        with pytest.raises(QuotaExceededError) as exc:
            quota.reserve_upload(sync_db, user.id, 4096)
        assert exc.value.quota == "stored_bytes"

    def test_concurrent_jobs_are_capped(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        for _ in range(settings.quota_concurrent_jobs):
            quota.reserve_upload(sync_db, user.id, 1024)

        with pytest.raises(QuotaExceededError) as exc:
            quota.reserve_upload(sync_db, user.id, 1024)
        assert exc.value.quota == "concurrent_jobs"

    def test_a_finished_job_frees_its_concurrency_slot(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        held = [
            quota.reserve_upload(sync_db, user.id, 1024)
            for _ in range(settings.quota_concurrent_jobs)
        ]
        quota.release(sync_db, held[0])

        assert quota.reserve_upload(sync_db, user.id, 1024) is not None

    def test_held_claims_count_toward_the_daily_limit(self, sync_db: Session):
        # Otherwise a burst of concurrent requests each sees an empty day.
        user = make_persistent_user(sync_db)
        row = quota._lock_day_row(sync_db, user.id, quota.utc_today())
        row.uploads_today = settings.quota_uploads_per_day - 1
        sync_db.flush()

        quota.reserve_upload(sync_db, user.id, 1024)
        with pytest.raises(QuotaExceededError) as exc:
            quota.reserve_upload(sync_db, user.id, 1024)
        assert exc.value.quota == "uploads_per_day"

    def test_another_users_usage_is_never_charged_to_this_one(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        other = make_persistent_user(sync_db, "other-beta@test.local")
        # Exhaust every one of the other account's budgets: concurrency first,
        # then storage, so the reservations themselves are not refused.
        for _ in range(settings.quota_concurrent_jobs):
            quota.reserve_upload(sync_db, other.id, 1024)
        make_upload(sync_db, other, settings.quota_stored_bytes - 1024)

        assert quota.reserve_upload(sync_db, user.id, 4096) is not None


class TestRefusalCarriesNothingSensitive:
    def test_the_message_names_the_budget_and_the_reset(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        row = quota._lock_day_row(sync_db, user.id, quota.utc_today())
        row.uploads_today = settings.quota_uploads_per_day
        sync_db.flush()

        with pytest.raises(QuotaExceededError) as exc:
            quota.reserve_upload(sync_db, user.id, 1024)

        assert "midnight UTC" in exc.value.detail
        headers = exc.value.headers()
        assert headers["X-Quota"] == "uploads_per_day"
        assert headers["X-Quota-Remaining"] == "0"

    def test_no_identifier_reaches_the_refusal(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        row = quota._lock_day_row(sync_db, user.id, quota.utc_today())
        row.uploads_today = settings.quota_uploads_per_day
        sync_db.flush()

        with pytest.raises(QuotaExceededError) as exc:
            quota.reserve_upload(sync_db, user.id, 1024)

        rendered = exc.value.detail + repr(exc.value.headers())
        assert user.email not in rendered
        assert str(user.clerk_user_id) not in rendered
        assert str(user.id) not in rendered


class TestSweep:
    def test_an_expired_claim_is_collected(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        past = datetime.now(UTC) - timedelta(hours=2)
        reservation = quota.reserve_upload(sync_db, user.id, 1024, now=past)

        assert quota.sweep_expired(sync_db) == 1
        assert sync_db.get(UsageReservation, reservation.id) is None

    def test_a_live_claim_survives_the_sweep(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 1024)

        assert quota.sweep_expired(sync_db) == 0
        assert sync_db.get(UsageReservation, reservation.id) is not None

    def test_an_expired_claim_no_longer_holds_a_concurrency_slot(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        past = datetime.now(UTC) - timedelta(hours=2)
        for _ in range(settings.quota_concurrent_jobs):
            quota.reserve_upload(sync_db, user.id, 1024, now=past)

        # A crash between reserving and committing must cost headroom for one
        # sweep interval, not a slot lost until midnight.
        assert quota.reserve_upload(sync_db, user.id, 1024) is not None

    def test_sweeping_an_empty_table_is_a_no_op(self, sync_db: Session):
        assert quota.sweep_expired(sync_db) == 0


class TestReconciliation:
    def test_a_drifted_counter_is_repaired_from_the_uploads_table(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        make_upload(sync_db, user, 1000)
        make_upload(sync_db, user, 2000)
        row = quota._lock_day_row(sync_db, user.id, quota.utc_today())
        row.uploads_today = 99
        row.bytes_today = 999_999
        sync_db.flush()

        report = quota.reconcile(sync_db, user.id)

        assert report["uploads_today"] == 2
        assert row.uploads_today == 2
        assert row.bytes_today == 3000

    def test_reconciling_twice_changes_nothing_the_second_time(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        make_upload(sync_db, user, 1000)

        first = quota.reconcile(sync_db, user.id)
        second = quota.reconcile(sync_db, user.id)

        assert first["uploads_today"] == second["uploads_today"] == 1

    def test_reconciliation_collects_expired_claims(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        past = datetime.now(UTC) - timedelta(hours=2)
        quota.reserve_upload(sync_db, user.id, 1024, now=past)

        assert quota.reconcile(sync_db, user.id)["reservations_swept"] == 1

    def test_reconciliation_repairs_a_missing_row(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        make_upload(sync_db, user, 1000)

        quota.reconcile(sync_db, user.id)

        row = sync_db.execute(
            select(UserUsage).where(UserUsage.user_id == user.id)
        ).scalar_one()
        assert row.uploads_today == 1


class TestDeletionAndStorage:
    def test_deleting_an_upload_frees_stored_bytes(self, sync_db: Session):
        # Stored bytes are counted, never accumulated, so deletion frees them
        # with nothing to decrement and nothing that can drift.
        user = make_persistent_user(sync_db)
        upload = make_upload(sync_db, user, settings.quota_stored_bytes - 1024)
        with pytest.raises(QuotaExceededError):
            quota.reserve_upload(sync_db, user.id, 4096)

        sync_db.delete(upload)
        sync_db.flush()

        assert quota.reserve_upload(sync_db, user.id, 4096) is not None

    def test_deleting_a_file_does_not_refund_the_days_upload_count(self, sync_db: Session):
        # Otherwise the daily limit is bypassable: upload, delete, repeat.
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 1024)
        upload = make_upload(sync_db, user, 1024)
        quota.attach_upload(sync_db, reservation, upload.id)
        quota.commit_by_upload(sync_db, upload.id)

        sync_db.delete(upload)
        sync_db.flush()

        row = sync_db.execute(
            select(UserUsage).where(UserUsage.user_id == user.id)
        ).scalar_one()
        assert row.uploads_today == 1


class TestSnapshot:
    def test_a_snapshot_reports_usage_against_every_budget(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        reservation = quota.reserve_upload(sync_db, user.id, 4096)
        upload = make_upload(sync_db, user, 4096)
        quota.attach_upload(sync_db, reservation, upload.id)
        quota.commit_by_upload(sync_db, upload.id)

        state = quota.snapshot(sync_db, user.id)

        assert state["uploads_today"] == 1
        assert state["bytes_today"] == 4096
        assert state["stored_bytes"] == 4096
        assert state["uploads_per_day"] == settings.quota_uploads_per_day
        assert state["jobs_in_flight"] == 0

    def test_held_claims_show_as_jobs_in_flight(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        quota.reserve_upload(sync_db, user.id, 4096)

        state = quota.snapshot(sync_db, user.id)

        assert state["jobs_in_flight"] == 1
        assert state["uploads_today"] == 1, "an in-flight upload already counts"

    def test_a_snapshot_names_no_account(self, sync_db: Session):
        user = make_persistent_user(sync_db)
        rendered = repr(quota.snapshot(sync_db, user.id))
        assert user.email not in rendered
        assert str(user.clerk_user_id) not in rendered


class TestConfiguredValues:
    def test_the_private_beta_defaults_are_what_was_agreed(self):
        assert settings.quota_uploads_per_day == 25
        assert settings.quota_upload_bytes_per_day == 50 * MB
        assert settings.quota_stored_bytes == 250 * MB
        assert settings.quota_transaction_rows == 25_000
        assert settings.quota_receipts == 500
        assert settings.quota_concurrent_jobs == 3
        assert settings.quota_max_job_attempts == 3

    def test_a_daily_byte_budget_below_the_file_limit_is_refused(self):
        # A configuration where no single permitted file fits in the day's
        # budget would refuse every upload with a message about the wrong limit.
        from pydantic import ValidationError

        from ledgerai.config import Settings

        with pytest.raises(ValidationError):
            Settings(max_upload_bytes=10 * MB, quota_upload_bytes_per_day=1 * MB)

    def test_a_non_positive_quota_is_refused(self):
        from pydantic import ValidationError

        from ledgerai.config import Settings

        with pytest.raises(ValidationError):
            Settings(quota_uploads_per_day=0)
