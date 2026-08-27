"""Privacy guarantees that must hold structurally, not by convention."""

from __future__ import annotations

import inspect
from datetime import date

from ledgerai.services.analysis import executor, narrate, planner_rules
from ledgerai.services.categorize.base import TransactionCandidate
from ledgerai.services.categorize.llm import redact_for_model

FORBIDDEN_FIELDS = {
    "amount_cents",
    "posted_date",
    "normalized_description",
    "raw_description",
    "account_id",
    "dedupe_hash",
}


def test_outbound_model_payload_contains_only_the_merchant_name() -> None:
    """The single projection allowed to leave the system for categorization."""
    payload = redact_for_model(
        TransactionCandidate(
            merchant="Blue Bottle Coffee",
            merchant_key="blue bottle coffee",
            normalized_description="blue bottle coffee austin tx",
            amount_cents=-725,
            posted_date=date(2026, 3, 1),
        )
    )
    assert payload == {"merchant": "Blue Bottle Coffee"}
    assert not FORBIDDEN_FIELDS & payload.keys()
    assert "725" not in str(payload)


def test_executor_never_imports_an_ai_client() -> None:
    """The module that computes every number must not be able to call a model."""
    source = inspect.getsource(executor)
    for token in ("openai", "OpenAI", "anthropic", "requests", "httpx"):
        assert token not in source, f"{token} must not be reachable from the executor"


def test_deterministic_planner_makes_no_network_calls() -> None:
    source = inspect.getsource(planner_rules)
    for token in ("openai", "requests", "httpx", "urllib"):
        assert token not in source


def test_narrator_does_not_query_the_database() -> None:
    """Narration must be written from the computed result only."""
    source = inspect.getsource(narrate)
    for token in ("session", "select(", "execute("):
        assert token not in source
