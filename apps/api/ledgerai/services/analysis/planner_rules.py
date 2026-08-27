"""Deterministic question planner.

No API key, no network, no model. Given a question and the vocabulary the user
actually has (their category and merchant names), it produces a validated
AnalysisPlan. Every decision it makes is reported in the 'understanding' step
so the user can see how their words were interpreted.

This is the default planner and remains fully functional forever — the Phase 2
LLM planner is an *addition* that falls back to this one, never a replacement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .dates import mentions_comparison, previous_period, resolve_date_phrase
from .plan import (
    AnalysisPlan,
    ChartHint,
    DateRange,
    Direction,
    Filters,
    GroupBy,
    Intent,
    Metric,
    Sort,
)

STOPWORDS = frozenset(
    """a an and the of on in for at to my me i how much did do does spend spent
    spending money show tell what which when where was were is are it that this
    last past previous next month months week weeks year years day days total
    all any some most more less than compare comparison versus vs about over
    under between during by from with please can you give list top biggest
    largest smallest highest lowest average avg mean sum count many number
    chart graph breakdown break down summary summarise summarize on
    """.split()
)

# Question-shape signals. Order matters: the first matching intent wins.
_TOP_N = re.compile(r"\b(top|biggest|largest|highest|most expensive|worst|ranked?)\b")
_TREND = re.compile(r"\b(trend|over time|each month|per month|monthly|by month|month by month|"
                    r"weekly|per week|each week|history|trajectory)\b")
_BREAKDOWN = re.compile(r"\b(breakdown|break down|by category|per category|by merchant|"
                        r"where.*(?:go|going)|categor|split|distribution|pie)\b")
_SEARCH = re.compile(r"\b(show|list|find|which transactions|what transactions|all my|"
                     r"search|look up|transactions? (?:for|from|at))\b")
_RECURRING = re.compile(r"\b(recurring|subscriptions?|repeat|every month|monthly charges?|"
                        r"regular(?:ly)? charge)\b")
_AVERAGE = re.compile(r"\b(average|avg|mean|typical|per transaction)\b")
_COUNT = re.compile(r"\b(how many|number of|count|times)\b")
_INCOME = re.compile(r"\b(income|earn|earned|paid me|salary|paycheck|deposits?|"
                     r"money (?:in|coming in)|revenue)\b")
_NET = re.compile(r"\b(net|cash ?flow|in and out|saved|savings rate)\b")
_TOP_N_NUMBER = re.compile(r"\btop\s+(\d{1,2})\b")
_INCLUDE_TRANSFERS = re.compile(r"\b(includ\w*\s+transfers?|with\s+transfers?|"
                                r"transfers?\s+included)\b")


@dataclass(slots=True)
class UserVocabulary:
    """What this user's data actually contains.

    Matching against the real vocabulary (rather than a fixed word list) is
    what lets "how much on Blue Bottle" resolve to a merchant filter without
    any model involved.
    """

    category_slugs: dict[str, str] = field(default_factory=dict)  # slug -> display name
    merchants: list[str] = field(default_factory=list)
    account_names: dict[str, str] = field(default_factory=dict)  # id -> name


@dataclass(slots=True)
class PlanExplanation:
    """The inspectable trace shown in the 'understanding' step."""

    matched_intent: str
    matched_period: str
    matched_filters: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


def _tokens(question: str) -> list[str]:
    words = re.findall(r"[a-z0-9']+", question.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]


def _match_categories(
    question: str, vocab: UserVocabulary, claimed_by_merchant: str = ""
) -> list[str]:
    """Match category display names and a few natural synonyms.

    `claimed_by_merchant` holds the text of merchants already matched. A word
    inside a merchant name must not also trigger its category: "Blue Bottle
    Coffee" should filter to that merchant, not to the whole Dining category.
    """
    text = question.lower()
    synonyms = {
        "groceries": ("grocery", "groceries", "supermarket", "food shopping"),
        "dining": ("dining", "restaurant", "restaurants", "eating out", "takeout",
                   "take out", "coffee", "lunch", "dinner", "food delivery"),
        "transport": ("transport", "transit", "gas", "fuel", "commute", "commuting",
                      "rideshare", "uber", "parking", "car"),
        "shopping": ("shopping", "retail", "clothes", "clothing", "amazon"),
        "subscriptions": ("subscription", "subscriptions", "streaming", "memberships"),
        "utilities": ("utility", "utilities", "electric", "internet", "phone bill"),
        "housing": ("housing", "rent", "mortgage", "housing costs"),
        "health": ("health", "medical", "pharmacy", "fitness", "gym", "doctor"),
        "entertainment": ("entertainment", "movies", "games", "concerts", "fun"),
        "travel": ("travel", "flights", "hotels", "vacation", "trips"),
        "income": ("income", "salary", "paycheck", "earnings"),
        "transfers": ("transfers", "payments"),
        "fees": ("fees", "charges", "bank fees"),
    }
    claimed = claimed_by_merchant.lower()
    matched: list[str] = []
    for slug, display in vocab.category_slugs.items():
        needles = {display.lower(), slug.replace("-", " ")} | set(synonyms.get(slug, ()))
        hit = next(
            (n for n in needles if n and re.search(rf"\b{re.escape(n)}\b", text)), None
        )
        if hit and hit not in claimed:
            matched.append(slug)
    return matched


def _match_merchants(question: str, vocab: UserVocabulary) -> list[str]:
    """Match merchant names present in the user's own data.

    Requires a whole-word match on a token of at least 4 characters so that
    short or generic merchant names cannot swallow ordinary English.
    """
    text = question.lower()
    matched: list[str] = []
    for merchant in vocab.merchants:
        lowered = merchant.lower()
        if len(lowered) >= 4 and re.search(rf"\b{re.escape(lowered)}\b", text):
            matched.append(merchant)
            continue
        head = lowered.split()[0] if lowered.split() else ""
        if len(head) >= 5 and re.search(rf"\b{re.escape(head)}\b", text):
            matched.append(merchant)
    return matched[:20]


def _residual_text_query(question: str, vocab: UserVocabulary, used: list[str]) -> str | None:
    """Leftover meaningful words become a description search."""
    consumed = {w for phrase in used for w in phrase.lower().split()}
    remaining = [t for t in _tokens(question) if t not in consumed]
    return " ".join(remaining[:4]) if remaining else None


class RulePlanner:
    """Deterministic planner. `name` is recorded on the run for transparency."""

    name = "rules"

    def plan(
        self,
        question: str,
        vocab: UserVocabulary,
        today: date,
    ) -> tuple[AnalysisPlan, PlanExplanation]:
        text = question.lower().strip()

        # --- period ---------------------------------------------------------
        period = resolve_date_phrase(text, today)
        wants_comparison = mentions_comparison(text)
        compare_to: DateRange | None = previous_period(period) if wants_comparison else None

        # --- direction ------------------------------------------------------
        if _NET.search(text):
            direction = Direction.NET
        elif _INCOME.search(text):
            direction = Direction.INCOME
        else:
            direction = Direction.SPEND

        # --- filters --------------------------------------------------------
        # Merchants first: a merchant name may contain a category synonym, and
        # the more specific filter should win.
        merchants = _match_merchants(text, vocab)
        categories = _match_categories(text, vocab, " ".join(merchants))
        matched_filters = [f"category: {vocab.category_slugs.get(c, c)}" for c in categories]
        matched_filters += [f"merchant: {m}" for m in merchants]

        # --- intent + shape -------------------------------------------------
        assumptions: list[str] = []
        metric = Metric.SUM
        if _AVERAGE.search(text):
            metric = Metric.AVG
        elif _COUNT.search(text):
            metric = Metric.COUNT

        if _RECURRING.search(text):
            intent, group_by, chart = Intent.RECURRING, GroupBy.MERCHANT, ChartHint.BAR
            matched_intent = "repeating charges"
        elif _TOP_N.search(text):
            intent = Intent.TOP_N
            group_by = GroupBy.CATEGORY if categories or "categor" in text else GroupBy.MERCHANT
            chart = ChartHint.BAR
            matched_intent = "ranked list"
        elif _TREND.search(text):
            intent = Intent.TREND
            group_by = GroupBy.WEEK if re.search(r"\bweek", text) else GroupBy.MONTH
            chart = ChartHint.LINE
            matched_intent = "trend over time"
        elif _BREAKDOWN.search(text):
            intent = Intent.BREAKDOWN
            group_by = GroupBy.MERCHANT if "merchant" in text else GroupBy.CATEGORY
            chart = ChartHint.PIE if "pie" in text else ChartHint.BAR
            matched_intent = "breakdown"
        elif _SEARCH.search(text):
            intent, group_by, chart = Intent.SEARCH, None, ChartHint.NONE
            matched_intent = "transaction search"
        elif wants_comparison:
            intent = Intent.COMPARISON
            group_by = GroupBy.CATEGORY if not (categories or merchants) else None
            chart = ChartHint.BAR
            matched_intent = "period comparison"
        else:
            intent, group_by, chart = Intent.TOTAL, None, ChartHint.NONE
            matched_intent = "single total"

        # A comparison always needs a baseline, even if the wording was vague.
        if intent == Intent.COMPARISON and compare_to is None:
            compare_to = previous_period(period)
            assumptions.append(f"Compared against {compare_to.label}.")

        # A bare total is more useful with its previous period beside it.
        if intent == Intent.TOTAL and compare_to is not None:
            intent = Intent.COMPARISON
            matched_intent = "period comparison"

        # A free-text description filter only makes sense for a search. Deriving
        # one for a trend or breakdown would silently filter the aggregate down
        # to rows whose description happens to contain a word from the question.
        text_query = None
        if intent == Intent.SEARCH and not categories and not merchants:
            text_query = _residual_text_query(text, vocab, [])
            if text_query:
                matched_filters.append(f"description contains: “{text_query}”")

        filters = Filters(
            category_slugs=categories,
            merchants=merchants,
            text_query=text_query,
            exclude_transfers=(
                not _INCLUDE_TRANSFERS.search(text) and direction != Direction.NET
            ),
        )

        limit = 25
        if match := _TOP_N_NUMBER.search(text):
            limit = max(1, min(100, int(match.group(1))))
        elif intent == Intent.TOP_N:
            limit = 10
            assumptions.append("Showing the top 10 by default.")
        elif intent == Intent.SEARCH:
            limit = 50

        if not categories and not merchants and not text_query:
            assumptions.append("No category or merchant named — including all spending.")
        if filters.exclude_transfers:
            assumptions.append(
                "Transfers and card payments excluded so money moved between your own "
                "accounts isn't counted as spending."
            )
        if "no period specified" in period.label:
            assumptions.append("No time period named — defaulted to the last 30 days.")

        plan = AnalysisPlan(
            intent=intent,
            direction=direction,
            date_range=period,
            compare_to=compare_to,
            filters=filters,
            group_by=group_by,
            metric=metric,
            sort=Sort.TIME_ASC if intent == Intent.TREND else Sort.VALUE_DESC,
            limit=limit,
            chart_hint=chart,
        )
        explanation = PlanExplanation(
            matched_intent=matched_intent,
            matched_period=f"{period.label} ({period.start} to {period.end})",
            matched_filters=matched_filters or ["none — all transactions in range"],
            assumptions=assumptions,
        )
        return plan, explanation
