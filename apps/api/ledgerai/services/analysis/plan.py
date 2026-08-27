"""The AnalysisPlan contract.

This is the single most important type in Ledger AI. Everything a question can
ask for must be expressible here, and nothing outside this vocabulary can ever
reach the database.

Why it exists: an LLM that emits SQL, or an LLM that emits an answer, can both
produce a number nobody can check. An LLM that emits *this* — a small,
closed, validated struct — cannot. The executor compiles it into a
parameterized SQLAlchemy query; the arithmetic is Postgres's.

Phase 1 fills this in with the deterministic RulePlanner. Phase 2 lets an LLM
propose one instead, validated by exactly the same model, with a fallback to
the RulePlanner when validation fails.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Intent(StrEnum):
    TOTAL = "total"             # "how much did I spend on X"
    BREAKDOWN = "breakdown"     # "spending by category"
    TREND = "trend"             # "spending over time"
    COMPARISON = "comparison"   # "this month vs last month"
    TOP_N = "top_n"             # "biggest merchants"
    SEARCH = "search"           # "show me all my coffee purchases"
    RECURRING = "recurring"     # "what charges repeat every month"


class GroupBy(StrEnum):
    CATEGORY = "category"
    MERCHANT = "merchant"
    MONTH = "month"
    WEEK = "week"
    DAY_OF_WEEK = "day_of_week"
    ACCOUNT = "account"


class Metric(StrEnum):
    SUM = "sum"
    AVG = "avg"
    COUNT = "count"
    MAX = "max"
    MIN = "min"


class Sort(StrEnum):
    VALUE_DESC = "value_desc"
    VALUE_ASC = "value_asc"
    TIME_ASC = "time_asc"


class ChartHint(StrEnum):
    BAR = "bar"
    LINE = "line"
    PIE = "pie"
    AREA = "area"
    NONE = "none"


class Direction(StrEnum):
    """Which side of the ledger the question is about."""

    SPEND = "spend"      # outflows only (amount < 0)
    INCOME = "income"    # inflows only (amount > 0)
    NET = "net"          # everything


class DateRange(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: date
    end: date
    label: str = Field(description="Human phrase this range came from, e.g. 'last month'")

    @model_validator(mode="after")
    def _ordered(self) -> DateRange:
        if self.end < self.start:
            raise ValueError("end date must not precede start date")
        return self


class Filters(BaseModel):
    model_config = ConfigDict(frozen=True)

    category_slugs: list[str] = Field(default_factory=list, max_length=20)
    merchants: list[str] = Field(default_factory=list, max_length=20)
    account_ids: list[str] = Field(default_factory=list, max_length=10)
    text_query: str | None = Field(default=None, max_length=120)
    min_amount_cents: int | None = None
    max_amount_cents: int | None = None
    exclude_transfers: bool = Field(
        default=True,
        description=(
            "Transfers and credit-card payments move money between the user's own "
            "accounts. Counting them as spending double-counts every purchase."
        ),
    )

    @property
    def is_empty(self) -> bool:
        return not (
            self.category_slugs
            or self.merchants
            or self.account_ids
            or self.text_query
            or self.min_amount_cents is not None
            or self.max_amount_cents is not None
        )


class AnalysisPlan(BaseModel):
    """A fully-resolved, executable description of one analysis.

    `model_config.extra = "forbid"` matters: when the Phase 2 LLM planner
    hallucinates a field, validation fails loudly and we fall back, rather than
    silently ignoring an instruction the user can see in the step payload.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: Intent
    direction: Direction = Direction.SPEND
    date_range: DateRange
    compare_to: DateRange | None = None
    filters: Filters = Field(default_factory=Filters)
    group_by: GroupBy | None = None
    metric: Metric = Metric.SUM
    sort: Sort = Sort.VALUE_DESC
    limit: int = Field(default=25, ge=1, le=100)
    chart_hint: ChartHint = ChartHint.BAR

    @model_validator(mode="after")
    def _coherent(self) -> AnalysisPlan:
        if self.intent == Intent.COMPARISON and self.compare_to is None:
            raise ValueError("comparison intent requires compare_to")
        if self.intent in {Intent.BREAKDOWN, Intent.TOP_N} and self.group_by is None:
            raise ValueError(f"{self.intent} intent requires group_by")
        if self.intent == Intent.TREND and self.group_by not in {GroupBy.MONTH, GroupBy.WEEK}:
            raise ValueError("trend intent must group by month or week")
        return self

    def fingerprint(self) -> str:
        """Stable hash of the plan, used as part of the analysis cache key."""
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]

    def describe(self) -> str:
        """One-line summary rendered in the 'understanding' step."""
        what = {
            Intent.TOTAL: "Total",
            Intent.BREAKDOWN: "Breakdown",
            Intent.TREND: "Trend",
            Intent.COMPARISON: "Period comparison",
            Intent.TOP_N: f"Top {self.limit}",
            Intent.SEARCH: "Matching transactions",
            Intent.RECURRING: "Repeating charges",
        }[self.intent]
        parts = [f"{what} of {self.direction.value}"]
        if self.group_by:
            parts.append(f"grouped by {self.group_by.value}")
        parts.append(f"for {self.date_range.label}")
        if self.compare_to:
            parts.append(f"compared with {self.compare_to.label}")
        return " ".join(parts)
