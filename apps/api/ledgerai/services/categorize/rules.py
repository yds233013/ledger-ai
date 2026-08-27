"""Deterministic categorizer.

This is the default engine and it requires no API key, no network and no
model. Stages run in order and the first confident hit wins:

  1. Correction memory  (1.00) — the user already told us about this merchant
  2. Merchant rules     (0.90) — seeded brand patterns
  3. Keyword heuristics (0.65) — words in the description itself
  4. Structural         (0.50) — amount sign / transfer shape
  5. Uncategorized      (0.00) — goes to the review queue

Phase 2 inserts the LLM categorizer between 3 and 4; nothing above it changes.
"""

from __future__ import annotations

from decimal import Decimal

from .base import (
    CategorizationContext,
    CategorySource,
    CategorySuggestion,
    TransactionCandidate,
)

UNCATEGORIZED = "uncategorized"

# Stage 3: words that appear inside the description rather than as a brand.
KEYWORD_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("payroll", "direct dep", "salary", "paycheck", "dir dep"), "income"),
    (("interest paid", "dividend", "tax refund", "irs treas"), "income"),
    (("overdraft", "nsf fee", "service charge", "maintenance fee", "atm fee",
      "late fee", "annual fee", "finance charge", "interest charge",
      "foreign transaction fee"), "fees"),
    (("transfer", "zelle", "venmo", "cash app", "autopay", "card payment",
      "payment thank you", "wire"), "transfers"),
    (("rent", "mortgage", "hoa dues", "property mgmt", "storage"), "housing"),
    (("electric", "gas company", "water", "sewer", "internet", "wireless",
      "cable", "utility", "broadband"), "utilities"),
    (("pharmacy", "dental", "medical", "clinic", "hospital", "urgent care",
      "physical therapy", "optometry", "fitness", "gym"), "health"),
    (("airlines", "airline", "hotel", "resort", "motel", "hostel", "baggage"), "travel"),
    (("coffee", "cafe", "restaurant", "bakery", "pizzeria", "tavern", "bistro",
      "grill", "diner", "deli", "brewery", "brewing"), "dining"),
    (("market", "grocery", "supermarket", "foods"), "groceries"),
    (("gas station", "fuel", "parking", "toll", "transit", "taxi", "rideshare"), "transport"),
    (("cinema", "theatre", "theater", "tickets", "games", "gaming"), "entertainment"),
]

CONFIDENCE_CORRECTION = Decimal("1.00")
CONFIDENCE_RULE = Decimal("0.90")
CONFIDENCE_KEYWORD = Decimal("0.65")
CONFIDENCE_STRUCTURAL = Decimal("0.50")
CONFIDENCE_NONE = Decimal("0.00")


class RuleCategorizer:
    """Deterministic, offline, and fully unit-testable."""

    name = "rules"

    def categorize(
        self, candidate: TransactionCandidate, context: CategorizationContext
    ) -> CategorySuggestion:
        key = candidate.merchant_key

        # 1. Correction memory — a user edit is the strongest possible signal.
        learned = context.correction_memory.get(key)
        if learned:
            return CategorySuggestion(
                category_slug=learned,
                confidence=CONFIDENCE_CORRECTION,
                source=CategorySource.CORRECTION,
                matched_on=f"you previously categorized “{candidate.merchant}”",
            )

        # 2. Seeded merchant patterns (longest first — "apple store" over "apple").
        haystack = f"{key} {candidate.normalized_description}"
        for pattern, slug in context.merchant_rules:
            if pattern in haystack:
                return CategorySuggestion(
                    category_slug=slug,
                    confidence=CONFIDENCE_RULE,
                    source=CategorySource.RULE,
                    matched_on=f"merchant pattern “{pattern}”",
                )

        # 3. Description keywords.
        for words, slug in KEYWORD_HINTS:
            hit = next((w for w in words if w in haystack), None)
            if hit:
                return CategorySuggestion(
                    category_slug=slug,
                    confidence=CONFIDENCE_KEYWORD,
                    source=CategorySource.KEYWORD,
                    matched_on=f"keyword “{hit}”",
                )

        # 4. Structural: money in that isn't an identified transfer is income.
        if candidate.amount_cents > 0:
            return CategorySuggestion(
                category_slug="income",
                confidence=CONFIDENCE_STRUCTURAL,
                source=CategorySource.HEURISTIC,
                matched_on="positive amount with no matching merchant rule",
            )

        # 5. Nothing matched — the review queue exists for exactly this case.
        return CategorySuggestion(
            category_slug=UNCATEGORIZED,
            confidence=CONFIDENCE_NONE,
            source=CategorySource.NONE,
            matched_on=None,
        )


def build_merchant_rule_index(rules: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Sort patterns longest-first so the most specific pattern wins."""
    return sorted(rules, key=lambda item: (-len(item[0]), item[0]))
