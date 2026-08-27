"""The synthetic dataset generator.

EVERY figure produced here is fabricated. No real person, account, balance or
transaction is represented. Merchant names are real-world brands used as
plausible labels only; nothing is contacted or integrated.

This lives in the application package rather than in `scripts/` because two
callers need exactly the same generator and must not drift apart:

  * `scripts/seed_synthetic.py` — the long-lived local development account,
    14 months of full-density history.
  * `services/demo.py` — the ephemeral per-visitor demo account, 8 months at
    reduced density (~250 transactions), provisioned on demand.

Determinism: callers pass their own seeded `random.Random`, so the development
seed stays byte-identical on every machine (screenshots and tests agree), while
each demo visitor gets their own dataset from their own seed.

The data is marked as synthetic in three visible ways:
  * the account holder's display name says so
  * every account name is prefixed "SANDBOX —" with a 0000-series mask
  * every transaction description ends with the marker "[SYNTHETIC]"
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import TypedDict

SYNTHETIC_MARKER = "[SYNTHETIC]"

# The development seed's history. The ephemeral demo uses DEMO_MONTHS below.
FULL_MONTHS_OF_HISTORY = 14

# Roughly eight months is enough for a month-over-month comparison, a seasonal
# utility curve and a believable merchant history, without making the first
# dashboard render a wall of rows.
DEMO_MONTHS_OF_HISTORY = 8

# Scales the per-month frequency of everyday spending. 1.0 is the development
# dataset. The demo runs lighter so eight months lands near 250 transactions —
# see tests/test_demo.py, which pins the count to a range rather than a magic
# number so tuning this does not silently change the product claim.
DEMO_DENSITY = 0.52

# (description, fixed amount in cents, monthly frequency)
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

# (name, institution, type, mask)
ACCOUNTS = [
    ("SANDBOX — Everyday Checking", "Sandbox Federal (Demo)", "checking", "0001"),
    ("SANDBOX — Rewards Credit Card", "Sandbox Federal (Demo)", "credit", "0002"),
    ("SANDBOX — Savings", "Sandbox Federal (Demo)", "savings", "0003"),
]


class SyntheticRow(TypedDict, total=False):
    """One generated transaction, before normalization and categorization."""

    date: date
    description: str
    cents: int
    account: int
    currency: str


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


def build_transactions(
    rng: random.Random,
    today: date,
    months: int = FULL_MONTHS_OF_HISTORY,
    density: float = 1.0,
) -> list[SyntheticRow]:
    """Produce the raw (date, description, cents, account) rows.

    `density` scales only everyday variable spending. Income, rent and
    subscriptions stay fixed, because a demo account whose rent payment came
    and went at random would not read as a real financial history.

    At density 1.0 the sequence of rng calls is identical to the original
    generator, so the development seed remains byte-for-byte reproducible.
    """
    rows: list[SyntheticRow] = []
    starts = month_starts(today, months)

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
            expected = per_month * density
            count = max(0, int(rng.gauss(expected, expected * 0.35)))
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

    rows.extend(build_anomalies(today))
    return rows


def build_anomalies(today: date) -> list[SyntheticRow]:
    """Deliberate anomalies, so the alerts surface is populated on first load.

    Fixed rather than random: the demo has to *reliably* show a duplicate, a
    near-duplicate and an outlier, or the alerts panel is empty for whichever
    visitor drew an unlucky seed.
    """
    rows: list[SyntheticRow] = []
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
