"""The alert-based transactions filter.

The dashboard's "view all flagged transactions" link used to point at
`?review=needs_review`, which selects the low-confidence categorization queue.
That is a different fact about a transaction: a duplicated charge at a
well-known merchant is categorized at confidence 1.00 and never enters the
review queue, so the link routinely showed none of the alerts it promised.

These tests pin the two filters apart, and pin that `flagged` finds alerted
rows regardless of how confidently they were categorized.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from ledgerai.models import Alert, AlertSeverity, AlertStatus, AlertType
from tests.conftest import make_transaction

pytestmark = pytest.mark.asyncio


@pytest.fixture
def flagged_data(sync_db: Session, demo_data: dict) -> dict:
    """Three rows that separate "alerted" from "needs review" cleanly.

      * confident_flagged — confidence 1.00, needs_review False, HAS an alert.
        The case the old link could never surface.
      * unconfident_clean — needs_review True, no alert. The case the old link
        showed instead.
      * dismissed_flagged — has an alert, but the user dismissed it.
    """
    user, account = demo_data["user"], demo_data["account"]

    confident_flagged = make_transaction(
        sync_db, user, account, posted=date(2026, 7, 2), cents=-8_999,
        description="SANDBOX ELECTRONICS", merchant="Sandbox Electronics",
        category_slug="shopping", index=300,
    )
    unconfident_clean = make_transaction(
        sync_db, user, account, posted=date(2026, 7, 3), cents=-2_100,
        description="ZORBLAX QUANTUM WIDGETS", merchant="Zorblax Quantum Widgets",
        category_slug=None, index=301,
    )
    dismissed_flagged = make_transaction(
        sync_db, user, account, posted=date(2026, 7, 4), cents=-4_200,
        description="SANDBOX HARDWARE", merchant="Sandbox Hardware",
        category_slug="shopping", index=302,
    )

    sync_db.add_all([
        Alert(
            user_id=user.id, transaction_id=confident_flagged.id,
            alert_type=AlertType.DUPLICATE, severity=AlertSeverity.HIGH,
            message="This looks like the same charge twice.",
            evidence={}, status=AlertStatus.OPEN,
        ),
        Alert(
            user_id=user.id, transaction_id=dismissed_flagged.id,
            alert_type=AlertType.NEW_MERCHANT, severity=AlertSeverity.LOW,
            message="First charge from this merchant.",
            evidence={}, status=AlertStatus.DISMISSED,
        ),
    ])
    sync_db.commit()

    return {
        "confident_flagged": confident_flagged,
        "unconfident_clean": unconfident_clean,
        "dismissed_flagged": dismissed_flagged,
    }


class TestFlaggedFilter:
    async def test_flagged_returns_transactions_with_open_alerts(
        self, client: AsyncClient, auth_headers: dict, flagged_data: dict
    ) -> None:
        response = await client.get(
            "/api/transactions", headers=auth_headers, params={"flagged": "true"}
        )
        assert response.status_code == 200

        ids = {item["id"] for item in response.json()["items"]}
        assert str(flagged_data["confident_flagged"].id) in ids

    async def test_a_confidently_categorized_alert_is_still_returned(
        self, client: AsyncClient, auth_headers: dict, flagged_data: dict
    ) -> None:
        """The precise regression: high confidence must not hide an alert."""
        response = await client.get(
            "/api/transactions", headers=auth_headers, params={"flagged": "true"}
        )
        rows = {item["id"]: item for item in response.json()["items"]}
        row = rows[str(flagged_data["confident_flagged"].id)]

        assert row["needs_review"] is False
        assert row["confidence"] == 1.0
        # Under the old ?review=needs_review link this row was unreachable.

    async def test_flagged_excludes_rows_whose_only_alert_is_dismissed(
        self, client: AsyncClient, auth_headers: dict, flagged_data: dict
    ) -> None:
        response = await client.get(
            "/api/transactions", headers=auth_headers, params={"flagged": "true"}
        )
        ids = {item["id"] for item in response.json()["items"]}
        assert str(flagged_data["dismissed_flagged"].id) not in ids

    async def test_flagged_excludes_unalerted_review_rows(
        self, client: AsyncClient, auth_headers: dict, flagged_data: dict
    ) -> None:
        response = await client.get(
            "/api/transactions", headers=auth_headers, params={"flagged": "true"}
        )
        ids = {item["id"] for item in response.json()["items"]}
        assert str(flagged_data["unconfident_clean"].id) not in ids

    async def test_the_two_filters_select_different_rows(
        self, client: AsyncClient, auth_headers: dict, flagged_data: dict
    ) -> None:
        """The claim the fix rests on, asserted directly."""
        flagged = await client.get(
            "/api/transactions", headers=auth_headers, params={"flagged": "true"}
        )
        review = await client.get(
            "/api/transactions", headers=auth_headers, params={"review": "needs_review"}
        )

        flagged_ids = {item["id"] for item in flagged.json()["items"]}
        review_ids = {item["id"] for item in review.json()["items"]}

        assert str(flagged_data["confident_flagged"].id) in flagged_ids
        assert str(flagged_data["confident_flagged"].id) not in review_ids
        assert str(flagged_data["unconfident_clean"].id) in review_ids
        assert str(flagged_data["unconfident_clean"].id) not in flagged_ids

    async def test_omitting_flagged_returns_everything(
        self, client: AsyncClient, auth_headers: dict, flagged_data: dict
    ) -> None:
        both = await client.get("/api/transactions", headers=auth_headers)
        ids = {item["id"] for item in both.json()["items"]}

        assert str(flagged_data["confident_flagged"].id) in ids
        assert str(flagged_data["unconfident_clean"].id) in ids
        assert str(flagged_data["dismissed_flagged"].id) in ids

    async def test_flagged_composes_with_other_filters(
        self, client: AsyncClient, auth_headers: dict, flagged_data: dict
    ) -> None:
        """Combining filters must narrow, not replace."""
        response = await client.get(
            "/api/transactions",
            headers=auth_headers,
            params={"flagged": "true", "search": "nothing-matches-this"},
        )
        assert response.json()["total"] == 0

    async def test_the_total_reflects_the_filter(
        self, client: AsyncClient, auth_headers: dict, flagged_data: dict
    ) -> None:
        flagged = await client.get(
            "/api/transactions", headers=auth_headers, params={"flagged": "true"}
        )
        everything = await client.get("/api/transactions", headers=auth_headers)

        assert 0 < flagged.json()["total"] < everything.json()["total"]


class TestFlaggedIsolation:
    async def test_another_users_alert_never_flags_your_transaction(
        self, client: AsyncClient, other_headers: dict, flagged_data: dict
    ) -> None:
        """The alerted rows belong to the first user; the second must see none."""
        response = await client.get(
            "/api/transactions", headers=other_headers, params={"flagged": "true"}
        )
        assert response.status_code == 200
        assert response.json()["total"] == 0


class TestFlaggedFacet:
    async def test_facets_report_a_flagged_count(
        self, client: AsyncClient, auth_headers: dict, flagged_data: dict
    ) -> None:
        response = await client.get("/api/transactions/facets", headers=auth_headers)
        body = response.json()

        assert body["flagged_count"] >= 1

    async def test_the_flagged_count_matches_the_filtered_total(
        self, client: AsyncClient, auth_headers: dict, flagged_data: dict
    ) -> None:
        facets = await client.get("/api/transactions/facets", headers=auth_headers)
        listing = await client.get(
            "/api/transactions", headers=auth_headers, params={"flagged": "true"}
        )

        assert facets.json()["flagged_count"] == listing.json()["total"]

    async def test_the_flagged_count_ignores_the_review_queue(
        self, client: AsyncClient, auth_headers: dict, sync_db: Session,
        demo_data: dict, flagged_data: dict,
    ) -> None:
        """Adding review-queue rows must not move the flagged count.

        Asserting the two counts merely *differ* would be a coincidence test —
        they can be equal while measuring different things. Perturbing one and
        watching the other hold still is the property that actually matters.
        """
        before = (
            await client.get("/api/transactions/facets", headers=auth_headers)
        ).json()

        for offset in range(3):
            make_transaction(
                sync_db, demo_data["user"], demo_data["account"],
                posted=date(2026, 7, 20 + offset), cents=-1_500 - offset,
                description=f"NOVACORP SUPPLY {offset}",
                merchant=f"Novacorp Supply {offset}",
                category_slug=None, index=400 + offset,
            )
        sync_db.commit()

        after = (
            await client.get("/api/transactions/facets", headers=auth_headers)
        ).json()

        assert after["review_count"] == before["review_count"] + 3
        assert after["flagged_count"] == before["flagged_count"], (
            "an unalerted low-confidence row must never count as flagged"
        )
