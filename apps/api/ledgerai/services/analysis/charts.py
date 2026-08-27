"""Turn an ExecutionResult into a Recharts-ready ChartSpec.

The backend decides the chart shape because the backend is the only side that
knows the data's cardinality and units. The frontend renders the spec without
making any analytical decisions of its own.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .executor import ExecutionResult
from .plan import AnalysisPlan, ChartHint, Direction, GroupBy, Intent, Metric

# Beyond this many slices a pie is unreadable; a bar chart is used instead.
MAX_PIE_SLICES = 7
DEFAULT_COLOR = "#6366f1"


@dataclass(slots=True)
class ChartSpec:
    kind: str                      # bar | line | area | pie | none
    data: list[dict[str, Any]] = field(default_factory=list)
    x_key: str = "label"
    y_key: str = "value"
    y_label: str = "Amount (USD)"
    x_label: str = ""
    title: str = ""
    value_format: str = "currency"  # currency | number
    stacked: bool = False
    colors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pretty_month(label: str) -> str:
    """2026-07 -> Jul 2026."""
    try:
        year, month = label.split("-")[:2]
        names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return f"{names[int(month)]} {year}"
    except (ValueError, IndexError):
        return label


def build_chart(plan: AnalysisPlan, result: ExecutionResult) -> ChartSpec:
    value_format = "number" if plan.metric == Metric.COUNT else "currency"
    unit = "transactions" if plan.metric == Metric.COUNT else "Amount (USD)"

    # A comparison with no grouping is best shown as two labelled bars.
    if result.comparison is not None and not result.rows:
        comparison = result.comparison
        return ChartSpec(
            kind="bar",
            data=[
                {
                    "label": comparison.previous_label.title(),
                    "value": round(comparison.previous_cents / 100, 2),
                    "color": "#94a3b8",
                },
                {
                    "label": comparison.current_label.title(),
                    "value": round(comparison.current_cents / 100, 2),
                    "color": DEFAULT_COLOR,
                },
            ],
            y_label=unit,
            title=(
                f"{result.metric_label}: {comparison.previous_label} "
                f"vs {comparison.current_label}"
            ),
            value_format=value_format,
            colors=["#94a3b8", DEFAULT_COLOR],
        )

    if not result.rows or plan.chart_hint == ChartHint.NONE:
        return ChartSpec(kind="none", title="", y_label=unit, value_format=value_format)

    is_time = plan.group_by in {GroupBy.MONTH, GroupBy.WEEK}
    data: list[dict[str, Any]] = [
        {
            "label": _pretty_month(row.label) if plan.group_by == GroupBy.MONTH else row.label,
            "value": round(row.value_cents / 100, 2),
            "count": row.transaction_count,
            "color": row.color or DEFAULT_COLOR,
        }
        for row in result.rows
    ]

    kind = plan.chart_hint.value
    if kind == "pie" and len(data) > MAX_PIE_SLICES:
        kind = "bar"
    if is_time and kind not in {"line", "area"}:
        kind = "line"

    noun = {
        Direction.SPEND: "Spending",
        Direction.INCOME: "Income",
        Direction.NET: "Net movement",
    }[plan.direction]
    grouping = plan.group_by.value.replace("_", " ") if plan.group_by else ""
    if plan.intent == Intent.TREND:
        title = f"{noun} by {grouping} — {plan.date_range.label}"
    elif plan.intent == Intent.TOP_N:
        title = f"Top {len(data)} {grouping}s by {noun.lower()} — {plan.date_range.label}"
    elif plan.intent == Intent.RECURRING:
        title = f"Charges repeating across months — {plan.date_range.label}"
    else:
        title = f"{noun} by {grouping} — {plan.date_range.label}"

    return ChartSpec(
        kind=kind,
        data=data,
        x_key="label",
        y_key="value",
        y_label=unit,
        x_label=grouping.title(),
        title=title,
        value_format=value_format,
        colors=[str(row["color"]) for row in data],
    )
