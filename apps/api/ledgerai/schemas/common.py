"""Response models shared across routers."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
    has_more: bool


class CategoryOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    color: str
    icon: str


class AccountOut(ORMModel):
    id: uuid.UUID
    name: str
    institution: str
    account_type: str
    mask: str


class TransactionOut(BaseModel):
    id: uuid.UUID
    posted_date: date
    amount_cents: int
    amount: float
    currency: str
    merchant: str
    raw_description: str
    category: CategoryOut | None
    confidence: float
    categorized_by: str
    needs_review: bool
    is_corrected: bool
    account_id: uuid.UUID
    account_name: str
    upload_id: uuid.UUID | None
    created_at: datetime


class TransactionUpdate(BaseModel):
    """Manual correction payload. Both fields optional; at least one required."""

    merchant: str | None = Field(default=None, min_length=1, max_length=200)
    category_id: uuid.UUID | None = None
    clear_review: bool = True
    apply_to_matching: bool = Field(
        default=True,
        description=(
            "Also correct the user's other transactions from the same merchant. "
            "Rows the user previously corrected individually are never touched."
        ),
    )


class CorrectionImpactOut(BaseModel):
    """Preview of what a bulk correction would change, shown before confirming."""

    merchant: str
    merchant_key: str
    matching_count: int
    affected_count: int
    protected_count: int
    already_correct_count: int
    affected_ids: list[str]


class TransactionUpdateResult(BaseModel):
    transaction: TransactionOut
    applied_to_matching: bool
    impact: CorrectionImpactOut


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_demo: bool
    # Set only on ephemeral demo accounts, so the UI can say when this session
    # ends instead of letting it stop working without explanation.
    demo_expires_at: datetime | None = None


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    user: UserOut
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth scheme name, not a credential
    expires_in: int


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
    context: dict[str, Any] | None = None
