"""Manual corrections, individual and retroactive.

A correction does three things:

  1. Fixes the transaction the user is looking at.
  2. Optionally fixes every *other* transaction from the same merchant that the
     user has not already corrected by hand.
  3. Records a rule, so future uploads of that merchant are categorized the same
     way without the user intervening again.

The protection rule in (2) is the subtle part. If the user deliberately set one
row to something different, a later "apply to all matching" must not silently
overwrite that decision — so any row carrying an INDIVIDUAL correction for the
same field is excluded from bulk updates.

Every query here is scoped by user_id. There is no code path that can reach
another user's rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import Select, and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    Category,
    CorrectionField,
    CorrectionScope,
    Transaction,
    TransactionCorrection,
)
from .normalize import merchant_key as normalize_merchant_key

# Cap on the ids returned to the client for optimistic updates. A bulk change
# larger than this still applies in full server-side; the UI simply refetches.
MAX_PREVIEW_IDS = 1000


@dataclass(slots=True)
class CorrectionImpact:
    """What a correction would do, computed before anything is written."""

    merchant_key: str
    matching_count: int = 0        # other rows sharing this merchant
    affected_count: int = 0        # of those, how many would actually change
    protected_count: int = 0       # excluded because individually corrected
    already_correct_count: int = 0  # excluded because they already hold the value
    affected_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "merchant_key": self.merchant_key,
            "matching_count": self.matching_count,
            "affected_count": self.affected_count,
            "protected_count": self.protected_count,
            "already_correct_count": self.already_correct_count,
            "affected_ids": self.affected_ids,
        }


def _siblings(user_id: uuid.UUID, merchant_key: str, exclude_id: uuid.UUID) -> Select:
    """Other transactions of the same user sharing this normalized merchant.

    user_id is the first predicate and is never optional — this is the single
    place bulk corrections select rows, so isolation is enforced once.
    """
    return (
        select(Transaction)
        # Eager-load: reading sibling.category.slug under an async session
        # would otherwise trigger a lazy load and raise.
        .options(selectinload(Transaction.category))
        .where(
            and_(
                Transaction.user_id == user_id,
                Transaction.merchant_key == merchant_key,
                Transaction.id != exclude_id,
            )
        )
    )


async def protected_transaction_ids(
    session: AsyncSession,
    user_id: uuid.UUID,
    field_name: CorrectionField,
    exclude_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Rows the user corrected one at a time, which bulk changes must not touch."""
    rows = (
        await session.execute(
            select(TransactionCorrection.transaction_id).where(
                TransactionCorrection.user_id == user_id,
                TransactionCorrection.field == field_name,
                TransactionCorrection.scope == CorrectionScope.INDIVIDUAL,
                TransactionCorrection.transaction_id != exclude_id,
            )
        )
    ).scalars().all()
    return set(rows)


async def compute_impact(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    transaction: Transaction,
    field_name: CorrectionField,
    new_category_id: uuid.UUID | None = None,
    new_merchant: str | None = None,
) -> CorrectionImpact:
    """Count what a bulk correction would change, without changing anything."""
    impact = CorrectionImpact(merchant_key=transaction.merchant_key)

    siblings = (
        await session.execute(_siblings(user_id, transaction.merchant_key, transaction.id))
    ).scalars().all()
    impact.matching_count = len(siblings)
    if not siblings:
        return impact

    protected = await protected_transaction_ids(session, user_id, field_name, transaction.id)

    for sibling in siblings:
        if sibling.id in protected:
            impact.protected_count += 1
            continue

        if field_name == CorrectionField.CATEGORY:
            unchanged = sibling.category_id == new_category_id
        else:
            unchanged = sibling.merchant == new_merchant

        if unchanged:
            impact.already_correct_count += 1
            continue

        impact.affected_count += 1
        if len(impact.affected_ids) < MAX_PREVIEW_IDS:
            impact.affected_ids.append(str(sibling.id))

    return impact


async def apply_bulk_correction(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    transaction: Transaction,
    field_name: CorrectionField,
    category: Category | None = None,
    new_merchant: str | None = None,
) -> CorrectionImpact:
    """Apply the correction to every unprotected sibling.

    Returns the same impact shape as the preview, computed from what was
    actually written rather than predicted.
    """
    new_category_id = category.id if category else None
    impact = await compute_impact(
        session,
        user_id=user_id,
        transaction=transaction,
        field_name=field_name,
        new_category_id=new_category_id,
        new_merchant=new_merchant,
    )
    if impact.affected_count == 0:
        return impact

    protected = await protected_transaction_ids(session, user_id, field_name, transaction.id)
    siblings = (
        await session.execute(_siblings(user_id, transaction.merchant_key, transaction.id))
    ).scalars().all()

    for sibling in siblings:
        if sibling.id in protected:
            continue

        if field_name == CorrectionField.CATEGORY:
            if sibling.category_id == new_category_id:
                continue
            old_value = sibling.category.slug if sibling.category else None
            sibling.category_id = new_category_id
            new_value = category.slug if category else "uncategorized"
            correction_key = sibling.merchant_key
        else:
            if sibling.merchant == new_merchant:
                continue
            old_value = sibling.merchant
            assert new_merchant is not None
            sibling.merchant = new_merchant
            # The key follows the merchant, so later corrections group correctly.
            sibling.merchant_key = normalize_merchant_key(new_merchant)
            new_value = new_merchant
            correction_key = sibling.merchant_key

        sibling.is_corrected = True
        sibling.confidence = 1
        sibling.categorized_by = "correction"
        sibling.needs_review = False

        session.add(
            TransactionCorrection(
                transaction_id=sibling.id,
                user_id=user_id,
                field=field_name,
                old_value=old_value,
                new_value=new_value,
                merchant_key=correction_key,
                scope=CorrectionScope.BULK,
            )
        )

    return impact
