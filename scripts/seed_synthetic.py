#!/usr/bin/env python
"""Generate the synthetic demo dataset.

EVERY figure produced here is fabricated. No real person, account, balance or
transaction is represented. Merchant names are real-world brands used as
plausible labels only; nothing is contacted or integrated.

The data is deliberately marked as synthetic in three visible ways:
  * the demo user is "Demo User (Synthetic Data)" at demo@ledgerai.local
  * every account name is prefixed "SANDBOX —" and masked 0000-series
  * every transaction description ends with the marker "[SYNTHETIC]"

Determinism: a fixed RNG seed means the dataset is byte-identical on every
machine, so screenshots and tests agree.
"""

from __future__ import annotations

import argparse
import random
import sys
import uuid
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from ledgerai.config import settings  # noqa: E402
from ledgerai.db import sync_session  # noqa: E402
from ledgerai.models import (  # noqa: E402
    Account,
    Alert,
    AnalysisRun,
    Category,
    MerchantRule,
    Transaction,
    TransactionCorrection,
    Upload,
    User,
)
from ledgerai.security.passwords import hash_password  # noqa: E402
from ledgerai.services.alerts import analyze_user  # noqa: E402
from ledgerai.services.categorize import (  # noqa: E402
    CategorizationContext,
    RuleCategorizer,
    TransactionCandidate,
    build_merchant_rule_index,
)
from ledgerai.services.demo_data import (  # noqa: E402
    ACCOUNTS,
    FULL_MONTHS_OF_HISTORY,
    SYNTHETIC_MARKER,
    build_transactions,
)
from ledgerai.services.ingest import (  # noqa: E402
    load_category_definitions,
    load_merchant_rule_definitions,
)
from ledgerai.services.normalize import (  # noqa: E402
    compute_dedupe_hash,
    extract_merchant,
    merchant_key,
    normalize_description,
)

SEED = 20260826


def seed(reset: bool = False) -> None:
    rng = random.Random(SEED)
    today = date.today()

    with sync_session() as session:
        # --- system categories -------------------------------------------
        for definition in load_category_definitions():
            statement = (
                pg_insert(Category)
                .values(
                    id=uuid.uuid4(),
                    user_id=None,
                    name=definition["name"],
                    slug=definition["slug"],
                    color=definition["color"],
                    icon=definition["icon"],
                    sort_order=definition["sort_order"],
                    is_system=True,
                )
                # Targets the partial unique index on (slug) WHERE user_id IS NULL,
                # so re-running the seed never duplicates a system category.
                .on_conflict_do_nothing(
                    index_elements=["slug"], index_where=text("user_id IS NULL")
                )
            )
            session.execute(statement)

        # --- merchant rules ------------------------------------------------
        for pattern, slug in load_merchant_rule_definitions():
            session.execute(
                pg_insert(MerchantRule)
                .values(
                    id=uuid.uuid4(),
                    pattern=pattern,
                    merchant_name=pattern.title(),
                    category_slug=slug,
                    # Longer patterns are more specific, so they must win.
                    priority=1000 - len(pattern),
                )
                .on_conflict_do_nothing(index_elements=["pattern"])
            )
        session.flush()

        # --- demo user ------------------------------------------------------
        user = session.execute(
            select(User).where(User.email == settings.demo_user_email)
        ).scalar_one_or_none()

        if user and reset:
            print(f"Resetting data for {user.email} …")
            for model in (Alert, AnalysisRun, TransactionCorrection, Transaction, Upload, Account):
                session.execute(delete(model).where(model.user_id == user.id))
            session.flush()
        elif user:
            existing = session.execute(
                select(Transaction.id).where(Transaction.user_id == user.id).limit(1)
            ).first()
            if existing:
                print(f"{user.email} already has data. Re-run with --reset to regenerate.")
                return

        if user is None:
            user = User(
                email=settings.demo_user_email,
                password_hash=hash_password(settings.demo_user_password),
                display_name="Demo User (Synthetic Data)",
                is_demo=True,
            )
            session.add(user)
            session.flush()

        # --- accounts --------------------------------------------------------
        accounts: list[Account] = []
        for name, institution, account_type, mask in ACCOUNTS:
            account = Account(
                user_id=user.id,
                name=name,
                institution=institution,
                account_type=account_type,
                mask=mask,
                currency="USD",
            )
            session.add(account)
            accounts.append(account)
        session.flush()

        # --- categorization context -----------------------------------------
        categorizer = RuleCategorizer()
        context = CategorizationContext(
            correction_memory={},
            merchant_rules=build_merchant_rule_index(load_merchant_rule_definitions()),
        )
        category_ids = {
            row.slug: row.id
            for row in session.execute(
                select(Category.slug, Category.id).where(Category.is_system.is_(True))
            ).all()
        }

        # --- transactions ------------------------------------------------------
        raw_rows = build_transactions(rng, today, FULL_MONTHS_OF_HISTORY, density=1.0)
        raw_rows.sort(key=lambda row: row["date"])

        payloads = []
        review_count = 0
        for index, row in enumerate(raw_rows):
            # The marker makes it impossible to mistake this for real data.
            description = f"{row['description']} {SYNTHETIC_MARKER}"
            merchant = extract_merchant(row["description"])
            normalized = normalize_description(description)
            account = accounts[row["account"]]

            suggestion = categorizer.categorize(
                TransactionCandidate(
                    merchant=merchant,
                    merchant_key=merchant_key(merchant),
                    normalized_description=normalized,
                    amount_cents=row["cents"],
                    posted_date=row["date"],
                ),
                context,
            )
            if suggestion.needs_review:
                review_count += 1

            payloads.append({
                "id": uuid.uuid4(),
                "user_id": user.id,
                "account_id": account.id,
                "upload_id": None,
                "posted_date": row["date"],
                "amount_cents": row["cents"],
                "currency": row.get("currency", "USD"),
                "raw_description": description,
                "normalized_description": normalized,
                "merchant": merchant,
                "merchant_key": merchant_key(merchant),
                "category_id": category_ids.get(suggestion.category_slug),
                "confidence": suggestion.confidence,
                "categorized_by": suggestion.source,
                "needs_review": suggestion.needs_review,
                "is_corrected": False,
                "dedupe_hash": compute_dedupe_hash(
                    user.id, account.id, row["date"], row["cents"], normalized, index
                ),
                "source_row_index": index,
            })

        session.execute(
            pg_insert(Transaction)
            .values(payloads)
            .on_conflict_do_nothing(index_elements=["dedupe_hash"])
        )

        session.flush()

        # Run duplicate and unusual-charge detection so the demo dataset has a
        # populated alerts surface rather than an empty panel.
        alerts_created = analyze_user(session, user.id)

        spend = sum(-p["amount_cents"] for p in payloads if p["amount_cents"] < 0)
        income = sum(p["amount_cents"] for p in payloads if p["amount_cents"] > 0)
        print(f"""
Seeded synthetic demo data
  user        : {user.email}  (password from DEMO_USER_PASSWORD)
  accounts    : {len(accounts)}
  transactions: {len(payloads)}  over {FULL_MONTHS_OF_HISTORY} months
  needs review: {review_count}
  alerts      : {alerts_created}
  gross spend : ${spend / 100:,.2f}   (synthetic)
  gross income: ${income / 100:,.2f}   (synthetic)

All figures are fabricated. Descriptions carry the {SYNTHETIC_MARKER} marker.
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed synthetic Ledger AI demo data")
    parser.add_argument(
        "--reset", action="store_true", help="Delete existing demo data and regenerate"
    )
    seed(reset=parser.parse_args().reset)
