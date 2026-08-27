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


def test_receipt_and_ocr_modules_never_log_sensitive_content(caplog) -> None:  # noqa: ANN001
    """Logs must not carry OCR text, merchant names, amounts or storage keys."""
    import logging
    import uuid as _uuid
    from datetime import date as _date

    from ledgerai.models import Receipt, ReceiptStatus
    from ledgerai.services.receipts import receipt_log_context

    receipt = Receipt(
        id=_uuid.uuid4(),
        user_id=_uuid.uuid4(),
        upload_id=_uuid.uuid4(),
        status=ReceiptStatus.NEEDS_REVIEW,
        page_count=1,
        raw_text="SANDBOX GROCERS\nTOTAL 30.36",
        merchant="Sandbox Grocers",
        posted_date=_date(2026, 8, 14),
        total_cents=3036,
        currency="USD",
    )

    context = receipt_log_context(receipt)
    rendered = str(context)
    for secret in ("Sandbox Grocers", "30.36", "3036", "SANDBOX GROCERS", "2026-08-14"):
        assert secret not in rendered

    with caplog.at_level(logging.INFO):
        logging.getLogger("ledgerai.services.receipts").info("Receipt processed %s", context)
    for record in caplog.records:
        message = record.getMessage()
        for secret in ("Sandbox Grocers", "30.36", "SANDBOX GROCERS"):
            assert secret not in message


def test_ocr_modules_do_not_log_page_content() -> None:
    """A logging call that interpolates OCR text would leak a whole receipt."""
    from ledgerai.jobs import process_upload
    from ledgerai.services.ocr import engine, parse, preprocess

    for module in (engine, parse, preprocess, process_upload):
        source = inspect.getsource(module)
        for line in source.splitlines():
            if "logger." not in line:
                continue
            for forbidden in ("raw_text", "result.text", "parsed.merchant", "total_cents",
                              "storage_key", "upload.storage_key"):
                assert forbidden not in line, f"{module.__name__} logs {forbidden}"


def test_narrator_does_not_query_the_database() -> None:
    """Narration must be written from the computed result only."""
    source = inspect.getsource(narrate)
    for token in ("session", "select(", "execute("):
        assert token not in source
