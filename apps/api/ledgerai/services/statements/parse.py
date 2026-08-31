"""Turning matched lines into rows, and checking the result arithmetically.

The balance column does the verification here. Where a statement prints a
running balance, consecutive deltas must equal the parsed amounts — a single
mis-read digit breaks the chain, which makes it a far stronger correctness
signal than any per-token confidence, and it costs nothing because the numbers
are already on the page.

Everything fails closed. A page whose columns cannot be resolved yields no
rows; a line that does not match its page's template is counted as skipped and
never guessed at; a broken chain drops the confidence of the rows it broke on
rather than quietly importing them.

Money is integer cents throughout, parsed from the printed digits. No float
ever touches an amount.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from .layout import Columns, Line, group_lines, infer_columns, line_matches, page_is_table

logger = logging.getLogger(__name__)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_CURRENCY_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR"}

# "1 January 2026 to 31 January 2026", "Statement period: 01/08/26 - 31/08/26"
_PERIOD = re.compile(
    r"(\d{1,2}[\s/\-.][A-Za-z0-9]{2,9}[\s/\-.]\d{2,4})\s*(?:to|–|—|-|until)\s*"
    r"(\d{1,2}[\s/\-.][A-Za-z0-9]{2,9}[\s/\-.]\d{2,4})",
    re.IGNORECASE,
)

_DIGITS_ONLY = re.compile(r"[^0-9]")


@dataclass(frozen=True, slots=True)
class ParsedRow:
    """One transaction as read off the page.

    `source_page` and `source_line` are provenance for review and for the
    idempotency key. They locate the row within the document the user already
    has; they are not statement content.
    """

    posted_date: date
    description: str
    amount_cents: int
    balance_cents: int | None
    source_page: int
    source_line: int
    confidence: float
    notes: tuple[str, ...] = ()

    @property
    def direction(self) -> str:
        return "credit" if self.amount_cents > 0 else "debit"


@dataclass(slots=True)
class ParsedStatement:
    """Everything read from one statement, plus how much to trust it."""

    rows: list[ParsedRow] = field(default_factory=list)
    page_count: int = 0
    table_pages: int = 0
    skipped_lines: int = 0
    currency: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    balance_chain_checked: bool = False
    balance_chain_ok: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def needs_review_count(self) -> int:
        return sum(1 for r in self.rows if r.confidence < 0.8)


def parse_amount(text: str) -> int | None:
    """Printed money to signed integer cents, or None when it is not money.

    Handles the sign conventions retail statements actually use: a leading
    minus, a unicode minus, parentheses, a trailing minus, and DR/CR suffixes.
    A bare positive number is returned positive and the caller decides what it
    means — on most statements that decision needs the balance column.
    """
    raw = text.strip()
    if not raw:
        return None

    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1].strip()

    upper = raw.upper()
    if upper.endswith(("DR", "D")) and not upper.endswith("USD"):
        negative = True
        raw = raw[: len(raw) - (2 if upper.endswith("DR") else 1)].strip()
    elif upper.endswith("CR"):
        raw = raw[:-2].strip()

    if raw.endswith("-"):
        negative = True
        raw = raw[:-1].strip()

    for symbol in _CURRENCY_SYMBOLS:
        raw = raw.replace(symbol, "")
    raw = raw.replace(" ", "").replace("−", "-").replace("−", "-")

    if raw.startswith("-"):
        negative = True
        raw = raw[1:]
    elif raw.startswith("+"):
        raw = raw[1:]

    if "." not in raw:
        return None
    whole, _, frac = raw.rpartition(".")
    whole = whole.replace(",", "")
    if not whole.isdigit() or len(frac) != 2 or not frac.isdigit():
        return None

    cents = int(whole) * 100 + int(frac)
    return -cents if negative else cents


def parse_date(text: str, *, year_hint: int | None = None) -> date | None:
    """Printed date to a real one, inferring the year where the page omits it.

    Statements routinely print "12 Aug" and leave the year to the period header
    at the top of the page. Without a hint that is genuinely ambiguous, so the
    caller gets None rather than a guess at the current year.
    """
    raw = text.strip().replace(".", " ").replace("/", " ").replace("-", " ")
    parts = [p for p in raw.split() if p]
    if not parts:
        return None

    def _month(token: str) -> int | None:
        if token.isdigit():
            value = int(token)
            return value if 1 <= value <= 12 else None
        return _MONTHS.get(token.lower()[:4].rstrip(".")) or _MONTHS.get(token.lower()[:3])

    try:
        if len(parts) == 3:
            if len(parts[0]) == 4 and parts[0].isdigit():  # 2026-08-12
                year, month, day = int(parts[0]), _month(parts[1]), int(parts[2])
            else:
                day, month = int(parts[0]), _month(parts[1])
                year = int(parts[2])
                if year < 100:
                    year += 2000
            if month is None:
                return None
            return date(year, month, day)

        if len(parts) == 2:
            if year_hint is None:
                return None
            first, second = parts
            if first.isdigit() and not second.isdigit():
                day, month = int(first), _month(second)
            elif second.isdigit() and not first.isdigit():
                month, day = _month(first), int(second)
            else:
                return None
            if month is None:
                return None
            return date(year_hint, month, day)
    except (ValueError, TypeError):
        return None
    return None


def _find_period(lines: list[Line]) -> tuple[date | None, date | None]:
    """The statement period, from whichever line prints it."""
    for line in lines[:40]:
        match = _PERIOD.search(line.text)
        if not match:
            continue
        start = parse_date(match.group(1))
        end = parse_date(match.group(2))
        if start and end and start <= end:
            return start, end
    return None, None


def _find_currency(lines: list[Line]) -> str | None:
    """One currency per statement, or None when the page never says.

    Mixed-currency statements are out of scope, so more than one symbol found
    is a refusal signal rather than something to reconcile.
    """
    found: set[str] = set()
    for line in lines:
        for symbol, code in _CURRENCY_SYMBOLS.items():
            if symbol in line.text:
                found.add(code)
    if len(found) == 1:
        return found.pop()
    return None


def _row_from_line(
    line: Line, columns: Columns, index: int, *, year_hint: int | None
) -> ParsedRow | None:
    """One matched line to a row, or None when its parts do not resolve."""
    date_text = ""
    amount_text = ""
    balance_text = ""
    description_parts: list[str] = []

    for word in line.words:
        column = columns.column_of(word)
        if column == "date" and not date_text:
            date_text = word.text
        elif column == "amount":
            amount_text = word.text
        elif column == "balance":
            balance_text = word.text
        elif column == "description":
            description_parts.append(word.text)

    posted = parse_date(date_text, year_hint=year_hint)
    amount = parse_amount(amount_text)
    description = " ".join(description_parts).strip()

    if posted is None or amount is None or not description:
        return None

    balance = parse_amount(balance_text) if balance_text else None

    notes: list[str] = []
    confidence = 1.0
    if len(date_text.split()) == 2 and year_hint is None:
        notes.append("year_inferred")
        confidence -= 0.2
    if balance is None:
        notes.append("no_balance_column")
        confidence -= 0.1
    if len(description) < 3:
        notes.append("short_description")
        confidence -= 0.2

    return ParsedRow(
        posted_date=posted,
        description=description,
        amount_cents=amount,
        balance_cents=balance,
        source_page=line.page,
        source_line=index,
        confidence=max(0.0, round(confidence, 3)),
        notes=tuple(notes),
    )


def _apply_balance_chain(rows: list[ParsedRow]) -> tuple[list[ParsedRow], bool, bool]:
    """Verify amounts against the running balance, and fix signs from it.

    Returns (rows, checked, ok). Where the chain holds, the delta also settles
    the debit/credit question that a bare printed number leaves open — which is
    why this runs even when every amount already parsed cleanly.
    """
    with_balance = [r for r in rows if r.balance_cents is not None]
    if len(with_balance) < 2:
        return rows, False, False

    adjusted: list[ParsedRow] = []
    breaks = 0
    previous_balance: int | None = None

    for row in rows:
        if row.balance_cents is None or previous_balance is None:
            if row.balance_cents is not None:
                previous_balance = row.balance_cents
            adjusted.append(row)
            continue

        delta = row.balance_cents - previous_balance
        previous_balance = row.balance_cents

        if delta == row.amount_cents:
            adjusted.append(row)
        elif abs(delta) == abs(row.amount_cents):
            # Magnitude agrees, sign does not: the statement printed the amount
            # unsigned and the balance movement is what says which way it went.
            adjusted.append(
                ParsedRow(
                    posted_date=row.posted_date,
                    description=row.description,
                    amount_cents=delta,
                    balance_cents=row.balance_cents,
                    source_page=row.source_page,
                    source_line=row.source_line,
                    confidence=row.confidence,
                    notes=row.notes + ("sign_from_balance",),
                )
            )
        else:
            breaks += 1
            adjusted.append(
                ParsedRow(
                    posted_date=row.posted_date,
                    description=row.description,
                    amount_cents=row.amount_cents,
                    balance_cents=row.balance_cents,
                    source_page=row.source_page,
                    source_line=row.source_line,
                    confidence=min(row.confidence, 0.4),
                    notes=row.notes + ("balance_mismatch",),
                )
            )

    # A statement that prints amounts unsigned leaves the first row with
    # nothing to diff against — there is no earlier balance. Every later row
    # was settled by the chain, so leaving the first one at its printed sign
    # would be a silent guess on exactly one transaction a month. Flag it.
    if any("sign_from_balance" in row.notes for row in adjusted):
        for index, row in enumerate(adjusted):
            if row.balance_cents is None:
                continue
            if "sign_from_balance" in row.notes or "balance_mismatch" in row.notes:
                continue
            adjusted[index] = ParsedRow(
                posted_date=row.posted_date,
                description=row.description,
                amount_cents=row.amount_cents,
                balance_cents=row.balance_cents,
                source_page=row.source_page,
                source_line=row.source_line,
                confidence=min(row.confidence, 0.5),
                notes=row.notes + ("sign_unresolved",),
            )
            break

    return adjusted, True, breaks == 0


def parse_statement(pages: list[list]) -> ParsedStatement:
    """Read a whole statement. Never raises for content; refuses by returning few rows."""
    result = ParsedStatement(page_count=len(pages))

    all_lines: list[Line] = []
    per_page: list[list[Line]] = []
    for words in pages:
        lines = group_lines(words)
        per_page.append(lines)
        all_lines.extend(lines)

    result.period_start, result.period_end = _find_period(all_lines)
    result.currency = _find_currency(all_lines)
    year_hint = result.period_end.year if result.period_end else None

    # A statement uses one layout throughout, so columns resolved across the
    # whole document stand in for a page that cannot resolve its own. Without
    # this a continuation page carrying the last two transactions of the month
    # is skipped in silence — and a short month then looks like a complete one
    # to anybody checking totals, which is the worst way to be wrong.
    document_columns = infer_columns(all_lines)

    rows: list[ParsedRow] = []
    for lines in per_page:
        own_columns = infer_columns(lines)
        columns = own_columns or document_columns
        if columns is None:
            result.skipped_lines += len(lines)
            continue
        # A page that resolved its own geometry must corroborate it; a page
        # borrowing the document's needs only one genuine row.
        minimum = None if own_columns is not None else 1
        if not page_is_table(lines, columns, minimum=minimum):
            result.skipped_lines += len(lines)
            continue
        result.table_pages += 1
        for index, line in enumerate(lines):
            if not line_matches(line, columns):
                result.skipped_lines += 1
                continue
            row = _row_from_line(line, columns, index, year_hint=year_hint)
            if row is None:
                result.skipped_lines += 1
                continue
            rows.append(row)

    rows, checked, ok = _apply_balance_chain(rows)
    result.rows = rows
    result.balance_chain_checked = checked
    result.balance_chain_ok = ok

    if checked and not ok:
        result.notes.append("balance_chain_broken")
    if not checked:
        result.notes.append("no_balance_column")
    if result.currency is None:
        result.notes.append("currency_not_stated")

    logger.info(
        "statement.parsed pages=%d table_pages=%d rows=%d skipped=%d chain=%s",
        result.page_count,
        result.table_pages,
        len(rows),
        result.skipped_lines,
        "ok" if ok else ("broken" if checked else "absent"),
    )
    return result
