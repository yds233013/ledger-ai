"""Finding the table in a page that has no table.

A bank statement PDF carries no structure — no cells, no headers, no markup.
It is a bag of positioned text runs that happens to look like a table when
drawn. Recovering the table means recovering the geometry: which runs share a
baseline, and which x-band each belongs to.

The approach is deliberately template-free. Per-bank templates are a treadmill
— a layout tweak in one bank's statement generator silently breaks imports for
everyone at that bank, and the failure looks like wrong data rather than an
error. Instead each page is asked what its own most common row shape is, and
lines that do not match it are counted as unparsed rather than guessed at.

Failing closed is the whole design. A line that does not fit the page's own
template contributes nothing and is reported as skipped; a page whose columns
cannot be resolved yields no rows at all.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .extract import Word

# Two runs are on the same line when their vertical centres are closer than
# this fraction of the taller run's height. Scaling by the text's own size is
# what lets one rule work for a 7pt statement and a 12pt one.
LINE_TOLERANCE = 0.6

# A page needs at least this many template-matching lines to be considered part
# of the transaction table rather than a summary or marketing page.
MIN_TABLE_ROWS_PER_PAGE = 3

# Currency-shaped: optional sign and symbol, thousands groups, 2 decimals.
_AMOUNT = re.compile(
    r"^[\(\-\+−]?\s*[£$€]?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})\s*[\)]?[A-Z]{0,2}$"
)
_AMOUNT_LOOSE = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}")

# Date-shaped, in the forms retail statements actually print.
_DATE_PATTERNS = (
    re.compile(r"^\d{1,2}[\s/\-.]{1}(?:\d{1,2}|[A-Za-z]{3,9})[\s/\-.]{1}\d{2,4}$"),
    re.compile(r"^\d{1,2}\s+[A-Za-z]{3,9}$"),
    re.compile(r"^[A-Za-z]{3,9}\s+\d{1,2}$"),
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
)


def looks_like_amount(text: str) -> bool:
    return bool(_AMOUNT.match(text.strip()))


def looks_like_date(text: str) -> bool:
    stripped = text.strip()
    return any(pattern.match(stripped) for pattern in _DATE_PATTERNS)


@dataclass(slots=True)
class Line:
    """Runs sharing a baseline, left to right."""

    words: list[Word] = field(default_factory=list)
    page: int = 0

    @property
    def y(self) -> float:
        return sum(w.cy for w in self.words) / len(self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def group_lines(words: list[Word]) -> list[Line]:
    """Cluster runs into lines by vertical proximity, top of page first."""
    if not words:
        return []

    ordered = sorted(words, key=lambda w: (-w.cy, w.x0))
    lines: list[Line] = []
    current: list[Word] = [ordered[0]]

    for word in ordered[1:]:
        reference = current[-1]
        tolerance = max(reference.height, word.height, 1.0) * LINE_TOLERANCE
        if abs(word.cy - reference.cy) <= tolerance:
            current.append(word)
        else:
            current.sort(key=lambda w: w.x0)
            lines.append(Line(words=current, page=current[0].page))
            current = [word]

    current.sort(key=lambda w: w.x0)
    lines.append(Line(words=current, page=current[0].page))
    return lines


@dataclass(frozen=True, slots=True)
class Columns:
    """Where this page's table columns sit, in PDF user space.

    `amount_x` and `balance_x` are right edges: money is right-aligned, so the
    right edge is the stable landmark and the left edge moves with the digit
    count.
    """

    date_x: float
    amount_x: float
    balance_x: float | None
    description_from: float
    description_to: float

    def column_of(self, word: Word) -> str:
        """Which column a run belongs to. Never guesses beyond its bands."""
        if abs(word.x1 - self.amount_x) <= COLUMN_TOLERANCE:
            return "amount"
        if self.balance_x is not None and abs(word.x1 - self.balance_x) <= COLUMN_TOLERANCE:
            return "balance"
        if abs(word.x0 - self.date_x) <= COLUMN_TOLERANCE:
            return "date"
        if self.description_from <= word.x0 < self.description_to:
            return "description"
        return "unknown"


# How far a run may sit from a column landmark and still belong to it. Generous
# enough for a proportional font's varying glyph widths, tight enough that an
# amount column and a balance column 70pt apart never merge.
COLUMN_TOLERANCE = 12.0


def _cluster(values: list[float], tolerance: float) -> list[tuple[float, int]]:
    """One-dimensional clustering: (centre, population), most populous first."""
    if not values:
        return []
    ordered = sorted(values)
    clusters: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    summary = [(sum(c) / len(c), len(c)) for c in clusters]
    summary.sort(key=lambda item: (-item[1], item[0]))
    return summary


def infer_columns(lines: list[Line]) -> Columns | None:
    """Derive this page's column geometry from its own most common row shape.

    Returns None when the page has no resolvable table — a summary page, a
    marketing insert, terms and conditions. That is a normal outcome and not an
    error: those pages simply contribute no rows.
    """
    money_right_edges: list[float] = []
    date_left_edges: list[float] = []

    for line in lines:
        for word in line.words:
            if looks_like_amount(word.text):
                money_right_edges.append(word.x1)
            elif looks_like_date(word.text):
                date_left_edges.append(word.x0)

    money_clusters = _cluster(money_right_edges, COLUMN_TOLERANCE)
    date_clusters = _cluster(date_left_edges, COLUMN_TOLERANCE)

    if not money_clusters or not date_clusters:
        return None

    # The date column is the leftmost well-populated date cluster: statements
    # sometimes print a second date inside the description ("card used 11 Aug"),
    # and that one is never the posting-date column.
    populated_dates = [c for c in date_clusters if c[1] >= MIN_TABLE_ROWS_PER_PAGE]
    if not populated_dates:
        return None
    date_x = min(centre for centre, _ in populated_dates)

    # Of the money columns, the amount is the more populous. A balance column
    # is present on every row when it exists at all, so a tie is broken by
    # position: balance sits to the right of the amount.
    populated_money = [c for c in money_clusters if c[1] >= MIN_TABLE_ROWS_PER_PAGE]
    if not populated_money:
        return None

    money_positions = sorted(centre for centre, _ in populated_money)
    if len(money_positions) == 1:
        amount_x, balance_x = money_positions[0], None
    else:
        # Rightmost is the balance; the one before it is the amount. Anything
        # further left on a retail statement is a debit/credit split, handled
        # by treating the populated pair as (amount, balance) and letting rows
        # that do not match the template fall out as unparsed.
        amount_x, balance_x = money_positions[-2], money_positions[-1]

    description_from = date_x + COLUMN_TOLERANCE
    description_to = amount_x - COLUMN_TOLERANCE * 2

    if description_to <= description_from:
        return None

    return Columns(
        date_x=date_x,
        amount_x=amount_x,
        balance_x=balance_x,
        description_from=description_from,
        description_to=description_to,
    )


def page_is_table(lines: list[Line], columns: Columns, *, minimum: int | None = None) -> bool:
    """Whether enough lines match the template to call this a table page.

    The threshold depends on where the geometry came from. Columns inferred
    from this page alone need corroboration — several matching lines — before
    the inference is trusted. Columns carried over from the whole document are
    already trusted, so a single matching line is enough, which is what lets a
    continuation page holding the last two transactions of the month parse.

    One row is a safe floor because `line_matches` is itself strict: a real
    date in the date column and real money in the amount column. Summary and
    marketing pages fail that test on every line regardless of the threshold.
    """
    threshold = MIN_TABLE_ROWS_PER_PAGE if minimum is None else minimum
    return sum(1 for line in lines if line_matches(line, columns)) >= threshold


def line_matches(line: Line, columns: Columns) -> bool:
    """A transaction row has a date in the date column and an amount in the amount column."""
    kinds = Counter(columns.column_of(word) for word in line.words)
    if not kinds["date"] or not kinds["amount"]:
        return False
    has_date = any(
        columns.column_of(w) == "date" and looks_like_date(w.text) for w in line.words
    )
    has_amount = any(
        columns.column_of(w) == "amount" and looks_like_amount(w.text) for w in line.words
    )
    return has_date and has_amount


def has_loose_amount(text: str) -> bool:
    """Whether a string contains anything money-shaped at all."""
    return bool(_AMOUNT_LOOSE.search(text))
