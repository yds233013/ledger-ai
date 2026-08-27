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
from datetime import date, timedelta
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
SYNTHETIC_MARKER = "[SYNTHETIC]"
MONTHS_OF_HISTORY = 14

# (description template, category hint, low, high, monthly frequency)
RECURRING = [
    ("NETFLIX.COM", 1599, 1),
    ("SPOTIFY USA", 1199, 1),
    ("ADOBE CREATIVE CLOUD", 5999, 1),
    ("PLANET FITNESS", 2499, 1),
    ("COMCAST XFINITY INTERNET", 8900, 1),
    ("T-MOBILE WIRELESS", 7500, 1),
    ("PG&E ELECTRIC", 0, 1),          # amount varies seasonally, see below
    ("RENT PAYMENT SANDBOX PROPERTIES", 235000, 1),
]

VARIABLE = [
    # (description, min cents, max cents, approx count per month)
    ("WHOLE FOODS MKT 10233 AUSTIN TX", 3200, 14500, 5),
    ("TRADER JOES #182 AUSTIN TX", 2400, 9800, 3),
    ("SQ *BLUE BOTTLE COFFEE #4821 AUSTIN TX", 450, 1250, 8),
    ("TST* SWEETGREEN - DOMAIN", 1200, 2200, 4),
    ("CHIPOTLE ONLINE 8811", 1100, 2600, 3),
    ("DOORDASH*ORDER 4471", 1800, 5400, 3),
    ("AMAZON.COM*MK4XY9Z11 AMZN.COM/BILL WA", 1200, 18900, 6),
    ("TARGET 00023481 AUSTIN TX", 2200, 12400, 2),
    ("UBER *TRIP HELP.UBER.COM", 900, 3400, 5),
    ("SHELL OIL 57445123456 AUSTIN TX", 3400, 7200, 2),
    ("CVS/PHARMACY #09812 AUSTIN TX", 800, 4600, 2),
    ("AMC THEATRES #1121", 1400, 3800, 1),
    ("STEAM GAMES PURCHASE", 999, 5999, 1),
    ("HOME DEPOT #6612 AUSTIN TX", 1500, 9900, 1),
]

OCCASIONAL = [
    ("UNITED AIRLINES 0162277881", 18000, 62000),
    ("MARRIOTT HOTELS AUSTIN", 14000, 38000),
    ("REI CO-OP #114", 4500, 22000),
    ("ZORBLAX QUANTUM WIDGETS LLC", 1900, 8800),   # intentionally unknown merchant
    ("NOVACORP SUPPLY CO", 2400, 7600),            # intentionally unknown merchant
]

PAYROLL_CENTS = 612500
ACCOUNTS = [
    ("SANDBOX — Everyday Checking", "Sandbox Federal (Demo)", "checking", "0001"),
    ("SANDBOX — Rewards Credit Card", "Sandbox Federal (Demo)", "credit", "0002"),
    ("SANDBOX — Savings", "Sandbox Federal (Demo)", "savings", "0003"),
]


def month_starts(today: date, count: int) -> list[date]:
    starts: list[date] = []
    cursor = date(today.year, today.month, 1)
    for _ in range(count):
        starts.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return sorted(starts)


def seasonal_electric(month: int, rng: random.Random) -> int:
    """Higher in summer and winter — makes the trend chart tell a story."""
    base = 9000
    swing = {6: 6500, 7: 8500, 8: 8000, 12: 4500, 1: 5200, 2: 4000}.get(month, 0)
    return base + swing + rng.randint(-1200, 1200)


def build_transactions(rng: random.Random, today: date) -> list[dict]:
    """Produce the raw (date, description, cents) triples."""
    rows: list[dict] = []
    starts = month_starts(today, MONTHS_OF_HISTORY)

    for month_start in starts:
        days_in_month = ((month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
                         - month_start).days
        last_day = (
            min(days_in_month, (today - month_start).days + 1)
            if month_start.month == today.month and month_start.year == today.year
            else days_in_month
        )
        if last_day <= 0:
            continue

        # Income: two paychecks a month.
        for day in (1, 15):
            if day <= last_day:
                rows.append({
                    "date": month_start.replace(day=day),
                    "description": "DIRECT DEP ACME ROBOTICS PAYROLL",
                    "cents": PAYROLL_CENTS + rng.randint(-4000, 4000),
                    "account": 0,
                })

        # Recurring charges on stable days.
        for index, (description, amount, _freq) in enumerate(RECURRING):
            day = min(2 + index * 3, last_day)
            cents = (
                seasonal_electric(month_start.month, rng)
                if "PG&E" in description
                else amount + rng.randint(-100, 100) if amount > 10000 else amount
            )
            rows.append({
                "date": month_start.replace(day=day),
                "description": description,
                "cents": -cents,
                "account": 0 if cents > 50000 else 1,
            })

        # Variable everyday spending.
        for description, low, high, per_month in VARIABLE:
            count = max(0, int(rng.gauss(per_month, per_month * 0.35)))
            for _ in range(count):
                day = rng.randint(1, last_day)
                rows.append({
                    "date": month_start.replace(day=day),
                    "description": description,
                    "cents": -rng.randint(low, high),
                    "account": rng.choice([0, 1, 1]),
                })

        # Occasional larger purchases.
        for description, low, high in OCCASIONAL:
            if rng.random() < 0.28:
                rows.append({
                    "date": month_start.replace(day=rng.randint(1, last_day)),
                    "description": description,
                    "cents": -rng.randint(low, high),
                    "account": 1,
                })

        # A transfer to savings — proves transfers are excluded from spending.
        if last_day >= 20:
            rows.append({
                "date": month_start.replace(day=20),
                "description": "ONLINE TRANSFER TO SANDBOX SAVINGS 0003",
                "cents": -rng.randint(40000, 90000),
                "account": 0,
            })
            rows.append({
                "date": month_start.replace(day=25),
                "description": "AUTOPAY CREDIT CARD PAYMENT THANK YOU",
                "cents": -rng.randint(80000, 160000),
                "account": 0,
            })

    # --- deliberate anomalies, for Phase 2 detection and for demo interest ---
    recent = date(today.year, today.month, 1) - timedelta(days=15)

    # An exact duplicate pair on the same day (two identical charges).
    for _ in range(2):
        rows.append({
            "date": recent,
            "description": "TST* SWEETGREEN - DOMAIN",
            "cents": -1850,
            "account": 1,
        })

    # A near-duplicate two days apart.
    rows.append({"date": recent + timedelta(days=2),
                 "description": "AMAZON.COM*MK4XY9Z11 AMZN.COM/BILL WA",
                 "cents": -8999, "account": 1})
    rows.append({"date": recent + timedelta(days=4),
                 "description": "AMAZON.COM*MK4XY9Z11 AMZN.COM/BILL WA",
                 "cents": -8999, "account": 1})

    # An outlier several times any normal charge at that merchant.
    rows.append({"date": recent + timedelta(days=6),
                 "description": "SQ *BLUE BOTTLE COFFEE #4821 AUSTIN TX",
                 "cents": -18400, "account": 1})

    # A bank fee, so the Fees category is populated.
    rows.append({"date": recent + timedelta(days=8),
                 "description": "MONTHLY MAINTENANCE FEE",
                 "cents": -1200, "account": 0})

    # A euro purchase. Ledger AI does not convert currencies, so this exists to
    # make the exclusion disclosure visible in the demo rather than theoretical.
    rows.append({"date": recent + timedelta(days=10),
                 "description": "SANDBOX BOOKS EU",
                 "cents": -3291, "account": 1, "currency": "EUR"})

    return rows


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
        raw_rows = build_transactions(rng, today)
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
  transactions: {len(payloads)}  over {MONTHS_OF_HISTORY} months
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
