"""Turn raw bank-statement strings into stable, comparable values.

Bank descriptions are noisy: store numbers, POS prefixes, reference IDs, dates
baked into the string. Normalization strips that noise so the same merchant
produces the same key across statements — which is what makes categorization,
correction memory and duplicate detection work at all.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Payment-network and channel prefixes that carry no merchant information.
_PREFIXES = re.compile(
    r"^(?:"
    r"pos\s+(?:debit|purchase)?|debit\s+card\s+purchase|card\s+purchase|"
    r"purchase\s+authorized\s+on|recurring\s+payment|preauthorized|"
    r"ach\s+(?:debit|credit)|sq\s?\*|tst\*|py\s?\*|pp\s?\*|paypal\s?\*|"
    r"visa\s+purchase|checkcard|ext\s+trnsfr|web\s+id:?"
    r")\s*",
    re.IGNORECASE,
)

# Trailing noise: store #, ref numbers, dates, phone numbers, state codes.
_STORE_NUM = re.compile(r"\s*#\s*\d{2,}\b", re.IGNORECASE)
_REF_NUM = re.compile(r"\b(?:ref|auth|trace|id|inv)[#:\s-]*[a-z0-9]{4,}\b", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"\b\d{5,}\b")
_INLINE_DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b")
_PHONE = re.compile(r"\b\d{3}[- ]\d{3}[- ]\d{4}\b")
# US state codes, used to anchor "MERCHANT CITY ST" trailing geography.
_STATE_CODES = frozenset(
    "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo "
    "mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc".split()
)
# Tokens that begin a multi-word city name; if one is left dangling after the
# city token is dropped, it belongs to the city too.
_CITY_PREFIXES = frozenset(
    {"san", "santa", "new", "los", "las", "fort", "ft", "saint", "st", "lake", "north",
     "south", "east", "west", "port", "mount", "el", "la"}
)
# Short tokens that are genuinely acronyms despite containing a vowel.
_ACRONYMS = frozenset({"amc", "usa", "atm", "ups", "irs", "dmv", "nyc", "abc", "nbc", "cbs"})
_VOWELS = frozenset("aeiou")

# Machine-generated reference junk: mixes letters and digits, e.g. mk4xy9z11.
_ALNUM_JUNK = re.compile(r"^(?=[a-z0-9./*-]*\d)(?=[a-z0-9./*-]*[a-z])[a-z0-9./*-]{6,}$")
# Trailing bracketed annotations: "[SYNTHETIC]", "(recurring)", "[card 1234]".
_TRAILING_TAG = re.compile(r"\s*[\[(][^\])]*[\])]\s*$")
_WHITESPACE = re.compile(r"\s+")
_PUNCT_EDGES = re.compile(r"^[^\w]+|[^\w]+$")

_AMOUNT_CLEAN = re.compile(r"[^0-9.\-()]")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%Y%m%d",
)


def normalize_description(raw: str) -> str:
    """Lower-cased, noise-stripped form used for matching and hashing."""
    text = raw.strip().lower()
    # Prefixes can stack ("pos debit sq *coffee").
    for _ in range(3):
        stripped = _PREFIXES.sub("", text)
        if stripped == text:
            break
        text = stripped
    # Trailing bracketed annotations are metadata about the row, not part of
    # the description. Stripped here, before the punctuation trim would eat the
    # closing bracket and leave an orphaned opening one behind.
    for _ in range(3):
        stripped = _TRAILING_TAG.sub("", text).strip()
        if stripped == text or not stripped:
            break
        text = stripped
    text = _PHONE.sub(" ", text)
    text = _INLINE_DATE.sub(" ", text)
    text = _REF_NUM.sub(" ", text)
    text = _STORE_NUM.sub(" ", text)
    text = _LONG_DIGITS.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _PUNCT_EDGES.sub("", text)
    return text or raw.strip().lower()


def _titlecase_token(token: str) -> str:
    """Title-case a token, keeping real acronyms upper.

    A blanket "short tokens are acronyms" rule produces "Payment Thank YOU" and
    "Direct DEP". Vowel-less short tokens (mkt, cvs, llc) really are acronyms;
    the handful that aren't are listed explicitly.
    """
    if not token.isalpha() or len(token) > 4:
        return token.capitalize()
    if token in _ACRONYMS or not (_VOWELS & set(token)):
        return token.upper()
    return token.capitalize()


def _strip_trailing_geography(tokens: list[str]) -> list[str]:
    """Drop a trailing "CITY ST" suffix, anchored on a real US state code.

    Anchoring on the state code matters: a blind "drop trailing words" rule
    turns "whole foods mkt austin tx" into "whole", which then fails to match
    any merchant rule.
    """
    if len(tokens) < 3 or tokens[-1] not in _STATE_CODES:
        return tokens
    trimmed = tokens[:-1]          # drop the state code
    if len(trimmed) > 1:
        trimmed = trimmed[:-1]     # drop the city token
    # "san francisco" / "new york": the dangling prefix belongs to the city.
    if len(trimmed) > 1 and trimmed[-1] in _CITY_PREFIXES:
        trimmed = trimmed[:-1]
    return trimmed


def extract_merchant(raw: str) -> str:
    """Human-readable merchant name derived from the normalized description."""
    text = normalize_description(raw)

    # "amazon.com*mk4xy9z11" — the brand is left of the star.
    if "*" in text:
        left, _, right = text.partition("*")
        text = left.strip() if len(left.strip()) >= 3 else right.strip()

    # " - mission" / " @ terminal 4" are location detail, not the brand.
    for sep in (" - ", " @ ", " / "):
        if sep in text:
            text = text.split(sep)[0].strip()

    # "cvs/pharmacy" — the brand is the first segment.
    if "/" in text:
        head = text.split("/")[0].strip()
        if len(head) >= 3:
            text = head

    tokens = [t for t in text.split() if not _ALNUM_JUNK.match(t)]
    tokens = _strip_trailing_geography(tokens)
    if not tokens:
        tokens = text.split()

    words = [_titlecase_token(t) for t in tokens]
    merchant = " ".join(words).strip()
    return merchant[:200] or "Unknown Merchant"


def merchant_key(merchant: str) -> str:
    """Stable lookup key for correction memory and rule matching."""
    return _WHITESPACE.sub(" ", re.sub(r"[^a-z0-9 ]", " ", merchant.lower())).strip()


def parse_amount_to_cents(value: str | float | int | Decimal) -> int:
    """Parse a money string to integer cents.

    Handles $1,234.56, (12.34) accounting negatives, trailing CR/DR, and unicode
    minus. Uses Decimal throughout — float would introduce rounding error on
    values as ordinary as 0.1.
    """
    if isinstance(value, int):
        return value * 100
    if isinstance(value, Decimal):
        return int((value * 100).to_integral_value())
    if isinstance(value, float):
        return int((Decimal(str(value)) * 100).to_integral_value())

    text = str(value).strip()
    if not text:
        raise ValueError("Empty amount")

    text = text.replace("−", "-").replace(",", "")
    negative = False
    upper = text.upper()
    if upper.endswith("CR"):
        text = text[:-2].strip()
    elif upper.endswith("DR"):
        negative = True
        text = text[:-2].strip()

    cleaned = _AMOUNT_CLEAN.sub("", text)
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1]
    cleaned = cleaned.replace("(", "").replace(")", "")
    if not cleaned or cleaned in {"-", "."}:
        raise ValueError(f"Unparseable amount: {value!r}")

    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Unparseable amount: {value!r}") from exc

    cents = int((amount * 100).quantize(Decimal("1")))
    return -abs(cents) if negative else cents


def parse_date(value: str) -> date:
    text = str(value).strip()
    if not text:
        raise ValueError("Empty date")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007 - date-only value
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(f"Unrecognized date format: {value!r}") from exc


def compute_dedupe_hash(
    user_id: uuid.UUID | str,
    account_id: uuid.UUID | str,
    posted_date: date,
    amount_cents: int,
    normalized_description: str,
    source_row_index: int,
) -> str:
    """Row-level idempotency key.

    source_row_index is included deliberately: a statement can legitimately
    contain the same charge twice on the same day (two identical coffees), and
    those are different rows of the same file. Re-processing that file produces
    the same indices, so retries stay idempotent while genuine repeats survive.
    """
    parts = [
        str(user_id),
        str(account_id),
        posted_date.isoformat(),
        str(amount_cents),
        normalized_description,
        str(source_row_index),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def compute_content_hash(data: bytes) -> str:
    """File-level idempotency key."""
    return hashlib.sha256(data).hexdigest()
