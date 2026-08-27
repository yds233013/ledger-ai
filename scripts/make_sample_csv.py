#!/usr/bin/env python
"""Generate the synthetic sample statement shipped in docs/samples/.

Every row is fabricated. Descriptions carry a [SYNTHETIC] marker and the
account is a sandbox placeholder, so the file cannot be mistaken for a real
bank export even out of context.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "docs" / "samples" / "sample_statement_synthetic.csv"
SEED = 4242
ROWS = 45

MERCHANTS = [
    ("SQ *BLUE BOTTLE COFFEE #4821 AUSTIN TX", 450, 1250),
    ("WHOLE FOODS MKT 10233 AUSTIN TX", 3200, 12500),
    ("TST* SWEETGREEN - DOMAIN", 1200, 2200),
    ("UBER *TRIP HELP.UBER.COM", 900, 3400),
    ("AMAZON.COM*MK4XY9Z11 AMZN.COM/BILL WA", 1200, 9900),
    ("NETFLIX.COM", 1599, 1599),
    ("SPOTIFY USA", 1199, 1199),
    ("SHELL OIL 57445123456 AUSTIN TX", 3400, 7200),
    ("TRADER JOES #182 AUSTIN TX", 2400, 8800),
    ("CVS/PHARMACY #09812 AUSTIN TX", 800, 4600),
    ("CHIPOTLE ONLINE 8811", 1100, 2600),
    ("PLANET FITNESS", 2499, 2499),
    ("ZORBLAX QUANTUM WIDGETS LLC", 1900, 8800),  # unknown on purpose
]


def main() -> None:
    rng = random.Random(SEED)
    today = date.today()
    start = today - timedelta(days=60)

    rows = []
    for index in range(ROWS):
        description, low, high = MERCHANTS[index % len(MERCHANTS)]
        posted = start + timedelta(days=rng.randint(0, 58))
        amount = -rng.randint(low, high) / 100
        rows.append(
            {
                "Date": posted.isoformat(),
                "Description": f"{description} [SYNTHETIC]",
                "Amount": f"{amount:.2f}",
                "Account": "SANDBOX — Everyday Checking",
            }
        )

    # A paycheck, so the file exercises the income path too.
    rows.append({
        "Date": (start + timedelta(days=15)).isoformat(),
        "Description": "DIRECT DEP ACME ROBOTICS PAYROLL [SYNTHETIC]",
        "Amount": "6125.00",
        "Account": "SANDBOX — Everyday Checking",
    })
    # A transfer, so exclusion behaviour is demonstrable.
    rows.append({
        "Date": (start + timedelta(days=20)).isoformat(),
        "Description": "ONLINE TRANSFER TO SANDBOX SAVINGS 0003 [SYNTHETIC]",
        "Amount": "-750.00",
        "Account": "SANDBOX — Everyday Checking",
    })

    rows.sort(key=lambda row: row["Date"])
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Date", "Description", "Amount", "Account"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} synthetic rows to {OUTPUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
