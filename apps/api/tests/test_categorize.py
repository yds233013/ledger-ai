"""The deterministic categorizer — the default engine, no API key involved."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ledgerai.services.categorize import (
    REVIEW_CONFIDENCE_THRESHOLD,
    CategorizationContext,
    CategorySource,
    RuleCategorizer,
    TransactionCandidate,
    build_categorizer,
    build_merchant_rule_index,
)
from ledgerai.services.ingest import load_merchant_rule_definitions
from ledgerai.services.normalize import extract_merchant, merchant_key, normalize_description


@pytest.fixture
def context() -> CategorizationContext:
    return CategorizationContext(
        correction_memory={},
        merchant_rules=build_merchant_rule_index(load_merchant_rule_definitions()),
    )


def candidate(description: str, cents: int = -1000) -> TransactionCandidate:
    merchant = extract_merchant(description)
    return TransactionCandidate(
        merchant=merchant,
        merchant_key=merchant_key(merchant),
        normalized_description=normalize_description(description),
        amount_cents=cents,
        posted_date=date(2026, 3, 1),
    )


@pytest.mark.parametrize(
    ("description", "slug"),
    [
        ("WHOLE FOODS MKT 10233 AUSTIN TX", "groceries"),
        ("SQ *BLUE BOTTLE COFFEE #4821", "dining"),
        ("RECURRING PAYMENT NETFLIX.COM", "subscriptions"),
        ("UBER *TRIP HELP.UBER.COM", "transport"),
        ("SHELL OIL 57445123456 AUSTIN TX", "transport"),
        ("AMAZON.COM*MK4XY9Z11", "shopping"),
        ("MONTHLY MAINTENANCE FEE", "fees"),
        ("VENMO PAYMENT TO ALEX", "transfers"),
        ("DIRECT DEP ACME ROBOTICS PAYROLL", "income"),
    ],
)
def test_known_merchants_are_categorized(
    description: str, slug: str, context: CategorizationContext
) -> None:
    suggestion = RuleCategorizer().categorize(candidate(description), context)
    assert suggestion.category_slug == slug
    assert not suggestion.needs_review


def test_unknown_merchant_goes_to_review_queue(context: CategorizationContext) -> None:
    suggestion = RuleCategorizer().categorize(candidate("ZORBLAX QUANTUM WIDGETS LLC"), context)
    assert suggestion.category_slug == "uncategorized"
    assert suggestion.confidence == Decimal("0.00")
    assert suggestion.needs_review


def test_correction_memory_outranks_every_rule(context: CategorizationContext) -> None:
    """A user edit must beat the seeded pattern for the same merchant."""
    plain = RuleCategorizer().categorize(candidate("WHOLE FOODS MKT AUSTIN TX"), context)
    assert plain.category_slug == "groceries"

    context.correction_memory["whole foods mkt"] = "shopping"
    learned = RuleCategorizer().categorize(candidate("WHOLE FOODS MKT AUSTIN TX"), context)
    assert learned.category_slug == "shopping"
    assert learned.source == CategorySource.CORRECTION
    assert learned.confidence == Decimal("1.00")


def test_specific_pattern_beats_generic_one(context: CategorizationContext) -> None:
    """Patterns are indexed longest-first, so "apple store" wins over "apple"."""
    patterns = [pattern for pattern, _ in context.merchant_rules]
    lengths = [len(pattern) for pattern in patterns]
    assert lengths == sorted(lengths, reverse=True)


def test_unmatched_credit_is_treated_as_income(context: CategorizationContext) -> None:
    suggestion = RuleCategorizer().categorize(candidate("MYSTERY CREDIT 998", cents=2500), context)
    assert suggestion.category_slug == "income"
    assert suggestion.source == CategorySource.HEURISTIC
    # A structural guess is not confident enough to skip review.
    assert suggestion.needs_review


def test_review_threshold_is_the_documented_boundary() -> None:
    assert REVIEW_CONFIDENCE_THRESHOLD == Decimal("0.60")


def test_default_engine_needs_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 1 must be fully functional with AI disabled."""
    from ledgerai.config import settings

    monkeypatch.setattr(settings, "ai_enabled", False)
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert isinstance(build_categorizer(), RuleCategorizer)


def test_llm_categorizer_cannot_be_constructed_in_phase_1() -> None:
    from ledgerai.services.categorize.llm import LLMCategorizer

    with pytest.raises(NotImplementedError):
        LLMCategorizer()
