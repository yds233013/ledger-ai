"""Categorization engines."""

from ...config import settings
from .base import (
    REVIEW_CONFIDENCE_THRESHOLD,
    CategorizationContext,
    Categorizer,
    CategorySource,
    CategorySuggestion,
    TransactionCandidate,
)
from .rules import UNCATEGORIZED, RuleCategorizer, build_merchant_rule_index


def build_categorizer() -> Categorizer:
    """Select the active engine.

    Phase 1 always returns the deterministic categorizer. The AI branch is
    intentionally explicit so the Phase 2 change is one line and the Phase 1
    behaviour is obvious to a reader.
    """
    if settings.ai_available:
        # Phase 2: return LLMCategorizer() wrapped so it falls back to rules
        # on any error, rate limit or schema mismatch.
        return RuleCategorizer()
    return RuleCategorizer()


__all__ = [
    "REVIEW_CONFIDENCE_THRESHOLD",
    "UNCATEGORIZED",
    "CategorizationContext",
    "CategorySource",
    "CategorySuggestion",
    "Categorizer",
    "RuleCategorizer",
    "TransactionCandidate",
    "build_categorizer",
    "build_merchant_rule_index",
]
