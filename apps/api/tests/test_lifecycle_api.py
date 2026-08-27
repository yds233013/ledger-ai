"""Export, deletion and rate limiting over HTTP."""

from __future__ import annotations

import csv
import io
import zipfile

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ledgerai.models import Receipt, Transaction, User
from ledgerai.security.ratelimit import (
    DESTRUCTIVE_LIMIT,
    EXPORT_LIMIT,
    LOGIN_LIMIT,
    get_limiter_redis,
    reset_limiter,
)
from tests.test_receipts_api import seed_receipt

pytestmark = pytest.mark.asyncio

CONFIRM = {"confirmation": "DELETE"}


class BrokenRedis:
    """A rate-limit store that is down. Every operation refuses."""

    async def incr(self, *_args, **_kwargs):
        raise ConnectionError("redis://user:hunter2@cache.internal:6379 is down")

    async def expire(self, *_args, **_kwargs):
        raise ConnectionError("redis://user:hunter2@cache.internal:6379 is down")

    async def ping(self, *_args, **_kwargs):
        raise ConnectionError("redis://user:hunter2@cache.internal:6379 is down")


@pytest.fixture(autouse=True)
async def _clear_rate_limits():
    """Each test starts with a fresh budget."""
    reset_limiter(None)
    try:
        client = get_limiter_redis()
        keys = [key async for key in client.scan_iter("ratelimit:*")]
        if keys:
            await client.delete(*keys)
    except Exception:  # noqa: BLE001, S110 - a limiter outage is not this test's concern
        reset_limiter(None)
    yield


class TestExportEndpoint:
    async def test_returns_a_zip_attachment(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get("/api/settings/export", headers=auth_headers)

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment" in response.headers["content-disposition"]
        # An export is personal data; it must never be cached by anything.
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"

    async def test_the_archive_opens_and_is_scoped(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get("/api/settings/export", headers=auth_headers)
        archive = zipfile.ZipFile(io.BytesIO(response.content))

        blob = b"".join(archive.read(name) for name in archive.namelist()).decode()
        assert "OTHER USER SECRET" not in blob

        rows = list(csv.DictReader(io.StringIO(archive.read("transactions.csv").decode())))
        assert rows

    async def test_each_user_exports_only_their_own(
        self, client: AsyncClient, auth_headers: dict, other_headers: dict
    ) -> None:
        mine = zipfile.ZipFile(
            io.BytesIO((await client.get("/api/settings/export", headers=auth_headers)).content)
        )
        theirs = zipfile.ZipFile(
            io.BytesIO((await client.get("/api/settings/export", headers=other_headers)).content)
        )

        mine_rows = list(csv.DictReader(io.StringIO(mine.read("transactions.csv").decode())))
        their_rows = list(csv.DictReader(io.StringIO(theirs.read("transactions.csv").decode())))

        assert {r["id"] for r in mine_rows}.isdisjoint({r["id"] for r in their_rows})

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/settings/export")).status_code == 401

    async def test_the_filename_is_readable_cross_origin(self) -> None:
        """The web app runs on a different origin from the API, and a browser
        cannot see Content-Disposition unless the server exposes it — without
        this the export saves under a generic name."""
        from starlette.middleware.cors import CORSMiddleware

        from ledgerai.main import app

        cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
        assert "Content-Disposition" in cors.kwargs["expose_headers"]

    async def test_contains_every_expected_file(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get("/api/settings/export", headers=auth_headers)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert set(archive.namelist()) == {
            "README.txt", "profile.json", "accounts.csv", "transactions.csv",
            "receipts.csv", "alerts.csv", "corrections.csv", "analysis_runs.json",
        }

    async def test_amounts_keep_their_sign_and_scale(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Integer cents internally; readable decimals on the way out."""
        response = await client.get("/api/settings/export", headers=auth_headers)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        rows = list(csv.DictReader(io.StringIO(archive.read("transactions.csv").decode())))

        amounts = {row["amount"] for row in rows}
        assert "-40.00" in amounts     # an outflow stays negative
        assert "3000.00" in amounts    # an inflow stays positive

    async def test_readme_states_the_data_is_synthetic(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get("/api/settings/export", headers=auth_headers)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert "SYNTHETIC" in archive.read("README.txt").decode()

    async def test_receipts_are_included(
        self, client: AsyncClient, auth_headers: dict, sync_db: Session, demo_data: dict
    ) -> None:
        seed_receipt(sync_db, demo_data["user"])

        response = await client.get("/api/settings/export", headers=auth_headers)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        rows = list(csv.DictReader(io.StringIO(archive.read("receipts.csv").decode())))

        assert rows[0]["merchant"] == "Sandbox Grocers"
        assert rows[0]["total"] == "30.36"


class TestDeletionEndpoints:
    async def test_confirmation_is_required(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.post(
            "/api/settings/delete-data",
            headers=auth_headers,
            json={"confirmation": "yes please"},
        )
        assert response.status_code == 422
        assert "Nothing has been removed" in response.json()["detail"]

    async def test_dry_run_reports_without_deleting(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        before = (await client.get("/api/transactions?limit=1", headers=auth_headers)).json()

        response = await client.post(
            "/api/settings/delete-data",
            headers=auth_headers,
            json={**CONFIRM, "dry_run": True},
        )
        body = response.json()

        assert body["dry_run"] is True
        assert body["total_rows"] > 0
        assert "nothing was removed" in body["message"]

        after = (await client.get("/api/transactions?limit=1", headers=auth_headers)).json()
        assert after["total"] == before["total"]

    async def test_delete_data_empties_the_account(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.post(
            "/api/settings/delete-data", headers=auth_headers, json=CONFIRM
        )
        assert response.status_code == 200

        listing = (await client.get("/api/transactions?limit=1", headers=auth_headers)).json()
        assert listing["total"] == 0

        # The account itself still works.
        assert (await client.get("/api/auth/me", headers=auth_headers)).status_code == 200

    async def test_delete_account_removes_the_user(
        self, client: AsyncClient, auth_headers: dict, sync_db: Session, demo_data: dict
    ) -> None:
        user_id = demo_data["user"].id
        response = await client.post(
            "/api/settings/delete-account", headers=auth_headers, json=CONFIRM
        )
        assert response.status_code == 200
        assert response.json()["account_removed"] is True

        sync_db.expire_all()
        assert sync_db.get(User, user_id) is None

        # The token now resolves to nobody.
        assert (await client.get("/api/auth/me", headers=auth_headers)).status_code == 401

    async def test_one_users_deletion_leaves_the_other_intact(
        self, client: AsyncClient, auth_headers: dict, other_headers: dict
    ) -> None:
        before = (await client.get("/api/transactions?limit=1", headers=other_headers)).json()

        await client.post("/api/settings/delete-account", headers=auth_headers, json=CONFIRM)

        after = (await client.get("/api/transactions?limit=1", headers=other_headers)).json()
        assert after["total"] == before["total"] > 0

    async def test_deletion_reports_what_it_reached(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        body = (
            await client.post(
                "/api/settings/delete-data", headers=auth_headers, json=CONFIRM
            )
        ).json()

        # Every surface deletion has to reach is accounted for in the response.
        for field in (
            "rows_by_table",
            "storage_objects_removed",
            "cache_keys_removed",
            "queued_jobs_cancelled",
        ):
            assert field in body

    async def test_deletion_requires_authentication(self, client: AsyncClient) -> None:
        assert (
            await client.post("/api/settings/delete-account", json=CONFIRM)
        ).status_code == 401


class TestReceiptDeletionEndpoint:
    async def test_deletes_a_receipt(
        self, client: AsyncClient, auth_headers: dict, sync_db: Session, demo_data: dict
    ) -> None:
        receipt = seed_receipt(sync_db, demo_data["user"])

        response = await client.delete(f"/api/receipts/{receipt.id}", headers=auth_headers)
        assert response.status_code == 200

        listing = (await client.get("/api/receipts", headers=auth_headers)).json()
        assert listing == []

    async def test_a_confirmed_receipt_keeps_its_transaction(
        self, client: AsyncClient, auth_headers: dict, sync_db: Session, demo_data: dict
    ) -> None:
        receipt = seed_receipt(sync_db, demo_data["user"])
        confirmed = (
            await client.post(
                f"/api/receipts/{receipt.id}/confirm",
                headers=auth_headers,
                json={"mode": "create"},
            )
        ).json()

        body = (
            await client.delete(f"/api/receipts/{receipt.id}", headers=auth_headers)
        ).json()

        assert body["detached_transaction_id"] == confirmed["transaction_id"]
        assert "does not change what you spent" in body["message"]

        sync_db.expire_all()
        assert sync_db.get(Transaction, __import__("uuid").UUID(confirmed["transaction_id"]))

    async def test_cannot_delete_another_users_receipt(
        self, client: AsyncClient, other_headers: dict, sync_db: Session, demo_data: dict
    ) -> None:
        receipt = seed_receipt(sync_db, demo_data["user"])

        response = await client.delete(f"/api/receipts/{receipt.id}", headers=other_headers)
        assert response.status_code == 404

        sync_db.expire_all()
        assert sync_db.get(Receipt, receipt.id) is not None


class TestLimiterFailureBehaviour:
    """What happens when the rate-limit store is unreachable.

    The policy is asymmetric on purpose, so development and production are
    asserted separately rather than through one shared expectation.
    """

    @pytest.fixture
    def broken_store(self):
        from ledgerai.security import ratelimit

        ratelimit.reset_limiter(BrokenRedis())  # type: ignore[arg-type]
        yield
        ratelimit.reset_limiter(None)

    @pytest.fixture
    def in_production(self, monkeypatch):
        from ledgerai.config import settings

        monkeypatch.setattr(settings, "environment", "production")
        assert settings.is_production
        yield

    # ---- development: usable while the store is down -------------------

    async def test_development_login_still_works_without_the_store(
        self, client: AsyncClient, broken_store
    ) -> None:
        """A developer whose Redis is down must still be able to sign in."""
        response = await client.post(
            "/api/auth/login",
            json={"email": "user@test.local", "password": "wrong-password"},
        )
        # 401 for the bad password — reached the check, was not blocked by it.
        assert response.status_code == 401

    async def test_development_uploads_and_analysis_are_not_blocked(self) -> None:
        from ledgerai.security.ratelimit import (
            ANALYSIS_LIMIT,
            UPLOAD_LIMIT,
            fails_closed,
        )

        assert fails_closed(UPLOAD_LIMIT) is False
        assert fails_closed(ANALYSIS_LIMIT) is False

    # ---- production: abuse-sensitive endpoints refuse ------------------

    async def test_production_login_fails_closed(
        self, client: AsyncClient, broken_store, in_production
    ) -> None:
        """An attacker must not get unlimited guesses by taking Redis down."""
        response = await client.post(
            "/api/auth/login",
            json={"email": "user@test.local", "password": "wrong-password"},
        )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "30"

    async def test_production_refusal_reveals_no_infrastructure(
        self, client: AsyncClient, broken_store, in_production
    ) -> None:
        response = await client.post(
            "/api/auth/login",
            json={"email": "user@test.local", "password": "wrong-password"},
        )
        detail = response.json()["detail"]
        assert detail == "Service temporarily unavailable. Please try again shortly."
        lowered = detail.lower()
        for leak in ("redis", "connection", "cache.internal", "6379", "hunter2"):
            assert leak not in lowered

    async def test_production_uploads_are_rejected_without_the_store(
        self, client: AsyncClient, auth_headers: dict, broken_store, in_production
    ) -> None:
        response = await client.post(
            "/api/uploads",
            headers=auth_headers,
            files={"file": ("x.csv", b"date,description,amount\n", "text/csv")},
        )
        assert response.status_code == 503

    async def test_production_analysis_is_rejected_without_the_store(
        self, client: AsyncClient, auth_headers: dict, broken_store, in_production
    ) -> None:
        response = await client.post(
            "/api/analysis/runs",
            headers=auth_headers,
            json={"question": "How much did I spend last month?", "use_cache": False},
        )
        assert response.status_code == 503

    async def test_every_public_limit_fails_closed_in_production(
        self, in_production
    ) -> None:
        """Covers demo-session provisioning, whose endpoint is Checkpoint B."""
        from ledgerai.security import ratelimit

        public = [
            ratelimit.LOGIN_LIMIT,
            ratelimit.DEMO_SESSION_LIMIT,
            ratelimit.UPLOAD_LIMIT,
            ratelimit.ANALYSIS_LIMIT,
        ]
        assert all(ratelimit.fails_closed(limit) for limit in public)

    async def test_self_directed_operations_still_fail_open_in_production(
        self, client: AsyncClient, auth_headers: dict, broken_store, in_production
    ) -> None:
        """Export is the caller's own data. An outage must not withhold it."""
        from ledgerai.security import ratelimit

        assert ratelimit.fails_closed(EXPORT_LIMIT) is False
        assert ratelimit.fails_closed(DESTRUCTIVE_LIMIT) is False

        response = await client.get("/api/settings/export", headers=auth_headers)
        assert response.status_code == 200

    # ---- health surfaces the degradation ------------------------------

    async def test_health_reports_a_healthy_store(self, client: AsyncClient) -> None:
        body = (await client.get("/health")).json()
        assert body["status"] == "ok"
        assert body["dependencies"]["rate_limit_store"] == "ok"
        assert body["rate_limiting"] == "enforced"

    async def test_health_reports_the_degraded_store(
        self, client: AsyncClient, broken_store
    ) -> None:
        response = await client.get("/health")

        # Still 200: killing every replica over a dependency outage would turn
        # a degraded limiter into a total outage.
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["dependencies"]["rate_limit_store"] == "unavailable"
        assert body["rate_limiting"] == "failing_open"

    async def test_health_says_which_policy_is_in_force(
        self, client: AsyncClient, broken_store, in_production
    ) -> None:
        body = (await client.get("/health")).json()
        assert body["status"] == "degraded"
        assert body["rate_limiting"] == "failing_closed"
        # Names the consequence, never the technology.
        assert "redis" not in str(body).lower()


class TestRateLimiting:
    async def test_login_is_throttled(self, client: AsyncClient) -> None:
        payload = {"email": "user@test.local", "password": "wrong-password"}

        codes = [
            (await client.post("/api/auth/login", json=payload)).status_code
            for _ in range(LOGIN_LIMIT.times + 2)
        ]

        assert 429 in codes
        assert codes.count(401) == LOGIN_LIMIT.times

    async def test_a_throttled_response_says_when_to_retry(
        self, client: AsyncClient
    ) -> None:
        payload = {"email": "user@test.local", "password": "wrong-password"}
        response = None
        for _ in range(LOGIN_LIMIT.times + 2):
            response = await client.post("/api/auth/login", json=payload)
            if response.status_code == 429:
                break

        assert response is not None
        assert response.status_code == 429
        assert response.headers["retry-after"] == str(LOGIN_LIMIT.seconds)
        # The message must not hint at whether the account exists.
        assert "password" not in response.json()["detail"].lower()

    async def test_export_is_throttled_per_user(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        codes = [
            (await client.get("/api/settings/export", headers=auth_headers)).status_code
            for _ in range(EXPORT_LIMIT.times + 1)
        ]
        assert codes[-1] == 429
        assert codes.count(200) == EXPORT_LIMIT.times

    async def test_destructive_operations_are_throttled(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        body = {**CONFIRM, "dry_run": True}
        codes = [
            (
                await client.post(
                    "/api/settings/delete-data", headers=auth_headers, json=body
                )
            ).status_code
            for _ in range(DESTRUCTIVE_LIMIT.times + 1)
        ]
        assert codes[-1] == 429

    async def test_one_users_budget_does_not_affect_another(
        self, client: AsyncClient, auth_headers: dict, other_headers: dict
    ) -> None:
        """Per-user budgets: one heavy user must not lock everyone else out."""
        for _ in range(EXPORT_LIMIT.times + 1):
            await client.get("/api/settings/export", headers=auth_headers)

        response = await client.get("/api/settings/export", headers=other_headers)
        assert response.status_code == 200

    async def test_the_limiter_fails_open(self, monkeypatch) -> None:
        """A Redis outage must not lock users out of their own data.

        For an authenticated, self-directed operation the safe failure is to
        allow the request. Denying a user their own export protects nobody.
        """
        from ledgerai.security import ratelimit

        ratelimit.reset_limiter(BrokenRedis())  # type: ignore[arg-type]
        try:
            allowed, remaining, available = await ratelimit.check_rate_limit(
                "anyone", EXPORT_LIMIT
            )
        finally:
            ratelimit.reset_limiter(None)

        assert allowed is True
        assert remaining == EXPORT_LIMIT.times
        assert available is False, "the outage must be reported, not hidden"


class TestCountsAfterDeletion:
    async def test_dashboard_is_empty_but_functional(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        await client.post("/api/settings/delete-data", headers=auth_headers, json=CONFIRM)

        dashboard = (await client.get("/api/dashboard", headers=auth_headers)).json()
        assert dashboard["transaction_count"] == 0
        assert dashboard["total_spend_cents"] == 0
        assert dashboard["alerts"] == []

    async def test_no_rows_remain_in_any_owned_table(
        self, client: AsyncClient, auth_headers: dict, sync_db: Session, demo_data: dict
    ) -> None:
        await client.post("/api/settings/delete-data", headers=auth_headers, json=CONFIRM)
        sync_db.expire_all()

        for model in (Transaction, Receipt):
            remaining = sync_db.execute(
                select(func.count()).select_from(model).where(
                    model.user_id == demo_data["user"].id
                )
            ).scalar_one()
            assert remaining == 0
