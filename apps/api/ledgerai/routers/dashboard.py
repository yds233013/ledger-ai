"""Dashboard aggregates.

Every figure here is a SQL aggregate over the caller's own transactions.
Transfers and credit-card payments are excluded from "spending" so money moved
between the user's own accounts isn't counted as a purchase.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Select, case, func, or_, select

from ..deps import CurrentUser, DbSession
from ..models import (
    Account,
    Alert,
    AlertSeverity,
    AlertStatus,
    Category,
    Receipt,
    ReceiptStatus,
    Transaction,
)
from ..services.alerts import SEVERITY_INTENT
from ..services.analysis.dates import month_bounds, shift_month
from ..services.analysis.executor import TRANSFER_SLUGS

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

TREND_MONTHS = 12
RECENT_LIMIT = 8
DASHBOARD_ALERT_LIMIT = 8


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


class AlertOut(BaseModel):
    id: str
    alert_type: str
    severity: str
    severity_note: str
    message: str
    status: str
    evidence: dict
    created_at: datetime
    transaction_id: str
    transaction_merchant: str
    transaction_date: date
    transaction_amount: float


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
    base_currency: str
    excluded_currencies: dict[str, int]
    currency_note: str | None
    pending_receipt_count: int
    alerts_enabled: bool = True
    open_alert_count: int = 0
    # How many of open_alert_count this response actually carries, so the UI
    # can say "showing 8 of 30" rather than implying it has them all.
    alerts_shown: int = 0
    alerts: list[AlertOut] = []
    alerts_note: str = (
        "Alerts describe unusual patterns in your own uploaded data. They are not "
        "fraud detection and do not mean anything is wrong."
    )


def _spend_conditions(  # noqa: ANN001
    user_id, start: date, end: date, currency: str
) -> list:
    """Outflows only, transfers excluded, one user, one currency.

    The currency term is here rather than at each call site for the same reason
    user_id is: a total that mixes currencies would be meaningless, and Ledger
    AI does not convert.
    """
    return [
        Transaction.user_id == user_id,
        Transaction.currency == currency,
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
    base_currency = user.base_currency
    bounds = (
        await session.execute(
            select(
                func.min(Transaction.posted_date), func.max(Transaction.posted_date)
            ).where(
                Transaction.user_id == user.id, Transaction.currency == base_currency
            )
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
            ).where(*_spend_conditions(user.id, current_start, current_end, base_currency))
        )
    ).one()

    previous_total = (
        await session.execute(
            _with_category(
                select(func.coalesce(func.sum(func.abs(Transaction.amount_cents)), 0))
            ).where(*_spend_conditions(user.id, previous_start, previous_end, base_currency))
        )
    ).scalar_one()

    income_total = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
                Transaction.user_id == user.id,
                Transaction.currency == base_currency,
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
            .where(*_spend_conditions(user.id, current_start, current_end, base_currency))
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
            .where(*_spend_conditions(user.id, trend_start, current_end, base_currency))
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

    # --- alerts ------------------------------------------------------------
    alert_rows = (
        await session.execute(
            select(Alert, Transaction)
            .join(Transaction, Alert.transaction_id == Transaction.id)
            .where(Alert.user_id == user.id, Alert.status == AlertStatus.OPEN)
            .order_by(
                case(
                    (Alert.severity == AlertSeverity.HIGH, 0),
                    (Alert.severity == AlertSeverity.MEDIUM, 1),
                    else_=2,
                ),
                Transaction.posted_date.desc(),
            )
            .limit(DASHBOARD_ALERT_LIMIT)
        )
    ).all()

    open_alert_count = (
        await session.execute(
            select(func.count(Alert.id)).where(
                Alert.user_id == user.id, Alert.status == AlertStatus.OPEN
            )
        )
    ).scalar_one()

    pending_receipts = (
        await session.execute(
            select(func.count(Receipt.id)).where(
                Receipt.user_id == user.id,
                Receipt.status.in_([ReceiptStatus.PENDING, ReceiptStatus.NEEDS_REVIEW]),
            )
        )
    ).scalar_one()

    # --- currencies this view deliberately leaves out ----------------------
    other_currency_rows = (
        await session.execute(
            select(Transaction.currency, func.count(Transaction.id))
            .where(
                Transaction.user_id == user.id,
                Transaction.currency != base_currency,
            )
            .group_by(Transaction.currency)
        )
    ).all()
    excluded = {row[0]: int(row[1]) for row in other_currency_rows}
    currency_note = None
    if excluded:
        summary = ", ".join(f"{count} in {code}" for code, count in sorted(excluded.items()))
        currency_note = (
            f"Totals cover {base_currency} only. {summary} not included — Ledger AI "
            "does not convert between currencies."
        )

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
        base_currency=base_currency,
        excluded_currencies=excluded,
        currency_note=currency_note,
        pending_receipt_count=pending_receipts,
        open_alert_count=open_alert_count,
        alerts_shown=len(alert_rows),
        alerts=[
            AlertOut(
                id=str(alert.id),
                alert_type=alert.alert_type,
                severity=alert.severity,
                severity_note=SEVERITY_INTENT.get(AlertSeverity(alert.severity), ""),
                message=alert.message,
                status=alert.status,
                evidence=alert.evidence,
                created_at=alert.created_at,
                transaction_id=str(transaction.id),
                transaction_merchant=transaction.merchant,
                transaction_date=transaction.posted_date,
                transaction_amount=round(transaction.amount_cents / 100, 2),
            )
            for alert, transaction in alert_rows
        ],
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
