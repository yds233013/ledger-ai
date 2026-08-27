"""Follow-up questions as explicit plan refinements.

A follow-up here is a *named, pure transform* of a plan that already exists:
`(run_id, refinement_name)` fully determines the new plan. There is no pronoun
resolution, no carried conversation, and no hidden state — the refined plan is
re-validated by AnalysisPlan and shown in the understanding step exactly as a
fresh question would be.

That is the whole reason follow-ups are shaped this way. "What about last
month?" requires remembering what "that" meant; "Group this by merchant" does
not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .dates import previous_period, shift_month
from .plan import (
    AnalysisPlan,
    ChartHint,
    DateRange,
    Filters,
    GroupBy,
    Intent,
    Sort,
)


@dataclass(slots=True, frozen=True)
class Refinement:
    key: str
    label: str
    description: str
    apply: Callable[[AnalysisPlan], AnalysisPlan]


def _group_by_merchant(plan: AnalysisPlan) -> AnalysisPlan:
    return plan.model_copy(
        update={
            "intent": Intent.BREAKDOWN,
            "group_by": GroupBy.MERCHANT,
            "chart_hint": ChartHint.BAR,
            "sort": Sort.VALUE_DESC,
        }
    )


def _group_by_category(plan: AnalysisPlan) -> AnalysisPlan:
    return plan.model_copy(
        update={
            "intent": Intent.BREAKDOWN,
            "group_by": GroupBy.CATEGORY,
            "chart_hint": ChartHint.BAR,
            "sort": Sort.VALUE_DESC,
        }
    )


def _compare_previous(plan: AnalysisPlan) -> AnalysisPlan:
    return plan.model_copy(
        update={
            "intent": Intent.COMPARISON,
            "compare_to": previous_period(plan.date_range),
            "group_by": None,
            "chart_hint": ChartHint.BAR,
        }
    )


def _as_monthly_trend(plan: AnalysisPlan) -> AnalysisPlan:
    start = shift_month(plan.date_range.end, -5).replace(day=1)
    return plan.model_copy(
        update={
            "intent": Intent.TREND,
            "group_by": GroupBy.MONTH,
            "chart_hint": ChartHint.LINE,
            "sort": Sort.TIME_ASC,
            "compare_to": None,
            "date_range": DateRange(
                start=start, end=plan.date_range.end, label="the last 6 months"
            ),
        }
    )


def _widen_to_year(plan: AnalysisPlan) -> AnalysisPlan:
    end = plan.date_range.end
    return plan.model_copy(
        update={
            "date_range": DateRange(
                start=end.replace(month=1, day=1), end=end, label=f"{end.year} so far"
            ),
            "compare_to": None,
            "intent": Intent.BREAKDOWN if plan.group_by else plan.intent,
        }
    )


def _show_transactions(plan: AnalysisPlan) -> AnalysisPlan:
    return plan.model_copy(
        update={
            "intent": Intent.SEARCH,
            "group_by": None,
            "chart_hint": ChartHint.NONE,
            "compare_to": None,
            "limit": 50,
        }
    )


def _filter_to_label(
    label: str, slug: str | None, merchant: str | None
) -> Callable[[AnalysisPlan], AnalysisPlan]:
    def apply(plan: AnalysisPlan) -> AnalysisPlan:
        filters = Filters(
            category_slugs=[slug] if slug else list(plan.filters.category_slugs),
            merchants=[merchant] if merchant else list(plan.filters.merchants),
            account_ids=list(plan.filters.account_ids),
            text_query=plan.filters.text_query,
            min_amount_cents=plan.filters.min_amount_cents,
            max_amount_cents=plan.filters.max_amount_cents,
            exclude_transfers=plan.filters.exclude_transfers,
        )
        return plan.model_copy(
            update={
                "filters": filters,
                "intent": Intent.TREND if plan.intent == Intent.TREND else Intent.TOTAL,
                "group_by": GroupBy.MONTH if plan.intent == Intent.TREND else None,
                "chart_hint": (
                    ChartHint.LINE if plan.intent == Intent.TREND else ChartHint.NONE
                ),
            }
        )

    return apply


REFINEMENTS: dict[str, Refinement] = {
    "group_by_merchant": Refinement(
        key="group_by_merchant",
        label="Break down by merchant",
        description="Same period and filters, grouped by merchant instead.",
        apply=_group_by_merchant,
    ),
    "group_by_category": Refinement(
        key="group_by_category",
        label="Break down by category",
        description="Same period and filters, grouped by category instead.",
        apply=_group_by_category,
    ),
    "compare_previous_period": Refinement(
        key="compare_previous_period",
        label="Compare with the previous period",
        description="Adds the preceding period as a baseline.",
        apply=_compare_previous,
    ),
    "monthly_trend": Refinement(
        key="monthly_trend",
        label="Show the 6-month trend",
        description="Same filters, plotted by month.",
        apply=_as_monthly_trend,
    ),
    "widen_to_year": Refinement(
        key="widen_to_year",
        label="Widen to this year",
        description="Same question over the whole year so far.",
        apply=_widen_to_year,
    ),
    "show_transactions": Refinement(
        key="show_transactions",
        label="Show the transactions",
        description="List the individual transactions behind this answer.",
        apply=_show_transactions,
    ),
}


def available_refinements(plan: AnalysisPlan, top_row_label: str | None) -> list[dict]:
    """Which refinements make sense for this plan.

    Offering a refinement that would not change anything is noise, so each one
    is only surfaced when it actually alters the plan.
    """
    offered: list[str] = []

    if plan.group_by != GroupBy.MERCHANT and plan.intent != Intent.SEARCH:
        offered.append("group_by_merchant")
    if plan.group_by != GroupBy.CATEGORY and not plan.filters.category_slugs:
        offered.append("group_by_category")
    if plan.compare_to is None and plan.intent != Intent.TREND:
        offered.append("compare_previous_period")
    if plan.intent != Intent.TREND:
        offered.append("monthly_trend")
    if plan.intent != Intent.SEARCH:
        offered.append("show_transactions")
    if plan.date_range.start.year == plan.date_range.end.year and (
        plan.date_range.end - plan.date_range.start
    ).days < 200:
        offered.append("widen_to_year")

    chips = [
        {
            "key": REFINEMENTS[key].key,
            "label": REFINEMENTS[key].label,
            "description": REFINEMENTS[key].description,
        }
        for key in offered
        if key in REFINEMENTS
    ]

    # A dynamic chip for the largest row, e.g. "Only Groceries".
    if top_row_label:
        chips.append(
            {
                "key": f"only:{top_row_label}",
                "label": f"Only {top_row_label}",
                "description": f"Narrow this to {top_row_label} and drop the grouping.",
            }
        )
    return chips[:6]


def apply_refinement(
    plan: AnalysisPlan,
    key: str,
    category_slugs: dict[str, str],
    merchants: list[str],
) -> tuple[AnalysisPlan, str]:
    """Apply a named refinement, returning the new plan and its label.

    Raises KeyError for anything not in the closed set — a refinement key is
    never free text.
    """
    if key.startswith("only:"):
        label = key.split(":", 1)[1]
        slug = next((s for s, name in category_slugs.items() if name == label), None)
        merchant = next((m for m in merchants if m == label), None)
        if slug is None and merchant is None:
            raise KeyError(key)
        return _filter_to_label(label, slug, merchant)(plan), f"Only {label}"

    refinement = REFINEMENTS[key]
    return refinement.apply(plan), refinement.label


def top_row_label(result_rows: list[dict]) -> str | None:
    if not result_rows:
        return None
    label = result_rows[0].get("label")
    return str(label) if label else None


__all__ = [
    "REFINEMENTS",
    "Refinement",
    "apply_refinement",
    "available_refinements",
    "top_row_label",
]
