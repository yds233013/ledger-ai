"""Quotas and sensitive-content rejection at the upload boundary.

These exercise the whole request rather than the services underneath, because
the ordering is the part that matters: the scan has to run before anything is
stored, the reservation has to be taken before the bytes are written, and every
failure path after that has to hand the reservation back.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from ledgerai.config import settings
from ledgerai.models import Upload, UsageReservation, User, UserConsent, UserUsage
from ledgerai.security.jwt import create_access_token
from ledgerai.services import consent, quota

VISA_TEST_PAN = "4111111111111111"

CLEAN_CSV = (
    b"Date,Description,Amount,Account Number\n"
    b"2026-01-04,WHOLE FOODS MKT,-64.21,****4821\n"
    b"2026-01-05,TRANSIT AUTHORITY,-2.75,****4821\n"
)


@pytest.fixture
def beta_user(sync_db: Session) -> User:
    """An invited account that has accepted every current consent."""
    user = User(
        email="beta-upload@test.local",
        password_hash=None,
        display_name="Beta User",
        is_demo=False,
        clerk_user_id=f"user_{uuid.uuid4().hex}",
    )
    sync_db.add(user)
    sync_db.flush()
    for consent_type in consent.UPLOAD_PREREQUISITES:
        consent.record_consent(sync_db, user_id=user.id, consent_type=consent_type)
    sync_db.commit()
    return user


@pytest.fixture
def beta_headers(beta_user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(beta_user.id, beta_user.email)}"}


def _csv_with(value: str) -> bytes:
    return (
        f"Date,Description,Amount\n2026-01-04,PAYMENT {value},-64.21\n"
    ).encode()


class TestSensitiveRejection:
    async def test_a_file_with_an_unmasked_card_number_is_refused(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", _csv_with(VISA_TEST_PAN), "text/csv")},
        )
        assert response.status_code == 422

    async def test_the_refusal_says_how_to_fix_it(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", _csv_with(VISA_TEST_PAN), "text/csv")},
        )
        assert "mask" in response.json()["detail"]
        assert response.headers["x-rejected-categories"] == "payment_card"

    async def test_the_refusal_names_no_row_column_or_value(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", _csv_with(VISA_TEST_PAN), "text/csv")},
        )
        body = response.text + repr(dict(response.headers))
        for leak in (VISA_TEST_PAN, "1111", "row 1", "Description", "PAYMENT"):
            assert leak not in body, leak

    async def test_a_rejected_file_is_never_stored(
        self, client: AsyncClient, beta_headers: dict, sync_db: Session
    ) -> None:
        await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", _csv_with(VISA_TEST_PAN), "text/csv")},
        )
        assert sync_db.execute(select(Upload)).scalars().all() == []

    async def test_a_rejected_file_costs_no_budget(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        # The scan runs before the reservation, so there is nothing to release.
        await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", _csv_with(VISA_TEST_PAN), "text/csv")},
        )
        sync_db.expire_all()
        assert sync_db.execute(select(UsageReservation)).scalars().all() == []
        row = sync_db.execute(
            select(UserUsage).where(UserUsage.user_id == beta_user.id)
        ).scalar_one_or_none()
        assert row is None or row.uploads_today == 0

    async def test_a_real_bank_export_with_a_masked_account_column_is_accepted(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        assert response.status_code == 201, response.text

    async def test_a_demo_account_is_scanned_too(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        # Quotas are for persistent accounts; the scan is for everybody. A demo
        # user pasting a real card number is exactly the mistake worth catching.
        response = await client.post(
            "/api/uploads",
            headers=auth_headers,
            files={"file": ("statement.csv", _csv_with(VISA_TEST_PAN), "text/csv")},
        )
        assert response.status_code == 422


class TestQuotaEnforcement:
    async def test_an_accepted_upload_holds_a_claim_while_its_job_runs(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        assert response.status_code == 201

        sync_db.expire_all()
        held = sync_db.execute(
            select(UsageReservation).where(UsageReservation.user_id == beta_user.id)
        ).scalars().all()
        assert len(held) == 1
        assert held[0].upload_id == uuid.UUID(response.json()["id"])
        assert held[0].bytes_reserved == len(CLEAN_CSV)

    async def test_the_daily_upload_limit_refuses_with_429(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        row = quota._lock_day_row(sync_db, beta_user.id, quota.utc_today())
        row.uploads_today = settings.quota_uploads_per_day
        sync_db.commit()

        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        assert response.status_code == 429
        assert response.headers["x-quota"] == "uploads_per_day"
        assert "midnight UTC" in response.json()["detail"]

    async def test_a_refused_upload_is_never_stored(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        row = quota._lock_day_row(sync_db, beta_user.id, quota.utc_today())
        row.uploads_today = settings.quota_uploads_per_day
        sync_db.commit()

        await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        sync_db.expire_all()
        assert sync_db.execute(select(Upload)).scalars().all() == []

    async def test_the_concurrency_limit_refuses_a_fourth_file(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        for _ in range(settings.quota_concurrent_jobs):
            quota.reserve_upload(sync_db, beta_user.id, 1024)
        sync_db.commit()

        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        assert response.status_code == 429
        assert response.headers["x-quota"] == "concurrent_jobs"

    async def test_the_refusal_leaks_no_identifier(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        row = quota._lock_day_row(sync_db, beta_user.id, quota.utc_today())
        row.uploads_today = settings.quota_uploads_per_day
        sync_db.commit()

        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        body = response.text + repr(dict(response.headers))
        assert beta_user.email not in body
        assert str(beta_user.clerk_user_id) not in body

    async def test_a_duplicate_upload_costs_no_additional_budget(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        # The duplicate check runs before the reservation, so re-uploading the
        # same file returns the original without spending a second slot.
        first = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        assert first.status_code == 201
        second = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        assert second.json()["duplicate_of_existing"] is True

        sync_db.expire_all()
        held = sync_db.execute(
            select(UsageReservation).where(UsageReservation.user_id == beta_user.id)
        ).scalars().all()
        assert len(held) == 1

    async def test_a_demo_account_is_never_charged(
        self, client: AsyncClient, auth_headers: dict, sync_db: Session
    ) -> None:
        response = await client.post(
            "/api/uploads",
            headers=auth_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        assert response.status_code == 201
        sync_db.expire_all()
        assert sync_db.execute(select(UsageReservation)).scalars().all() == []
        assert sync_db.execute(select(UserUsage)).scalars().all() == []


class TestConsentGate:
    async def test_upload_is_refused_until_the_current_versions_are_accepted(
        self, client: AsyncClient, beta_user: User, beta_headers: dict, sync_db: Session
    ) -> None:
        sync_db.execute(
            UserConsent.__table__.delete().where(UserConsent.user_id == beta_user.id)
        )
        sync_db.commit()

        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        assert response.status_code == 403
        assert "accept" in response.json()["detail"].lower()

    async def test_a_refused_upload_reserves_nothing(
        self, client: AsyncClient, beta_user: User, beta_headers: dict, sync_db: Session
    ) -> None:
        sync_db.execute(
            UserConsent.__table__.delete().where(UserConsent.user_id == beta_user.id)
        )
        sync_db.commit()

        await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        sync_db.expire_all()
        assert sync_db.execute(select(UsageReservation)).scalars().all() == []


class TestUsageEndpoint:
    async def test_an_invited_account_sees_its_budgets(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        body = (await client.get("/api/settings/usage", headers=beta_headers)).json()
        assert body["applies"] is True
        assert body["uploads_per_day"] == settings.quota_uploads_per_day
        assert body["stored_bytes_limit"] == settings.quota_stored_bytes
        assert body["max_upload_bytes"] == settings.max_upload_bytes
        assert body["resets_at"].endswith("+00:00")

    async def test_usage_reflects_an_upload(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )
        body = (await client.get("/api/settings/usage", headers=beta_headers)).json()
        assert body["uploads_today"] == 1
        assert body["jobs_in_flight"] == 1

    async def test_a_demo_account_is_told_quotas_do_not_apply(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        body = (await client.get("/api/settings/usage", headers=auth_headers)).json()
        assert body["applies"] is False

    async def test_usage_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/settings/usage")).status_code == 401

    async def test_usage_describes_only_the_caller(
        self, client: AsyncClient, beta_headers: dict, beta_user: User
    ) -> None:
        body = (await client.get("/api/settings/usage", headers=beta_headers)).text
        assert beta_user.email not in body
        assert str(beta_user.clerk_user_id) not in body
        assert str(beta_user.id) not in body


class TestQueueOutage:
    async def test_an_unqueueable_upload_is_undone_entirely(
        self, client: AsyncClient, beta_headers: dict, sync_db: Session, monkeypatch
    ) -> None:
        # Nothing will ever process the file, so leaving a row that says
        # "processing" forever — and a claim holding a slot until it expires —
        # would be worse than refusing outright.
        class _DeadQueue:
            def enqueue(self, *args: object, **kwargs: object) -> None:
                raise ConnectionError("queue unavailable")

        monkeypatch.setattr("ledgerai.routers.uploads.get_queue", lambda: _DeadQueue())

        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )

        assert response.status_code == 503
        sync_db.expire_all()
        assert sync_db.execute(select(Upload)).scalars().all() == []
        assert sync_db.execute(select(UsageReservation)).scalars().all() == []

    async def test_the_message_names_no_dependency(
        self, client: AsyncClient, beta_headers: dict, monkeypatch
    ) -> None:
        class _DeadQueue:
            def enqueue(self, *args: object, **kwargs: object) -> None:
                raise ConnectionError("redis://queue-host:6379 refused")

        monkeypatch.setattr("ledgerai.routers.uploads.get_queue", lambda: _DeadQueue())

        response = await client.post(
            "/api/uploads",
            headers=beta_headers,
            files={"file": ("statement.csv", CLEAN_CSV, "text/csv")},
        )

        body = response.text
        assert "redis" not in body.lower()
        assert "queue-host" not in body
