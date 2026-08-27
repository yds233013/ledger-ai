"""Receipt review, confirmation and image serving."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from ..deps import CurrentUser, DbSession
from ..models import Account, Category, Receipt, ReceiptStatus, Upload
from ..schemas.common import AccountOut, CategoryOut
from ..security.filenames import sanitize_filename
from ..security.validators import safe_response_content_type
from ..services.ocr.preprocess import load_pages, render_preview
from ..services.receipts import (
    SYNTHETIC_ACCOUNT_NAME,
    ReceiptError,
    confirm_create,
    confirm_link,
    find_match_candidates,
    foreign_currency_warning,
    reject_candidate,
)
from ..services.storage import StorageError, get_storage

router = APIRouter(prefix="/api/receipts", tags=["receipts"])

# Responses that carry a stored file are never cacheable and never sniffable.
FILE_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store, max-age=0",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}


class ReceiptSummary(BaseModel):
    id: uuid.UUID
    status: str
    merchant: str | None
    posted_date: date | None
    total_cents: int | None
    total: float | None
    currency: str
    ocr_confidence: float
    needs_review: bool
    page_count: int
    original_filename: str
    content_type: str
    transaction_id: uuid.UUID | None
    link_mode: str | None
    created_at: datetime


class ReceiptDetail(ReceiptSummary):
    subtotal_cents: int | None
    tax_cents: int | None
    tip_cents: int | None
    field_confidence: dict[str, float]
    parse_notes: dict[str, str]
    raw_text: str
    currency_warning: str | None
    base_currency: str
    categories: list[CategoryOut]
    # The review page must let the user choose where the transaction goes; it
    # is never silently attached to an arbitrary bank account.
    accounts: list[AccountOut]
    default_account_name: str


class ReceiptUpdate(BaseModel):
    """Manual corrections to the extracted fields, before confirming."""

    merchant: str | None = Field(default=None, max_length=200)
    posted_date: date | None = None
    subtotal_cents: int | None = Field(default=None, ge=0)
    tax_cents: int | None = Field(default=None, ge=0)
    tip_cents: int | None = Field(default=None, ge=0)
    total_cents: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class ConfirmRequest(BaseModel):
    mode: str = Field(pattern="^(create|link)$")
    account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    transaction_id: uuid.UUID | None = None


class ConfirmResponse(BaseModel):
    receipt_id: uuid.UUID
    transaction_id: uuid.UUID
    mode: str
    amount_cents: int
    message: str


class RejectRequest(BaseModel):
    transaction_id: uuid.UUID


async def _load_receipt(session: DbSession, user_id: uuid.UUID, receipt_id: uuid.UUID) -> Receipt:
    receipt = (
        await session.execute(
            select(Receipt)
            .options(selectinload(Receipt.upload))
            .where(Receipt.id == receipt_id, Receipt.user_id == user_id)
        )
    ).scalar_one_or_none()
    if receipt is None:
        # 404 rather than 403: never confirm another user's receipt exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
    return receipt


def _summary(receipt: Receipt, upload: Upload) -> dict:
    return {
        "id": receipt.id,
        "status": receipt.status,
        "merchant": receipt.merchant,
        "posted_date": receipt.posted_date,
        "total_cents": receipt.total_cents,
        "total": round(receipt.total_cents / 100, 2) if receipt.total_cents else None,
        "currency": receipt.currency,
        "ocr_confidence": float(receipt.ocr_confidence),
        "needs_review": receipt.status == ReceiptStatus.NEEDS_REVIEW,
        "page_count": receipt.page_count,
        "original_filename": upload.original_filename,
        "content_type": upload.content_type,
        "transaction_id": receipt.transaction_id,
        "link_mode": receipt.link_mode,
        "created_at": receipt.created_at,
    }


@router.get("", response_model=list[ReceiptSummary])
async def list_receipts(
    user: CurrentUser, session: DbSession, status_filter: str | None = None
) -> list[ReceiptSummary]:
    query = (
        select(Receipt, Upload)
        .join(Upload, Receipt.upload_id == Upload.id)
        .where(Receipt.user_id == user.id)
        .order_by(desc(Receipt.created_at))
        .limit(100)
    )
    if status_filter in {s.value for s in ReceiptStatus}:
        query = query.where(Receipt.status == status_filter)

    rows = (await session.execute(query)).all()
    return [ReceiptSummary(**_summary(receipt, upload)) for receipt, upload in rows]


@router.get("/{receipt_id}", response_model=ReceiptDetail)
async def get_receipt(
    receipt_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> ReceiptDetail:
    receipt = await _load_receipt(session, user.id, receipt_id)
    categories = (
        await session.execute(
            select(Category).where(Category.is_system.is_(True)).order_by(Category.sort_order)
        )
    ).scalars().all()

    accounts = (
        await session.execute(
            select(Account).where(Account.user_id == user.id).order_by(Account.name)
        )
    ).scalars().all()

    return ReceiptDetail(
        **_summary(receipt, receipt.upload),
        subtotal_cents=receipt.subtotal_cents,
        tax_cents=receipt.tax_cents,
        tip_cents=receipt.tip_cents,
        field_confidence=receipt.field_confidence,
        parse_notes=receipt.parse_notes,
        raw_text=receipt.raw_text,
        currency_warning=foreign_currency_warning(receipt, user.base_currency),
        base_currency=user.base_currency,
        categories=[CategoryOut.model_validate(c) for c in categories],
        accounts=[AccountOut.model_validate(a) for a in accounts],
        default_account_name=SYNTHETIC_ACCOUNT_NAME,
    )


@router.get("/{receipt_id}/image")
async def get_receipt_image(
    receipt_id: uuid.UUID, user: CurrentUser, session: DbSession, page: int = 1
) -> Response:
    """Serve the stored receipt.

    A PDF is rasterized server-side to a PNG page image, so the review page
    never asks a browser to execute an untrusted PDF. Headers are locked down:
    a fixed content type from an allow-list (never the upload's claim), nosniff,
    no-store, and a sandboxing CSP.
    """
    receipt = await _load_receipt(session, user.id, receipt_id)
    upload = receipt.upload

    try:
        data = get_storage().get(upload.storage_key)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Receipt file is unavailable"
        ) from exc

    if upload.content_type == "application/pdf":
        pages = load_pages(data, upload.content_type)
        index = min(max(page, 1), len(pages)) - 1
        body = render_preview(pages[index])
        media_type = "image/png"
        filename = f"{sanitize_filename(upload.original_filename)}-p{index + 1}.png"
    else:
        body = data
        media_type = safe_response_content_type(upload.content_type)
        filename = sanitize_filename(upload.original_filename)

    return Response(
        content=body,
        media_type=media_type,
        headers={
            **FILE_SECURITY_HEADERS,
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


@router.patch("/{receipt_id}", response_model=ReceiptDetail)
async def update_receipt(
    receipt_id: uuid.UUID, payload: ReceiptUpdate, user: CurrentUser, session: DbSession
) -> ReceiptDetail:
    """Apply the user's corrections to the extracted fields."""
    receipt = await _load_receipt(session, user.id, receipt_id)
    if receipt.transaction_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This receipt has already been confirmed and can no longer be edited.",
        )

    updates = payload.model_dump(exclude_unset=True)
    for field_name, value in updates.items():
        if value is None:
            continue
        if field_name == "currency":
            receipt.currency = str(value).upper()
        elif field_name == "merchant":
            receipt.merchant = str(value).strip()[:200]
        else:
            setattr(receipt, field_name, value)
        # A field the user typed is certain by definition.
        receipt.field_confidence = {**receipt.field_confidence, field_name: 1.0}

    await session.commit()
    await session.refresh(receipt)
    return await get_receipt(receipt_id, user, session)


@router.get("/{receipt_id}/match-candidates")
async def match_candidates(
    receipt_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict:
    """Existing transactions that might already represent this receipt."""
    receipt = await _load_receipt(session, user.id, receipt_id)
    candidates = await find_match_candidates(session, user.id, receipt)
    return {
        "receipt_id": str(receipt.id),
        "candidates": [candidate.as_dict() for candidate in candidates],
        "note": (
            "These are suggestions only. Nothing is linked until you choose a "
            "transaction and confirm."
        ),
    }


@router.post("/{receipt_id}/reject-candidate", status_code=status.HTTP_204_NO_CONTENT)
async def reject_match(
    receipt_id: uuid.UUID, payload: RejectRequest, user: CurrentUser, session: DbSession
) -> Response:
    """Dismiss a suggestion so it stops being offered for this receipt."""
    receipt = await _load_receipt(session, user.id, receipt_id)
    await reject_candidate(
        session,
        user_id=user.id,
        receipt_id=receipt.id,
        transaction_id=payload.transaction_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{receipt_id}/confirm", response_model=ConfirmResponse)
async def confirm_receipt(
    receipt_id: uuid.UUID, payload: ConfirmRequest, user: CurrentUser, session: DbSession
) -> ConfirmResponse:
    """Turn a reviewed receipt into exactly one transaction, or link it to one."""
    receipt = await _load_receipt(session, user.id, receipt_id)

    try:
        if payload.mode == "create":
            transaction = await confirm_create(
                session,
                user_id=user.id,
                receipt=receipt,
                account_id=payload.account_id,
                category_id=payload.category_id,
            )
            message = (
                f"Created one transaction for {round(abs(transaction.amount_cents) / 100, 2)} "
                f"{transaction.currency}."
            )
        else:
            if payload.transaction_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Choose a transaction to link this receipt to.",
                )
            transaction = await confirm_link(
                session,
                user_id=user.id,
                receipt=receipt,
                transaction_id=payload.transaction_id,
            )
            message = (
                "Linked to the existing transaction. No new transaction was created, "
                "and nothing on it was changed."
            )
    except ReceiptError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await session.commit()
    return ConfirmResponse(
        receipt_id=receipt.id,
        transaction_id=transaction.id,
        mode=payload.mode,
        amount_cents=transaction.amount_cents,
        message=message,
    )
