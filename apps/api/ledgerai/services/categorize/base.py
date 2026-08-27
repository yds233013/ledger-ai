"""Categorizer interface shared by the deterministic and (Phase 2) LLM engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

# Below this, a transaction lands in the manual-review queue.
REVIEW_CONFIDENCE_THRESHOLD = Decimal("0.60")


class CategorySource:
    """Which stage produced the answer. Surfaced in the UI so a user can see
    *why* something was categorized the way it was."""

    CORRECTION = "correction"   # the user taught us this merchant
    RULE = "rule"               # seeded merchant pattern
    KEYWORD = "keyword"         # description keyword heuristic
    HEURISTIC = "heuristic"     # amount-sign / structural heuristic
    LLM = "llm"                 # Phase 2, only when a key is configured
    NONE = "none"               # fell through to Uncategorized


@dataclass(slots=True)
class TransactionCandidate:
    """The minimal transaction facts a categorizer is allowed to see.

    Deliberately narrow: this is also the exact payload shape the Phase 2 LLM
    categorizer may send upstream, and it contains no account identifiers, no
    balances and no raw file content.
    """

    merchant: str
    merchant_key: str
    normalized_description: str
    amount_cents: int
    posted_date: date


@dataclass(slots=True)
class CategorySuggestion:
    category_slug: str
    confidence: Decimal
    source: str
    matched_on: str | None = None

    @property
    def needs_review(self) -> bool:
        return self.confidence < REVIEW_CONFIDENCE_THRESHOLD


@dataclass(slots=True)
class CategorizationContext:
    """Per-job lookup tables, built once instead of per row."""

    # merchant_key -> category slug, learned from transaction_corrections.
    correction_memory: dict[str, str] = field(default_factory=dict)
    # (pattern, category_slug), longest pattern first so specific beats generic.
    merchant_rules: list[tuple[str, str]] = field(default_factory=list)


class Categorizer(Protocol):
    """Every categorizer takes one candidate and returns one suggestion."""

    name: str

    def categorize(
        self, candidate: TransactionCandidate, context: CategorizationContext
    ) -> CategorySuggestion: ...
