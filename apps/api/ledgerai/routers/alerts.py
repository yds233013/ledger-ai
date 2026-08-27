"""Alerts surface: list, inspect and act on detected patterns."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select

from ..deps import CurrentUser, DbSession
from ..models import Alert, AlertSeverity, AlertStatus, Transaction
from ..services.alerts import ALERT_DISCLAIMER, SEVERITY_INTENT

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertOut(BaseModel):
    id: uuid.UUID
    alert_type: str
    severity: str
    # What the severity means for the user, decided once in the detectors
    # rather than re-invented in the UI.
    severity_note: str
    status: str
    message: str
    evidence: dict
    created_at: datetime
    transaction_id: uuid.UUID
    transaction_merchant: str
    transaction_date: date
    transaction_amount: float
    transaction_category: str | None


class AlertListOut(BaseModel):
    items: list[AlertOut]
    open_count: int
    dismissed_count: int
    resolved_count: int
    disclaimer: str = ALERT_DISCLAIMER


class AlertUpdate(BaseModel):
    status: str = Field(pattern="^(open|dismissed|resolved)$")


def _to_out(alert, transaction, category) -> AlertOut:  # noqa: ANN001
    return AlertOut(
        id=alert.id,
        alert_type=alert.alert_type,
        severity=alert.severity,
        severity_note=SEVERITY_INTENT.get(AlertSeverity(alert.severity), ""),
        status=alert.status,
        message=alert.message,
        evidence=alert.evidence,
        created_at=alert.created_at,
        transaction_id=transaction.id,
        transaction_merchant=transaction.merchant,
        transaction_date=transaction.posted_date,
        transaction_amount=round(transaction.amount_cents / 100, 2),
        transaction_category=category.name if category else None,
    )


@router.get("", response_model=AlertListOut)
async def list_alerts(
    user: CurrentUser,
    session: DbSession,
    status_filter: str = Query(default="open", pattern="^(open|dismissed|resolved|all)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> AlertListOut:
    from ..models import Category

    query = (
        select(Alert, Transaction, Category)
        .join(Transaction, Alert.transaction_id == Transaction.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(Alert.user_id == user.id)
    )
    if status_filter != "all":
        query = query.where(Alert.status == status_filter)

    rows = (
        await session.execute(
            query.order_by(
                # Most serious first, then most recent.
                case(
                    (Alert.severity == AlertSeverity.HIGH, 0),
                    (Alert.severity == AlertSeverity.MEDIUM, 1),
                    else_=2,
                ),
                Transaction.posted_date.desc(),
            ).limit(limit)
        )
    ).all()

    count_rows = (
        await session.execute(
            select(Alert.status, func.count(Alert.id))
            .where(Alert.user_id == user.id)
            .group_by(Alert.status)
        )
    ).all()
    counts: dict[str, int] = {str(row[0]): int(row[1]) for row in count_rows}

    return AlertListOut(
        items=[
            _to_out(alert, transaction, category)
            for alert, transaction, category in rows
        ],
        open_count=counts.get(AlertStatus.OPEN.value, 0),
        dismissed_count=counts.get(AlertStatus.DISMISSED.value, 0),
        resolved_count=counts.get(AlertStatus.RESOLVED.value, 0),
    )


@router.patch("/{alert_id}", response_model=AlertOut)
async def update_alert(
    alert_id: uuid.UUID, payload: AlertUpdate, user: CurrentUser, session: DbSession
) -> AlertOut:
    from ..models import Category

    row = (
        await session.execute(
            select(Alert, Transaction, Category)
            .join(Transaction, Alert.transaction_id == Transaction.id)
            .outerjoin(Category, Transaction.category_id == Category.id)
            .where(Alert.id == alert_id, Alert.user_id == user.id)
        )
    ).first()
    if row is None:
        # 404, not 403 — another user's alert must not be shown to exist.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")

    alert, transaction, category = row
    alert.status = AlertStatus(payload.status)
    await session.commit()
    return _to_out(alert, transaction, category)
