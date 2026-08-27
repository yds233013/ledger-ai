"""Alerts over HTTP: detection during import, the surface, and isolation."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from ledgerai.models import Account, Alert, AlertStatus, AlertType
from ledgerai.services.alerts import ALERT_DISCLAIMER, analyze_transaction, analyze_user
from tests.conftest import make_transaction

pytestmark = pytest.mark.asyncio


@pytest.fixture
def alerts_present(sync_db: Session, demo_data: dict) -> int:
    """Give the user an unmistakable outlier plus a near-duplicate pair."""
    user, account = demo_data["user"], demo_data["account"]
    for index, cents in enumerate((-1100, -1150, -1050, -1200, -1000, -1120, -1080, -1090)):
        make_transaction(
            sync_db, user, account, posted=date(2026, 6, 1 + index), cents=cents,
            description="SANDBOX CAFE", merchant="Sandbox Cafe",
            category_slug="dining", index=100 + index,
        )
    # Far outside that distribution, and far outside the merchant's own range.
    make_transaction(
        sync_db, user, account, posted=date(2026, 6, 20), cents=-45_000,
        description="SANDBOX CAFE", merchant="Sandbox Cafe",
        category_slug="dining", index=120,
    )
    # A near-duplicate pair two days apart.
    for index, day in enumerate((10, 12)):
        make_transaction(
            sync_db, user, account, posted=date(2026, 6, day), cents=-8_999,
            description="SANDBOX ELECTRONICS", merchant="Sandbox Electronics",
            category_slug="shopping", index=130 + index,
        )
    sync_db.commit()

    created = analyze_user(sync_db, user.id)
    sync_db.commit()
    return created


class TestDetectionPersistence:
    async def test_backfill_creates_alerts(self, alerts_present: int) -> None:
        assert alerts_present > 0

    async def test_detection_is_idempotent(
        self, sync_db: Session, demo_data: dict, alerts_present: int
    ) -> None:
        """Re-running detection must insert nothing — the unique constraint on
        (transaction_id, alert_type) makes a retry free."""
        second = analyze_user(sync_db, demo_data["user"].id)
        sync_db.commit()
        assert second == 0

    async def test_income_is_never_alerted_on(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        payroll = make_transaction(
            sync_db, demo_data["user"], demo_data["account"],
            posted=date(2026, 6, 1), cents=+500_000,
            description="PAYROLL", merchant="Payroll", category_slug="income", index=200,
        )
        sync_db.commit()
        assert analyze_transaction(sync_db, demo_data["user"].id, payroll) == 0

    async def test_transfers_are_never_alerted_on(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        transfer = make_transaction(
            sync_db, demo_data["user"], demo_data["account"],
            posted=date(2026, 6, 1), cents=-250_000,
            description="ONLINE TRANSFER TO SAVINGS", merchant="Online Transfer",
            category_slug="transfers", index=201,
        )
        sync_db.commit()
        assert analyze_transaction(sync_db, demo_data["user"].id, transfer) == 0


class TestAlertsApi:
    async def test_lists_open_alerts_with_the_disclaimer(
        self, client: AsyncClient, auth_headers: dict, alerts_present: int
    ) -> None:
        data = (await client.get("/api/alerts", headers=auth_headers)).json()
        assert data["open_count"] > 0
        assert data["disclaimer"] == ALERT_DISCLAIMER

    async def test_each_alert_explains_itself(
        self, client: AsyncClient, auth_headers: dict, alerts_present: int
    ) -> None:
        data = (await client.get("/api/alerts", headers=auth_headers)).json()
        for alert in data["items"]:
            assert alert["message"]
            assert alert["evidence"].get("rule")
            assert alert["evidence"]["disclaimer"] == ALERT_DISCLAIMER
            # And points at the transaction it is about.
            assert alert["transaction_merchant"]
            assert alert["transaction_amount"] is not None

    async def test_no_alert_claims_fraud(
        self, client: AsyncClient, auth_headers: dict, alerts_present: int
    ) -> None:
        data = (await client.get("/api/alerts", headers=auth_headers)).json()
        for alert in data["items"]:
            lowered = alert["message"].lower()
            assert "fraud" not in lowered
            assert "unauthorized" not in lowered

    async def test_dismissing_an_alert(
        self, client: AsyncClient, auth_headers: dict, alerts_present: int
    ) -> None:
        listing = (await client.get("/api/alerts", headers=auth_headers)).json()
        alert_id = listing["items"][0]["id"]

        response = await client.patch(
            f"/api/alerts/{alert_id}", headers=auth_headers, json={"status": "dismissed"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "dismissed"

        after = (await client.get("/api/alerts", headers=auth_headers)).json()
        assert after["open_count"] == listing["open_count"] - 1
        assert after["dismissed_count"] == 1

    async def test_resolving_an_alert(
        self, client: AsyncClient, auth_headers: dict, alerts_present: int
    ) -> None:
        listing = (await client.get("/api/alerts", headers=auth_headers)).json()
        alert_id = listing["items"][0]["id"]
        response = await client.patch(
            f"/api/alerts/{alert_id}", headers=auth_headers, json={"status": "resolved"}
        )
        assert response.json()["status"] == "resolved"

    async def test_invalid_status_is_rejected(
        self, client: AsyncClient, auth_headers: dict, alerts_present: int
    ) -> None:
        listing = (await client.get("/api/alerts", headers=auth_headers)).json()
        response = await client.patch(
            f"/api/alerts/{listing['items'][0]['id']}",
            headers=auth_headers,
            json={"status": "ignored-forever"},
        )
        assert response.status_code == 422

    async def test_dashboard_surfaces_alerts(
        self, client: AsyncClient, auth_headers: dict, alerts_present: int
    ) -> None:
        data = (await client.get("/api/dashboard", headers=auth_headers)).json()
        assert data["alerts_enabled"] is True
        assert data["open_alert_count"] > 0
        assert data["alerts"]
        assert "not fraud detection" in data["alerts_note"]


class TestAlertIsolation:
    async def test_alerts_are_scoped_to_the_caller(
        self, client: AsyncClient, other_headers: dict, alerts_present: int
    ) -> None:
        data = (await client.get("/api/alerts", headers=other_headers)).json()
        assert data["items"] == []
        assert data["open_count"] == 0

    async def test_cannot_dismiss_another_users_alert(
        self, client: AsyncClient, auth_headers: dict, other_headers: dict, alerts_present: int
    ) -> None:
        listing = (await client.get("/api/alerts", headers=auth_headers)).json()
        response = await client.patch(
            f"/api/alerts/{listing['items'][0]['id']}",
            headers=other_headers,
            json={"status": "dismissed"},
        )
        # 404, not 403 — never confirm another user's alert exists.
        assert response.status_code == 404

    async def test_detection_never_reads_across_users(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        """The other user has an identical merchant; it must not become history
        for this user's detection."""
        other_account = sync_db.execute(
            select(Account).where(Account.user_id == demo_data["other"].id)
        ).scalars().first()
        make_transaction(
            sync_db, demo_data["other"], other_account,
            posted=date(2026, 6, 1), cents=-99_999,
            description="SANDBOX CAFE", merchant="Sandbox Cafe",
            category_slug="dining", index=300,
        )
        sync_db.commit()

        target = make_transaction(
            sync_db, demo_data["user"], demo_data["account"],
            posted=date(2026, 6, 2), cents=-5_000,
            description="SANDBOX CAFE", merchant="Sandbox Cafe",
            category_slug="dining", index=301,
        )
        sync_db.commit()
        analyze_transaction(sync_db, demo_data["user"].id, target)
        sync_db.commit()

        alerts = sync_db.execute(
            select(Alert).where(Alert.transaction_id == target.id)
        ).scalars().all()
        # With no history of its own this is simply a new merchant, never an
        # anomaly derived from someone else's spending.
        assert all(alert.alert_type == AlertType.NEW_MERCHANT for alert in alerts)

    async def test_alert_status_starts_open(
        self, sync_db: Session, demo_data: dict, alerts_present: int
    ) -> None:
        statuses = {
            alert.status
            for alert in sync_db.execute(
                select(Alert).where(Alert.user_id == demo_data["user"].id)
            ).scalars()
        }
        assert statuses == {AlertStatus.OPEN}


class TestUnknownAlert:
    async def test_missing_alert_is_404(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.patch(
            f"/api/alerts/{uuid.uuid4()}", headers=auth_headers, json={"status": "dismissed"}
        )
        assert response.status_code == 404
