"""Account settings.

Phase 1 exposes the profile and an honest capability disclosure. Export,
deletion and connected accounts are declared here as explicitly unavailable
rather than shown as broken buttons.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from ..config import settings as app_settings
from ..deps import CurrentUser, DbSession
from ..models import Account, Transaction, Upload

router = APIRouter(prefix="/api/settings", tags=["settings"])


class FeatureStatus(BaseModel):
    key: str
    label: str
    available: bool
    note: str


class ProfileOut(BaseModel):
    email: str
    display_name: str
    is_demo: bool
    transaction_count: int
    account_count: int
    upload_count: int
    ai_enabled: bool
    ai_disclosure: str
    features: list[FeatureStatus]


@router.get("/profile", response_model=ProfileOut)
async def profile(user: CurrentUser, session: DbSession) -> ProfileOut:
    counts = (
        await session.execute(
            select(
                select(func.count(Transaction.id))
                .where(Transaction.user_id == user.id)
                .scalar_subquery(),
                select(func.count(Account.id))
                .where(Account.user_id == user.id)
                .scalar_subquery(),
                select(func.count(Upload.id))
                .where(Upload.user_id == user.id)
                .scalar_subquery(),
            )
        )
    ).one()

    return ProfileOut(
        email=user.email,
        display_name=user.display_name,
        is_demo=user.is_demo,
        transaction_count=counts[0],
        account_count=counts[1],
        upload_count=counts[2],
        ai_enabled=app_settings.ai_available,
        ai_disclosure=(
            "No language model is configured. Questions are interpreted by a "
            "deterministic rules engine, categories come from seeded merchant "
            "patterns and your own corrections, and every figure is computed by "
            "SQL over your data."
            if not app_settings.ai_available
            else "A language model assists with question interpretation, "
            "categorization and explanation wording. All figures remain SQL-computed "
            "and are verified against the result set before display."
        ),
        features=[
            FeatureStatus(
                key="csv_upload",
                label="CSV statement upload",
                available=True,
                note="Available now.",
            ),
            FeatureStatus(
                key="ask_ledger",
                label="Ask Ledger analysis",
                available=True,
                note="Available now, running the deterministic engine.",
            ),
            FeatureStatus(
                key="receipt_ocr",
                label="Receipt image OCR",
                available=False,
                note="Planned for Phase 2 (Tesseract).",
            ),
            FeatureStatus(
                key="alerts",
                label="Duplicate and unusual-charge alerts",
                available=False,
                note="Planned for Phase 2.",
            ),
            FeatureStatus(
                key="export",
                label="Data export and deletion",
                available=False,
                note="Planned for Phase 3.",
            ),
            FeatureStatus(
                key="connected_accounts",
                label="Connected bank accounts",
                available=False,
                note=(
                    "Future integration. Ledger AI does not connect to any real "
                    "financial institution, and all data in this build is synthetic."
                ),
            ),
        ],
    )
