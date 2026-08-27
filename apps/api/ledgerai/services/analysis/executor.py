"""Plan execution: the only place Ledger AI computes a number.

Every figure the user sees comes from a parameterized SQLAlchemy aggregate
built from a validated AnalysisPlan. No model-authored SQL is ever executed,
and no language model is ever asked to add, average or compare anything.

The user predicate is applied in `_base_conditions`, which every query path
goes through — a route cannot forget it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Select, cast, func, literal_column, or_, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.sqltypes import Numeric

from ...models import Account, Category, Transaction
from .plan import AnalysisPlan, DateRange, Direction, GroupBy, Intent, Metric, Sort

# Money moved between a user's own accounts is not spending.
TRANSFER_SLUGS = ("transfers",)
RECURRING_MIN_MONTHS = 3


@dataclass(slots=True)
class GroupedRow:
    label: str
    value_cents: int
    transaction_count: int
    key: str | None = None
    color: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "label": self.label,
            "value_cents": self.value_cents,
            "value": round(self.value_cents / 100, 2),
            "transaction_count": self.transaction_count,
        }
        if self.key:
            payload["key"] = self.key
        if self.color:
            payload["color"] = self.color
        payload.update(self.extra)
        return payload


@dataclass(slots=True)
class ComparisonResult:
    current_cents: int
    previous_cents: int
    current_label: str
    previous_label: str

    @property
    def delta_cents(self) -> int:
        return self.current_cents - self.previous_cents

    @property
    def delta_pct(self) -> float | None:
        if self.previous_cents == 0:
            return None
        return round((self.delta_cents / abs(self.previous_cents)) * 100, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_cents": self.current_cents,
            "previous_cents": self.previous_cents,
            "current": round(self.current_cents / 100, 2),
            "previous": round(self.previous_cents / 100, 2),
            "current_label": self.current_label,
            "previous_label": self.previous_label,
            "delta_cents": self.delta_cents,
            "delta": round(self.delta_cents / 100, 2),
            "delta_pct": self.delta_pct,
            "direction": (
                "up" if self.delta_cents > 0 else "down" if self.delta_cents < 0 else "flat"
            ),
        }


@dataclass(slots=True)
class ExecutionResult:
    """Everything computed for one question. Serialized verbatim into the
    analysis step payloads, so the UI can show the user exactly this."""

    total_cents: int = 0
    transaction_count: int = 0
    rows: list[GroupedRow] = field(default_factory=list)
    comparison: ComparisonResult | None = None
    supporting: list[dict[str, Any]] = field(default_factory=list)
    metric_label: str = "Total"
    sql_description: str = ""
    sql_text: str = ""
    caveats: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.transaction_count == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_cents": self.total_cents,
            "total": round(self.total_cents / 100, 2),
            "transaction_count": self.transaction_count,
            "rows": [row.as_dict() for row in self.rows],
            "comparison": self.comparison.as_dict() if self.comparison else None,
            "metric_label": self.metric_label,
            "caveats": self.caveats,
        }


# --------------------------------------------------------------------------
# Query construction
# --------------------------------------------------------------------------


def _base_conditions(
    user_id: uuid.UUID, plan: AnalysisPlan, period: DateRange
) -> list[Any]:
    """The predicate list every query path shares.

    The user_id term is first and unconditional — this is the single point of
    enforcement for data isolation in analysis.
    """
    conditions: list[Any] = [
        Transaction.user_id == user_id,
        Transaction.posted_date >= period.start,
        Transaction.posted_date <= period.end,
    ]

    if plan.direction == Direction.SPEND:
        conditions.append(Transaction.amount_cents < 0)
    elif plan.direction == Direction.INCOME:
        conditions.append(Transaction.amount_cents > 0)

    filters = plan.filters
    if filters.category_slugs:
        conditions.append(Category.slug.in_(filters.category_slugs))
    if filters.merchants:
        conditions.append(Transaction.merchant.in_(filters.merchants))
    if filters.account_ids:
        conditions.append(Transaction.account_id.in_([uuid.UUID(a) for a in filters.account_ids]))
    if filters.text_query:
        needle = f"%{filters.text_query.lower()}%"
        conditions.append(
            or_(
                func.lower(Transaction.normalized_description).like(needle),
                func.lower(Transaction.merchant).like(needle),
            )
        )
    if filters.min_amount_cents is not None:
        conditions.append(func.abs(Transaction.amount_cents) >= filters.min_amount_cents)
    if filters.max_amount_cents is not None:
        conditions.append(func.abs(Transaction.amount_cents) <= filters.max_amount_cents)
    if filters.exclude_transfers:
        conditions.append(
            or_(Category.slug.is_(None), Category.slug.notin_(TRANSFER_SLUGS))
        )
    return conditions


def _amount_expr(plan: AnalysisPlan):
    """Spending is reported as a positive magnitude; net keeps its sign."""
    if plan.direction == Direction.NET:
        return Transaction.amount_cents
    return func.abs(Transaction.amount_cents)


def _metric_expr(plan: AnalysisPlan):
    amount = _amount_expr(plan)
    match plan.metric:
        case Metric.SUM:
            return func.coalesce(func.sum(amount), 0)
        case Metric.AVG:
            return func.coalesce(cast(func.avg(amount), Numeric(18, 0)), 0)
        case Metric.COUNT:
            return func.count(Transaction.id)
        case Metric.MAX:
            return func.coalesce(func.max(amount), 0)
        case Metric.MIN:
            return func.coalesce(func.min(amount), 0)


def _group_expr(group_by: GroupBy):
    match group_by:
        case GroupBy.CATEGORY:
            return func.coalesce(Category.name, "Uncategorized")
        case GroupBy.MERCHANT:
            return Transaction.merchant
        case GroupBy.MONTH:
            return func.to_char(func.date_trunc("month", Transaction.posted_date), "YYYY-MM")
        case GroupBy.WEEK:
            return func.to_char(func.date_trunc("week", Transaction.posted_date), "YYYY-MM-DD")
        case GroupBy.DAY_OF_WEEK:
            return func.to_char(Transaction.posted_date, "Day")
        case GroupBy.ACCOUNT:
            return Account.name


def _with_joins(stmt: Select, plan: AnalysisPlan) -> Select:
    """Outer-join so uncategorized rows are never silently dropped."""
    stmt = stmt.outerjoin(Category, Transaction.category_id == Category.id)
    if plan.group_by == GroupBy.ACCOUNT:
        stmt = stmt.join(Account, Transaction.account_id == Account.id)
    return stmt


def _render_sql(stmt: Select) -> str:
    """Compile the statement for display in the inspectable step payload."""
    try:
        return str(
            stmt.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
    except Exception:  # noqa: BLE001 - display only; never fail an analysis for this
        return str(stmt.compile(dialect=postgresql.dialect()))


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


async def _scalar_total(
    session: AsyncSession, user_id: uuid.UUID, plan: AnalysisPlan, period: DateRange
) -> tuple[int, int, str]:
    stmt = select(
        _metric_expr(plan).label("value"), func.count(Transaction.id).label("n")
    ).select_from(Transaction)
    stmt = _with_joins(stmt, plan).where(*_base_conditions(user_id, plan, period))
    row = (await session.execute(stmt)).one()
    return int(row.value or 0), int(row.n or 0), _render_sql(stmt)


async def _grouped(
    session: AsyncSession,
    user_id: uuid.UUID,
    plan: AnalysisPlan,
    period: DateRange,
) -> tuple[list[GroupedRow], str]:
    assert plan.group_by is not None
    group = _group_expr(plan.group_by).label("bucket")
    value = _metric_expr(plan).label("value")
    count = func.count(Transaction.id).label("n")
    color = func.max(func.coalesce(Category.color, "#64748b")).label("color")

    stmt = select(group, value, count, color).select_from(Transaction)
    stmt = _with_joins(stmt, plan).where(*_base_conditions(user_id, plan, period)).group_by(group)

    if plan.intent == Intent.RECURRING:
        months = func.count(func.distinct(func.date_trunc("month", Transaction.posted_date)))
        stmt = stmt.having(months >= RECURRING_MIN_MONTHS)

    stmt = stmt.order_by(
        literal_column("bucket").asc()
        if plan.sort == Sort.TIME_ASC
        else literal_column("value").asc()
        if plan.sort == Sort.VALUE_ASC
        else literal_column("value").desc()
    ).limit(plan.limit)

    rows = (await session.execute(stmt)).all()
    grouped = [
        GroupedRow(
            label=str(row.bucket).strip(),
            value_cents=int(row.value or 0),
            transaction_count=int(row.n or 0),
            key=str(row.bucket).strip(),
            color=row.color,
        )
        for row in rows
    ]
    return grouped, _render_sql(stmt)


async def _supporting_transactions(
    session: AsyncSession,
    user_id: uuid.UUID,
    plan: AnalysisPlan,
    period: DateRange,
    limit: int,
) -> list[dict[str, Any]]:
    """The rows behind the number, so the user can audit the total."""
    stmt = (
        select(
            Transaction.id,
            Transaction.posted_date,
            Transaction.merchant,
            Transaction.amount_cents,
            Transaction.raw_description,
            Transaction.needs_review,
            func.coalesce(Category.name, "Uncategorized").label("category"),
            func.coalesce(Category.color, "#64748b").label("color"),
        )
        .select_from(Transaction)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(*_base_conditions(user_id, plan, period))
        .order_by(func.abs(Transaction.amount_cents).desc(), Transaction.posted_date.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": str(row.id),
            "posted_date": row.posted_date.isoformat(),
            "merchant": row.merchant,
            "description": row.raw_description,
            "category": row.category,
            "color": row.color,
            "amount_cents": row.amount_cents,
            "amount": round(row.amount_cents / 100, 2),
            "needs_review": row.needs_review,
        }
        for row in rows
    ]


def _fill_time_gaps(rows: list[GroupedRow], plan: AnalysisPlan) -> list[GroupedRow]:
    """A month with no spending is a real data point — show the zero.

    Without this, a trend line silently skips empty months and implies
    continuity that doesn't exist.
    """
    if plan.group_by != GroupBy.MONTH or not rows:
        return rows

    existing = {row.label: row for row in rows}
    filled: list[GroupedRow] = []
    cursor = date(plan.date_range.start.year, plan.date_range.start.month, 1)
    end = date(plan.date_range.end.year, plan.date_range.end.month, 1)
    while cursor <= end:
        label = cursor.strftime("%Y-%m")
        filled.append(
            existing.get(
                label,
                GroupedRow(label=label, value_cents=0, transaction_count=0, key=label),
            )
        )
        cursor = date(cursor.year + (cursor.month // 12), cursor.month % 12 + 1, 1)
    return filled


METRIC_LABELS = {
    Metric.SUM: "Total",
    Metric.AVG: "Average",
    Metric.COUNT: "Count",
    Metric.MAX: "Largest",
    Metric.MIN: "Smallest",
}


async def execute_plan(
    session: AsyncSession, user_id: uuid.UUID, plan: AnalysisPlan
) -> ExecutionResult:
    """Run a validated plan and return every number the answer will contain."""
    result = ExecutionResult(metric_label=METRIC_LABELS[plan.metric])

    total, count, sql = await _scalar_total(session, user_id, plan, plan.date_range)
    result.total_cents = total
    result.transaction_count = count
    result.sql_text = sql

    direction_word = {
        Direction.SPEND: "spending",
        Direction.INCOME: "income",
        Direction.NET: "net movement",
    }[plan.direction]

    if plan.group_by is not None:
        rows, grouped_sql = await _grouped(session, user_id, plan, plan.date_range)
        result.rows = _fill_time_gaps(rows, plan)
        result.sql_text = grouped_sql
        result.sql_description = (
            f"{plan.metric.value.upper()}(amount) over {direction_word}, "
            f"GROUP BY {plan.group_by.value}, "
            f"{plan.date_range.start} to {plan.date_range.end}"
        )
    else:
        result.sql_description = (
            f"{plan.metric.value.upper()}(amount) over {direction_word}, "
            f"{plan.date_range.start} to {plan.date_range.end}"
        )

    if plan.compare_to is not None:
        previous_total, _, _ = await _scalar_total(session, user_id, plan, plan.compare_to)
        result.comparison = ComparisonResult(
            current_cents=total,
            previous_cents=previous_total,
            current_label=plan.date_range.label,
            previous_label=plan.compare_to.label,
        )

    supporting_limit = plan.limit if plan.intent == Intent.SEARCH else 10
    result.supporting = await _supporting_transactions(
        session, user_id, plan, plan.date_range, supporting_limit
    )

    if plan.filters.exclude_transfers:
        result.caveats.append(
            "Transfers and credit-card payments are excluded so money moved between "
            "your own accounts isn't counted as spending."
        )
    if plan.intent == Intent.RECURRING:
        result.caveats.append(
            "This shows charges that repeat across at least "
            f"{RECURRING_MIN_MONTHS} different months. Transaction data can show that "
            "a subscription was charged — it cannot show whether you used it."
        )
    return result


async def load_vocabulary(
    session: AsyncSession, user_id: uuid.UUID
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Distinct categories, merchants and accounts belonging to this user.

    This is also the exact (and only) vocabulary the Phase 2 LLM planner is
    given — names, never amounts, dates or account numbers.
    """
    category_rows = (
        await session.execute(
            select(Category.slug, Category.name)
            .join(Transaction, Transaction.category_id == Category.id)
            .where(Transaction.user_id == user_id)
            .group_by(Category.slug, Category.name)
        )
    ).all()

    merchant_rows = (
        await session.execute(
            select(Transaction.merchant, func.count(Transaction.id).label("n"))
            .where(Transaction.user_id == user_id)
            .group_by(Transaction.merchant)
            .order_by(func.count(Transaction.id).desc())
            .limit(300)
        )
    ).all()

    account_rows = (
        await session.execute(select(Account.id, Account.name).where(Account.user_id == user_id))
    ).all()

    return (
        {row.slug: row.name for row in category_rows},
        [row.merchant for row in merchant_rows],
        {str(row.id): row.name for row in account_rows},
    )


async def data_watermark(session: AsyncSession, user_id: uuid.UUID) -> str:
    """Latest change to this user's transactions.

    Folded into the analysis cache key so an edit invalidates cached answers
    automatically — no manual cache busting anywhere in the codebase.
    """
    row = (
        await session.execute(
            select(
                func.coalesce(func.max(Transaction.updated_at), func.now()),
                func.count(Transaction.id),
            ).where(Transaction.user_id == user_id)
        )
    ).one()
    return f"{row[0].isoformat()}:{row[1]}"


async def count_matching(
    session: AsyncSession, user_id: uuid.UUID, plan: AnalysisPlan
) -> tuple[int, date | None, date | None, str]:
    """Selection-step query: how many transactions the filters actually match.

    Run separately from the aggregate so the 'selecting transactions' step
    reports a real, independently-checkable number rather than a narrative.
    """
    stmt = select(
        func.count(Transaction.id).label("n"),
        func.min(Transaction.posted_date).label("first"),
        func.max(Transaction.posted_date).label("last"),
    ).select_from(Transaction)
    stmt = _with_joins(stmt, plan).where(*_base_conditions(user_id, plan, plan.date_range))
    row = (await session.execute(stmt)).one()
    return int(row.n or 0), row.first, row.last, _render_sql(stmt)
