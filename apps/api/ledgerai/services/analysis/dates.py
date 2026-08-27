"""Relative date-phrase resolution.

Hand-rolled rather than pulled from a library: the vocabulary is small and
closed, the behaviour must be exactly reproducible in tests, and every
resolution is shown to the user in the 'understanding' step — so it has to be
explainable, not just correct.

Every function takes an explicit `today` so tests never depend on the clock.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from .plan import DateRange

MONTH_NAMES = {
    name.lower(): index
    for index, name in enumerate(calendar.month_name)
    if name
} | {
    abbr.lower(): index
    for index, abbr in enumerate(calendar.month_abbr)
    if abbr
}


def month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def shift_month(anchor: date, months: int) -> date:
    """Move by whole months, clamping to the last valid day."""
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def previous_period(period: DateRange) -> DateRange:
    """The equivalent window immediately before `period`.

    Calendar months map to the previous calendar month (so a 31-day month
    compares against a 30-day month, which is what a person means). Arbitrary
    windows shift back by their own length.
    """
    start, end = period.start, period.end
    is_full_month = start.day == 1 and end == month_bounds(end.year, end.month)[1]

    if is_full_month and start.year == end.year and start.month == end.month:
        prev_anchor = shift_month(start, -1)
        prev_start, prev_end = month_bounds(prev_anchor.year, prev_anchor.month)
        return DateRange(
            start=prev_start,
            end=prev_end,
            label=f"{calendar.month_name[prev_start.month]} {prev_start.year}",
        )

    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    return DateRange(
        start=prev_start,
        end=prev_end,
        label=f"the previous {length} days",
    )


_RELATIVE_N = re.compile(r"\b(?:last|past|previous|recent)\s+(\d{1,3})\s+(day|week|month|year)s?\b")
_EXPLICIT_MONTH_YEAR = re.compile(
    r"\b(" + "|".join(sorted(MONTH_NAMES, key=len, reverse=True)) + r")\.?\s*(\d{4})?\b"
)
_QUARTER = re.compile(r"\bq([1-4])\s*(\d{4})?\b")
_YEAR = re.compile(r"\b(20\d{2})\b")


def resolve_date_phrase(question: str, today: date) -> DateRange:  # noqa: PLR0911
    """Map a natural phrase to an absolute range.

    Falls back to the last 30 days when a question names no period at all —
    and the label says so, so the user can see the assumption rather than
    guess at it.
    """
    text = question.lower()

    if re.search(r"\b(this|current)\s+month\b", text):
        start, end = month_bounds(today.year, today.month)
        return DateRange(start=start, end=min(end, today), label="this month")

    if re.search(r"\blast\s+month\b", text) or re.search(r"\bprevious\s+month\b", text):
        anchor = shift_month(today.replace(day=1), -1)
        start, end = month_bounds(anchor.year, anchor.month)
        return DateRange(start=start, end=end, label="last month")

    if re.search(r"\b(this|current)\s+year\b", text):
        return DateRange(
            start=date(today.year, 1, 1), end=today, label=f"{today.year} so far"
        )

    if re.search(r"\blast\s+year\b", text):
        return DateRange(
            start=date(today.year - 1, 1, 1),
            end=date(today.year - 1, 12, 31),
            label=str(today.year - 1),
        )

    if re.search(r"\b(this|current)\s+week\b", text):
        start = today - timedelta(days=today.weekday())
        return DateRange(start=start, end=today, label="this week")

    if re.search(r"\blast\s+week\b", text):
        this_week = today - timedelta(days=today.weekday())
        start = this_week - timedelta(days=7)
        return DateRange(start=start, end=start + timedelta(days=6), label="last week")

    if match := _RELATIVE_N.search(text):
        count, unit = int(match.group(1)), match.group(2)
        if unit == "day":
            start = today - timedelta(days=count - 1)
        elif unit == "week":
            start = today - timedelta(weeks=count)
        elif unit == "month":
            # Whole calendar months, not "today minus N months".
            #
            # Counting back from the day produces a partial bucket at the far
            # edge: asked on 27 August, "the last 6 months" would start on 27
            # February and group two days of February as a month. Plotted, that
            # is a point near zero at the start of the line and a narration
            # calling February the lowest-spending month — arithmetically right,
            # and a false impression. Aligning to the first of the month also
            # matches how the dashboard trend already buckets, so the two
            # surfaces cannot disagree about the same period.
            start = shift_month(today.replace(day=1), -(count - 1))
        else:
            start = date(today.year - count, today.month, today.day)
        return DateRange(start=start, end=today, label=f"the last {count} {unit}s")

    if match := _QUARTER.search(text):
        quarter = int(match.group(1))
        year = int(match.group(2)) if match.group(2) else today.year
        start_month = (quarter - 1) * 3 + 1
        start = date(year, start_month, 1)
        end = month_bounds(year, start_month + 2)[1]
        return DateRange(start=start, end=end, label=f"Q{quarter} {year}")

    if match := _EXPLICIT_MONTH_YEAR.search(text):
        month = MONTH_NAMES[match.group(1)]
        year = int(match.group(2)) if match.group(2) else today.year
        # "December" asked in March means last December, not this one.
        if match.group(2) is None and month > today.month:
            year -= 1
        start, end = month_bounds(year, month)
        return DateRange(start=start, end=end, label=f"{calendar.month_name[month]} {year}")

    if match := _YEAR.search(text):
        year = int(match.group(1))
        return DateRange(start=date(year, 1, 1), end=date(year, 12, 31), label=str(year))

    if re.search(r"\b(all\s+time|ever|overall|total(?:ly)?)\b", text):
        return DateRange(start=date(today.year - 5, 1, 1), end=today, label="all available data")

    start = today - timedelta(days=29)
    return DateRange(start=start, end=today, label="the last 30 days (no period specified)")


def mentions_comparison(question: str) -> bool:
    return bool(
        re.search(
            r"\b(compared?\s+(?:to|with)|versus|vs\.?|than\s+last|change|difference|"
            r"more\s+than|less\s+than|up\s+or\s+down)\b",
            question.lower(),
        )
    )
