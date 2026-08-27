"""Insert parsed rows as transactions, idempotently.

Idempotency is a database property here, not an application check:
`transactions.dedupe_hash` is UNIQUE and inserts use ON CONFLICT DO NOTHING.
That means a retried job, a re-queued job, or a re-uploaded file converge to
the same state without a read-then-write race.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models import Account, Category, MerchantRule, Transaction, TransactionCorrection
from .categorize import (
    CategorizationContext,
    Categorizer,
    TransactionCandidate,
    build_merchant_rule_index,
)
from .categorize.rules import UNCATEGORIZED
from .csv_parser import ParsedRow
from .normalize import compute_dedupe_hash, merchant_key

RULES_PATH = Path(__file__).parent / "categorize" / "merchant_rules.yaml"
CATEGORIES_PATH = Path(__file__).parent / "categorize" / "categories.yaml"


@dataclass(slots=True)
class IngestResult:
    imported: int = 0
    skipped_duplicates: int = 0
    needs_review: int = 0


def load_category_definitions() -> list[dict]:
    return yaml.safe_load(CATEGORIES_PATH.read_text())


def load_merchant_rule_definitions() -> list[tuple[str, str]]:
    """Flatten the YAML into (pattern, category_slug) pairs."""
    raw: dict[str, list[str]] = yaml.safe_load(RULES_PATH.read_text())
    return [
        (pattern.strip().lower(), slug)
        for slug, patterns in raw.items()
        for pattern in patterns
    ]


def build_context(session: Session, user_id: uuid.UUID) -> CategorizationContext:
    """Assemble per-job lookup tables once, rather than per row."""
    rules = session.execute(
        select(MerchantRule.pattern, MerchantRule.category_slug).order_by(MerchantRule.priority)
    ).all()
    rule_pairs = [(r.pattern, r.category_slug) for r in rules]
    if not rule_pairs:
        # Table not seeded (e.g. a bare test DB) — fall back to the YAML source.
        rule_pairs = load_merchant_rule_definitions()

    corrections = session.execute(
        select(TransactionCorrection.merchant_key, TransactionCorrection.new_value)
        .where(
            TransactionCorrection.user_id == user_id,
            TransactionCorrection.field == "category",
        )
        .order_by(TransactionCorrection.created_at)
    ).all()
    # Later corrections overwrite earlier ones — most recent wins.
    memory = {row.merchant_key: row.new_value for row in corrections}

    return CategorizationContext(
        correction_memory=memory,
        merchant_rules=build_merchant_rule_index(rule_pairs),
    )


def resolve_category_ids(session: Session) -> dict[str, uuid.UUID]:
    rows = session.execute(
        select(Category.slug, Category.id).where(Category.is_system.is_(True))
    ).all()
    return {row.slug: row.id for row in rows}


def ingest_rows(
    session: Session,
    *,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    upload_id: uuid.UUID,
    rows: list[ParsedRow],
    categorizer: Categorizer,
    context: CategorizationContext,
    category_ids: dict[str, uuid.UUID],
) -> IngestResult:
    result = IngestResult()
    if not rows:
        return result

    payloads: list[dict] = []
    for row in rows:
        merchant = row.merchant
        key = merchant_key(merchant)
        candidate = TransactionCandidate(
            merchant=merchant,
            merchant_key=key,
            normalized_description=row.normalized_description,
            amount_cents=row.amount_cents,
            posted_date=row.posted_date,
        )
        suggestion = categorizer.categorize(candidate, context)
        category_id = category_ids.get(suggestion.category_slug) or category_ids.get(UNCATEGORIZED)

        payloads.append(
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "account_id": account_id,
                "upload_id": upload_id,
                "posted_date": row.posted_date,
                "amount_cents": row.amount_cents,
                "currency": "USD",
                "raw_description": row.raw_description,
                "normalized_description": row.normalized_description,
                "merchant": merchant,
                "merchant_key": key,
                "category_id": category_id,
                "confidence": suggestion.confidence,
                "categorized_by": suggestion.source,
                "needs_review": suggestion.needs_review,
                "is_corrected": False,
                "dedupe_hash": compute_dedupe_hash(
                    user_id,
                    account_id,
                    row.posted_date,
                    row.amount_cents,
                    row.normalized_description,
                    row.row_index,
                ),
                "source_row_index": row.row_index,
            }
        )
        if suggestion.needs_review:
            result.needs_review += 1

    # ON CONFLICT DO NOTHING is what makes reprocessing safe. RETURNING tells
    # us exactly how many rows were genuinely new.
    statement = (
        pg_insert(Transaction)
        .values(payloads)
        .on_conflict_do_nothing(index_elements=[Transaction.dedupe_hash])
        .returning(Transaction.id)
    )
    inserted = session.execute(statement).scalars().all()
    result.imported = len(inserted)
    result.skipped_duplicates = len(payloads) - result.imported
    return result


def resolve_account(
    session: Session, user_id: uuid.UUID, account_hint: str | None
) -> Account:
    """Match the CSV's account column to one of the user's accounts.

    Falls back to a single 'Imported Account' rather than failing the upload —
    the user can still see and correct their data.
    """
    accounts = session.execute(select(Account).where(Account.user_id == user_id)).scalars().all()

    if account_hint:
        hint = account_hint.strip().lower()
        for account in accounts:
            if hint == account.name.lower() or (len(hint) >= 4 and hint[-4:] == account.mask):
                return account
            if hint in account.name.lower() or account.name.lower() in hint:
                return account

    if accounts:
        return accounts[0]

    account = Account(
        user_id=user_id,
        name="Imported Account",
        institution="Imported",
        account_type="checking",
        mask="0000",
        currency="USD",
    )
    session.add(account)
    session.flush()
    return account
