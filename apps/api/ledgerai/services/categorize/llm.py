"""LLM categorizer — Phase 2.

Deliberately present in Phase 1 as a fixed interface so switching it on later
is a registry change, not a refactor. It is never selected while
`settings.ai_available` is False, which is the case with no API key.

Privacy contract for Phase 2, encoded here so it cannot drift:
  * Only `merchant` strings are sent upstream — never amounts, dates, account
    identifiers, descriptions, or any part of an uploaded file.
  * Results are cached in Redis keyed by merchant, so any given merchant is
    sent at most once, ever.
"""

from __future__ import annotations

from .base import (
    CategorizationContext,
    CategorySuggestion,
    TransactionCandidate,
)


class LLMCategorizer:
    name = "llm"

    def __init__(self) -> None:
        raise NotImplementedError(
            "The LLM categorizer ships in Phase 2. Phase 1 runs the deterministic "
            "RuleCategorizer, which requires no API key."
        )

    def categorize(  # pragma: no cover - Phase 2
        self, candidate: TransactionCandidate, context: CategorizationContext
    ) -> CategorySuggestion:
        raise NotImplementedError


def redact_for_model(candidate: TransactionCandidate) -> dict[str, str]:
    """The ONLY projection of a transaction that may leave the system.

    Tested in tests/test_privacy.py to guarantee no amount, date, account or
    description field can be added to the outbound payload by accident.
    """
    return {"merchant": candidate.merchant}
