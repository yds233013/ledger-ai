"""The privacy projection for merchant categorization.

The implementation lives in services/ai/categorizer_llm.py; this module holds
the one function that decides what may leave the system.

Privacy contract, encoded here so it cannot drift:
  * Only `merchant` strings are sent upstream — never amounts, dates, account
    identifiers, descriptions, or any part of an uploaded file.
  * Results are cached in Redis keyed by merchant, so any given merchant is
    sent at most once, ever.
"""

from __future__ import annotations

from .base import TransactionCandidate


def redact_for_model(candidate: TransactionCandidate) -> dict[str, str]:
    """The ONLY projection of a transaction that may leave the system.

    Tested in tests/test_privacy.py to guarantee no amount, date, account or
    description field can be added to the outbound payload by accident.
    """
    return {"merchant": candidate.merchant}
