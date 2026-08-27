"""The Ask Ledger pipeline: plan contract, date resolution, and the guarantee
that no number reaches the user without having been computed."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from ledgerai.services.analysis.charts import build_chart
from ledgerai.services.analysis.dates import previous_period, resolve_date_phrase
from ledgerai.services.analysis.executor import ComparisonResult, ExecutionResult, GroupedRow
from ledgerai.services.analysis.narrate import (
    advice_response,
    build_narration,
    is_advice_request,
    verify_numeric_claims,
)
from ledgerai.services.analysis.plan import (
    AnalysisPlan,
    ChartHint,
    DateRange,
    Direction,
    Filters,
    GroupBy,
    Intent,
)
from ledgerai.services.analysis.planner_rules import RulePlanner, UserVocabulary

TODAY = date(2026, 8, 26)


@pytest.fixture
def vocab() -> UserVocabulary:
    return UserVocabulary(
        category_slugs={
            "groceries": "Groceries",
            "dining": "Dining & Restaurants",
            "transport": "Transport",
            "subscriptions": "Subscriptions",
            "income": "Income",
            "transfers": "Transfers & Payments",
            "fees": "Fees & Charges",
        },
        merchants=["Whole Foods MKT", "Blue Bottle Coffee", "Netflix.com", "Uber"],
    )


# --- the plan contract ----------------------------------------------------


def test_plan_rejects_unknown_fields() -> None:
    """A hallucinated field must fail loudly rather than be ignored."""
    with pytest.raises(ValidationError):
        AnalysisPlan(
            intent=Intent.TOTAL,
            date_range=DateRange(start=TODAY, end=TODAY, label="today"),
            drop_table="transactions",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"intent": Intent.COMPARISON},                       # no compare_to
        {"intent": Intent.BREAKDOWN},                        # no group_by
        {"intent": Intent.TREND, "group_by": GroupBy.MERCHANT},  # wrong grouping
        {"intent": Intent.TOTAL, "limit": 5000},             # limit out of range
    ],
)
def test_plan_rejects_incoherent_shapes(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        AnalysisPlan(date_range=DateRange(start=TODAY, end=TODAY, label="today"), **kwargs)


def test_plan_fingerprint_is_stable_and_content_sensitive() -> None:
    base = AnalysisPlan(
        intent=Intent.TOTAL, date_range=DateRange(start=TODAY, end=TODAY, label="today")
    )
    same = AnalysisPlan(
        intent=Intent.TOTAL, date_range=DateRange(start=TODAY, end=TODAY, label="today")
    )
    different = base.model_copy(update={"filters": Filters(category_slugs=["dining"])})
    assert base.fingerprint() == same.fingerprint()
    assert base.fingerprint() != different.fingerprint()


# --- date resolution -------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [
        ("last month", date(2026, 7, 1), date(2026, 7, 31)),
        ("this month", date(2026, 8, 1), date(2026, 8, 26)),
        ("last year", date(2025, 1, 1), date(2025, 12, 31)),
        ("Q2", date(2026, 4, 1), date(2026, 6, 30)),
        ("in March", date(2026, 3, 1), date(2026, 3, 31)),
        ("the last 90 days", date(2026, 5, 29), date(2026, 8, 26)),
    ],
)
def test_date_phrases(phrase: str, start: date, end: date) -> None:
    resolved = resolve_date_phrase(phrase, TODAY)
    assert (resolved.start, resolved.end) == (start, end)


def test_named_month_in_the_future_resolves_to_last_year() -> None:
    """Asked in August, "December" means last December."""
    assert resolve_date_phrase("December spending", TODAY).start == date(2025, 12, 1)


def test_missing_period_defaults_visibly() -> None:
    resolved = resolve_date_phrase("how much on coffee", TODAY)
    assert "no period specified" in resolved.label


def test_previous_period_of_a_month_is_the_previous_calendar_month() -> None:
    """A 31-day month must compare against a 30-day month, not 31 days back."""
    july = resolve_date_phrase("last month", TODAY)
    june = previous_period(july)
    assert (june.start, june.end) == (date(2026, 6, 1), date(2026, 6, 30))


# --- the deterministic planner ---------------------------------------------


def test_planner_works_without_any_api_key(vocab: UserVocabulary) -> None:
    plan, explanation = RulePlanner().plan(
        "How much did I spend on groceries last month compared to the month before?",
        vocab,
        TODAY,
    )
    assert plan.intent == Intent.COMPARISON
    assert plan.filters.category_slugs == ["groceries"]
    assert plan.compare_to is not None
    assert "Groceries" in explanation.matched_filters[0]


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("Break down my spending by category for July", Intent.BREAKDOWN),
        ("Show me my spending trend over the last 6 months", Intent.TREND),
        ("What are my top 5 merchants this year?", Intent.TOP_N),
        ("Show me all my Blue Bottle Coffee transactions", Intent.SEARCH),
        ("Which charges repeat every month?", Intent.RECURRING),
        ("How much did I spend in total this month?", Intent.TOTAL),
    ],
)
def test_intent_classification(question: str, intent: Intent, vocab: UserVocabulary) -> None:
    plan, _ = RulePlanner().plan(question, vocab, TODAY)
    assert plan.intent == intent


def test_trend_question_does_not_add_a_text_filter(vocab: UserVocabulary) -> None:
    """Deriving a description filter from a trend question would silently
    reduce the aggregate to rows containing the word "trend"."""
    plan, _ = RulePlanner().plan("Show me my spending trend over 6 months", vocab, TODAY)
    assert plan.filters.text_query is None


def test_merchant_match_beats_its_category(vocab: UserVocabulary) -> None:
    """"Blue Bottle Coffee" is a merchant filter, not the whole Dining category."""
    plan, _ = RulePlanner().plan("Show me all my Blue Bottle Coffee transactions", vocab, TODAY)
    assert plan.filters.merchants == ["Blue Bottle Coffee"]
    assert plan.filters.category_slugs == []


def test_transfers_excluded_by_default(vocab: UserVocabulary) -> None:
    plan, _ = RulePlanner().plan("how much did I spend last month", vocab, TODAY)
    assert plan.filters.exclude_transfers


def test_income_questions_flip_direction(vocab: UserVocabulary) -> None:
    plan, _ = RulePlanner().plan("How much income did I earn this year?", vocab, TODAY)
    assert plan.direction == Direction.INCOME


# --- narration and the numeric guard ---------------------------------------


def result_fixture() -> ExecutionResult:
    result = ExecutionResult(total_cents=48273, transaction_count=12, metric_label="Total")
    result.rows = [GroupedRow("Whole Foods MKT", 28100, 6), GroupedRow("Trader Joes", 20173, 6)]
    result.comparison = ComparisonResult(48273, 51200, "last month", "June 2026")
    return result


def test_narration_only_uses_computed_numbers() -> None:
    plan = AnalysisPlan(
        intent=Intent.TOTAL, date_range=DateRange(start=TODAY, end=TODAY, label="last month")
    )
    narration = build_narration(plan, result_fixture(), {"groceries": "Groceries"})
    verified, unverified = verify_numeric_claims(narration, result_fixture())
    assert verified, f"template narration produced unverifiable numbers: {unverified}"


def test_fabricated_number_is_caught() -> None:
    """This is the guard that makes the no-LLM-arithmetic claim structural."""
    text = "Your spending was $482.73 across 12 transactions, and you saved $1,204.99 overall."
    verified, unverified = verify_numeric_claims(text, result_fixture())
    assert not verified
    assert "1204.99" in unverified


def test_years_and_small_counts_are_not_treated_as_claims() -> None:
    verified, _ = verify_numeric_claims("During 2026 you had 12 transactions.", result_fixture())
    assert verified


def test_empty_result_narration_explains_itself() -> None:
    plan = AnalysisPlan(
        intent=Intent.TOTAL, date_range=DateRange(start=TODAY, end=TODAY, label="last month")
    )
    narration = build_narration(plan, ExecutionResult(), {})
    assert "No spending" in narration
    assert "last month" in narration


def test_singular_transaction_is_not_pluralized() -> None:
    plan = AnalysisPlan(
        intent=Intent.TOTAL, date_range=DateRange(start=TODAY, end=TODAY, label="last month")
    )
    single = ExecutionResult(total_cents=1000, transaction_count=1)
    assert "1 transaction." in build_narration(plan, single, {})


@pytest.mark.parametrize(
    "question",
    [
        "should I invest in index funds?",
        "what stocks should I buy",
        "is it worth cancelling netflix",
        "how should I pay off my debt",
    ],
)
def test_advice_requests_are_detected(question: str) -> None:
    assert is_advice_request(question)


@pytest.mark.parametrize(
    "question",
    ["how much did I spend on groceries?", "show me my top merchants", "spending by category"],
)
def test_analysis_questions_are_not_mistaken_for_advice(question: str) -> None:
    assert not is_advice_request(question)


def test_advice_response_declines_without_recommending() -> None:
    text = advice_response("should I invest?")
    assert "doesn't give financial advice" in text
    assert "$" not in text


# --- charts ----------------------------------------------------------------


def test_time_series_always_renders_as_a_line() -> None:
    plan = AnalysisPlan(
        intent=Intent.TREND,
        date_range=DateRange(start=date(2026, 1, 1), end=TODAY, label="2026"),
        group_by=GroupBy.MONTH,
        chart_hint=ChartHint.PIE,  # deliberately wrong hint
    )
    result = ExecutionResult(total_cents=1000, transaction_count=2)
    result.rows = [GroupedRow("2026-01", 500, 1), GroupedRow("2026-02", 500, 1)]
    assert build_chart(plan, result).kind == "line"


def test_pie_downgrades_to_bar_when_there_are_too_many_slices() -> None:
    plan = AnalysisPlan(
        intent=Intent.BREAKDOWN,
        date_range=DateRange(start=date(2026, 1, 1), end=TODAY, label="2026"),
        group_by=GroupBy.CATEGORY,
        chart_hint=ChartHint.PIE,
    )
    result = ExecutionResult(total_cents=1000, transaction_count=20)
    result.rows = [GroupedRow(f"cat-{i}", 100, 2) for i in range(12)]
    assert build_chart(plan, result).kind == "bar"


def test_no_rows_means_no_chart() -> None:
    plan = AnalysisPlan(
        intent=Intent.TOTAL, date_range=DateRange(start=TODAY, end=TODAY, label="today")
    )
    assert build_chart(plan, ExecutionResult()).kind == "none"


class TestRecurringWindow:
    """Repetition cannot be observed inside a 30-day default window."""

    def test_recurring_widens_when_no_period_is_named(self, vocab: UserVocabulary) -> None:
        from ledgerai.services.analysis.planner_rules import RECURRING_DEFAULT_MONTHS

        plan, explanation = RulePlanner().plan("Which charges repeat every month?", vocab, TODAY)
        span_days = (plan.date_range.end - plan.date_range.start).days

        assert plan.intent == Intent.RECURRING
        assert span_days > 150  # roughly six months, not thirty days
        assert f"last {RECURRING_DEFAULT_MONTHS} months" in plan.date_range.label
        assert any("repetition cannot be seen" in note for note in explanation.assumptions)

    def test_an_explicit_period_is_respected(self, vocab: UserVocabulary) -> None:
        plan, _ = RulePlanner().plan("Which charges repeat every month in 2025?", vocab, TODAY)
        assert plan.date_range.label == "2025"

    def test_other_intents_keep_the_thirty_day_default(self, vocab: UserVocabulary) -> None:
        plan, _ = RulePlanner().plan("How much do I spend on coffee?", vocab, TODAY)
        assert "no period specified" in plan.date_range.label


class TestGenericWordsAreNotCategories:
    """Ordinary English words must not be read as category names."""

    def test_charges_does_not_mean_the_fees_category(self, vocab: UserVocabulary) -> None:
        plan, _ = RulePlanner().plan("Which charges repeat every month?", vocab, TODAY)
        assert plan.filters.category_slugs == []

    def test_payments_does_not_mean_the_transfers_category(
        self, vocab: UserVocabulary
    ) -> None:
        plan, _ = RulePlanner().plan("Show me my largest payments this year", vocab, TODAY)
        assert "transfers" not in plan.filters.category_slugs

    def test_an_explicit_category_name_still_matches(self, vocab: UserVocabulary) -> None:
        plan, _ = RulePlanner().plan("How much did I pay in bank fees this year?", vocab, TODAY)
        assert plan.filters.category_slugs == ["fees"]
