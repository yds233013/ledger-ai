"""Transaction listing, filtering and manual correction."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import Select, and_, asc, desc, func, or_, select

from ..deps import CurrentUser, DbSession
from ..models import (
    Account,
    Category,
    CorrectionField,
    CorrectionScope,
    Transaction,
    TransactionCorrection,
)
from ..schemas.common import (
    AccountOut,
    CategoryOut,
    CorrectionImpactOut,
    Page,
    TransactionOut,
    TransactionUpdate,
    TransactionUpdateResult,
)
from ..services.corrections import apply_bulk_correction, compute_impact
from ..services.normalize import merchant_key

router = APIRouter(prefix="/api/transactions", tags=["transactions"])

SORTABLE = {
    "date": Transaction.posted_date,
    "amount": Transaction.amount_cents,
    "merchant": Transaction.merchant,
    "confidence": Transaction.confidence,
}


class FacetsOut(BaseModel):
    categories: list[CategoryOut]
    accounts: list[AccountOut]
    merchants: list[str]
    review_count: int
    total_count: int


def _serialize(row) -> TransactionOut:  # noqa: ANN001 - Row from the select below
    transaction = row.Transaction
    return TransactionOut(
        id=transaction.id,
        posted_date=transaction.posted_date,
        amount_cents=transaction.amount_cents,
        amount=round(transaction.amount_cents / 100, 2),
        currency=transaction.currency,
        merchant=transaction.merchant,
        raw_description=transaction.raw_description,
        category=(
            CategoryOut(
                id=row.category_id_out,
                name=row.category_name,
                slug=row.category_slug,
                color=row.category_color,
                icon=row.category_icon,
            )
            if row.category_id_out
            else None
        ),
        confidence=float(transaction.confidence),
        categorized_by=transaction.categorized_by,
        needs_review=transaction.needs_review,
        is_corrected=transaction.is_corrected,
        account_id=transaction.account_id,
        account_name=row.account_name,
        upload_id=transaction.upload_id,
        created_at=transaction.created_at,
    )


def _apply_filters(  # noqa: PLR0913
    stmt: Select,
    *,
    search: str | None,
    start_date: date | None,
    end_date: date | None,
    account_id: uuid.UUID | None,
    category_slug: str | None,
    merchant: str | None,
    review: str | None,
    min_amount: float | None,
    max_amount: float | None,
) -> Select:
    if search:
        needle = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Transaction.merchant).like(needle),
                func.lower(Transaction.raw_description).like(needle),
            )
        )
    if start_date:
        stmt = stmt.where(Transaction.posted_date >= start_date)
    if end_date:
        stmt = stmt.where(Transaction.posted_date <= end_date)
    if account_id:
        stmt = stmt.where(Transaction.account_id == account_id)
    if category_slug:
        stmt = (
            stmt.where(Category.id.is_(None))
            if category_slug == "uncategorized"
            else stmt.where(Category.slug == category_slug)
        )
    if merchant:
        stmt = stmt.where(Transaction.merchant == merchant)
    if review == "needs_review":
        stmt = stmt.where(Transaction.needs_review.is_(True))
    elif review == "corrected":
        stmt = stmt.where(Transaction.is_corrected.is_(True))
    elif review == "reviewed":
        stmt = stmt.where(Transaction.needs_review.is_(False))
    if min_amount is not None:
        stmt = stmt.where(func.abs(Transaction.amount_cents) >= int(min_amount * 100))
    if max_amount is not None:
        stmt = stmt.where(func.abs(Transaction.amount_cents) <= int(max_amount * 100))
    return stmt


@router.get("", response_model=Page[TransactionOut])
async def list_transactions(  # noqa: PLR0913
    user: CurrentUser,
    session: DbSession,
    search: str | None = Query(default=None, max_length=200),
    start_date: date | None = None,
    end_date: date | None = None,
    account_id: uuid.UUID | None = None,
    category_slug: str | None = Query(default=None, max_length=80),
    merchant: str | None = Query(default=None, max_length=200),
    review: str | None = Query(default=None, pattern="^(needs_review|corrected|reviewed)$"),
    min_amount: float | None = None,
    max_amount: float | None = None,
    sort: str = Query(default="date", pattern="^(date|amount|merchant|confidence)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[TransactionOut]:
    base = (
        select(
            Transaction,
            Category.id.label("category_id_out"),
            Category.name.label("category_name"),
            Category.slug.label("category_slug"),
            Category.color.label("category_color"),
            Category.icon.label("category_icon"),
            Account.name.label("account_name"),
        )
        .select_from(Transaction)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .join(Account, Transaction.account_id == Account.id)
        .where(Transaction.user_id == user.id)
    )
    filtered = _apply_filters(
        base,
        search=search,
        start_date=start_date,
        end_date=end_date,
        account_id=account_id,
        category_slug=category_slug,
        merchant=merchant,
        review=review,
        min_amount=min_amount,
        max_amount=max_amount,
    )

    count_stmt = (
        select(func.count())
        .select_from(filtered.order_by(None).subquery())
    )
    total = (await session.execute(count_stmt)).scalar_one()

    column = SORTABLE[sort]
    direction = asc if order == "asc" else desc
    rows = (
        await session.execute(
            filtered.order_by(direction(column), desc(Transaction.id)).limit(limit).offset(offset)
        )
    ).all()

    return Page[TransactionOut](
        items=[_serialize(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
    )


@router.get("/facets", response_model=FacetsOut)
async def facets(user: CurrentUser, session: DbSession) -> FacetsOut:
    """Filter-dropdown vocabulary, scoped to what this user actually has."""
    categories = (
        await session.execute(
            select(Category)
            .where(or_(Category.user_id == user.id, Category.is_system.is_(True)))
            .order_by(Category.sort_order)
        )
    ).scalars().all()

    accounts = (
        await session.execute(
            select(Account).where(Account.user_id == user.id).order_by(Account.name)
        )
    ).scalars().all()

    merchants = (
        await session.execute(
            select(Transaction.merchant)
            .where(Transaction.user_id == user.id)
            .group_by(Transaction.merchant)
            .order_by(func.count(Transaction.id).desc())
            .limit(200)
        )
    ).scalars().all()

    counts = (
        await session.execute(
            select(
                func.count(Transaction.id),
                func.count(Transaction.id).filter(Transaction.needs_review.is_(True)),
            ).where(Transaction.user_id == user.id)
        )
    ).one()

    return FacetsOut(
        categories=[CategoryOut.model_validate(c) for c in categories],
        accounts=[AccountOut.model_validate(a) for a in accounts],
        merchants=list(merchants),
        total_count=counts[0],
        review_count=counts[1],
    )


async def _load_owned(
    session: DbSession, user_id: uuid.UUID, transaction_id: uuid.UUID
) -> Transaction:
    """Fetch a transaction or 404.

    404 rather than 403 for another user's row: a 403 would confirm it exists.
    """
    transaction = (
        await session.execute(
            select(Transaction).where(
                and_(Transaction.id == transaction_id, Transaction.user_id == user_id)
            )
        )
    ).scalar_one_or_none()
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found"
        )
    return transaction


async def _resolve_category(
    session: DbSession, user_id: uuid.UUID, category_id: uuid.UUID
) -> Category:
    category = (
        await session.execute(
            select(Category).where(
                Category.id == category_id,
                or_(Category.user_id == user_id, Category.is_system.is_(True)),
            )
        )
    ).scalar_one_or_none()
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown category"
        )
    return category


async def _serialize_by_id(
    session: DbSession, user_id: uuid.UUID, transaction_id: uuid.UUID
) -> TransactionOut:
    row = (
        await session.execute(
            select(
                Transaction,
                Category.id.label("category_id_out"),
                Category.name.label("category_name"),
                Category.slug.label("category_slug"),
                Category.color.label("category_color"),
                Category.icon.label("category_icon"),
                Account.name.label("account_name"),
            )
            .select_from(Transaction)
            .outerjoin(Category, Transaction.category_id == Category.id)
            .join(Account, Transaction.account_id == Account.id)
            .where(Transaction.id == transaction_id, Transaction.user_id == user_id)
        )
    ).one()
    return _serialize(row)


@router.get("/{transaction_id}/correction-impact", response_model=CorrectionImpactOut)
async def correction_impact(
    transaction_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    category_id: uuid.UUID | None = None,
    merchant: str | None = Query(default=None, max_length=200),
) -> CorrectionImpactOut:
    """How many other transactions a bulk correction would change.

    Called before the user confirms, so the count they see is the count that
    will actually be written — computed by the same code that does the writing.
    """
    if category_id is None and merchant is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide a category_id or a merchant to preview.",
        )

    transaction = await _load_owned(session, user.id, transaction_id)

    if category_id is not None:
        await _resolve_category(session, user.id, category_id)
        field_name = CorrectionField.CATEGORY
    else:
        field_name = CorrectionField.MERCHANT

    impact = await compute_impact(
        session,
        user_id=user.id,
        transaction=transaction,
        field_name=field_name,
        new_category_id=category_id,
        new_merchant=merchant.strip()[:200] if merchant else None,
    )
    return CorrectionImpactOut(merchant=transaction.merchant, **impact.as_dict())


@router.patch("/{transaction_id}", response_model=TransactionUpdateResult)
async def update_transaction(
    transaction_id: uuid.UUID,
    payload: TransactionUpdate,
    user: CurrentUser,
    session: DbSession,
) -> TransactionUpdateResult:
    """Apply a manual correction, optionally to every matching transaction.

    Corrections are recorded in transaction_corrections, which is both the audit
    trail and the highest-priority categorization signal — so correcting a row
    teaches every later import about that merchant, whether or not the user
    also applied it retroactively.
    """
    if payload.merchant is None and payload.category_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide a merchant, a category, or both.",
        )

    transaction = await _load_owned(session, user.id, transaction_id)

    corrections: list[TransactionCorrection] = []
    impact = CorrectionImpactOut(
        merchant=transaction.merchant,
        merchant_key=transaction.merchant_key,
        matching_count=0,
        affected_count=0,
        protected_count=0,
        already_correct_count=0,
        affected_ids=[],
    )
    # An explicit edit to one row is an individual decision, and later bulk
    # changes to that merchant must not overwrite it.
    scope = CorrectionScope.BULK if payload.apply_to_matching else CorrectionScope.INDIVIDUAL
    applied_to_matching = False

    # --- category --------------------------------------------------------
    if payload.category_id is not None and payload.category_id != transaction.category_id:
        category = await _resolve_category(session, user.id, payload.category_id)
        previous = (
            await session.execute(
                select(Category.slug).where(Category.id == transaction.category_id)
            )
        ).scalar_one_or_none()

        if payload.apply_to_matching:
            bulk = await apply_bulk_correction(
                session,
                user_id=user.id,
                transaction=transaction,
                field_name=CorrectionField.CATEGORY,
                category=category,
            )
            impact = CorrectionImpactOut(merchant=transaction.merchant, **bulk.as_dict())
            applied_to_matching = True

        corrections.append(
            TransactionCorrection(
                transaction_id=transaction.id,
                user_id=user.id,
                field=CorrectionField.CATEGORY,
                old_value=previous,
                new_value=category.slug,
                merchant_key=transaction.merchant_key,
                scope=scope,
            )
        )
        transaction.category_id = category.id

    # --- merchant --------------------------------------------------------
    if payload.merchant is not None and payload.merchant.strip() != transaction.merchant:
        new_merchant = payload.merchant.strip()[:200]

        if payload.apply_to_matching:
            bulk = await apply_bulk_correction(
                session,
                user_id=user.id,
                transaction=transaction,
                field_name=CorrectionField.MERCHANT,
                new_merchant=new_merchant,
            )
            # A merchant rename may accompany a category change; report the
            # larger blast radius rather than overwriting it with a smaller one.
            if bulk.affected_count >= impact.affected_count:
                impact = CorrectionImpactOut(merchant=transaction.merchant, **bulk.as_dict())
            applied_to_matching = True

        corrections.append(
            TransactionCorrection(
                transaction_id=transaction.id,
                user_id=user.id,
                field=CorrectionField.MERCHANT,
                old_value=transaction.merchant,
                new_value=new_merchant,
                merchant_key=merchant_key(new_merchant),
                scope=scope,
            )
        )
        transaction.merchant = new_merchant
        transaction.merchant_key = merchant_key(new_merchant)

    if corrections:
        session.add_all(corrections)
        transaction.is_corrected = True
        transaction.confidence = Decimal("1.00")
        transaction.categorized_by = "correction"
        if payload.clear_review:
            transaction.needs_review = False

    await session.commit()

    return TransactionUpdateResult(
        transaction=await _serialize_by_id(session, user.id, transaction.id),
        applied_to_matching=applied_to_matching,
        impact=impact,
    )
