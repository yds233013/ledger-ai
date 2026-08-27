"""Receipt lifecycle: matching candidates, confirming, and linking.

Three rules drive the design:

  * A receipt total is **spending**, so the transaction it creates stores a
    NEGATIVE amount_cents. The receipt record keeps the positive figures that
    were printed on it; the sign is applied here, once.
  * A receipt-created transaction must belong to an account the user chose. It
    is never silently attached to an arbitrary bank account — with no choice it
    goes to a clearly-named synthetic holding account.
  * Linking is non-destructive. It records the association and nothing else,
    unless the user separately asks for the receipt's merchant and category to
    be applied, which then runs through the audited correction path.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from rapidfuzz import fuzz
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Account,
    Category,
    Receipt,
    ReceiptLinkMode,
    ReceiptMatchRejection,
    ReceiptStatus,
    Transaction,
    Upload,
)
from .normalize import merchant_key, normalize_description

logger = logging.getLogger(__name__)

SYNTHETIC_ACCOUNT_NAME = "Cash / Receipt Purchases"
MATCH_DATE_WINDOW_DAYS = 4
# Two charges of the same value are the interesting case; allow a couple of
# cents for OCR rounding but no more.
MATCH_AMOUNT_TOLERANCE_CENTS = 2
MIN_CANDIDATE_SCORE = 0.35


class ReceiptError(Exception):
    """A user-facing problem with confirming a receipt."""


@dataclass(slots=True)
class MatchSignal:
    name: str
    detail: str
    contribution: float


@dataclass(slots=True)
class MatchCandidate:
    """A possible existing transaction for this receipt.

    Everything the user needs to tell two similar charges apart is returned
    here: account, date, merchant, amount and the upload it came from.
    """

    transaction_id: str
    posted_date: str
    merchant: str
    amount_cents: int
    amount: float
    currency: str
    account_id: str
    account_name: str
    category: str | None
    source_upload_id: str | None
    source_filename: str | None
    score: float
    signals: list[MatchSignal] = field(default_factory=list)

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["signals"] = [asdict(signal) for signal in self.signals]
        return payload


async def get_or_create_synthetic_account(
    session: AsyncSession, user_id: uuid.UUID, currency: str
) -> Account:
    """The holding account for receipts with no account chosen.

    Named and flagged so it is obvious in every account list that this is not a
    real bank account.
    """
    existing = (
        await session.execute(
            select(Account).where(
                Account.user_id == user_id,
                Account.name == SYNTHETIC_ACCOUNT_NAME,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    account = Account(
        user_id=user_id,
        name=SYNTHETIC_ACCOUNT_NAME,
        institution="Ledger AI (not a bank)",
        account_type="cash",
        mask="0000",
        currency=currency,
        is_synthetic=True,
    )
    session.add(account)
    await session.flush()
    return account


async def resolve_account(
    session: AsyncSession,
    user_id: uuid.UUID,
    account_id: uuid.UUID | None,
    currency: str,
) -> Account:
    """Resolve the destination account for a receipt-created transaction."""
    if account_id is not None:
        account = (
            await session.execute(
                select(Account).where(
                    Account.id == account_id, Account.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if account is None:
            # Another user's account id must not be usable, and must not
            # confirm that it exists.
            raise ReceiptError("That account was not found.")
        return account

    return await get_or_create_synthetic_account(session, user_id, currency)


def receipt_dedupe_hash(receipt_id: uuid.UUID | str) -> str:
    """Deterministic per receipt.

    A retried confirmation computes the same hash, collides on the UNIQUE
    index, and creates nothing — one receipt can only ever produce one
    transaction.
    """
    return hashlib.sha256(f"receipt:{receipt_id}".encode()).hexdigest()


async def find_match_candidates(
    session: AsyncSession, user_id: uuid.UUID, receipt: Receipt
) -> list[MatchCandidate]:
    """Existing transactions that might already represent this receipt.

    Scoped to the caller. Amounts are compared as magnitudes because an
    existing outflow is stored negative while the extracted receipt total is
    positive — comparing them raw would match a refund to a purchase.
    """
    if receipt.total_cents is None or receipt.posted_date is None:
        return []

    rejected = set(
        (
            await session.execute(
                select(ReceiptMatchRejection.transaction_id).where(
                    ReceiptMatchRejection.receipt_id == receipt.id,
                    ReceiptMatchRejection.user_id == user_id,
                )
            )
        ).scalars().all()
    )

    window_start = receipt.posted_date - timedelta(days=MATCH_DATE_WINDOW_DAYS)
    window_end = receipt.posted_date + timedelta(days=MATCH_DATE_WINDOW_DAYS)

    rows = (
        await session.execute(
            select(Transaction, Account, Category, Upload)
            .join(Account, Transaction.account_id == Account.id)
            .outerjoin(Category, Transaction.category_id == Category.id)
            .outerjoin(Upload, Transaction.upload_id == Upload.id)
            .where(
                Transaction.user_id == user_id,
                Transaction.posted_date >= window_start,
                Transaction.posted_date <= window_end,
                # Only outflows: a receipt records money spent.
                Transaction.amount_cents < 0,
                # Never match across currencies; Ledger AI does not convert.
                Transaction.currency == receipt.currency,
                func.abs(func.abs(Transaction.amount_cents) - receipt.total_cents)
                <= MATCH_AMOUNT_TOLERANCE_CENTS,
            )
        )
    ).all()

    candidates: list[MatchCandidate] = []
    for transaction, account, category, upload in rows:
        if transaction.id in rejected:
            continue
        # A transaction already claimed by another receipt is not a candidate.
        claimed = (
            await session.execute(
                select(Receipt.id).where(
                    Receipt.transaction_id == transaction.id, Receipt.id != receipt.id
                )
            )
        ).scalar_one_or_none()
        if claimed is not None:
            continue

        score, signals = _score_candidate(receipt, transaction, account)
        if score < MIN_CANDIDATE_SCORE:
            continue

        candidates.append(
            MatchCandidate(
                transaction_id=str(transaction.id),
                posted_date=transaction.posted_date.isoformat(),
                merchant=transaction.merchant,
                amount_cents=transaction.amount_cents,
                amount=round(transaction.amount_cents / 100, 2),
                currency=transaction.currency,
                account_id=str(account.id),
                account_name=account.name,
                category=category.name if category else None,
                source_upload_id=str(upload.id) if upload else None,
                source_filename=upload.original_filename if upload else None,
                score=round(score, 3),
                signals=signals,
            )
        )

    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:10]


def _score_candidate(
    receipt: Receipt, transaction: Transaction, account: Account
) -> tuple[float, list[MatchSignal]]:
    """Score a candidate and explain every contribution.

    The explanation is the point: the user has to be able to see why a
    suggestion was made before accepting it.
    """
    signals: list[MatchSignal] = []
    total = receipt.total_cents or 0

    difference = abs(abs(transaction.amount_cents) - total)
    amount_score = 0.5 if difference == 0 else 0.35
    signals.append(
        MatchSignal(
            name="amount",
            detail=(
                f"Exact match: both are {total / 100:.2f} {receipt.currency}"
                if difference == 0
                else f"Within {difference}¢ of the receipt total"
            ),
            contribution=amount_score,
        )
    )

    assert receipt.posted_date is not None
    day_gap = abs((transaction.posted_date - receipt.posted_date).days)
    date_score = {0: 0.3, 1: 0.22}.get(day_gap, max(0.0, 0.18 - 0.03 * day_gap))
    signals.append(
        MatchSignal(
            name="date",
            detail=(
                "Same day as the receipt"
                if day_gap == 0
                else f"{day_gap} day{'s' if day_gap != 1 else ''} from the receipt date"
            ),
            contribution=round(date_score, 3),
        )
    )

    merchant_score = 0.0
    if receipt.merchant:
        similarity = (
            fuzz.token_set_ratio(
                merchant_key(receipt.merchant), merchant_key(transaction.merchant)
            )
            / 100.0
        )
        merchant_score = round(0.2 * similarity, 3)
        signals.append(
            MatchSignal(
                name="merchant",
                detail=(
                    f"“{transaction.merchant}” is {similarity * 100:.0f}% similar to "
                    f"“{receipt.merchant}”"
                ),
                contribution=merchant_score,
            )
        )

    signals.append(
        MatchSignal(
            name="currency",
            detail=f"Both are in {receipt.currency}",
            contribution=0.0,
        )
    )
    signals.append(
        MatchSignal(
            name="account",
            detail=f"Charged to {account.name}",
            contribution=0.0,
        )
    )

    return amount_score + date_score + merchant_score, signals


async def confirm_create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    receipt: Receipt,
    account_id: uuid.UUID | None,
    category_id: uuid.UUID | None,
) -> Transaction:
    """Create exactly one transaction from a confirmed receipt."""
    if receipt.total_cents is None:
        raise ReceiptError("This receipt has no total. Enter one before confirming.")
    if receipt.posted_date is None:
        raise ReceiptError("This receipt has no date. Enter one before confirming.")

    if receipt.transaction_id is not None:
        existing = await session.get(Transaction, receipt.transaction_id)
        if existing is not None:
            return existing

    account = await resolve_account(session, user_id, account_id, receipt.currency)
    merchant = receipt.merchant or "Unknown Merchant"
    description = f"Receipt: {merchant}"

    # NEGATIVE: a receipt records money going out.
    amount_cents = -abs(receipt.total_cents)

    statement = (
        pg_insert(Transaction)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            account_id=account.id,
            upload_id=receipt.upload_id,
            posted_date=receipt.posted_date,
            amount_cents=amount_cents,
            currency=receipt.currency,
            raw_description=description,
            normalized_description=normalize_description(description),
            merchant=merchant,
            merchant_key=merchant_key(merchant),
            category_id=category_id,
            confidence=1,
            categorized_by="receipt",
            needs_review=False,
            is_corrected=False,
            dedupe_hash=receipt_dedupe_hash(receipt.id),
            source_row_index=0,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_hash"])
        .returning(Transaction.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()

    if inserted is None:
        # A retry: the row already exists. Find it by its deterministic hash.
        transaction = (
            await session.execute(
                select(Transaction).where(
                    Transaction.dedupe_hash == receipt_dedupe_hash(receipt.id),
                    Transaction.user_id == user_id,
                )
            )
        ).scalar_one()
    else:
        transaction = await session.get(Transaction, inserted)  # type: ignore[assignment]

    receipt.transaction_id = transaction.id
    receipt.link_mode = ReceiptLinkMode.CREATED
    receipt.status = ReceiptStatus.CONFIRMED
    return transaction


async def confirm_link(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    receipt: Receipt,
    transaction_id: uuid.UUID,
) -> Transaction:
    """Attach the receipt to an existing transaction.

    Deliberately non-destructive: merchant, category and amount on the existing
    transaction are untouched. Applying the receipt's merchant/category is a
    separate, explicit action that goes through the correction service.
    """
    if receipt.transaction_id is not None and receipt.transaction_id == transaction_id:
        existing = await session.get(Transaction, transaction_id)
        if existing is not None:
            return existing

    transaction = (
        await session.execute(
            select(Transaction).where(
                and_(Transaction.id == transaction_id, Transaction.user_id == user_id)
            )
        )
    ).scalar_one_or_none()
    if transaction is None:
        raise ReceiptError("That transaction was not found.")

    if transaction.currency != receipt.currency:
        raise ReceiptError(
            f"This receipt is in {receipt.currency} but that transaction is in "
            f"{transaction.currency}. Ledger AI does not convert between currencies."
        )

    claimed = (
        await session.execute(
            select(Receipt.id).where(
                Receipt.transaction_id == transaction.id, Receipt.id != receipt.id
            )
        )
    ).scalar_one_or_none()
    if claimed is not None:
        raise ReceiptError("Another receipt is already linked to that transaction.")

    receipt.transaction_id = transaction.id
    receipt.link_mode = ReceiptLinkMode.LINKED
    receipt.status = ReceiptStatus.CONFIRMED
    return transaction


async def reject_candidate(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    receipt_id: uuid.UUID,
    transaction_id: uuid.UUID,
) -> None:
    """Record that the user dismissed a suggestion, so it does not return."""
    await session.execute(
        pg_insert(ReceiptMatchRejection)
        .values(
            id=uuid.uuid4(),
            receipt_id=receipt_id,
            transaction_id=transaction_id,
            user_id=user_id,
        )
        .on_conflict_do_nothing(index_elements=["receipt_id", "transaction_id"])
    )


def is_foreign_currency(receipt: Receipt, base_currency: str) -> bool:
    return receipt.currency.upper() != base_currency.upper()


def foreign_currency_warning(receipt: Receipt, base_currency: str) -> str | None:
    if not is_foreign_currency(receipt, base_currency):
        return None
    return (
        f"This receipt is in {receipt.currency}, but your base currency is "
        f"{base_currency}. Ledger AI does not convert between currencies, so this "
        f"transaction will be excluded from your {base_currency} totals."
    )


def receipt_log_context(receipt: Receipt) -> dict[str, object]:
    """Safe logging context.

    Deliberately excludes raw OCR text, merchant, amounts, dates and the
    storage key — none of that belongs in a log line.
    """
    return {
        "receipt_id": str(receipt.id),
        "status": str(receipt.status),
        "page_count": receipt.page_count,
    }
