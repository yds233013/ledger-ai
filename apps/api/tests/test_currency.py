"""Mixed-currency correctness.

Ledger AI does not convert between currencies, so the rule is enforced by
restriction and disclosure: aggregates cover one currency, and anything left
out is named rather than silently dropped.

The fixture user holds a EUR charge alongside their USD ones.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from ledgerai.models import Transaction, User

pytestmark = pytest.mark.asyncio

EUR_CENTS = 7000  # the fixture's single EUR charge, €70.00


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = None
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: ") and name:
            events.append((name, json.loads(line[6:])))
    return events


class TestDashboardCurrency:
    async def test_totals_cover_only_the_base_currency(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """July USD spending is 155.00. The €70 charge must not be added in."""
        data = (await client.get("/api/dashboard", headers=auth_headers)).json()
        assert data["base_currency"] == "USD"
        assert data["total_spend"] == 155.00

    async def test_excluded_currencies_are_reported(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        data = (await client.get("/api/dashboard", headers=auth_headers)).json()
        assert data["excluded_currencies"] == {"EUR": 1}
        assert data["currency_note"] is not None
        assert "EUR" in data["currency_note"]
        assert "does not convert" in data["currency_note"]

    async def test_no_combined_total_is_claimed(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """155.00 + 70.00 = 225.00 must never appear as a total."""
        data = (await client.get("/api/dashboard", headers=auth_headers)).json()
        assert data["total_spend_cents"] != 15500 + EUR_CENTS

    async def test_category_breakdown_excludes_other_currencies(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        data = (await client.get("/api/dashboard", headers=auth_headers)).json()
        shopping = [row for row in data["by_category"] if row["slug"] == "shopping"]
        # The only Shopping charge in the window is the EUR one.
        assert shopping == []

    async def test_a_user_with_one_currency_gets_no_note(
        self, client: AsyncClient, other_headers: dict
    ) -> None:
        data = (await client.get("/api/dashboard", headers=other_headers)).json()
        assert data["excluded_currencies"] == {}
        assert data["currency_note"] is None


class TestAskLedgerCurrency:
    async def test_plan_records_the_currency_it_restricted_to(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.post(
            "/api/analysis/runs",
            headers=auth_headers,
            json={"question": "How much did I spend last month?", "use_cache": False},
        )
        result = next(data for name, data in parse_sse(response.text) if name == "result")
        assert result["plan"]["currency"] == "USD"

    async def test_total_excludes_other_currencies(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.post(
            "/api/analysis/runs",
            headers=auth_headers,
            json={"question": "How much did I spend last month?", "use_cache": False},
        )
        result = next(data for name, data in parse_sse(response.text) if name == "result")
        assert result["result"]["total"] == 155.00

    async def test_the_answer_discloses_what_it_left_out(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.post(
            "/api/analysis/runs",
            headers=auth_headers,
            json={"question": "How much did I spend last month?", "use_cache": False},
        )
        result = next(data for name, data in parse_sse(response.text) if name == "result")
        caveats = " ".join(result["caveats"])
        assert "EUR" in caveats
        assert "does not convert" in caveats

    async def test_understanding_step_states_the_currency(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.post(
            "/api/analysis/runs",
            headers=auth_headers,
            json={"question": "How much did I spend last month?", "use_cache": False},
        )
        understand = next(
            data
            for name, data in parse_sse(response.text)
            if name == "step" and data["step"] == "understand" and data["status"] == "completed"
        )
        assert "USD" in understand["title"]

    async def test_supporting_rows_are_all_one_currency(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.post(
            "/api/analysis/runs",
            headers=auth_headers,
            json={"question": "Show me all my transactions last month", "use_cache": False},
        )
        result = next(data for name, data in parse_sse(response.text) if name == "result")
        for row in result["supporting_transactions"]:
            assert "EUR" not in row["description"]


class TestBaseCurrencyIsRespected:
    async def test_changing_the_base_currency_changes_the_scope(
        self, client: AsyncClient, auth_headers: dict, sync_db: Session, demo_data: dict
    ) -> None:
        """Switching the user to EUR must report the EUR charge and exclude USD,
        never sum the two."""
        user = sync_db.execute(
            select(User).where(User.id == demo_data["user"].id)
        ).scalar_one()
        user.base_currency = "EUR"
        sync_db.commit()

        data = (await client.get("/api/dashboard", headers=auth_headers)).json()
        assert data["base_currency"] == "EUR"
        assert data["total_spend_cents"] == EUR_CENTS
        assert set(data["excluded_currencies"]) == {"USD"}

    async def test_transactions_carry_their_own_currency(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        currencies = {
            row.currency
            for row in sync_db.execute(
                select(Transaction).where(Transaction.user_id == demo_data["user"].id)
            ).scalars()
        }
        assert currencies == {"USD", "EUR"}
