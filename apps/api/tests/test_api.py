"""HTTP-level integration tests against a real Postgres test database."""

from __future__ import annotations

import json
from datetime import date

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_login_succeeds_and_returns_a_usable_token(client: AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login", json={"email": "user@test.local", "password": "test-password"}
    )
    assert response.status_code == 200
    token = response.json()["access_token"]

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["email"] == "user@test.local"


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("user@test.local", "wrong-password"),
        ("nobody@test.local", "test-password"),
    ],
)
async def test_bad_credentials_give_identical_responses(
    client: AsyncClient, email: str, password: str
) -> None:
    """No account enumeration: unknown user and wrong password look the same."""
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password"


@pytest.mark.parametrize(
    "path",
    [
        "/api/dashboard",
        "/api/transactions",
        "/api/transactions/facets",
        "/api/uploads",
        "/api/settings/profile",
        "/api/analysis/capabilities",
    ],
)
async def test_endpoints_require_authentication(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401


async def test_garbage_token_is_rejected(client: AsyncClient) -> None:
    response = await client.get(
        "/api/dashboard", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


# --- dashboard --------------------------------------------------------------


async def test_dashboard_excludes_transfers_from_spending(
    client: AsyncClient, auth_headers: dict
) -> None:
    """July: 100.00 groceries + 55.00 dining, plus a 500.00 transfer.
    Spending must be 155.00, not 655.00."""
    response = await client.get("/api/dashboard", headers=auth_headers)
    data = response.json()
    assert data["period_label"] == "July 2026"
    assert data["total_spend"] == 155.00
    assert data["total_income"] == 3000.00


async def test_dashboard_month_over_month(client: AsyncClient, auth_headers: dict) -> None:
    data = (await client.get("/api/dashboard", headers=auth_headers)).json()
    assert data["previous_spend"] == 80.00      # June groceries
    assert data["delta_cents"] == 7500          # 155.00 - 80.00
    assert data["delta_direction"] == "up"


async def test_dashboard_is_scoped_to_the_caller(
    client: AsyncClient, other_headers: dict
) -> None:
    """The other user sees only their own two July rows."""
    data = (await client.get("/api/dashboard", headers=other_headers)).json()
    assert data["total_spend"] == 1032.99   # 999.99 groceries + 33.00 dining
    assert data["transaction_count"] == 2


# --- transactions -----------------------------------------------------------


async def test_transaction_list_is_scoped(client: AsyncClient, auth_headers: dict) -> None:
    # 8 USD rows plus the one EUR row the currency tests rely on.
    data = (await client.get("/api/transactions", headers=auth_headers)).json()
    assert data["total"] == 9
    assert all("OTHER USER SECRET" not in item["raw_description"] for item in data["items"])


async def test_other_user_sees_only_their_own_row(
    client: AsyncClient, other_headers: dict
) -> None:
    data = (await client.get("/api/transactions", headers=other_headers)).json()
    assert data["total"] == 2
    assert {item["merchant"] for item in data["items"]} == {"Other Secret", "Sweetgreen"}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("category_slug=groceries", 3),
        ("category_slug=dining", 3),
        ("search=whole", 2),
        ("start_date=2026-07-01", 8),
        ("min_amount=100", 2),
        ("review=needs_review", 0),
    ],
)
async def test_transaction_filters(
    client: AsyncClient, auth_headers: dict, query: str, expected: int
) -> None:
    data = (await client.get(f"/api/transactions?{query}", headers=auth_headers)).json()
    assert data["total"] == expected


async def test_transaction_pagination(client: AsyncClient, auth_headers: dict) -> None:
    page = (await client.get("/api/transactions?limit=2&offset=0", headers=auth_headers)).json()
    assert len(page["items"]) == 2
    assert page["has_more"] is True

    last = (await client.get("/api/transactions?limit=2&offset=8", headers=auth_headers)).json()
    assert last["has_more"] is False


async def test_correction_updates_and_records_history(
    client: AsyncClient, auth_headers: dict
) -> None:
    listing = (
        await client.get("/api/transactions?category_slug=dining", headers=auth_headers)
    ).json()
    transaction_id = listing["items"][0]["id"]

    facets = (await client.get("/api/transactions/facets", headers=auth_headers)).json()
    groceries = next(c["id"] for c in facets["categories"] if c["slug"] == "groceries")

    response = await client.patch(
        f"/api/transactions/{transaction_id}",
        headers=auth_headers,
        json={"category_id": groceries, "apply_to_matching": False},
    )
    assert response.status_code == 200
    body = response.json()["transaction"]
    assert body["category"]["slug"] == "groceries"
    assert body["is_corrected"] is True
    assert body["confidence"] == 1.0
    assert body["needs_review"] is False


async def test_correction_requires_a_field(client: AsyncClient, auth_headers: dict) -> None:
    listing = (await client.get("/api/transactions?limit=1", headers=auth_headers)).json()
    response = await client.patch(
        f"/api/transactions/{listing['items'][0]['id']}", headers=auth_headers, json={}
    )
    assert response.status_code == 422


async def test_cannot_correct_another_users_transaction(
    client: AsyncClient, auth_headers: dict, other_headers: dict
) -> None:
    """The response must be 404, not 403 — never confirm the row exists."""
    listing = (await client.get("/api/transactions?limit=1", headers=auth_headers)).json()
    facets = (await client.get("/api/transactions/facets", headers=other_headers)).json()
    category = facets["categories"][0]["id"]

    response = await client.patch(
        f"/api/transactions/{listing['items'][0]['id']}",
        headers=other_headers,
        json={"category_id": category},
    )
    assert response.status_code == 404


# --- retroactive corrections -----------------------------------------------


async def sweetgreen_rows(client: AsyncClient, headers: dict) -> list[dict]:
    listing = (
        await client.get("/api/transactions?merchant=Sweetgreen&limit=50", headers=headers)
    ).json()
    return listing["items"]


async def category_id(client: AsyncClient, headers: dict, slug: str) -> str:
    facets = (await client.get("/api/transactions/facets", headers=headers)).json()
    return next(c["id"] for c in facets["categories"] if c["slug"] == slug)


async def test_impact_preview_counts_siblings_without_changing_anything(
    client: AsyncClient, auth_headers: dict
) -> None:
    rows = await sweetgreen_rows(client, auth_headers)
    assert len(rows) == 3
    travel = await category_id(client, auth_headers, "travel")

    impact = (
        await client.get(
            f"/api/transactions/{rows[0]['id']}/correction-impact?category_id={travel}",
            headers=auth_headers,
        )
    ).json()

    assert impact["merchant"] == "Sweetgreen"
    assert impact["matching_count"] == 2
    assert impact["affected_count"] == 2
    assert impact["protected_count"] == 0
    assert len(impact["affected_ids"]) == 2

    # A preview must not write anything.
    after = await sweetgreen_rows(client, auth_headers)
    assert all(row["category"]["slug"] == "dining" for row in after)


async def test_impact_preview_requires_a_field(
    client: AsyncClient, auth_headers: dict
) -> None:
    rows = await sweetgreen_rows(client, auth_headers)
    response = await client.get(
        f"/api/transactions/{rows[0]['id']}/correction-impact", headers=auth_headers
    )
    assert response.status_code == 422


async def test_individual_correction_leaves_siblings_alone(
    client: AsyncClient, auth_headers: dict
) -> None:
    rows = await sweetgreen_rows(client, auth_headers)
    travel = await category_id(client, auth_headers, "travel")

    response = await client.patch(
        f"/api/transactions/{rows[0]['id']}",
        headers=auth_headers,
        json={"category_id": travel, "apply_to_matching": False},
    )
    body = response.json()
    assert body["applied_to_matching"] is False
    assert body["impact"]["affected_count"] == 0

    after = {
        row["id"]: row["category"]["slug"]
        for row in await sweetgreen_rows(client, auth_headers)
    }
    assert after[rows[0]["id"]] == "travel"
    assert after[rows[1]["id"]] == "dining"
    assert after[rows[2]["id"]] == "dining"


async def test_bulk_correction_updates_every_matching_transaction(
    client: AsyncClient, auth_headers: dict
) -> None:
    rows = await sweetgreen_rows(client, auth_headers)
    travel = await category_id(client, auth_headers, "travel")

    response = await client.patch(
        f"/api/transactions/{rows[0]['id']}",
        headers=auth_headers,
        json={"category_id": travel, "apply_to_matching": True},
    )
    body = response.json()
    assert body["applied_to_matching"] is True
    assert body["impact"]["affected_count"] == 2

    after = await sweetgreen_rows(client, auth_headers)
    assert all(row["category"]["slug"] == "travel" for row in after)
    assert all(row["is_corrected"] is True for row in after)
    assert all(row["confidence"] == 1.0 for row in after)


async def test_bulk_correction_never_overwrites_an_individual_one(
    client: AsyncClient, auth_headers: dict
) -> None:
    """The headline protection rule: a deliberate one-off edit survives a later
    "apply to all matching"."""
    rows = await sweetgreen_rows(client, auth_headers)
    travel = await category_id(client, auth_headers, "travel")
    shopping = await category_id(client, auth_headers, "shopping")

    # The user deliberately sets one row to Travel, on its own.
    await client.patch(
        f"/api/transactions/{rows[1]['id']}",
        headers=auth_headers,
        json={"category_id": travel, "apply_to_matching": False},
    )

    # The preview must already report it as protected.
    impact = (
        await client.get(
            f"/api/transactions/{rows[0]['id']}/correction-impact?category_id={shopping}",
            headers=auth_headers,
        )
    ).json()
    assert impact["matching_count"] == 2
    assert impact["protected_count"] == 1
    assert impact["affected_count"] == 1

    # And the bulk change must honour it.
    body = (
        await client.patch(
            f"/api/transactions/{rows[0]['id']}",
            headers=auth_headers,
            json={"category_id": shopping, "apply_to_matching": True},
        )
    ).json()
    assert body["impact"]["protected_count"] == 1

    after = {
        row["id"]: row["category"]["slug"]
        for row in await sweetgreen_rows(client, auth_headers)
    }
    assert after[rows[0]["id"]] == "shopping"
    assert after[rows[1]["id"]] == "travel"    # protected
    assert after[rows[2]["id"]] == "shopping"


async def test_bulk_correction_cannot_reach_another_users_rows(
    client: AsyncClient, auth_headers: dict, other_headers: dict
) -> None:
    """Both users have a Sweetgreen transaction with an identical merchant key."""
    mine = await sweetgreen_rows(client, auth_headers)
    theirs_before = await sweetgreen_rows(client, other_headers)
    assert len(theirs_before) == 1
    assert theirs_before[0]["category"]["slug"] == "dining"

    travel = await category_id(client, auth_headers, "travel")
    body = (
        await client.patch(
            f"/api/transactions/{mine[0]['id']}",
            headers=auth_headers,
            json={"category_id": travel, "apply_to_matching": True},
        )
    ).json()
    # Only my own two siblings, never the other user's row.
    assert body["impact"]["affected_count"] == 2

    theirs_after = await sweetgreen_rows(client, other_headers)
    assert theirs_after[0]["category"]["slug"] == "dining"
    assert theirs_after[0]["is_corrected"] is False


async def test_impact_preview_is_scoped_to_the_caller(
    client: AsyncClient, auth_headers: dict, other_headers: dict
) -> None:
    mine = await sweetgreen_rows(client, auth_headers)
    travel = await category_id(client, other_headers, "travel")
    response = await client.get(
        f"/api/transactions/{mine[0]['id']}/correction-impact?category_id={travel}",
        headers=other_headers,
    )
    assert response.status_code == 404


async def test_bulk_correction_teaches_future_imports(
    client: AsyncClient, auth_headers: dict, sync_db
) -> None:
    """Requirement 5: the correction is saved as a rule, so a later upload of the
    same merchant is categorized without the user intervening again."""
    from ledgerai.services.categorize import RuleCategorizer, TransactionCandidate
    from ledgerai.services.ingest import build_context
    from ledgerai.services.normalize import merchant_key as make_key

    rows = await sweetgreen_rows(client, auth_headers)
    travel = await category_id(client, auth_headers, "travel")
    await client.patch(
        f"/api/transactions/{rows[0]['id']}",
        headers=auth_headers,
        json={"category_id": travel, "apply_to_matching": True},
    )

    from sqlalchemy import select

    from ledgerai.models import User

    user = sync_db.execute(
        select(User).where(User.email == "user@test.local")
    ).scalar_one()
    context = build_context(sync_db, user.id)

    suggestion = RuleCategorizer().categorize(
        TransactionCandidate(
            merchant="Sweetgreen",
            merchant_key=make_key("Sweetgreen"),
            normalized_description="sweetgreen",
            amount_cents=-1500,
            posted_date=date(2026, 9, 1),
        ),
        context,
    )
    assert suggestion.category_slug == "travel"
    assert suggestion.source == "correction"


async def test_bulk_merchant_rename_moves_the_key(
    client: AsyncClient, auth_headers: dict
) -> None:
    rows = await sweetgreen_rows(client, auth_headers)

    body = (
        await client.patch(
            f"/api/transactions/{rows[0]['id']}",
            headers=auth_headers,
            json={"merchant": "Sweetgreen Salads", "apply_to_matching": True},
        )
    ).json()
    assert body["impact"]["affected_count"] == 2

    renamed = (
        await client.get("/api/transactions?merchant=Sweetgreen%20Salads", headers=auth_headers)
    ).json()
    assert renamed["total"] == 3
    # The old name no longer matches anything of this user's.
    old = (await client.get("/api/transactions?merchant=Sweetgreen", headers=auth_headers)).json()
    assert old["total"] == 0


# --- uploads ----------------------------------------------------------------


async def test_upload_rejects_a_csv_without_an_amount_column(
    client: AsyncClient, auth_headers: dict
) -> None:
    response = await client.post(
        "/api/uploads",
        headers=auth_headers,
        files={"file": ("bad.csv", b"Date,Description\n2026-01-01,COFFEE\n", "text/csv")},
    )
    assert response.status_code == 422
    assert "amount" in response.json()["detail"].lower()


async def test_upload_rejects_an_empty_file(client: AsyncClient, auth_headers: dict) -> None:
    response = await client.post(
        "/api/uploads", headers=auth_headers, files={"file": ("empty.csv", b"", "text/csv")}
    )
    assert response.status_code == 422


async def test_upload_rejects_an_unsupported_type(
    client: AsyncClient, auth_headers: dict
) -> None:
    response = await client.post(
        "/api/uploads",
        headers=auth_headers,
        files={"file": ("payload.exe", b"MZ\x90\x00" + b"\x00" * 64, "application/exe")},
    )
    assert response.status_code == 422


async def test_missing_upload_job_is_404(client: AsyncClient, auth_headers: dict) -> None:
    response = await client.get(
        "/api/uploads/00000000-0000-0000-0000-000000000000/job", headers=auth_headers
    )
    assert response.status_code == 404


# --- Ask Ledger -------------------------------------------------------------


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = None
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: ") and name:
            events.append((name, json.loads(line[6:])))
    return events


async def test_capabilities_report_no_ai_configured(
    client: AsyncClient, auth_headers: dict
) -> None:
    data = (await client.get("/api/analysis/capabilities", headers=auth_headers)).json()
    assert data["ai_enabled"] is False
    assert data["planner"] == "rules"
    assert "no language model" in data["disclosure"].lower()
    assert len(data["suggested_questions"]) >= 4


async def test_ask_streams_all_five_steps(client: AsyncClient, auth_headers: dict) -> None:
    response = await client.post(
        "/api/analysis/runs",
        headers=auth_headers,
        json={
            "question": (
                "How much did I spend on groceries last month "
                "compared to the month before?"
            ),
            "use_cache": False,
        },
    )
    assert response.status_code == 200
    events = parse_sse(response.text)

    completed = [
        data["step"] for name, data in events if name == "step" and data["status"] == "completed"
    ]
    assert completed == ["understand", "select", "aggregate", "visualize", "explain"]


async def test_ask_computes_the_correct_comparison(
    client: AsyncClient, auth_headers: dict
) -> None:
    """July groceries 100.00 vs June groceries 80.00 — checked by hand."""
    response = await client.post(
        "/api/analysis/runs",
        headers=auth_headers,
        json={
            "question": (
                "How much did I spend on groceries last month "
                "compared to the month before?"
            ),
            "use_cache": False,
        },
    )
    result = next(data for name, data in parse_sse(response.text) if name == "result")
    comparison = result["result"]["comparison"]
    assert comparison["current"] == 100.00
    assert comparison["previous"] == 80.00
    assert comparison["delta"] == 20.00
    assert comparison["direction"] == "up"


async def test_every_number_in_the_answer_was_computed(
    client: AsyncClient, auth_headers: dict
) -> None:
    response = await client.post(
        "/api/analysis/runs",
        headers=auth_headers,
        json={
            "question": "Break down my spending by category for last month",
            "use_cache": False,
        },
    )
    events = parse_sse(response.text)
    explain = next(
        data for name, data in events
        if name == "step" and data["step"] == "explain" and data["status"] == "completed"
    )
    verification = explain["payload"]["numeric_verification"]
    assert verification["checked"] is True
    assert verification["passed"] is True
    assert verification["unverified_numbers"] == []


async def test_aggregate_step_exposes_its_sql(client: AsyncClient, auth_headers: dict) -> None:
    """Inspectability: the user can read the query that produced the number."""
    response = await client.post(
        "/api/analysis/runs",
        headers=auth_headers,
        json={
            "question": "Break down my spending by category for last month",
            "use_cache": False,
        },
    )
    aggregate = next(
        data for name, data in parse_sse(response.text)
        if name == "step" and data["step"] == "aggregate" and data["status"] == "completed"
    )
    assert "SELECT" in aggregate["payload"]["sql"].upper()
    assert aggregate["payload"]["supporting_transactions"]


async def test_analysis_never_leaks_another_users_data(
    client: AsyncClient, other_headers: dict
) -> None:
    """The other user's only row is 999.99; the first user's data must not
    appear in their totals."""
    response = await client.post(
        "/api/analysis/runs",
        headers=other_headers,
        json={"question": "How much did I spend last month?", "use_cache": False},
    )
    result = next(data for name, data in parse_sse(response.text) if name == "result")
    assert result["result"]["total"] == 1032.99


async def test_advice_questions_are_declined(client: AsyncClient, auth_headers: dict) -> None:
    response = await client.post(
        "/api/analysis/runs",
        headers=auth_headers,
        json={"question": "Should I invest my savings in index funds?", "use_cache": False},
    )
    result = next(data for name, data in parse_sse(response.text) if name == "result")
    assert result["declined"] is True
    assert "financial advice" in result["narration"]


async def test_cached_run_replays_identical_steps(
    client: AsyncClient, auth_headers: dict
) -> None:
    payload = {"question": "What are my top merchants this year?", "use_cache": True}

    async def ask() -> list[tuple[str, dict]]:
        response = await client.post("/api/analysis/runs", headers=auth_headers, json=payload)
        return parse_sse(response.text)

    first = await ask()
    second = await ask()

    assert next(d for n, d in first if n == "run")["cached"] is False
    assert next(d for n, d in second if n == "run")["cached"] is True

    first_result = next(d for n, d in first if n == "result")
    second_result = next(d for n, d in second if n == "result")
    assert first_result["result"] == second_result["result"]
    assert first_result["narration"] == second_result["narration"]


async def test_correction_invalidates_the_cache(
    client: AsyncClient, auth_headers: dict
) -> None:
    """An edit must never leave a stale number cached."""
    payload = {"question": "Break down my spending by category for last month", "use_cache": True}
    await client.post("/api/analysis/runs", headers=auth_headers, json=payload)

    cached = parse_sse(
        (await client.post("/api/analysis/runs", headers=auth_headers, json=payload)).text
    )
    assert next(d for n, d in cached if n == "run")["cached"] is True

    listing = (
        await client.get("/api/transactions?category_slug=dining", headers=auth_headers)
    ).json()
    facets = (await client.get("/api/transactions/facets", headers=auth_headers)).json()
    travel = next(c["id"] for c in facets["categories"] if c["slug"] == "travel")
    await client.patch(
        f"/api/transactions/{listing['items'][0]['id']}",
        headers=auth_headers,
        json={"category_id": travel},
    )

    after = parse_sse(
        (await client.post("/api/analysis/runs", headers=auth_headers, json=payload)).text
    )
    assert next(d for n, d in after if n == "run")["cached"] is False


async def test_empty_result_is_handled_gracefully(
    client: AsyncClient, auth_headers: dict
) -> None:
    response = await client.post(
        "/api/analysis/runs",
        headers=auth_headers,
        json={"question": "How much did I spend on travel in 2019?", "use_cache": False},
    )
    result = next(data for name, data in parse_sse(response.text) if name == "result")
    assert result["result"]["total"] == 0
    assert "No spending" in result["narration"]
    assert result["chart"]["kind"] == "none"


async def test_run_history_and_replay(client: AsyncClient, auth_headers: dict) -> None:
    await client.post(
        "/api/analysis/runs",
        headers=auth_headers,
        json={
            "question": "Break down my spending by category for last month",
            "use_cache": False,
        },
    )
    history = (await client.get("/api/analysis/runs", headers=auth_headers)).json()
    assert history

    detail = (
        await client.get(f"/api/analysis/runs/{history[0]['id']}", headers=auth_headers)
    ).json()
    assert len(detail["steps"]) == 10  # started + completed for each of five steps
    assert detail["chart"] is not None


async def test_cannot_read_another_users_analysis(
    client: AsyncClient, auth_headers: dict, other_headers: dict
) -> None:
    await client.post(
        "/api/analysis/runs",
        headers=auth_headers,
        json={
            "question": "Break down my spending by category for last month",
            "use_cache": False,
        },
    )
    history = (await client.get("/api/analysis/runs", headers=auth_headers)).json()
    response = await client.get(f"/api/analysis/runs/{history[0]['id']}", headers=other_headers)
    assert response.status_code == 404


# --- settings ---------------------------------------------------------------


async def test_profile_discloses_what_is_actually_available(
    client: AsyncClient, auth_headers: dict
) -> None:
    data = (await client.get("/api/settings/profile", headers=auth_headers)).json()
    assert data["ai_enabled"] is False
    assert "No language model is configured" in data["ai_disclosure"]

    features = {f["key"]: f for f in data["features"]}
    assert features["csv_upload"]["available"] is True
    assert features["ask_ledger"]["available"] is True
    assert features["receipt_ocr"]["available"] is True
    assert features["alerts"]["available"] is True

    # Still honest about what is not built.
    assert features["currency_conversion"]["available"] is False
    assert "never added to your base-currency totals" in features["currency_conversion"]["note"]
    assert features["export"]["available"] is False
    assert "Phase 3" in features["export"]["note"]
    assert features["connected_accounts"]["available"] is False
    assert "does not connect to any real" in features["connected_accounts"]["note"]
