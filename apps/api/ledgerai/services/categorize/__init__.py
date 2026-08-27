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


def build_categorizer(allowed_slugs: list[str] | None = None) -> Categorizer:
    """Select the active engine.

    With no API key this always returns the deterministic categorizer, which is
    the only engine Phase 1 ever had and remains the default. When AI is
    configured, the LLM categorizer *wraps* the rules engine rather than
    replacing it: correction memory and seeded merchant patterns still win, and
    the model only ever sees merchants nothing else could place.
    """
    if not settings.ai_available:
        return RuleCategorizer()

    from ..ai import get_ai_client

    client = get_ai_client()
    if client is None:
        return RuleCategorizer()

    from ..ai.categorizer_llm import build_llm_categorizer

    return build_llm_categorizer(client, allowed_slugs or [])


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
