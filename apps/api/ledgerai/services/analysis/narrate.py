"""Plain-language explanation of a computed result.

Two rules govern this module, and both are enforced by code rather than by a
prompt:

  1. Narration is written from the ExecutionResult only. It never queries the
     database and never performs arithmetic of its own.
  2. Every number in the narration must already exist in the result payload.
     `verify_numeric_claims` checks that; the Phase 2 LLM narrator runs through
     it and is discarded on any mismatch, falling back to the template below.

Ledger AI also does not give financial advice. `is_advice_request` catches
questions that ask for a recommendation and the runner answers them with a
scoped decline instead of an analysis.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .executor import ExecutionResult
from .plan import AnalysisPlan, Direction, GroupBy, Intent, Metric

_ADVICE = re.compile(
    r"\b(should i|shall i|do you recommend|recommend(?:ation)?s?\b|is it (?:worth|smart|wise)|"
    r"how (?:much )?should|what should i (?:do|buy|invest)|advise|advice|"
    r"invest(?:ing|ment)?\b|stocks?\b|crypto\b|retirement|401k|portfolio|"
    r"pay off my|get out of debt|financial plan)\b",
    re.IGNORECASE,
)

# Digits that carry meaning, ignoring formatting characters.
_NUMBER = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?%?")


def is_advice_request(question: str) -> bool:
    """True when the question asks what the user *should* do with their money."""
    return bool(_ADVICE.search(question))


def plural(count: int, noun: str = "transaction") -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(Decimal(cents)) / 100
    return f"{sign}${value:,.2f}"


def _numeric_tokens(text: str) -> set[str]:
    """Normalize numbers so $1,234.50, 1234.5 and 1234.50 compare equal."""
    tokens: set[str] = set()
    for raw in _NUMBER.findall(text):
        cleaned = raw.replace("$", "").replace(",", "").replace("%", "").lstrip("-")
        if not cleaned:
            continue
        try:
            tokens.add(str(Decimal(cleaned).normalize()))
        except InvalidOperation:  # noqa: S112 - non-numeric noise is simply skipped
            continue
    return tokens


def allowed_numeric_tokens(result: ExecutionResult) -> set[str]:
    """Every number the narration is permitted to contain."""
    allowed: set[str] = set()

    def add(value: object) -> None:
        try:
            allowed.add(str(Decimal(str(value)).normalize()))
        except (InvalidOperation, ValueError, TypeError):
            return

    add(result.total_cents)
    add(round(result.total_cents / 100, 2))
    add(result.transaction_count)
    add(len(result.rows))

    for row in result.rows:
        add(row.value_cents)
        add(round(row.value_cents / 100, 2))
        add(row.transaction_count)
        # Percentage share of the total is derived here, so it is allowed.
        if result.total_cents:
            add(round(abs(row.value_cents) / abs(result.total_cents) * 100, 1))

    if result.comparison:
        comparison = result.comparison
        for cents in (comparison.current_cents, comparison.previous_cents, comparison.delta_cents):
            add(cents)
            add(round(cents / 100, 2))
            add(round(abs(cents) / 100, 2))
        if comparison.delta_pct is not None:
            add(comparison.delta_pct)
            add(abs(comparison.delta_pct))

    for transaction in result.supporting:
        add(transaction["amount_cents"])
        add(abs(transaction["amount_cents"]))
        add(transaction["amount"])
        add(abs(transaction["amount"]))

    return allowed


def verify_numeric_claims(text: str, result: ExecutionResult) -> tuple[bool, list[str]]:
    """Reject narration containing a number the computation never produced.

    This is what makes "the model never did the math" a property of the system
    rather than a claim about a prompt.
    """
    allowed = allowed_numeric_tokens(result)
    # Years and small ordinals are formatting, not claims.
    unverified = [
        token
        for token in _numeric_tokens(text)
        if token not in allowed and not _is_benign(token)
    ]
    return (not unverified), unverified


def _is_benign(token: str) -> bool:
    try:
        value = Decimal(token)
    except InvalidOperation:
        return True
    if value == value.to_integral_value():
        integral = int(value)
        if 1900 <= integral <= 2100:      # a year
            return True
        if 0 <= integral <= 31:           # a day, a month, a small count
            return True
    return False


def _direction_noun(plan: AnalysisPlan) -> str:
    return {
        Direction.SPEND: "spending",
        Direction.INCOME: "income",
        Direction.NET: "net movement",
    }[plan.direction]


def _scope_phrase(plan: AnalysisPlan, category_names: dict[str, str]) -> str:
    filters = plan.filters
    if filters.merchants:
        return " at " + _join(filters.merchants)
    if filters.category_slugs:
        names = [category_names.get(slug, slug.title()) for slug in filters.category_slugs]
        return " on " + _join(names)
    if filters.text_query:
        return f" matching “{filters.text_query}”"
    return ""


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def build_narration(
    plan: AnalysisPlan,
    result: ExecutionResult,
    category_names: dict[str, str],
) -> str:
    """Deterministic explanation. Always available, no API key required."""
    if result.is_empty:
        return (
            f"No {_direction_noun(plan)}{_scope_phrase(plan, category_names)} was found for "
            f"{plan.date_range.label}. Either there are no matching transactions in that "
            "period, or they were filtered out by the criteria shown above."
        )

    scope = _scope_phrase(plan, category_names)
    period = plan.date_range.label
    noun = _direction_noun(plan)
    sentences: list[str] = []

    if plan.metric == Metric.COUNT:
        sentences.append(
            f"You had {plural(result.total_cents)}{scope} during {period}."
        )
    elif plan.metric == Metric.AVG:
        sentences.append(
            f"Your average transaction{scope} during {period} was "
            f"{money(result.total_cents)}, across {plural(result.transaction_count)}."
        )
    else:
        sentences.append(
            f"Your {noun}{scope} during {period} was {money(result.total_cents)} "
            f"across {plural(result.transaction_count)}."
        )

    if result.comparison is not None:
        comparison = result.comparison
        delta = abs(comparison.delta_cents)
        if comparison.delta_cents == 0:
            sentences.append(f"That is unchanged from {comparison.previous_label}.")
        else:
            word = "more" if comparison.delta_cents > 0 else "less"
            arrow = "up" if comparison.delta_cents > 0 else "down"
            pct = (
                f" ({abs(comparison.delta_pct):.1f}% {arrow})"
                if comparison.delta_pct is not None
                else ""
            )
            sentences.append(
                f"That is {money(delta)} {word}{pct} than {comparison.previous_label}, "
                f"when the same measure was {money(comparison.previous_cents)}."
            )

    if result.rows and plan.group_by is not None:
        top = result.rows[0]
        if plan.group_by in {GroupBy.MONTH, GroupBy.WEEK}:
            busiest = max(result.rows, key=lambda row: row.value_cents)
            quietest = min(result.rows, key=lambda row: row.value_cents)
            sentences.append(
                f"The highest period was {busiest.label} at {money(busiest.value_cents)}; "
                f"the lowest was {quietest.label} at {money(quietest.value_cents)}."
            )
        elif result.total_cents:
            share = abs(top.value_cents) / abs(result.total_cents) * 100
            sentences.append(
                f"{top.label} was the largest at {money(top.value_cents)}, "
                f"{share:.1f}% of the total, over {plural(top.transaction_count)}."
            )

    if plan.intent == Intent.RECURRING and result.rows:
        sentences.append(
            f"{len(result.rows)} merchants charged you in at least three separate months. "
            "This shows what recurred — it does not show whether you used any of them."
        )

    return " ".join(sentences)


def advice_response(question: str) -> str:
    """Answer for a question asking what the user should do."""
    return (
        "Ledger AI analyses the transaction data you upload — it doesn't give financial "
        "advice or product recommendations, and it isn't a substitute for a qualified "
        "financial professional. I can tell you what you spent, where it went, how it "
        "changed over time, and which charges repeat. Try asking something like "
        "“how much did I spend on groceries last month compared to the month before?”"
    )
