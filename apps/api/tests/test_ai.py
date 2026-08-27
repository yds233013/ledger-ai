"""Optional AI: fallback on every failure mode, and the privacy contract.

Every test injects a fake client. The suite never constructs a real OpenAI
client, never needs a key, and never touches the network — asserted below.
"""

from __future__ import annotations

import inspect
import pathlib
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from ledgerai.models import NarratorKind, PlannerKind
from ledgerai.services.ai import client as client_module
from ledgerai.services.ai.categorizer_llm import LLMCategorizer
from ledgerai.services.ai.client import (
    AiError,
    AiRateLimitError,
    AiTimeoutError,
    AiUnavailableError,
    get_ai_client,
    reset_ai_client,
)
from ledgerai.services.ai.narrator_llm import narrate_with_model
from ledgerai.services.ai.planner_llm import LLMPlanner
from ledgerai.services.analysis.executor import ComparisonResult, ExecutionResult, GroupedRow
from ledgerai.services.analysis.plan import AnalysisPlan, DateRange, Intent
from ledgerai.services.analysis.planner_rules import UserVocabulary
from ledgerai.services.categorize.base import CategorizationContext, TransactionCandidate
from ledgerai.services.categorize.rules import UNCATEGORIZED

TODAY = date(2026, 8, 26)


class FakeClient:
    """Records what it was sent and returns whatever the test dictates."""

    name = "fake"

    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self, *, schema: dict, schema_name: str, system: str, user: str
    ) -> dict:
        self.calls.append(
            {"schema": schema, "schema_name": schema_name, "system": system, "user": user}
        )
        if self._error is not None:
            raise self._error
        return self._response or {}


@pytest.fixture
def vocab() -> UserVocabulary:
    return UserVocabulary(
        category_slugs={"groceries": "Groceries", "dining": "Dining & Restaurants"},
        merchants=["Whole Foods MKT", "Sweetgreen"],
        base_currency="USD",
    )


@pytest.fixture(autouse=True)
def _clear_client():
    reset_ai_client(None)
    yield
    reset_ai_client(None)


def result_fixture() -> ExecutionResult:
    result = ExecutionResult(total_cents=48273, transaction_count=12, metric_label="Total")
    result.rows = [GroupedRow("Whole Foods MKT", 28100, 6)]
    result.comparison = ComparisonResult(48273, 51200, "last month", "June 2026")
    return result


def plan_fixture() -> AnalysisPlan:
    return AnalysisPlan(
        intent=Intent.TOTAL,
        date_range=DateRange(start=date(2026, 7, 1), end=date(2026, 7, 31), label="last month"),
    )


VALID_PLAN_PAYLOAD = {
    "intent": "breakdown",
    "direction": "spend",
    "currency": "USD",
    "date_range": {"start": "2026-07-01", "end": "2026-07-31", "label": "last month"},
    "compare_to": None,
    "filters": {
        "category_slugs": ["groceries"],
        "merchants": [],
        "account_ids": [],
        "text_query": None,
        "min_amount_cents": None,
        "max_amount_cents": None,
        "exclude_transfers": True,
    },
    "group_by": "category",
    "metric": "sum",
    "sort": "value_desc",
    "limit": 25,
    "chart_hint": "bar",
}


class TestNoKeyMeansNoAi:
    def test_get_ai_client_returns_none_without_a_key(self, monkeypatch) -> None:
        from ledgerai.config import settings

        monkeypatch.setattr(settings, "ai_enabled", False)
        monkeypatch.setattr(settings, "openai_api_key", "")
        reset_ai_client(None)
        assert get_ai_client() is None

    def test_flag_without_a_key_is_still_off(self, monkeypatch) -> None:
        from ledgerai.config import settings

        monkeypatch.setattr(settings, "ai_enabled", True)
        monkeypatch.setattr(settings, "openai_api_key", "   ")
        reset_ai_client(None)
        assert settings.ai_available is False
        assert get_ai_client() is None

    def test_no_test_module_constructs_a_real_client(self) -> None:
        """A guard against a test quietly acquiring a network dependency.

        Scans sibling test modules rather than this one, whose assertions
        necessarily mention the class by name.
        """
        tests_dir = pathlib.Path(__file__).parent
        offenders = [
            path.name
            for path in tests_dir.glob("test_*.py")
            if path.name != pathlib.Path(__file__).name
            and "OpenAiClient(" in path.read_text()
        ]
        assert offenders == []


class TestPlannerFallback:
    @pytest.mark.parametrize(
        "error",
        [
            AiTimeoutError("timed out"),
            AiRateLimitError("rate limited"),
            AiUnavailableError("breaker open"),
            AiError("network exploded"),
        ],
    )
    def test_every_failure_mode_falls_back_to_rules(
        self, error: Exception, vocab: UserVocabulary
    ) -> None:
        planner = LLMPlanner(FakeClient(error=error))
        plan, _, kind, reason = planner.plan("How much on groceries last month?", vocab, TODAY)

        assert kind == PlannerKind.RULES
        assert reason is not None
        assert plan.filters.category_slugs == ["groceries"]

    def test_malformed_payload_falls_back(self, vocab: UserVocabulary) -> None:
        planner = LLMPlanner(FakeClient(response={"intent": "not-a-real-intent"}))
        _, _, kind, reason = planner.plan("spending last month", vocab, TODAY)
        assert kind == PlannerKind.RULES
        assert "failed validation" in (reason or "")

    def test_schema_violating_plan_falls_back(self, vocab: UserVocabulary) -> None:
        bad = {**VALID_PLAN_PAYLOAD, "limit": 99999}
        _, _, kind, reason = LLMPlanner(FakeClient(response=bad)).plan("x", vocab, TODAY)
        assert kind == PlannerKind.RULES
        assert reason is not None

    def test_hallucinated_field_is_rejected(self, vocab: UserVocabulary) -> None:
        bad = {**VALID_PLAN_PAYLOAD, "drop_table": "transactions"}
        _, _, kind, _ = LLMPlanner(FakeClient(response=bad)).plan("x", vocab, TODAY)
        assert kind == PlannerKind.RULES

    def test_valid_plan_is_used(self, vocab: UserVocabulary) -> None:
        plan, explanation, kind, reason = LLMPlanner(
            FakeClient(response=dict(VALID_PLAN_PAYLOAD))
        ).plan("break down groceries", vocab, TODAY)

        assert kind == PlannerKind.LLM
        assert reason is None
        assert plan.intent == Intent.BREAKDOWN
        assert any("language model" in note for note in explanation.assumptions)

    def test_model_cannot_choose_the_currency(self, vocab: UserVocabulary) -> None:
        """Mixing currencies is not a decision available to anyone, model
        included: the resolved base currency always wins."""
        payload = {**VALID_PLAN_PAYLOAD, "currency": "EUR"}
        plan, _, kind, _ = LLMPlanner(FakeClient(response=payload)).plan("x", vocab, TODAY)
        assert kind == PlannerKind.LLM
        assert plan.currency == "USD"


class TestPlannerPrivacy:
    def test_only_names_and_the_question_are_sent(self, vocab: UserVocabulary) -> None:
        fake = FakeClient(response=dict(VALID_PLAN_PAYLOAD))
        LLMPlanner(fake).plan("How much on groceries last month?", vocab, TODAY)

        sent = fake.calls[0]["user"]
        assert "groceries" in sent
        assert "Whole Foods MKT" in sent
        # No amounts, no account ids, no transaction dates.
        for forbidden in ("amount_cents", "account_id", "dedupe_hash", "raw_description"):
            assert forbidden not in sent


class TestNarratorGuard:
    def test_fabricated_number_is_discarded(self) -> None:
        """The core guarantee: a figure the computation never produced never
        reaches the user, even when a model writes it."""
        fake = FakeClient(
            response={
                "explanation": "You spent $482.73, and also saved $1,204.99 overall."
            }
        )
        narration, narrator, verification = narrate_with_model(
            fake, plan_fixture(), result_fixture(), {}
        )
        assert narrator == NarratorKind.TEMPLATE
        assert "1,204.99" not in narration
        assert "not in the computed result" in verification["fallback_reason"]

    def test_faithful_narration_is_kept(self) -> None:
        fake = FakeClient(
            response={"explanation": "Your spending was $482.73 across 12 transactions."}
        )
        narration, narrator, verification = narrate_with_model(
            fake, plan_fixture(), result_fixture(), {}
        )
        assert narrator == NarratorKind.LLM
        assert verification["passed"] is True
        assert "482.73" in narration

    @pytest.mark.parametrize(
        "error", [AiTimeoutError("t"), AiRateLimitError("r"), AiError("boom")]
    )
    def test_failures_fall_back_to_the_template(self, error: Exception) -> None:
        _, narrator, verification = narrate_with_model(
            FakeClient(error=error), plan_fixture(), result_fixture(), {}
        )
        assert narrator == NarratorKind.TEMPLATE
        assert "failed" in verification["fallback_reason"]

    def test_empty_response_falls_back(self) -> None:
        _, narrator, _ = narrate_with_model(
            FakeClient(response={"explanation": "   "}), plan_fixture(), result_fixture(), {}
        )
        assert narrator == NarratorKind.TEMPLATE

    def test_narrator_receives_only_computed_output(self) -> None:
        fake = FakeClient(response={"explanation": "Your spending was $482.73."})
        narrate_with_model(fake, plan_fixture(), result_fixture(), {})
        sent = fake.calls[0]["user"]
        for forbidden in ("raw_description", "dedupe_hash", "account_id", "raw_text"):
            assert forbidden not in sent


class TestCategorizerFallback:
    @staticmethod
    def candidate(merchant: str = "Zorblax Quantum Widgets") -> TransactionCandidate:
        return TransactionCandidate(
            merchant=merchant,
            merchant_key=merchant.lower(),
            normalized_description=merchant.lower(),
            amount_cents=-4200,
            posted_date=TODAY,
        )

    def test_deterministic_stages_win_before_the_model_is_asked(self) -> None:
        fake = FakeClient(response={"category_slug": "travel"})
        categorizer = LLMCategorizer(fake, ["groceries", "travel"])
        context = CategorizationContext(
            correction_memory={"whole foods mkt": "groceries"}, merchant_rules=[]
        )
        suggestion = categorizer.categorize(self.candidate("Whole Foods MKT"), context)

        assert suggestion.category_slug == "groceries"
        assert fake.calls == []  # the model was never consulted

    def test_model_result_is_used_for_an_unknown_merchant(self) -> None:
        fake = FakeClient(response={"category_slug": "shopping"})
        categorizer = LLMCategorizer(fake, ["shopping", "groceries"])
        suggestion = categorizer.categorize(
            self.candidate(), CategorizationContext(correction_memory={}, merchant_rules=[])
        )
        assert suggestion.category_slug == "shopping"
        assert suggestion.confidence == Decimal("0.80")

    def test_unknown_slug_from_the_model_is_ignored(self) -> None:
        fake = FakeClient(response={"category_slug": "crypto-moonshots"})
        categorizer = LLMCategorizer(fake, ["shopping"])
        suggestion = categorizer.categorize(
            self.candidate(), CategorizationContext(correction_memory={}, merchant_rules=[])
        )
        assert suggestion.category_slug == UNCATEGORIZED

    def test_failure_leaves_the_merchant_for_review(self) -> None:
        categorizer = LLMCategorizer(FakeClient(error=AiTimeoutError("t")), ["shopping"])
        suggestion = categorizer.categorize(
            self.candidate(), CategorizationContext(correction_memory={}, merchant_rules=[])
        )
        assert suggestion.category_slug == UNCATEGORIZED
        assert suggestion.needs_review is True

    def test_a_merchant_is_only_ever_sent_once(self) -> None:
        fake = FakeClient(response={"category_slug": "shopping"})
        categorizer = LLMCategorizer(fake, ["shopping"])
        context = CategorizationContext(correction_memory={}, merchant_rules=[])
        for _ in range(4):
            categorizer.categorize(self.candidate(), context)
        assert len(fake.calls) == 1

    def test_only_the_merchant_name_is_sent(self) -> None:
        fake = FakeClient(response={"category_slug": "shopping"})
        LLMCategorizer(fake, ["shopping"]).categorize(
            self.candidate(), CategorizationContext(correction_memory={}, merchant_rules=[])
        )
        sent = fake.calls[0]["user"]
        assert "Zorblax" in sent
        assert "4200" not in sent
        assert "2026-08-26" not in sent


class TestCircuitBreaker:
    def test_opens_after_repeated_failures(self) -> None:
        breaker = client_module._CircuitBreaker()
        for _ in range(client_module.BREAKER_THRESHOLD):
            breaker.record_failure()
        with pytest.raises(AiUnavailableError):
            breaker.check()

    def test_success_resets_it(self) -> None:
        breaker = client_module._CircuitBreaker()
        breaker.record_failure()
        breaker.record_success()
        breaker.check()  # must not raise


class TestReceiptDataNeverLeaves:
    def test_no_ai_module_imports_receipt_or_ocr_code(self) -> None:
        """Stored OCR text and receipt images must be unreachable from the AI
        package — the same structural guarantee as the executor test."""
        import ledgerai.services.ai.categorizer_llm as categorizer
        import ledgerai.services.ai.narrator_llm as narrator
        import ledgerai.services.ai.planner_llm as planner

        for module in (client_module, planner, narrator, categorizer):
            source = inspect.getsource(module)
            for forbidden in ("services.ocr", "from ..ocr", "Receipt", "raw_text"):
                assert forbidden not in source, f"{module.__name__} references {forbidden}"
