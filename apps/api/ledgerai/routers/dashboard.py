"""Dashboard aggregates.

Every figure here is a SQL aggregate over the caller's own transactions.
Transfers and credit-card payments are excluded from "spending" so money moved
between the user's own accounts isn't counted as a purchase.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Select, func, or_, select

from ..deps import CurrentUser, DbSession
from ..models import Account, Category, Transaction
from ..services.analysis.dates import month_bounds, shift_month
from ..services.analysis.executor import TRANSFER_SLUGS

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

TREND_MONTHS = 12
RECENT_LIMIT = 8


class CategorySlice(BaseModel):
    label: str
    slug: str
    value: float
    value_cents: int
    color: str
    transaction_count: int


class TrendPoint(BaseModel):
    label: str
    month: str
    value: float
    value_cents: int


class RecentTransaction(BaseModel):
    id: str
    posted_date: date
    merchant: str
    amount: float
    amount_cents: int
    category: str
    color: str
    needs_review: bool


class DashboardOut(BaseModel):
    period_label: str
    total_spend: float
    total_spend_cents: int
    previous_spend: float
    previous_spend_cents: int
    delta_cents: int
    delta_pct: float | None
    delta_direction: str
    transaction_count: int
    total_income: float
    total_income_cents: int
    net_cents: int
    by_category: list[CategorySlice]
    trend: list[TrendPoint]
    recent: list[RecentTransaction]
    needs_review_count: int
    account_count: int
    earliest_transaction: date | None
    latest_transaction: date | None
    alerts_enabled: bool = False
    alerts_note: str = (
        "Duplicate and unusual-charge detection is a Phase 2 feature and is not "
        "active yet."
    )


def _spend_conditions(user_id, start: date, end: date) -> list:  # noqa: ANN001
    """Outflows only, transfers excluded, always scoped to one user."""
    return [
        Transaction.user_id == user_id,
        Transaction.posted_date >= start,
        Transaction.posted_date <= end,
        Transaction.amount_cents < 0,
        or_(Category.slug.is_(None), Category.slug.notin_(TRANSFER_SLUGS)),
    ]


def _with_category(stmt: Select) -> Select:
    return stmt.select_from(Transaction).outerjoin(
        Category, Transaction.category_id == Category.id
    )


@router.get("", response_model=DashboardOut)
async def get_dashboard(user: CurrentUser, session: DbSession) -> DashboardOut:
    bounds = (
        await session.execute(
            select(
                func.min(Transaction.posted_date), func.max(Transaction.posted_date)
            ).where(Transaction.user_id == user.id)
        )
    ).one()
    earliest, latest = bounds[0], bounds[1]

    # Anchor on the latest month that actually has data, so a demo dataset
    # never shows an empty "this month".
    anchor = latest or date.today()
    current_start, current_end = month_bounds(anchor.year, anchor.month)
    previous_anchor = shift_month(current_start, -1)
    previous_start, previous_end = month_bounds(previous_anchor.year, previous_anchor.month)

    current = (
        await session.execute(
            _with_category(
                select(
                    func.coalesce(func.sum(func.abs(Transaction.amount_cents)), 0),
                    func.count(Transaction.id),
                )
            ).where(*_spend_conditions(user.id, current_start, current_end))
        )
    ).one()

    previous_total = (
        await session.execute(
            _with_category(
                select(func.coalesce(func.sum(func.abs(Transaction.amount_cents)), 0))
            ).where(*_spend_conditions(user.id, previous_start, previous_end))
        )
    ).scalar_one()

    income_total = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
                Transaction.user_id == user.id,
                Transaction.amount_cents > 0,
                Transaction.posted_date >= current_start,
                Transaction.posted_date <= current_end,
            )
        )
    ).scalar_one()

    category_rows = (
        await session.execute(
            _with_category(
                select(
                    func.coalesce(Category.name, "Uncategorized").label("label"),
                    func.coalesce(Category.slug, "uncategorized").label("slug"),
                    func.coalesce(Category.color, "#64748b").label("color"),
                    func.sum(func.abs(Transaction.amount_cents)).label("value"),
                    func.count(Transaction.id).label("n"),
                )
            )
            .where(*_spend_conditions(user.id, current_start, current_end))
            .group_by("label", "slug", "color")
            .order_by(func.sum(func.abs(Transaction.amount_cents)).desc())
        )
    ).all()

    trend_start = month_bounds(
        *shift_month(current_start, -(TREND_MONTHS - 1)).timetuple()[:2]
    )[0]
    trend_rows = (
        await session.execute(
            _with_category(
                select(
                    func.to_char(
                        func.date_trunc("month", Transaction.posted_date), "YYYY-MM"
                    ).label("month"),
                    func.sum(func.abs(Transaction.amount_cents)).label("value"),
                )
            )
            .where(*_spend_conditions(user.id, trend_start, current_end))
            .group_by("month")
            .order_by("month")
        )
    ).all()

    recent_rows = (
        await session.execute(
            select(
                Transaction.id,
                Transaction.posted_date,
                Transaction.merchant,
                Transaction.amount_cents,
                Transaction.needs_review,
                func.coalesce(Category.name, "Uncategorized").label("category"),
                func.coalesce(Category.color, "#64748b").label("color"),
            )
            .select_from(Transaction)
            .outerjoin(Category, Transaction.category_id == Category.id)
            .where(Transaction.user_id == user.id)
            .order_by(Transaction.posted_date.desc(), Transaction.created_at.desc())
            .limit(RECENT_LIMIT)
        )
    ).all()

    review_count = (
        await session.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == user.id, Transaction.needs_review.is_(True)
            )
        )
    ).scalar_one()

    account_count = (
        await session.execute(
            select(func.count(Account.id)).where(Account.user_id == user.id)
        )
    ).scalar_one()

    total_cents = int(current[0] or 0)
    previous_cents = int(previous_total or 0)
    delta = total_cents - previous_cents
    delta_pct = round(delta / previous_cents * 100, 1) if previous_cents else None

    months = _month_labels(trend_start, current_start)
    trend_by_month = {row.month: int(row.value or 0) for row in trend_rows}

    return DashboardOut(
        period_label=current_start.strftime("%B %Y"),
        total_spend=round(total_cents / 100, 2),
        total_spend_cents=total_cents,
        previous_spend=round(previous_cents / 100, 2),
        previous_spend_cents=previous_cents,
        delta_cents=delta,
        delta_pct=delta_pct,
        delta_direction="up" if delta > 0 else "down" if delta < 0 else "flat",
        transaction_count=int(current[1] or 0),
        total_income=round(int(income_total or 0) / 100, 2),
        total_income_cents=int(income_total or 0),
        net_cents=int(income_total or 0) - total_cents,
        by_category=[
            CategorySlice(
                label=row.label,
                slug=row.slug,
                color=row.color,
                value=round(int(row.value) / 100, 2),
                value_cents=int(row.value),
                transaction_count=int(row.n),
            )
            for row in category_rows
        ],
        trend=[
            TrendPoint(
                month=month,
                label=_pretty(month),
                value=round(trend_by_month.get(month, 0) / 100, 2),
                value_cents=trend_by_month.get(month, 0),
            )
            for month in months
        ],
        recent=[
            RecentTransaction(
                id=str(row.id),
                posted_date=row.posted_date,
                merchant=row.merchant,
                amount=round(row.amount_cents / 100, 2),
                amount_cents=row.amount_cents,
                category=row.category,
                color=row.color,
                needs_review=row.needs_review,
            )
            for row in recent_rows
        ],
        needs_review_count=review_count,
        account_count=account_count,
        earliest_transaction=earliest,
        latest_transaction=latest,
    )


def _month_labels(start: date, end: date) -> list[str]:
    labels: list[str] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        labels.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month // 12), cursor.month % 12 + 1, 1)
    return labels


def _pretty(month: str) -> str:
    names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    year, index = month.split("-")
    return f"{names[int(index)]} {year[2:]}"
