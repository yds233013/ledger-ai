"""Bank-statement CSV parsing.

Real statements differ per institution, so the parser maps *logical* fields
(date / description / amount) onto whatever headers the file actually uses,
and tolerates the two common amount layouts:

  * one signed "Amount" column
  * separate "Debit" and "Credit" columns

Rows that cannot be parsed are collected as errors rather than aborting the
import — a single malformed row should not cost the user the other 400.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date

from ..security.validators import ValidationError, decode_csv, normalize_header
from .normalize import extract_merchant, normalize_description, parse_amount_to_cents, parse_date

DATE_ALIASES = ("date", "posteddate", "transactiondate", "postingdate", "posted")
DESC_ALIASES = ("description", "details", "memo", "narrative", "name", "payee")
AMOUNT_ALIASES = ("amount", "value", "amountusd")
DEBIT_ALIASES = ("debit", "withdrawal", "withdrawals", "moneyout")
CREDIT_ALIASES = ("credit", "deposit", "deposits", "moneyin")
ACCOUNT_ALIASES = ("account", "accountname", "accountnumber", "card")


@dataclass(slots=True)
class ParsedRow:
    row_index: int
    posted_date: date
    amount_cents: int
    raw_description: str
    normalized_description: str
    merchant: str
    account_hint: str | None = None


@dataclass(slots=True)
class RowError:
    row_index: int
    message: str
    raw: str


@dataclass(slots=True)
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    header_mapping: dict[str, str] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return len(self.rows) + len(self.errors)


def _find(normalized: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    return next((normalized[a] for a in aliases if a in normalized), None)


def parse_statement_csv(data: bytes) -> ParseResult:
    """Parse raw CSV bytes into normalized rows.

    Raises ValidationError only for file-level problems (no header, no usable
    date/description/amount columns). Row-level problems become RowErrors.
    """
    text = decode_csv(data)
    if not text.strip():
        raise ValidationError("CSV is empty")

    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            text[:4096], delimiters=",;\t|"
        )
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValidationError("CSV has no header row")

    normalized = {normalize_header(c): c for c in reader.fieldnames if c and c.strip()}
    date_col = _find(normalized, DATE_ALIASES)
    desc_col = _find(normalized, DESC_ALIASES)
    amount_col = _find(normalized, AMOUNT_ALIASES)
    debit_col = _find(normalized, DEBIT_ALIASES)
    credit_col = _find(normalized, CREDIT_ALIASES)
    account_col = _find(normalized, ACCOUNT_ALIASES)

    missing = [
        label
        for label, present in (
            ("date", date_col),
            ("description", desc_col),
            ("amount", amount_col or debit_col or credit_col),
        )
        if not present
    ]
    if missing:
        raise ValidationError(
            "CSV is missing required column(s): "
            + ", ".join(missing)
            + ". Expected a date column, a description column, and either an "
            "amount column or debit/credit columns."
        )

    mapping = {
        "date": date_col or "",
        "description": desc_col or "",
        "amount": amount_col or f"{debit_col or ''}/{credit_col or ''}",
    }
    if account_col:
        mapping["account"] = account_col

    result = ParseResult(header_mapping=mapping)

    for index, row in enumerate(reader):
        raw_repr = ",".join(f"{v}" for v in row.values() if v)[:200]
        try:
            posted = parse_date((row.get(date_col or "") or "").strip())
            description = (row.get(desc_col or "") or "").strip()
            if not description:
                raise ValueError("Description is blank")

            cents = _resolve_amount(row, amount_col, debit_col, credit_col)
            if cents == 0:
                raise ValueError("Amount is zero")

            result.rows.append(
                ParsedRow(
                    row_index=index,
                    posted_date=posted,
                    amount_cents=cents,
                    raw_description=description[:512],
                    normalized_description=normalize_description(description)[:512],
                    merchant=extract_merchant(description),
                    account_hint=(row.get(account_col) or "").strip() or None
                    if account_col
                    else None,
                )
            )
        except (ValueError, TypeError) as exc:
            result.errors.append(RowError(row_index=index, message=str(exc), raw=raw_repr))

    if not result.rows:
        detail = f" First error: {result.errors[0].message}" if result.errors else ""
        raise ValidationError(f"No valid transaction rows could be parsed from this CSV.{detail}")

    return result


def _resolve_amount(
    row: dict[str, str | None],
    amount_col: str | None,
    debit_col: str | None,
    credit_col: str | None,
) -> int:
    """Single signed column, or a debit/credit pair.

    In the debit/credit layout, debits are written as positive numbers and mean
    money out, so they are negated to match our sign convention.
    """
    if amount_col:
        value = (row.get(amount_col) or "").strip()
        if value:
            return parse_amount_to_cents(value)

    debit = (row.get(debit_col) or "").strip() if debit_col else ""
    credit = (row.get(credit_col) or "").strip() if credit_col else ""

    if debit:
        return -abs(parse_amount_to_cents(debit))
    if credit:
        return abs(parse_amount_to_cents(credit))

    raise ValueError("No amount found in amount/debit/credit columns")
