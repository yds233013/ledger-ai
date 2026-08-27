"""Extract structured fields from OCR'd receipt text.

Every rule here exists because a real Tesseract run produced the failure it
guards against:

  * A naive ``TOTAL\\s+([\\d.]+)`` matches *inside* ``SUBTOTAL`` and returns the
    wrong number, so money labels are matched line-anchored and the more
    specific labels are tried first.
  * Tesseract emits ``4,99`` for ``4.99``, so a comma is accepted as a decimal
    separator.
  * ``TAX 8.25%   2.31`` defeats a trailing-anchored pattern, so the *last*
    money-shaped token on the line wins and percentage tokens are skipped.

Confidence is per field: the mean OCR confidence of the words that actually
produced the value, adjusted by an arithmetic consistency check. Nothing here
is a guess about the document as a whole.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from ..normalize import parse_date
from .engine import OcrResult, OcrWord

# A money token: 1234.56 / 1,234.56 / 4,99 / 12.34. Requires a decimal part so
# quantities and order numbers are not mistaken for amounts.
_MONEY = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})*|\d+)[.,](\d{2})(?![\d])")
_PERCENT = re.compile(r"\d+[.,]?\d*\s*%")

# Ordered most-specific-first. The first label to match a line claims it, so
# SUBTOTAL can never be read as TOTAL.
MONEY_LABELS: list[tuple[str, tuple[str, ...]]] = [
    ("subtotal", ("subtotal", "sub total", "sub-total", "net total", "merchandise")),
    ("tip", ("tip", "gratuity", "service charge")),
    ("tax", ("tax", "vat", "gst", "hst", "sales tax")),
    ("total", ("grand total", "total due", "amount due", "balance due", "total")),
]

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
CURRENCY_CODES = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD"}

DATE_PATTERNS = [
    r"\b(\d{4}-\d{2}-\d{2})\b",
    r"\b(\d{1,2}/\d{1,2}/\d{4})\b",
    r"\b(\d{1,2}/\d{1,2}/\d{2})\b",
    r"\b(\d{1,2}-\d{1,2}-\d{4})\b",
    r"\b([A-Z][a-z]{2}\s+\d{1,2},?\s+\d{4})\b",
    r"\b(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})\b",
]

# Lines that are never a merchant name.
_MERCHANT_NOISE = re.compile(
    r"(receipt|invoice|order|terminal|register|cashier|thank you|welcome|"
    r"customer copy|merchant copy|www\.|http|@|\btel\b|phone|synthetic|demo|"
    r"not a real|^\W*$|^\d)",
    re.IGNORECASE,
)
_ADDRESSY = re.compile(
    r"\b(\d+\s+\w+\s+(st|street|ave|avenue|rd|road|blvd|way|lane|ln|dr|drive)"
    r"|suite|ste\.?|apt\.?|\b[A-Z]{2}\s+\d{5}\b)",
    re.IGNORECASE,
)

# Below this a field is not trusted and the receipt goes to manual review.
FIELD_REVIEW_THRESHOLD = 0.75
# Only the fields that become the transaction gate review. Currency is always
# reported with its confidence, but an *assumed* USD is a disclosure, not a
# reason to make the user re-check an otherwise clean receipt.
REVIEW_GATING_FIELDS = ("merchant", "posted_date", "subtotal", "tax", "tip", "total")
# subtotal + tax + tip should equal total. Allow rounding slack.
CONSISTENCY_TOLERANCE_CENTS = 2
CONSISTENCY_BONUS = 0.10
CONSISTENCY_PENALTY = 0.25


@dataclass(slots=True)
class ParsedReceipt:
    merchant: str | None = None
    posted_date: date | None = None
    subtotal_cents: int | None = None
    tax_cents: int | None = None
    tip_cents: int | None = None
    total_cents: int | None = None
    currency: str = "USD"
    field_confidence: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def needs_review(self) -> bool:
        """A receipt with no total, or any weak transaction field, is never
        auto-trusted. Arithmetic that does not add up also forces review."""
        if self.total_cents is None or self.merchant is None or self.posted_date is None:
            return True
        if self.is_arithmetically_consistent is False:
            return True
        return any(
            score < FIELD_REVIEW_THRESHOLD
            for name, score in self.field_confidence.items()
            if name in REVIEW_GATING_FIELDS
        )

    @property
    def is_arithmetically_consistent(self) -> bool | None:
        """None when there is not enough extracted to check."""
        if self.total_cents is None or self.subtotal_cents is None:
            return None
        parts = self.subtotal_cents + (self.tax_cents or 0) + (self.tip_cents or 0)
        return abs(parts - self.total_cents) <= CONSISTENCY_TOLERANCE_CENTS


def money_to_cents(whole: str, fraction: str) -> int | None:
    try:
        value = Decimal(f"{whole.replace(',', '')}.{fraction}")
    except InvalidOperation:
        return None
    return int((value * 100).to_integral_value())


def last_money_on_line(text: str) -> tuple[int, str] | None:
    """The last money token on a line, ignoring percentages.

    ``TAX 8.25%   2.31`` must yield 2.31, not 8.25.
    """
    without_percent = _PERCENT.sub(" ", text)
    matches = list(_MONEY.finditer(without_percent))
    if not matches:
        return None
    match = matches[-1]
    cents = money_to_cents(match.group(1), match.group(2))
    if cents is None:
        return None
    return cents, match.group(0)


def _line_text(words: list[OcrWord]) -> str:
    return " ".join(word.text for word in words)


def _line_confidence(words: list[OcrWord]) -> float:
    if not words:
        return 0.0
    return sum(word.confidence for word in words) / len(words)


def _detect_currency(text: str) -> tuple[str, float]:
    for code in CURRENCY_CODES:
        if re.search(rf"\b{code}\b", text):
            return code, 0.95
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code, 0.9
    # Default rather than guess, and say so via the confidence.
    return "USD", 0.5


def _extract_merchant(lines: list[list[OcrWord]]) -> tuple[str | None, float]:
    """The merchant is the first substantive line at the top of the receipt."""
    for words in lines[:8]:
        text = _line_text(words).strip()
        if len(text) < 3 or _MERCHANT_NOISE.search(text) or _ADDRESSY.search(text):
            continue
        if _MONEY.search(text):
            continue
        letters = sum(character.isalpha() for character in text)
        if letters < 3:
            continue
        cleaned = re.sub(r"[^\w &'/.-]+", " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            return cleaned[:200], _line_confidence(words)
    return None, 0.0


def _extract_date(lines: list[list[OcrWord]]) -> tuple[date | None, float]:
    for words in lines:
        text = _line_text(words)
        for pattern in DATE_PATTERNS:
            match = re.search(pattern, text)
            if not match:
                continue
            try:
                parsed = parse_date(match.group(1))
            except ValueError:
                continue
            # A receipt dated in the future is an OCR error, not a receipt.
            if parsed > date.today():
                continue
            return parsed, _line_confidence(words)
    return None, 0.0


def parse_receipt(result: OcrResult) -> ParsedReceipt:
    """Parse an OCR result into structured, confidence-scored fields."""
    parsed = ParsedReceipt()
    lines = result.lines()
    if not lines:
        parsed.notes["ocr"] = "No text was recognized in this file."
        return parsed

    merchant, merchant_confidence = _extract_merchant(lines)
    parsed.merchant = merchant
    if merchant:
        parsed.field_confidence["merchant"] = round(merchant_confidence, 3)
    else:
        parsed.notes["merchant"] = "No merchant line could be identified."

    posted, date_confidence = _extract_date(lines)
    parsed.posted_date = posted
    if posted:
        parsed.field_confidence["posted_date"] = round(date_confidence, 3)
    else:
        parsed.notes["posted_date"] = "No date was found on this receipt."

    currency, currency_confidence = _detect_currency(result.text)
    parsed.currency = currency
    parsed.field_confidence["currency"] = round(currency_confidence, 3)
    if currency_confidence < 0.6:
        parsed.notes["currency"] = "No currency symbol found; assumed USD."

    # --- money fields, most-specific label first ---------------------------
    claimed: set[int] = set()
    for index, words in enumerate(lines):
        text = _line_text(words)
        lowered = text.lower()
        for field_name, labels in MONEY_LABELS:
            if getattr(parsed, f"{field_name}_cents") is not None:
                continue
            # Anchor at the start of the line so SUBTOTAL cannot match TOTAL.
            if not any(re.match(rf"^\W*{re.escape(label)}\b", lowered) for label in labels):
                continue
            if index in claimed:
                continue
            found = last_money_on_line(text)
            if found is None:
                continue
            cents, token = found
            setattr(parsed, f"{field_name}_cents", cents)
            parsed.field_confidence[field_name] = round(_line_confidence(words), 3)
            parsed.notes[field_name] = f"read “{token}” from a line labelled “{field_name}”"
            claimed.add(index)
            break

    # A receipt with no labelled total: fall back to the largest money token,
    # and say so, at reduced confidence.
    if parsed.total_cents is None:
        amounts: list[tuple[int, float]] = []
        for words in lines:
            found = last_money_on_line(_line_text(words))
            if found:
                amounts.append((found[0], _line_confidence(words)))
        if amounts:
            cents, confidence = max(amounts, key=lambda item: item[0])
            parsed.total_cents = cents
            parsed.field_confidence["total"] = round(confidence * 0.6, 3)
            parsed.notes["total"] = (
                "No line was labelled TOTAL; used the largest amount found. "
                "Please confirm."
            )
        else:
            parsed.notes["total"] = "No amount could be read from this receipt."

    _apply_consistency(parsed)
    return parsed


def _apply_consistency(parsed: ParsedReceipt) -> None:
    """Deterministic verification of the extraction itself.

    If the parts add up to the total, the read is corroborated and every money
    field gains confidence. If they disagree, something was misread and the
    fields lose confidence — which pushes the receipt into manual review rather
    than letting a wrong number through quietly.
    """
    consistent = parsed.is_arithmetically_consistent
    if consistent is None:
        return

    adjustment = CONSISTENCY_BONUS if consistent else -CONSISTENCY_PENALTY
    for field_name in ("subtotal", "tax", "tip", "total"):
        if field_name in parsed.field_confidence:
            score = parsed.field_confidence[field_name] + adjustment
            parsed.field_confidence[field_name] = round(min(1.0, max(0.0, score)), 3)

    if consistent:
        parsed.notes["consistency"] = "Subtotal + tax + tip matches the total."
    else:
        parts = (
            (parsed.subtotal_cents or 0)
            + (parsed.tax_cents or 0)
            + (parsed.tip_cents or 0)
        )
        parsed.notes["consistency"] = (
            f"Subtotal + tax + tip is {parts / 100:.2f}, but the total reads "
            f"{(parsed.total_cents or 0) / 100:.2f}. Please check these figures."
        )
