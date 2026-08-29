"""Account settings.

Phase 1 exposes the profile and an honest capability disclosure. Export,
deletion and connected accounts are declared here as explicitly unavailable
rather than shown as broken buttons.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..config import settings as app_settings
from ..db import SyncSessionLocal
from ..deps import CurrentUser, DbSession, SyncSessionFactory
from ..models import Account, Transaction, Upload
from ..security.ratelimit import DESTRUCTIVE_LIMIT, EXPORT_LIMIT, enforce
from ..services import consent
from ..services.account_deletion import record_deletion_intent
from ..services.analysis.cache import purge_user_cache
from ..services.demo import (
    DEMO_DATA_NOTICE,
    DEMO_LIFETIME_HOURS,
    is_ephemeral_demo,
)
from ..services.lifecycle import TABLE_LABELS, build_export, delete_user_data

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
    # True only for ephemeral per-visitor demo accounts, not for the permanent
    # local development demo user.
    is_ephemeral_demo: bool
    demo_expires_at: datetime | None
    demo_notice: str | None
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

    ephemeral = is_ephemeral_demo(user)
    return ProfileOut(
        email=user.email,
        display_name=user.display_name,
        is_demo=user.is_demo,
        is_ephemeral_demo=ephemeral,
        demo_expires_at=user.demo_expires_at if ephemeral else None,
        demo_notice=(
            f"{DEMO_DATA_NOTICE} It expires {DEMO_LIFETIME_HOURS} hours after it was "
            "created, and everything in it is deleted then."
            if ephemeral
            else None
        ),
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
                label="Receipt OCR (JPEG, PNG, PDF)",
                available=True,
                note=(
                    "Available now, using Tesseract locally. English only; "
                    "handwritten receipts are out of scope."
                ),
            ),
            FeatureStatus(
                key="alerts",
                label="Duplicate and unusual-charge alerts",
                available=True,
                note=(
                    "Available now. These are statistical observations about your own "
                    "data, not fraud detection."
                ),
            ),
            FeatureStatus(
                key="currency_conversion",
                label="Currency conversion",
                available=False,
                note=(
                    "Not implemented. Amounts in other currencies are reported "
                    "separately and never added to your base-currency totals."
                ),
            ),
            FeatureStatus(
                key="export",
                label="Data export",
                available=True,
                note="Download every record held for this account as a ZIP.",
            ),
            FeatureStatus(
                key="deletion",
                label="Data and account deletion",
                available=True,
                note=(
                    "Removes database records, stored receipt files, cached analyses "
                    "and any queued processing jobs. Permanent."
                ),
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


# --------------------------------------------------------------------------
# Export and deletion
# --------------------------------------------------------------------------


class DeletionRequest(BaseModel):
    """Typed confirmation.

    Deleting reaches the database, object storage, the analysis cache and the
    queue, and none of it comes back. A checkbox is not enough friction.
    """

    confirmation: str = Field(
        description="Must be exactly DELETE for the request to proceed.",
        max_length=32,
    )
    dry_run: bool = Field(
        default=False,
        description="Report what would be removed without removing anything.",
    )


class DeletionResponse(BaseModel):
    dry_run: bool
    account_removed: bool
    total_rows: int
    rows_by_table: dict[str, int]
    # Readable names for the keys of rows_by_table, so the confirmation screen
    # says "manual corrections" rather than "transaction_corrections".
    table_labels: dict[str, str]
    # What this operation deliberately keeps. Saying so is half of an honest
    # preview: "everything else" is not something a user should have to infer.
    retained: list[str]
    storage_objects_removed: int
    cache_keys_removed: int
    queued_jobs_cancelled: int
    errors: list[str]
    message: str


def _require_confirmation(payload: DeletionRequest) -> None:
    if payload.confirmation.strip() != "DELETE":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Type DELETE exactly to confirm. Nothing has been removed.',
        )


class ConsentStateOut(BaseModel):
    required: dict[str, str]
    accepted: dict[str, str]
    missing: list[str]


class ConsentAcceptIn(BaseModel):
    consent_types: list[str]


@router.get("/consents", response_model=ConsentStateOut)
async def get_consents(user: CurrentUser, session: DbSession) -> ConsentStateOut:
    """What this account has accepted, and what it still needs to."""
    accepted = await consent.accepted_versions(session, user.id)
    return ConsentStateOut(
        required={t: consent.required_version(t) for t in consent.UPLOAD_PREREQUISITES},
        accepted=accepted,
        missing=await consent.missing_consents(session, user),
    )


@router.post("/consents", response_model=ConsentStateOut)
async def accept_consents(
    payload: ConsentAcceptIn,
    request: Request,
    user: CurrentUser,
    factory: SyncSessionFactory,
    session: DbSession,
) -> ConsentStateOut:
    """Record acceptance of one or more documents at their current version."""
    unknown = set(payload.consent_types) - set(consent.REQUIRED_VERSIONS)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown consent type(s): {', '.join(sorted(unknown))}",
        )

    request_id = request.headers.get("x-request-id", "")[:64]

    def _record() -> None:
        with factory() as sync_session:
            for consent_type in payload.consent_types:
                consent.record_consent(
                    sync_session,
                    user_id=user.id,
                    consent_type=consent_type,
                    request_id=request_id,
                )
            sync_session.commit()

    await to_thread.run_sync(_record)

    accepted = await consent.accepted_versions(session, user.id)
    return ConsentStateOut(
        required={t: consent.required_version(t) for t in consent.UPLOAD_PREREQUISITES},
        accepted=accepted,
        missing=await consent.missing_consents(session, user),
    )


@router.get("/export")
async def export_data(
    request: Request, user: CurrentUser, session: DbSession
) -> Response:
    """Download everything held for this account as a ZIP.

    Runs on the request's own session so it sees exactly what the caller can
    see; every query inside build_export filters on user_id.
    """
    await enforce(request, EXPORT_LIMIT, key=str(user.id))
    archive = await build_export(session, user)
    stamp = datetime.now().strftime("%Y%m%d")  # noqa: DTZ005 - filename only
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="ledgerai-export-{stamp}.zip"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _run_deletion(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    payload: DeletionRequest,
    *,
    delete_account: bool,
) -> DeletionResponse:
    await enforce(request, DESTRUCTIVE_LIMIT, key=str(user.id))
    _require_confirmation(payload)

    user_id = uuid.UUID(str(user.id))
    result = await delete_user_data(
        session, user, delete_account=delete_account, dry_run=payload.dry_run
    )
    report = result.as_dict()
    report["table_labels"] = {
        table: TABLE_LABELS.get(table, table.replace("_", " "))
        for table in report["rows_by_table"]
    }

    if not payload.dry_run:
        # The cache is keyed by digest, so it is purged through the per-user
        # index rather than by pattern-matching keys.
        report["cache_keys_removed"] = await purge_user_cache(user_id)
        await session.commit()

    if payload.dry_run:
        message = (
            f"Dry run — nothing was removed. This would delete "
            f"{report['total_rows']} record(s)"
            + (" and your account." if delete_account else ".")
        )
    elif delete_account:
        message = "Your account and all of its data have been permanently deleted."
    else:
        message = (
            f"{report['total_rows']} record(s), {report['storage_objects_removed']} "
            "stored file(s) and every cached analysis have been permanently deleted. "
            "Your sign-in and your accounts remain."
        )

    return DeletionResponse(**report, message=message)


@router.post("/delete-data", response_model=DeletionResponse)
async def delete_data(
    payload: DeletionRequest, request: Request, user: CurrentUser, session: DbSession
) -> DeletionResponse:
    """Remove transactions, uploads, receipts, alerts and analyses. Keep the account."""
    return await _run_deletion(request, user, session, payload, delete_account=False)


@router.post("/delete-account", response_model=DeletionResponse)
async def delete_account(
    payload: DeletionRequest, request: Request, user: CurrentUser, session: DbSession
) -> DeletionResponse:
    """Remove the account and everything belonging to it.

    For a Clerk-backed account the tombstone is written FIRST, before anything
    is removed. That single row denies every subsequent request and stops lazy
    provisioning from rebuilding the profile — a token minted a minute ago is
    still cryptographically valid, and without the tombstone the user's next
    request would recreate exactly what they just asked us to erase.

    If the purge below fails halfway, the tombstone is what the reconciliation
    sweep picks up, so the deletion finishes rather than silently stopping.
    """
    if user.clerk_user_id:
        clerk_user_id = user.clerk_user_id

        def _record() -> None:
            with SyncSessionLocal() as sync_session:
                record_deletion_intent(sync_session, clerk_user_id=clerk_user_id)
                sync_session.commit()

        await to_thread.run_sync(_record)

    return await _run_deletion(request, user, session, payload, delete_account=True)
