"""Refusing files that carry unmasked sensitive identifiers.

The upload consent asks people to remove or mask full account numbers, Social
Security numbers and similar identifiers, and promises that Ledger AI *tries*
to catch them. This is that attempt. It is deliberately best-effort, and the
consent text says so — a hand-typed account number with no checksum will pass.

**Checksums, not shapes.** Every rejection class is validated arithmetically:
Luhn for card numbers, the ABA weighted checksum for routing numbers, mod-97
for IBANs. Shape alone would reject order numbers, invoice references and
confirmation codes constantly, and a detector that cries wolf is one people
learn to route around.

**A sensitive header is a signal, not a verdict.** A column called "Account
Number" is exactly what a real bank export contains, and its values are almost
always masked. Rejecting on the header would refuse the ordinary case. So a
header only raises the scrutiny applied to that column's values; the rejection
still requires an unmasked, checksum-valid value.

**Masked forms are expected and always allowed.** `••••4821`, `****4821`,
`XXXX-4821`, `x4821` and a bare last-four are what statements actually contain.

**Nothing about a match escapes.** The caller receives category names and
counts. Never a value, never a row number, never a column name, never an
offset — a report precise enough to locate the sensitive data would itself
describe where the sensitive data is.

Out of scope for now, deliberately: passport numbers, driver's licence numbers
and dates of birth. None has a checksum, all vary by jurisdiction, and a date
of birth is indistinguishable from a transaction date. Scanning them would
reject legitimate statements often enough to make the check untrustworthy.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

# Digit runs long enough to be worth testing. Bounded so a long numeric blob
# does not become a quadratic scan.
_DIGIT_RUN = re.compile(r"(?<![0-9])(?:[0-9][ \-]?){8,34}[0-9](?![0-9])")
_SSN_DASHED = re.compile(r"(?<![0-9-])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9-])")
_IBAN_CANDIDATE = re.compile(r"(?<![A-Z0-9])[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}(?![A-Z0-9])")

# Any of these means the writer already masked the value.
_MASK_CHARS = frozenset("*x•·X#")
_MASK_MARKERS = ("****", "••••", "xxxx", "XXXX", "####", "…", "...")

# Column names that raise scrutiny. Matching one is not a rejection.
_SENSITIVE_HEADERS = (
    "ssn",
    "social security",
    "account number",
    "account no",
    "acct number",
    "card number",
    "card no",
    "pan",
    "routing",
    "aba",
    "iban",
    "sort code",
    "tax id",
    "national insurance",
)

# Columns whose values are amounts or dates. Never identifier candidates: a
# five-figure amount and a long reference are both just digits.
_NUMERIC_HEADERS = (
    "amount",
    "balance",
    "debit",
    "credit",
    "value",
    "total",
    "price",
    "date",
    "posted",
    "time",
)


class Category(StrEnum):
    """What was found. The only thing ever reported."""

    PAYMENT_CARD = "payment_card"
    US_SSN = "us_ssn"
    US_ROUTING = "us_routing"
    IBAN = "iban"


REMEDIATION = {
    Category.PAYMENT_CARD: (
        "A full payment card number appears in this file. Please mask it — the "
        "last four digits alone are fine — and upload again."
    ),
    Category.US_SSN: (
        "A Social Security number appears in this file. Ledger AI does not need "
        "it. Please remove it and upload again."
    ),
    Category.US_ROUTING: (
        "A full bank routing number appears in this file. Please remove or mask "
        "it and upload again."
    ),
    Category.IBAN: (
        "A full IBAN appears in this file. Please mask it — the last four "
        "characters alone are fine — and upload again."
    ),
}


@dataclass
class Findings:
    """Categories and counts. Deliberately incapable of carrying a value."""

    counts: dict[Category, int] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return bool(self.counts)

    @property
    def categories(self) -> list[str]:
        return sorted(c.value for c in self.counts)

    def add(self, category: Category) -> None:
        self.counts[category] = self.counts.get(category, 0) + 1

    def guidance(self) -> str:
        """One sentence per category, in a stable order."""
        return " ".join(REMEDIATION[c] for c in sorted(self.counts, key=lambda c: c.value))

    def as_report(self) -> dict[str, object]:
        return {
            "categories": self.categories,
            "counts": {
                c.value: n
                for c, n in sorted(self.counts.items(), key=lambda kv: kv[0].value)
            },
            "guidance": self.guidance(),
        }


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def luhn_valid(digits: str) -> bool:
    """The check every payment card number satisfies."""
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, ch in enumerate(reversed(digits)):
        digit = ord(ch) - 48
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def aba_valid(digits: str) -> bool:
    """The ABA routing checksum: weights 3,7,1 repeating, sum divisible by 10."""
    if not digits.isdigit() or len(digits) != 9:
        return False
    if digits == "0" * 9:
        return False
    weights = (3, 7, 1, 3, 7, 1, 3, 7, 1)
    total = sum(int(d) * w for d, w in zip(digits, weights, strict=True))
    return total % 10 == 0


def iban_valid(candidate: str) -> bool:
    """Structural check plus mod-97, per ISO 13616."""
    compact = candidate.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34:
        return False
    if not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    if not compact.isalnum():
        return False
    rearranged = compact[4:] + compact[:4]
    converted = "".join(
        str(ord(ch) - 55) if ch.isalpha() else ch for ch in rearranged
    )
    if not converted.isdigit():
        return False
    return int(converted) % 97 == 1


def ssn_plausible(digits: str) -> bool:
    """Ranges the SSA has never issued, so obviously-fake values do not reject."""
    if len(digits) != 9 or not digits.isdigit():
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in ("000", "666") or area[0] == "9":
        return False
    return group != "00" and serial != "0000"


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def is_masked(value: str) -> bool:
    """Whether the writer already obscured this value.

    Masked forms are what real statements contain, so this runs before every
    other test and short-circuits it.
    """
    if not value:
        return True
    if any(marker in value for marker in _MASK_MARKERS):
        return True
    if any(ch in _MASK_CHARS for ch in value):
        return True
    # A bare last-four (or fewer) is not an identifier.
    return len(_digits(value)) <= 4


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _scan_value(value: str, findings: Findings, *, sensitive_column: bool) -> None:
    """Test one cell. Masked values return immediately."""
    if not value or is_masked(value):
        return

    for match in _IBAN_CANDIDATE.finditer(value.upper()):
        if iban_valid(match.group()):
            findings.add(Category.IBAN)

    for match in _SSN_DASHED.finditer(value):
        if ssn_plausible(_digits(match.group())):
            findings.add(Category.US_SSN)

    for match in _DIGIT_RUN.finditer(value):
        digits = _digits(match.group())
        if luhn_valid(digits):
            findings.add(Category.PAYMENT_CARD)
            continue
        if len(digits) == 9 and aba_valid(digits):
            # Nine digits passing ABA is a routing number often enough to act
            # on — but only where the column suggests it. Elsewhere a
            # nine-digit reference passes ABA by chance about a tenth of the
            # time, which is far too often to reject an ordinary file.
            if sensitive_column:
                findings.add(Category.US_ROUTING)
            continue
        # An undashed SSN only counts under a column that says so; nine bare
        # digits are otherwise just as likely to be a reference number.
        if sensitive_column and len(digits) == 9 and ssn_plausible(digits):
            findings.add(Category.US_SSN)


def header_is_sensitive(header: str) -> bool:
    """Whether this column name warrants stricter value checks.

    A signal, never a verdict: real bank exports ship an "Account Number"
    column and fill it with masked values.
    """
    lowered = header.strip().lower()
    if any(token in lowered for token in _NUMERIC_HEADERS):
        return False
    return any(token in lowered for token in _SENSITIVE_HEADERS)


def header_is_numeric(header: str) -> bool:
    """Amount and date columns are never identifier candidates."""
    return any(token in header.strip().lower() for token in _NUMERIC_HEADERS)


def scan_rows(
    headers: Sequence[str] | None,
    rows: Iterable[Sequence[str]],
    *,
    max_cells: int = 200_000,
) -> Findings:
    """Scan tabular data. Bounded so a hostile file cannot pin a worker."""
    findings = Findings()
    sensitive = [header_is_sensitive(h) for h in (headers or [])]
    numeric = [header_is_numeric(h) for h in (headers or [])]

    # Header names alone never reject — but they are scanned as values, because
    # a file can carry an identifier in its header row.
    for header in headers or []:
        _scan_value(header, findings, sensitive_column=False)

    cells = 0
    for row in rows:
        for index, value in enumerate(row):
            cells += 1
            if cells > max_cells:
                return findings
            if index < len(numeric) and numeric[index]:
                continue
            _scan_value(
                str(value),
                findings,
                sensitive_column=index < len(sensitive) and sensitive[index],
            )
    return findings


def scan_text(text: str, *, max_chars: int = 500_000) -> Findings:
    """Scan free text — receipt OCR output, filenames.

    No column context exists here, so only the classes that stand on their own
    apply: Luhn-valid cards, dashed SSNs and valid IBANs. A bare nine-digit run
    in OCR output is left alone.
    """
    findings = Findings()
    _scan_value(text[:max_chars], findings, sensitive_column=False)
    return findings


def scan_csv(data: bytes, *, max_cells: int = 200_000) -> Findings:
    """Scan a CSV upload before any of it is stored.

    Parsed with the same dialect sniffing the structural validator uses, so the
    columns this sees are the columns the importer will see. A file that cannot
    be parsed is not passed as clean — it is simply not scannable here, and the
    structural validator has already rejected it by this point.
    """
    from ..security.validators import decode_csv  # local: avoids an import cycle

    text = decode_csv(data)
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            text[:4096], delimiters=",;\t|"
        )
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        headers = next(reader)
    except StopIteration:
        return Findings()
    return scan_rows(headers, reader, max_cells=max_cells)
