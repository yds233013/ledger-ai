"""Normalization: the layer everything else depends on being stable."""

from __future__ import annotations

from datetime import date

import pytest

from ledgerai.services.normalize import (
    compute_dedupe_hash,
    extract_merchant,
    merchant_key,
    normalize_description,
    parse_amount_to_cents,
    parse_date,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("POS DEBIT SQ *BLUE BOTTLE COFFEE #4821 SAN FRANCISCO CA", "Blue Bottle Coffee"),
        ("PURCHASE AUTHORIZED ON 03/14 WHOLE FOODS MKT 10233 AUSTIN TX", "Whole Foods MKT"),
        ("RECURRING PAYMENT NETFLIX.COM  REF#8837261", "Netflix.com"),
        ("TST* SWEETGREEN - MISSION", "Sweetgreen"),
        ("AMAZON.COM*MK4XY9Z11 AMZN.COM/BILL WA", "Amazon.com"),
        ("CHECKCARD TRADER JOES #182 NEW YORK NY", "Trader Joes"),
        ("UBER   *TRIP HELP.UBER.COM", "Uber"),
        ("NETFLIX.COM [SYNTHETIC]", "Netflix.com"),
    ],
)
def test_extract_merchant(raw: str, expected: str) -> None:
    assert extract_merchant(raw) == expected


def test_state_code_anchoring_keeps_merchant_words() -> None:
    """A blind trailing-word strip would reduce this to "Whole"."""
    assert extract_merchant("WHOLE FOODS MKT 10233 AUSTIN TX") == "Whole Foods MKT"


def test_merchant_key_is_stable_across_noise() -> None:
    a = extract_merchant("POS DEBIT SQ *BLUE BOTTLE COFFEE #4821 AUSTIN TX")
    b = extract_merchant("SQ *BLUE BOTTLE COFFEE #9001 PORTLAND OR")
    assert merchant_key(a) == merchant_key(b)


@pytest.mark.parametrize(
    ("value", "cents"),
    [
        ("$1,234.56", 123456),
        ("(45.00)", -4500),
        ("-12.3", -1230),
        ("89.99CR", 8999),
        ("15.00DR", -1500),
        ("0.1", 10),
        ("  $2,000  ", 200000),
        ("−7.50", -750),  # unicode minus
        (25, 2500),
    ],
)
def test_parse_amount_to_cents(value, cents: int) -> None:
    assert parse_amount_to_cents(value) == cents


def test_amount_parsing_uses_decimal_not_float() -> None:
    """0.1 + 0.2 in float is 0.30000000000000004; cents must be exact."""
    assert parse_amount_to_cents("0.1") + parse_amount_to_cents("0.2") == 30


@pytest.mark.parametrize("bad", ["", "   ", "abc", "$", "-"])
def test_parse_amount_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_amount_to_cents(bad)


@pytest.mark.parametrize(
    "value", ["2024-03-14", "03/14/2024", "Mar 14, 2024", "14 Mar 2024", "20240314"]
)
def test_parse_date(value: str) -> None:
    assert parse_date(value) == date(2024, 3, 14)


def test_parse_date_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_date("not-a-date")


def test_dedupe_hash_is_deterministic_and_row_sensitive() -> None:
    args = ("user", "account", date(2026, 1, 1), -1500, "coffee")
    assert compute_dedupe_hash(*args, 0) == compute_dedupe_hash(*args, 0)
    # Two identical charges on the same day are different rows, not duplicates.
    assert compute_dedupe_hash(*args, 0) != compute_dedupe_hash(*args, 1)


def test_normalize_strips_reference_noise() -> None:
    assert normalize_description("RECURRING PAYMENT NETFLIX.COM REF#8837261") == "netflix.com"
