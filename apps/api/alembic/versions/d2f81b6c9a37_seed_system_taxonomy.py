"""seed system category taxonomy and merchant rules

Revision ID: d2f81b6c9a37
Revises: c1a7e3b95d24
Create Date: 2026-08-28 22:10:00.000000

Production had an empty `categories` table and no merchant rules, so every
transaction fell through the deterministic categorizer to Uncategorized. The
rows existed only in `scripts/seed_synthetic.py`, which is not in the runtime
image — the image copies `ledgerai/`, `alembic/` and the lock files, and
nothing else. Nothing in the application ever created them, and no earlier
migration inserted them, so a deployed database never had a taxonomy at all.

The data below is a FROZEN SNAPSHOT of ledgerai/services/categorize/*.yaml as
of this revision. It is embedded rather than imported so this migration keeps
producing the same rows forever: a migration that read the YAML at run time
would apply whatever the taxonomy said on the day it ran, and two environments
migrated a year apart would diverge. `tests/test_taxonomy_migration.py` asserts
the snapshot still matches the canonical YAML, so the two cannot drift apart
silently — editing the YAML fails CI until a new migration is written for it.

Idempotent in both directions:

* Categories conflict on the partial unique index `uq_categories_system_slug`
  (slug WHERE user_id IS NULL), so re-running inserts nothing and a user's own
  category with the same slug is untouched — their rows have a non-NULL user_id
  and are not covered by that index.
* Merchant rules conflict on the unique `pattern` column.

The downgrade removes only rows this migration is responsible for: system
categories (user_id IS NULL) whose slug is in the snapshot, and merchant rules
whose pattern is in the snapshot. User-created categories and any rule added
later survive it. Transactions referencing a removed category are set back to
NULL rather than deleted, because ON DELETE for that FK would otherwise decide
the matter for us.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f81b6c9a37"
down_revision: str | None = "c1a7e3b95d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Snapshot fingerprint. tests/test_taxonomy_migration.py compares this against
# ledgerai.services.categorize.taxonomy.TAXONOMY_FINGERPRINT.
TAXONOMY_FINGERPRINT = "d7e0bba0c07a9ed20a65cf8a0d02bb40191e39f03c5bb400fa19e680d47b0561"

SYSTEM_CATEGORIES: list[dict] = [
    {
        "slug": "groceries",
        "name": "Groceries",
        "color": "#10b981",
        "icon": "shopping-cart",
        "sort_order": 10
    },
    {
        "slug": "dining",
        "name": "Dining & Restaurants",
        "color": "#f59e0b",
        "icon": "utensils",
        "sort_order": 20
    },
    {
        "slug": "transport",
        "name": "Transport",
        "color": "#3b82f6",
        "icon": "car",
        "sort_order": 30
    },
    {
        "slug": "shopping",
        "name": "Shopping",
        "color": "#8b5cf6",
        "icon": "shopping-bag",
        "sort_order": 40
    },
    {
        "slug": "subscriptions",
        "name": "Subscriptions",
        "color": "#ec4899",
        "icon": "repeat",
        "sort_order": 50
    },
    {
        "slug": "utilities",
        "name": "Utilities",
        "color": "#06b6d4",
        "icon": "zap",
        "sort_order": 60
    },
    {
        "slug": "housing",
        "name": "Housing",
        "color": "#6366f1",
        "icon": "home",
        "sort_order": 70
    },
    {
        "slug": "health",
        "name": "Health & Fitness",
        "color": "#14b8a6",
        "icon": "heart-pulse",
        "sort_order": 80
    },
    {
        "slug": "entertainment",
        "name": "Entertainment",
        "color": "#f43f5e",
        "icon": "film",
        "sort_order": 90
    },
    {
        "slug": "travel",
        "name": "Travel",
        "color": "#0ea5e9",
        "icon": "plane",
        "sort_order": 100
    },
    {
        "slug": "income",
        "name": "Income",
        "color": "#22c55e",
        "icon": "trending-up",
        "sort_order": 110
    },
    {
        "slug": "transfers",
        "name": "Transfers & Payments",
        "color": "#94a3b8",
        "icon": "arrow-left-right",
        "sort_order": 120
    },
    {
        "slug": "fees",
        "name": "Fees & Charges",
        "color": "#ef4444",
        "icon": "alert-circle",
        "sort_order": 130
    },
    {
        "slug": "uncategorized",
        "name": "Uncategorized",
        "color": "#64748b",
        "icon": "help-circle",
        "sort_order": 999
    }
]

MERCHANT_RULES: list[dict] = [
    {
        "pattern": "whole foods",
        "merchant_name": "Whole Foods",
        "category_slug": "groceries",
        "priority": 989
    },
    {
        "pattern": "trader joes",
        "merchant_name": "Trader Joes",
        "category_slug": "groceries",
        "priority": 989
    },
    {
        "pattern": "safeway",
        "merchant_name": "Safeway",
        "category_slug": "groceries",
        "priority": 993
    },
    {
        "pattern": "kroger",
        "merchant_name": "Kroger",
        "category_slug": "groceries",
        "priority": 994
    },
    {
        "pattern": "publix",
        "merchant_name": "Publix",
        "category_slug": "groceries",
        "priority": 994
    },
    {
        "pattern": "aldi",
        "merchant_name": "Aldi",
        "category_slug": "groceries",
        "priority": 996
    },
    {
        "pattern": "wegmans",
        "merchant_name": "Wegmans",
        "category_slug": "groceries",
        "priority": 993
    },
    {
        "pattern": "sprouts",
        "merchant_name": "Sprouts",
        "category_slug": "groceries",
        "priority": 993
    },
    {
        "pattern": "heb",
        "merchant_name": "Heb",
        "category_slug": "groceries",
        "priority": 997
    },
    {
        "pattern": "ralphs",
        "merchant_name": "Ralphs",
        "category_slug": "groceries",
        "priority": 994
    },
    {
        "pattern": "food lion",
        "merchant_name": "Food Lion",
        "category_slug": "groceries",
        "priority": 991
    },
    {
        "pattern": "giant eagle",
        "merchant_name": "Giant Eagle",
        "category_slug": "groceries",
        "priority": 989
    },
    {
        "pattern": "stop shop",
        "merchant_name": "Stop Shop",
        "category_slug": "groceries",
        "priority": 991
    },
    {
        "pattern": "harris teeter",
        "merchant_name": "Harris Teeter",
        "category_slug": "groceries",
        "priority": 987
    },
    {
        "pattern": "costco",
        "merchant_name": "Costco",
        "category_slug": "groceries",
        "priority": 994
    },
    {
        "pattern": "sams club",
        "merchant_name": "Sams Club",
        "category_slug": "groceries",
        "priority": 991
    },
    {
        "pattern": "instacart",
        "merchant_name": "Instacart",
        "category_slug": "groceries",
        "priority": 991
    },
    {
        "pattern": "grocery outlet",
        "merchant_name": "Grocery Outlet",
        "category_slug": "groceries",
        "priority": 986
    },
    {
        "pattern": "fresh market",
        "merchant_name": "Fresh Market",
        "category_slug": "groceries",
        "priority": 988
    },
    {
        "pattern": "starbucks",
        "merchant_name": "Starbucks",
        "category_slug": "dining",
        "priority": 991
    },
    {
        "pattern": "dunkin",
        "merchant_name": "Dunkin",
        "category_slug": "dining",
        "priority": 994
    },
    {
        "pattern": "blue bottle",
        "merchant_name": "Blue Bottle",
        "category_slug": "dining",
        "priority": 989
    },
    {
        "pattern": "peets coffee",
        "merchant_name": "Peets Coffee",
        "category_slug": "dining",
        "priority": 988
    },
    {
        "pattern": "chipotle",
        "merchant_name": "Chipotle",
        "category_slug": "dining",
        "priority": 992
    },
    {
        "pattern": "sweetgreen",
        "merchant_name": "Sweetgreen",
        "category_slug": "dining",
        "priority": 990
    },
    {
        "pattern": "panera",
        "merchant_name": "Panera",
        "category_slug": "dining",
        "priority": 994
    },
    {
        "pattern": "mcdonalds",
        "merchant_name": "Mcdonalds",
        "category_slug": "dining",
        "priority": 991
    },
    {
        "pattern": "burger king",
        "merchant_name": "Burger King",
        "category_slug": "dining",
        "priority": 989
    },
    {
        "pattern": "wendys",
        "merchant_name": "Wendys",
        "category_slug": "dining",
        "priority": 994
    },
    {
        "pattern": "taco bell",
        "merchant_name": "Taco Bell",
        "category_slug": "dining",
        "priority": 991
    },
    {
        "pattern": "subway sandwiches",
        "merchant_name": "Subway Sandwiches",
        "category_slug": "dining",
        "priority": 983
    },
    {
        "pattern": "dominos",
        "merchant_name": "Dominos",
        "category_slug": "dining",
        "priority": 993
    },
    {
        "pattern": "pizza hut",
        "merchant_name": "Pizza Hut",
        "category_slug": "dining",
        "priority": 991
    },
    {
        "pattern": "papa johns",
        "merchant_name": "Papa Johns",
        "category_slug": "dining",
        "priority": 990
    },
    {
        "pattern": "shake shack",
        "merchant_name": "Shake Shack",
        "category_slug": "dining",
        "priority": 989
    },
    {
        "pattern": "five guys",
        "merchant_name": "Five Guys",
        "category_slug": "dining",
        "priority": 991
    },
    {
        "pattern": "in n out",
        "merchant_name": "In N Out",
        "category_slug": "dining",
        "priority": 992
    },
    {
        "pattern": "chick fil a",
        "merchant_name": "Chick Fil A",
        "category_slug": "dining",
        "priority": 989
    },
    {
        "pattern": "olive garden",
        "merchant_name": "Olive Garden",
        "category_slug": "dining",
        "priority": 988
    },
    {
        "pattern": "cheesecake factory",
        "merchant_name": "Cheesecake Factory",
        "category_slug": "dining",
        "priority": 982
    },
    {
        "pattern": "doordash",
        "merchant_name": "Doordash",
        "category_slug": "dining",
        "priority": 992
    },
    {
        "pattern": "uber eats",
        "merchant_name": "Uber Eats",
        "category_slug": "dining",
        "priority": 991
    },
    {
        "pattern": "grubhub",
        "merchant_name": "Grubhub",
        "category_slug": "dining",
        "priority": 993
    },
    {
        "pattern": "postmates",
        "merchant_name": "Postmates",
        "category_slug": "dining",
        "priority": 991
    },
    {
        "pattern": "seamless",
        "merchant_name": "Seamless",
        "category_slug": "dining",
        "priority": 992
    },
    {
        "pattern": "caviar",
        "merchant_name": "Caviar",
        "category_slug": "dining",
        "priority": 994
    },
    {
        "pattern": "panda express",
        "merchant_name": "Panda Express",
        "category_slug": "dining",
        "priority": 987
    },
    {
        "pattern": "kfc",
        "merchant_name": "Kfc",
        "category_slug": "dining",
        "priority": 997
    },
    {
        "pattern": "popeyes",
        "merchant_name": "Popeyes",
        "category_slug": "dining",
        "priority": 993
    },
    {
        "pattern": "dairy queen",
        "merchant_name": "Dairy Queen",
        "category_slug": "dining",
        "priority": 989
    },
    {
        "pattern": "sonic drive",
        "merchant_name": "Sonic Drive",
        "category_slug": "dining",
        "priority": 989
    },
    {
        "pattern": "jimmy johns",
        "merchant_name": "Jimmy Johns",
        "category_slug": "dining",
        "priority": 989
    },
    {
        "pattern": "potbelly",
        "merchant_name": "Potbelly",
        "category_slug": "dining",
        "priority": 992
    },
    {
        "pattern": "noodles company",
        "merchant_name": "Noodles Company",
        "category_slug": "dining",
        "priority": 985
    },
    {
        "pattern": "cava",
        "merchant_name": "Cava",
        "category_slug": "dining",
        "priority": 996
    },
    {
        "pattern": "dig inn",
        "merchant_name": "Dig Inn",
        "category_slug": "dining",
        "priority": 993
    },
    {
        "pattern": "pret a manger",
        "merchant_name": "Pret A Manger",
        "category_slug": "dining",
        "priority": 987
    },
    {
        "pattern": "la colombe",
        "merchant_name": "La Colombe",
        "category_slug": "dining",
        "priority": 990
    },
    {
        "pattern": "philz coffee",
        "merchant_name": "Philz Coffee",
        "category_slug": "dining",
        "priority": 988
    },
    {
        "pattern": "caribou coffee",
        "merchant_name": "Caribou Coffee",
        "category_slug": "dining",
        "priority": 986
    },
    {
        "pattern": "tim hortons",
        "merchant_name": "Tim Hortons",
        "category_slug": "dining",
        "priority": 989
    },
    {
        "pattern": "restaurant",
        "merchant_name": "Restaurant",
        "category_slug": "dining",
        "priority": 990
    },
    {
        "pattern": "cafe",
        "merchant_name": "Cafe",
        "category_slug": "dining",
        "priority": 996
    },
    {
        "pattern": "coffee",
        "merchant_name": "Coffee",
        "category_slug": "dining",
        "priority": 994
    },
    {
        "pattern": "bakery",
        "merchant_name": "Bakery",
        "category_slug": "dining",
        "priority": 994
    },
    {
        "pattern": "brewing",
        "merchant_name": "Brewing",
        "category_slug": "dining",
        "priority": 993
    },
    {
        "pattern": "tavern",
        "merchant_name": "Tavern",
        "category_slug": "dining",
        "priority": 994
    },
    {
        "pattern": "bistro",
        "merchant_name": "Bistro",
        "category_slug": "dining",
        "priority": 994
    },
    {
        "pattern": "grill",
        "merchant_name": "Grill",
        "category_slug": "dining",
        "priority": 995
    },
    {
        "pattern": "pub",
        "merchant_name": "Pub",
        "category_slug": "dining",
        "priority": 997
    },
    {
        "pattern": "uber",
        "merchant_name": "Uber",
        "category_slug": "transport",
        "priority": 996
    },
    {
        "pattern": "lyft",
        "merchant_name": "Lyft",
        "category_slug": "transport",
        "priority": 996
    },
    {
        "pattern": "shell",
        "merchant_name": "Shell",
        "category_slug": "transport",
        "priority": 995
    },
    {
        "pattern": "chevron",
        "merchant_name": "Chevron",
        "category_slug": "transport",
        "priority": 993
    },
    {
        "pattern": "exxon",
        "merchant_name": "Exxon",
        "category_slug": "transport",
        "priority": 995
    },
    {
        "pattern": "mobil",
        "merchant_name": "Mobil",
        "category_slug": "transport",
        "priority": 995
    },
    {
        "pattern": "bp",
        "merchant_name": "Bp",
        "category_slug": "transport",
        "priority": 998
    },
    {
        "pattern": "texaco",
        "merchant_name": "Texaco",
        "category_slug": "transport",
        "priority": 994
    },
    {
        "pattern": "valero",
        "merchant_name": "Valero",
        "category_slug": "transport",
        "priority": 994
    },
    {
        "pattern": "arco",
        "merchant_name": "Arco",
        "category_slug": "transport",
        "priority": 996
    },
    {
        "pattern": "sunoco",
        "merchant_name": "Sunoco",
        "category_slug": "transport",
        "priority": 994
    },
    {
        "pattern": "citgo",
        "merchant_name": "Citgo",
        "category_slug": "transport",
        "priority": 995
    },
    {
        "pattern": "marathon petroleum",
        "merchant_name": "Marathon Petroleum",
        "category_slug": "transport",
        "priority": 982
    },
    {
        "pattern": "speedway",
        "merchant_name": "Speedway",
        "category_slug": "transport",
        "priority": 992
    },
    {
        "pattern": "wawa",
        "merchant_name": "Wawa",
        "category_slug": "transport",
        "priority": 996
    },
    {
        "pattern": "quiktrip",
        "merchant_name": "Quiktrip",
        "category_slug": "transport",
        "priority": 992
    },
    {
        "pattern": "parking",
        "merchant_name": "Parking",
        "category_slug": "transport",
        "priority": 993
    },
    {
        "pattern": "spothero",
        "merchant_name": "Spothero",
        "category_slug": "transport",
        "priority": 992
    },
    {
        "pattern": "metro transit",
        "merchant_name": "Metro Transit",
        "category_slug": "transport",
        "priority": 987
    },
    {
        "pattern": "mta",
        "merchant_name": "Mta",
        "category_slug": "transport",
        "priority": 997
    },
    {
        "pattern": "bart",
        "merchant_name": "Bart",
        "category_slug": "transport",
        "priority": 996
    },
    {
        "pattern": "caltrain",
        "merchant_name": "Caltrain",
        "category_slug": "transport",
        "priority": 992
    },
    {
        "pattern": "amtrak",
        "merchant_name": "Amtrak",
        "category_slug": "transport",
        "priority": 994
    },
    {
        "pattern": "septa",
        "merchant_name": "Septa",
        "category_slug": "transport",
        "priority": 995
    },
    {
        "pattern": "cta",
        "merchant_name": "Cta",
        "category_slug": "transport",
        "priority": 997
    },
    {
        "pattern": "wmata",
        "merchant_name": "Wmata",
        "category_slug": "transport",
        "priority": 995
    },
    {
        "pattern": "zipcar",
        "merchant_name": "Zipcar",
        "category_slug": "transport",
        "priority": 994
    },
    {
        "pattern": "turo",
        "merchant_name": "Turo",
        "category_slug": "transport",
        "priority": 996
    },
    {
        "pattern": "hertz",
        "merchant_name": "Hertz",
        "category_slug": "transport",
        "priority": 995
    },
    {
        "pattern": "avis",
        "merchant_name": "Avis",
        "category_slug": "transport",
        "priority": 996
    },
    {
        "pattern": "enterprise rent",
        "merchant_name": "Enterprise Rent",
        "category_slug": "transport",
        "priority": 985
    },
    {
        "pattern": "budget rent",
        "merchant_name": "Budget Rent",
        "category_slug": "transport",
        "priority": 989
    },
    {
        "pattern": "gas station",
        "merchant_name": "Gas Station",
        "category_slug": "transport",
        "priority": 989
    },
    {
        "pattern": "toll",
        "merchant_name": "Toll",
        "category_slug": "transport",
        "priority": 996
    },
    {
        "pattern": "ez pass",
        "merchant_name": "Ez Pass",
        "category_slug": "transport",
        "priority": 993
    },
    {
        "pattern": "fastrak",
        "merchant_name": "Fastrak",
        "category_slug": "transport",
        "priority": 993
    },
    {
        "pattern": "car wash",
        "merchant_name": "Car Wash",
        "category_slug": "transport",
        "priority": 992
    },
    {
        "pattern": "jiffy lube",
        "merchant_name": "Jiffy Lube",
        "category_slug": "transport",
        "priority": 990
    },
    {
        "pattern": "valvoline",
        "merchant_name": "Valvoline",
        "category_slug": "transport",
        "priority": 991
    },
    {
        "pattern": "autozone",
        "merchant_name": "Autozone",
        "category_slug": "transport",
        "priority": 992
    },
    {
        "pattern": "oreilly auto",
        "merchant_name": "Oreilly Auto",
        "category_slug": "transport",
        "priority": 988
    },
    {
        "pattern": "discount tire",
        "merchant_name": "Discount Tire",
        "category_slug": "transport",
        "priority": 987
    },
    {
        "pattern": "les schwab",
        "merchant_name": "Les Schwab",
        "category_slug": "transport",
        "priority": 990
    },
    {
        "pattern": "amazon",
        "merchant_name": "Amazon",
        "category_slug": "shopping",
        "priority": 994
    },
    {
        "pattern": "target",
        "merchant_name": "Target",
        "category_slug": "shopping",
        "priority": 994
    },
    {
        "pattern": "walmart",
        "merchant_name": "Walmart",
        "category_slug": "shopping",
        "priority": 993
    },
    {
        "pattern": "best buy",
        "merchant_name": "Best Buy",
        "category_slug": "shopping",
        "priority": 992
    },
    {
        "pattern": "home depot",
        "merchant_name": "Home Depot",
        "category_slug": "shopping",
        "priority": 990
    },
    {
        "pattern": "lowes",
        "merchant_name": "Lowes",
        "category_slug": "shopping",
        "priority": 995
    },
    {
        "pattern": "ikea",
        "merchant_name": "Ikea",
        "category_slug": "shopping",
        "priority": 996
    },
    {
        "pattern": "wayfair",
        "merchant_name": "Wayfair",
        "category_slug": "shopping",
        "priority": 993
    },
    {
        "pattern": "etsy",
        "merchant_name": "Etsy",
        "category_slug": "shopping",
        "priority": 996
    },
    {
        "pattern": "ebay",
        "merchant_name": "Ebay",
        "category_slug": "shopping",
        "priority": 996
    },
    {
        "pattern": "macys",
        "merchant_name": "Macys",
        "category_slug": "shopping",
        "priority": 995
    },
    {
        "pattern": "nordstrom",
        "merchant_name": "Nordstrom",
        "category_slug": "shopping",
        "priority": 991
    },
    {
        "pattern": "kohls",
        "merchant_name": "Kohls",
        "category_slug": "shopping",
        "priority": 995
    },
    {
        "pattern": "tj maxx",
        "merchant_name": "Tj Maxx",
        "category_slug": "shopping",
        "priority": 993
    },
    {
        "pattern": "marshalls",
        "merchant_name": "Marshalls",
        "category_slug": "shopping",
        "priority": 991
    },
    {
        "pattern": "ross stores",
        "merchant_name": "Ross Stores",
        "category_slug": "shopping",
        "priority": 989
    },
    {
        "pattern": "old navy",
        "merchant_name": "Old Navy",
        "category_slug": "shopping",
        "priority": 992
    },
    {
        "pattern": "gap",
        "merchant_name": "Gap",
        "category_slug": "shopping",
        "priority": 997
    },
    {
        "pattern": "banana republic",
        "merchant_name": "Banana Republic",
        "category_slug": "shopping",
        "priority": 985
    },
    {
        "pattern": "uniqlo",
        "merchant_name": "Uniqlo",
        "category_slug": "shopping",
        "priority": 994
    },
    {
        "pattern": "zara",
        "merchant_name": "Zara",
        "category_slug": "shopping",
        "priority": 996
    },
    {
        "pattern": "h m",
        "merchant_name": "H M",
        "category_slug": "shopping",
        "priority": 997
    },
    {
        "pattern": "forever 21",
        "merchant_name": "Forever 21",
        "category_slug": "shopping",
        "priority": 990
    },
    {
        "pattern": "lululemon",
        "merchant_name": "Lululemon",
        "category_slug": "shopping",
        "priority": 991
    },
    {
        "pattern": "nike",
        "merchant_name": "Nike",
        "category_slug": "shopping",
        "priority": 996
    },
    {
        "pattern": "adidas",
        "merchant_name": "Adidas",
        "category_slug": "shopping",
        "priority": 994
    },
    {
        "pattern": "foot locker",
        "merchant_name": "Foot Locker",
        "category_slug": "shopping",
        "priority": 989
    },
    {
        "pattern": "rei",
        "merchant_name": "Rei",
        "category_slug": "shopping",
        "priority": 997
    },
    {
        "pattern": "dicks sporting",
        "merchant_name": "Dicks Sporting",
        "category_slug": "shopping",
        "priority": 986
    },
    {
        "pattern": "bath body works",
        "merchant_name": "Bath Body Works",
        "category_slug": "shopping",
        "priority": 985
    },
    {
        "pattern": "sephora",
        "merchant_name": "Sephora",
        "category_slug": "shopping",
        "priority": 993
    },
    {
        "pattern": "ulta",
        "merchant_name": "Ulta",
        "category_slug": "shopping",
        "priority": 996
    },
    {
        "pattern": "michaels",
        "merchant_name": "Michaels",
        "category_slug": "shopping",
        "priority": 992
    },
    {
        "pattern": "joann",
        "merchant_name": "Joann",
        "category_slug": "shopping",
        "priority": 995
    },
    {
        "pattern": "staples",
        "merchant_name": "Staples",
        "category_slug": "shopping",
        "priority": 993
    },
    {
        "pattern": "office depot",
        "merchant_name": "Office Depot",
        "category_slug": "shopping",
        "priority": 988
    },
    {
        "pattern": "container store",
        "merchant_name": "Container Store",
        "category_slug": "shopping",
        "priority": 985
    },
    {
        "pattern": "williams sonoma",
        "merchant_name": "Williams Sonoma",
        "category_slug": "shopping",
        "priority": 985
    },
    {
        "pattern": "crate barrel",
        "merchant_name": "Crate Barrel",
        "category_slug": "shopping",
        "priority": 988
    },
    {
        "pattern": "west elm",
        "merchant_name": "West Elm",
        "category_slug": "shopping",
        "priority": 992
    },
    {
        "pattern": "pottery barn",
        "merchant_name": "Pottery Barn",
        "category_slug": "shopping",
        "priority": 988
    },
    {
        "pattern": "apple store",
        "merchant_name": "Apple Store",
        "category_slug": "shopping",
        "priority": 989
    },
    {
        "pattern": "newegg",
        "merchant_name": "Newegg",
        "category_slug": "shopping",
        "priority": 994
    },
    {
        "pattern": "chewy",
        "merchant_name": "Chewy",
        "category_slug": "shopping",
        "priority": 995
    },
    {
        "pattern": "petco",
        "merchant_name": "Petco",
        "category_slug": "shopping",
        "priority": 995
    },
    {
        "pattern": "petsmart",
        "merchant_name": "Petsmart",
        "category_slug": "shopping",
        "priority": 992
    },
    {
        "pattern": "netflix",
        "merchant_name": "Netflix",
        "category_slug": "subscriptions",
        "priority": 993
    },
    {
        "pattern": "spotify",
        "merchant_name": "Spotify",
        "category_slug": "subscriptions",
        "priority": 993
    },
    {
        "pattern": "hulu",
        "merchant_name": "Hulu",
        "category_slug": "subscriptions",
        "priority": 996
    },
    {
        "pattern": "disney plus",
        "merchant_name": "Disney Plus",
        "category_slug": "subscriptions",
        "priority": 989
    },
    {
        "pattern": "disneyplus",
        "merchant_name": "Disneyplus",
        "category_slug": "subscriptions",
        "priority": 990
    },
    {
        "pattern": "hbo max",
        "merchant_name": "Hbo Max",
        "category_slug": "subscriptions",
        "priority": 993
    },
    {
        "pattern": "max com",
        "merchant_name": "Max Com",
        "category_slug": "subscriptions",
        "priority": 993
    },
    {
        "pattern": "peacock",
        "merchant_name": "Peacock",
        "category_slug": "subscriptions",
        "priority": 993
    },
    {
        "pattern": "paramount plus",
        "merchant_name": "Paramount Plus",
        "category_slug": "subscriptions",
        "priority": 986
    },
    {
        "pattern": "apple tv",
        "merchant_name": "Apple Tv",
        "category_slug": "subscriptions",
        "priority": 992
    },
    {
        "pattern": "apple music",
        "merchant_name": "Apple Music",
        "category_slug": "subscriptions",
        "priority": 989
    },
    {
        "pattern": "apple icloud",
        "merchant_name": "Apple Icloud",
        "category_slug": "subscriptions",
        "priority": 988
    },
    {
        "pattern": "icloud",
        "merchant_name": "Icloud",
        "category_slug": "subscriptions",
        "priority": 994
    },
    {
        "pattern": "youtube premium",
        "merchant_name": "Youtube Premium",
        "category_slug": "subscriptions",
        "priority": 985
    },
    {
        "pattern": "amazon prime",
        "merchant_name": "Amazon Prime",
        "category_slug": "subscriptions",
        "priority": 988
    },
    {
        "pattern": "audible",
        "merchant_name": "Audible",
        "category_slug": "subscriptions",
        "priority": 993
    },
    {
        "pattern": "kindle unlimited",
        "merchant_name": "Kindle Unlimited",
        "category_slug": "subscriptions",
        "priority": 984
    },
    {
        "pattern": "adobe",
        "merchant_name": "Adobe",
        "category_slug": "subscriptions",
        "priority": 995
    },
    {
        "pattern": "microsoft 365",
        "merchant_name": "Microsoft 365",
        "category_slug": "subscriptions",
        "priority": 987
    },
    {
        "pattern": "office 365",
        "merchant_name": "Office 365",
        "category_slug": "subscriptions",
        "priority": 990
    },
    {
        "pattern": "dropbox",
        "merchant_name": "Dropbox",
        "category_slug": "subscriptions",
        "priority": 993
    },
    {
        "pattern": "notion",
        "merchant_name": "Notion",
        "category_slug": "subscriptions",
        "priority": 994
    },
    {
        "pattern": "figma",
        "merchant_name": "Figma",
        "category_slug": "subscriptions",
        "priority": 995
    },
    {
        "pattern": "canva",
        "merchant_name": "Canva",
        "category_slug": "subscriptions",
        "priority": 995
    },
    {
        "pattern": "github",
        "merchant_name": "Github",
        "category_slug": "subscriptions",
        "priority": 994
    },
    {
        "pattern": "openai",
        "merchant_name": "Openai",
        "category_slug": "subscriptions",
        "priority": 994
    },
    {
        "pattern": "anthropic",
        "merchant_name": "Anthropic",
        "category_slug": "subscriptions",
        "priority": 991
    },
    {
        "pattern": "chatgpt",
        "merchant_name": "Chatgpt",
        "category_slug": "subscriptions",
        "priority": 993
    },
    {
        "pattern": "claude ai",
        "merchant_name": "Claude Ai",
        "category_slug": "subscriptions",
        "priority": 991
    },
    {
        "pattern": "grammarly",
        "merchant_name": "Grammarly",
        "category_slug": "subscriptions",
        "priority": 991
    },
    {
        "pattern": "1password",
        "merchant_name": "1Password",
        "category_slug": "subscriptions",
        "priority": 991
    },
    {
        "pattern": "lastpass",
        "merchant_name": "Lastpass",
        "category_slug": "subscriptions",
        "priority": 992
    },
    {
        "pattern": "nordvpn",
        "merchant_name": "Nordvpn",
        "category_slug": "subscriptions",
        "priority": 993
    },
    {
        "pattern": "expressvpn",
        "merchant_name": "Expressvpn",
        "category_slug": "subscriptions",
        "priority": 990
    },
    {
        "pattern": "duolingo",
        "merchant_name": "Duolingo",
        "category_slug": "subscriptions",
        "priority": 992
    },
    {
        "pattern": "masterclass",
        "merchant_name": "Masterclass",
        "category_slug": "subscriptions",
        "priority": 989
    },
    {
        "pattern": "coursera",
        "merchant_name": "Coursera",
        "category_slug": "subscriptions",
        "priority": 992
    },
    {
        "pattern": "udemy",
        "merchant_name": "Udemy",
        "category_slug": "subscriptions",
        "priority": 995
    },
    {
        "pattern": "skillshare",
        "merchant_name": "Skillshare",
        "category_slug": "subscriptions",
        "priority": 990
    },
    {
        "pattern": "medium com",
        "merchant_name": "Medium Com",
        "category_slug": "subscriptions",
        "priority": 990
    },
    {
        "pattern": "substack",
        "merchant_name": "Substack",
        "category_slug": "subscriptions",
        "priority": 992
    },
    {
        "pattern": "patreon",
        "merchant_name": "Patreon",
        "category_slug": "subscriptions",
        "priority": 993
    },
    {
        "pattern": "new york times",
        "merchant_name": "New York Times",
        "category_slug": "subscriptions",
        "priority": 986
    },
    {
        "pattern": "wall street journal",
        "merchant_name": "Wall Street Journal",
        "category_slug": "subscriptions",
        "priority": 981
    },
    {
        "pattern": "washington post",
        "merchant_name": "Washington Post",
        "category_slug": "subscriptions",
        "priority": 985
    },
    {
        "pattern": "the athletic",
        "merchant_name": "The Athletic",
        "category_slug": "subscriptions",
        "priority": 988
    },
    {
        "pattern": "strava",
        "merchant_name": "Strava",
        "category_slug": "subscriptions",
        "priority": 994
    },
    {
        "pattern": "calm com",
        "merchant_name": "Calm Com",
        "category_slug": "subscriptions",
        "priority": 992
    },
    {
        "pattern": "headspace",
        "merchant_name": "Headspace",
        "category_slug": "subscriptions",
        "priority": 991
    },
    {
        "pattern": "ancestry",
        "merchant_name": "Ancestry",
        "category_slug": "subscriptions",
        "priority": 992
    },
    {
        "pattern": "linkedin premium",
        "merchant_name": "Linkedin Premium",
        "category_slug": "subscriptions",
        "priority": 984
    },
    {
        "pattern": "zoom video",
        "merchant_name": "Zoom Video",
        "category_slug": "subscriptions",
        "priority": 990
    },
    {
        "pattern": "slack technologies",
        "merchant_name": "Slack Technologies",
        "category_slug": "subscriptions",
        "priority": 982
    },
    {
        "pattern": "comcast",
        "merchant_name": "Comcast",
        "category_slug": "utilities",
        "priority": 993
    },
    {
        "pattern": "xfinity",
        "merchant_name": "Xfinity",
        "category_slug": "utilities",
        "priority": 993
    },
    {
        "pattern": "spectrum",
        "merchant_name": "Spectrum",
        "category_slug": "utilities",
        "priority": 992
    },
    {
        "pattern": "verizon",
        "merchant_name": "Verizon",
        "category_slug": "utilities",
        "priority": 993
    },
    {
        "pattern": "at t wireless",
        "merchant_name": "At T Wireless",
        "category_slug": "utilities",
        "priority": 987
    },
    {
        "pattern": "att wireless",
        "merchant_name": "Att Wireless",
        "category_slug": "utilities",
        "priority": 988
    },
    {
        "pattern": "t mobile",
        "merchant_name": "T Mobile",
        "category_slug": "utilities",
        "priority": 992
    },
    {
        "pattern": "tmobile",
        "merchant_name": "Tmobile",
        "category_slug": "utilities",
        "priority": 993
    },
    {
        "pattern": "sprint",
        "merchant_name": "Sprint",
        "category_slug": "utilities",
        "priority": 994
    },
    {
        "pattern": "google fi",
        "merchant_name": "Google Fi",
        "category_slug": "utilities",
        "priority": 991
    },
    {
        "pattern": "mint mobile",
        "merchant_name": "Mint Mobile",
        "category_slug": "utilities",
        "priority": 989
    },
    {
        "pattern": "visible wireless",
        "merchant_name": "Visible Wireless",
        "category_slug": "utilities",
        "priority": 984
    },
    {
        "pattern": "cox communications",
        "merchant_name": "Cox Communications",
        "category_slug": "utilities",
        "priority": 982
    },
    {
        "pattern": "centurylink",
        "merchant_name": "Centurylink",
        "category_slug": "utilities",
        "priority": 989
    },
    {
        "pattern": "frontier communications",
        "merchant_name": "Frontier Communications",
        "category_slug": "utilities",
        "priority": 977
    },
    {
        "pattern": "pge",
        "merchant_name": "Pge",
        "category_slug": "utilities",
        "priority": 997
    },
    {
        "pattern": "pacific gas",
        "merchant_name": "Pacific Gas",
        "category_slug": "utilities",
        "priority": 989
    },
    {
        "pattern": "con edison",
        "merchant_name": "Con Edison",
        "category_slug": "utilities",
        "priority": 990
    },
    {
        "pattern": "duke energy",
        "merchant_name": "Duke Energy",
        "category_slug": "utilities",
        "priority": 989
    },
    {
        "pattern": "southern california edison",
        "merchant_name": "Southern California Edison",
        "category_slug": "utilities",
        "priority": 974
    },
    {
        "pattern": "national grid",
        "merchant_name": "National Grid",
        "category_slug": "utilities",
        "priority": 987
    },
    {
        "pattern": "dominion energy",
        "merchant_name": "Dominion Energy",
        "category_slug": "utilities",
        "priority": 985
    },
    {
        "pattern": "xcel energy",
        "merchant_name": "Xcel Energy",
        "category_slug": "utilities",
        "priority": 989
    },
    {
        "pattern": "ameren",
        "merchant_name": "Ameren",
        "category_slug": "utilities",
        "priority": 994
    },
    {
        "pattern": "dte energy",
        "merchant_name": "Dte Energy",
        "category_slug": "utilities",
        "priority": 990
    },
    {
        "pattern": "nicor",
        "merchant_name": "Nicor",
        "category_slug": "utilities",
        "priority": 995
    },
    {
        "pattern": "water district",
        "merchant_name": "Water District",
        "category_slug": "utilities",
        "priority": 986
    },
    {
        "pattern": "waste management",
        "merchant_name": "Waste Management",
        "category_slug": "utilities",
        "priority": 984
    },
    {
        "pattern": "republic services",
        "merchant_name": "Republic Services",
        "category_slug": "utilities",
        "priority": 983
    },
    {
        "pattern": "city utilities",
        "merchant_name": "City Utilities",
        "category_slug": "utilities",
        "priority": 986
    },
    {
        "pattern": "electric company",
        "merchant_name": "Electric Company",
        "category_slug": "utilities",
        "priority": 984
    },
    {
        "pattern": "internet service",
        "merchant_name": "Internet Service",
        "category_slug": "utilities",
        "priority": 984
    },
    {
        "pattern": "rent payment",
        "merchant_name": "Rent Payment",
        "category_slug": "housing",
        "priority": 988
    },
    {
        "pattern": "apartment",
        "merchant_name": "Apartment",
        "category_slug": "housing",
        "priority": 991
    },
    {
        "pattern": "property management",
        "merchant_name": "Property Management",
        "category_slug": "housing",
        "priority": 981
    },
    {
        "pattern": "mortgage",
        "merchant_name": "Mortgage",
        "category_slug": "housing",
        "priority": 992
    },
    {
        "pattern": "hoa",
        "merchant_name": "Hoa",
        "category_slug": "housing",
        "priority": 997
    },
    {
        "pattern": "landlord",
        "merchant_name": "Landlord",
        "category_slug": "housing",
        "priority": 992
    },
    {
        "pattern": "zillow rental",
        "merchant_name": "Zillow Rental",
        "category_slug": "housing",
        "priority": 987
    },
    {
        "pattern": "avail rent",
        "merchant_name": "Avail Rent",
        "category_slug": "housing",
        "priority": 990
    },
    {
        "pattern": "buildium",
        "merchant_name": "Buildium",
        "category_slug": "housing",
        "priority": 992
    },
    {
        "pattern": "appfolio",
        "merchant_name": "Appfolio",
        "category_slug": "housing",
        "priority": 992
    },
    {
        "pattern": "realpage",
        "merchant_name": "Realpage",
        "category_slug": "housing",
        "priority": 992
    },
    {
        "pattern": "greystar",
        "merchant_name": "Greystar",
        "category_slug": "housing",
        "priority": 992
    },
    {
        "pattern": "equity residential",
        "merchant_name": "Equity Residential",
        "category_slug": "housing",
        "priority": 982
    },
    {
        "pattern": "home insurance",
        "merchant_name": "Home Insurance",
        "category_slug": "housing",
        "priority": 986
    },
    {
        "pattern": "renters insurance",
        "merchant_name": "Renters Insurance",
        "category_slug": "housing",
        "priority": 983
    },
    {
        "pattern": "storage unit",
        "merchant_name": "Storage Unit",
        "category_slug": "housing",
        "priority": 988
    },
    {
        "pattern": "public storage",
        "merchant_name": "Public Storage",
        "category_slug": "housing",
        "priority": 986
    },
    {
        "pattern": "extra space storage",
        "merchant_name": "Extra Space Storage",
        "category_slug": "housing",
        "priority": 981
    },
    {
        "pattern": "cubesmart",
        "merchant_name": "Cubesmart",
        "category_slug": "housing",
        "priority": 991
    },
    {
        "pattern": "cvs",
        "merchant_name": "Cvs",
        "category_slug": "health",
        "priority": 997
    },
    {
        "pattern": "walgreens",
        "merchant_name": "Walgreens",
        "category_slug": "health",
        "priority": 991
    },
    {
        "pattern": "rite aid",
        "merchant_name": "Rite Aid",
        "category_slug": "health",
        "priority": 992
    },
    {
        "pattern": "pharmacy",
        "merchant_name": "Pharmacy",
        "category_slug": "health",
        "priority": 992
    },
    {
        "pattern": "quest diagnostics",
        "merchant_name": "Quest Diagnostics",
        "category_slug": "health",
        "priority": 983
    },
    {
        "pattern": "labcorp",
        "merchant_name": "Labcorp",
        "category_slug": "health",
        "priority": 993
    },
    {
        "pattern": "kaiser",
        "merchant_name": "Kaiser",
        "category_slug": "health",
        "priority": 994
    },
    {
        "pattern": "blue cross",
        "merchant_name": "Blue Cross",
        "category_slug": "health",
        "priority": 990
    },
    {
        "pattern": "blue shield",
        "merchant_name": "Blue Shield",
        "category_slug": "health",
        "priority": 989
    },
    {
        "pattern": "aetna",
        "merchant_name": "Aetna",
        "category_slug": "health",
        "priority": 995
    },
    {
        "pattern": "cigna",
        "merchant_name": "Cigna",
        "category_slug": "health",
        "priority": 995
    },
    {
        "pattern": "united healthcare",
        "merchant_name": "United Healthcare",
        "category_slug": "health",
        "priority": 983
    },
    {
        "pattern": "humana",
        "merchant_name": "Humana",
        "category_slug": "health",
        "priority": 994
    },
    {
        "pattern": "dentist",
        "merchant_name": "Dentist",
        "category_slug": "health",
        "priority": 993
    },
    {
        "pattern": "dental",
        "merchant_name": "Dental",
        "category_slug": "health",
        "priority": 994
    },
    {
        "pattern": "optometry",
        "merchant_name": "Optometry",
        "category_slug": "health",
        "priority": 991
    },
    {
        "pattern": "vision center",
        "merchant_name": "Vision Center",
        "category_slug": "health",
        "priority": 987
    },
    {
        "pattern": "warby parker",
        "merchant_name": "Warby Parker",
        "category_slug": "health",
        "priority": 988
    },
    {
        "pattern": "lenscrafters",
        "merchant_name": "Lenscrafters",
        "category_slug": "health",
        "priority": 988
    },
    {
        "pattern": "planet fitness",
        "merchant_name": "Planet Fitness",
        "category_slug": "health",
        "priority": 986
    },
    {
        "pattern": "equinox",
        "merchant_name": "Equinox",
        "category_slug": "health",
        "priority": 993
    },
    {
        "pattern": "la fitness",
        "merchant_name": "La Fitness",
        "category_slug": "health",
        "priority": 990
    },
    {
        "pattern": "gold gym",
        "merchant_name": "Gold Gym",
        "category_slug": "health",
        "priority": 992
    },
    {
        "pattern": "orangetheory",
        "merchant_name": "Orangetheory",
        "category_slug": "health",
        "priority": 988
    },
    {
        "pattern": "f45",
        "merchant_name": "F45",
        "category_slug": "health",
        "priority": 997
    },
    {
        "pattern": "crossfit",
        "merchant_name": "Crossfit",
        "category_slug": "health",
        "priority": 992
    },
    {
        "pattern": "soulcycle",
        "merchant_name": "Soulcycle",
        "category_slug": "health",
        "priority": 991
    },
    {
        "pattern": "peloton",
        "merchant_name": "Peloton",
        "category_slug": "health",
        "priority": 993
    },
    {
        "pattern": "classpass",
        "merchant_name": "Classpass",
        "category_slug": "health",
        "priority": 991
    },
    {
        "pattern": "barrys bootcamp",
        "merchant_name": "Barrys Bootcamp",
        "category_slug": "health",
        "priority": 985
    },
    {
        "pattern": "ymca",
        "merchant_name": "Ymca",
        "category_slug": "health",
        "priority": 996
    },
    {
        "pattern": "physical therapy",
        "merchant_name": "Physical Therapy",
        "category_slug": "health",
        "priority": 984
    },
    {
        "pattern": "urgent care",
        "merchant_name": "Urgent Care",
        "category_slug": "health",
        "priority": 989
    },
    {
        "pattern": "medical center",
        "merchant_name": "Medical Center",
        "category_slug": "health",
        "priority": 986
    },
    {
        "pattern": "hospital",
        "merchant_name": "Hospital",
        "category_slug": "health",
        "priority": 992
    },
    {
        "pattern": "clinic",
        "merchant_name": "Clinic",
        "category_slug": "health",
        "priority": 994
    },
    {
        "pattern": "teladoc",
        "merchant_name": "Teladoc",
        "category_slug": "health",
        "priority": 993
    },
    {
        "pattern": "hims hers",
        "merchant_name": "Hims Hers",
        "category_slug": "health",
        "priority": 991
    },
    {
        "pattern": "ro health",
        "merchant_name": "Ro Health",
        "category_slug": "health",
        "priority": 991
    },
    {
        "pattern": "zocdoc",
        "merchant_name": "Zocdoc",
        "category_slug": "health",
        "priority": 994
    },
    {
        "pattern": "amc theatres",
        "merchant_name": "Amc Theatres",
        "category_slug": "entertainment",
        "priority": 988
    },
    {
        "pattern": "regal cinemas",
        "merchant_name": "Regal Cinemas",
        "category_slug": "entertainment",
        "priority": 987
    },
    {
        "pattern": "cinemark",
        "merchant_name": "Cinemark",
        "category_slug": "entertainment",
        "priority": 992
    },
    {
        "pattern": "alamo drafthouse",
        "merchant_name": "Alamo Drafthouse",
        "category_slug": "entertainment",
        "priority": 984
    },
    {
        "pattern": "movie theater",
        "merchant_name": "Movie Theater",
        "category_slug": "entertainment",
        "priority": 987
    },
    {
        "pattern": "ticketmaster",
        "merchant_name": "Ticketmaster",
        "category_slug": "entertainment",
        "priority": 988
    },
    {
        "pattern": "stubhub",
        "merchant_name": "Stubhub",
        "category_slug": "entertainment",
        "priority": 993
    },
    {
        "pattern": "seatgeek",
        "merchant_name": "Seatgeek",
        "category_slug": "entertainment",
        "priority": 992
    },
    {
        "pattern": "axs com",
        "merchant_name": "Axs Com",
        "category_slug": "entertainment",
        "priority": 993
    },
    {
        "pattern": "eventbrite",
        "merchant_name": "Eventbrite",
        "category_slug": "entertainment",
        "priority": 990
    },
    {
        "pattern": "live nation",
        "merchant_name": "Live Nation",
        "category_slug": "entertainment",
        "priority": 989
    },
    {
        "pattern": "steam games",
        "merchant_name": "Steam Games",
        "category_slug": "entertainment",
        "priority": 989
    },
    {
        "pattern": "steampowered",
        "merchant_name": "Steampowered",
        "category_slug": "entertainment",
        "priority": 988
    },
    {
        "pattern": "playstation",
        "merchant_name": "Playstation",
        "category_slug": "entertainment",
        "priority": 989
    },
    {
        "pattern": "xbox",
        "merchant_name": "Xbox",
        "category_slug": "entertainment",
        "priority": 996
    },
    {
        "pattern": "nintendo",
        "merchant_name": "Nintendo",
        "category_slug": "entertainment",
        "priority": 992
    },
    {
        "pattern": "epic games",
        "merchant_name": "Epic Games",
        "category_slug": "entertainment",
        "priority": 990
    },
    {
        "pattern": "riot games",
        "merchant_name": "Riot Games",
        "category_slug": "entertainment",
        "priority": 990
    },
    {
        "pattern": "blizzard",
        "merchant_name": "Blizzard",
        "category_slug": "entertainment",
        "priority": 992
    },
    {
        "pattern": "humble bundle",
        "merchant_name": "Humble Bundle",
        "category_slug": "entertainment",
        "priority": 987
    },
    {
        "pattern": "twitch",
        "merchant_name": "Twitch",
        "category_slug": "entertainment",
        "priority": 994
    },
    {
        "pattern": "patreon creator",
        "merchant_name": "Patreon Creator",
        "category_slug": "entertainment",
        "priority": 985
    },
    {
        "pattern": "bowling",
        "merchant_name": "Bowling",
        "category_slug": "entertainment",
        "priority": 993
    },
    {
        "pattern": "golf club",
        "merchant_name": "Golf Club",
        "category_slug": "entertainment",
        "priority": 991
    },
    {
        "pattern": "museum",
        "merchant_name": "Museum",
        "category_slug": "entertainment",
        "priority": 994
    },
    {
        "pattern": "aquarium",
        "merchant_name": "Aquarium",
        "category_slug": "entertainment",
        "priority": 992
    },
    {
        "pattern": "zoo",
        "merchant_name": "Zoo",
        "category_slug": "entertainment",
        "priority": 997
    },
    {
        "pattern": "theme park",
        "merchant_name": "Theme Park",
        "category_slug": "entertainment",
        "priority": 990
    },
    {
        "pattern": "six flags",
        "merchant_name": "Six Flags",
        "category_slug": "entertainment",
        "priority": 991
    },
    {
        "pattern": "cedar point",
        "merchant_name": "Cedar Point",
        "category_slug": "entertainment",
        "priority": 989
    },
    {
        "pattern": "universal studios",
        "merchant_name": "Universal Studios",
        "category_slug": "entertainment",
        "priority": 983
    },
    {
        "pattern": "disneyland",
        "merchant_name": "Disneyland",
        "category_slug": "entertainment",
        "priority": 990
    },
    {
        "pattern": "walt disney world",
        "merchant_name": "Walt Disney World",
        "category_slug": "entertainment",
        "priority": 983
    },
    {
        "pattern": "concert",
        "merchant_name": "Concert",
        "category_slug": "entertainment",
        "priority": 993
    },
    {
        "pattern": "comedy club",
        "merchant_name": "Comedy Club",
        "category_slug": "entertainment",
        "priority": 989
    },
    {
        "pattern": "united airlines",
        "merchant_name": "United Airlines",
        "category_slug": "travel",
        "priority": 985
    },
    {
        "pattern": "delta air",
        "merchant_name": "Delta Air",
        "category_slug": "travel",
        "priority": 991
    },
    {
        "pattern": "american airlines",
        "merchant_name": "American Airlines",
        "category_slug": "travel",
        "priority": 983
    },
    {
        "pattern": "southwest airlines",
        "merchant_name": "Southwest Airlines",
        "category_slug": "travel",
        "priority": 982
    },
    {
        "pattern": "jetblue",
        "merchant_name": "Jetblue",
        "category_slug": "travel",
        "priority": 993
    },
    {
        "pattern": "alaska airlines",
        "merchant_name": "Alaska Airlines",
        "category_slug": "travel",
        "priority": 985
    },
    {
        "pattern": "spirit airlines",
        "merchant_name": "Spirit Airlines",
        "category_slug": "travel",
        "priority": 985
    },
    {
        "pattern": "frontier airlines",
        "merchant_name": "Frontier Airlines",
        "category_slug": "travel",
        "priority": 983
    },
    {
        "pattern": "airbnb",
        "merchant_name": "Airbnb",
        "category_slug": "travel",
        "priority": 994
    },
    {
        "pattern": "vrbo",
        "merchant_name": "Vrbo",
        "category_slug": "travel",
        "priority": 996
    },
    {
        "pattern": "booking com",
        "merchant_name": "Booking Com",
        "category_slug": "travel",
        "priority": 989
    },
    {
        "pattern": "expedia",
        "merchant_name": "Expedia",
        "category_slug": "travel",
        "priority": 993
    },
    {
        "pattern": "hotels com",
        "merchant_name": "Hotels Com",
        "category_slug": "travel",
        "priority": 990
    },
    {
        "pattern": "priceline",
        "merchant_name": "Priceline",
        "category_slug": "travel",
        "priority": 991
    },
    {
        "pattern": "kayak",
        "merchant_name": "Kayak",
        "category_slug": "travel",
        "priority": 995
    },
    {
        "pattern": "marriott",
        "merchant_name": "Marriott",
        "category_slug": "travel",
        "priority": 992
    },
    {
        "pattern": "hilton",
        "merchant_name": "Hilton",
        "category_slug": "travel",
        "priority": 994
    },
    {
        "pattern": "hyatt",
        "merchant_name": "Hyatt",
        "category_slug": "travel",
        "priority": 995
    },
    {
        "pattern": "ihg",
        "merchant_name": "Ihg",
        "category_slug": "travel",
        "priority": 997
    },
    {
        "pattern": "wyndham",
        "merchant_name": "Wyndham",
        "category_slug": "travel",
        "priority": 993
    },
    {
        "pattern": "best western",
        "merchant_name": "Best Western",
        "category_slug": "travel",
        "priority": 988
    },
    {
        "pattern": "holiday inn",
        "merchant_name": "Holiday Inn",
        "category_slug": "travel",
        "priority": 989
    },
    {
        "pattern": "motel",
        "merchant_name": "Motel",
        "category_slug": "travel",
        "priority": 995
    },
    {
        "pattern": "hostel",
        "merchant_name": "Hostel",
        "category_slug": "travel",
        "priority": 994
    },
    {
        "pattern": "travelocity",
        "merchant_name": "Travelocity",
        "category_slug": "travel",
        "priority": 989
    },
    {
        "pattern": "orbitz",
        "merchant_name": "Orbitz",
        "category_slug": "travel",
        "priority": 994
    },
    {
        "pattern": "tripadvisor",
        "merchant_name": "Tripadvisor",
        "category_slug": "travel",
        "priority": 989
    },
    {
        "pattern": "viator",
        "merchant_name": "Viator",
        "category_slug": "travel",
        "priority": 994
    },
    {
        "pattern": "get your guide",
        "merchant_name": "Get Your Guide",
        "category_slug": "travel",
        "priority": 986
    },
    {
        "pattern": "global entry",
        "merchant_name": "Global Entry",
        "category_slug": "travel",
        "priority": 988
    },
    {
        "pattern": "tsa precheck",
        "merchant_name": "Tsa Precheck",
        "category_slug": "travel",
        "priority": 988
    },
    {
        "pattern": "passport",
        "merchant_name": "Passport",
        "category_slug": "travel",
        "priority": 992
    },
    {
        "pattern": "currency exchange",
        "merchant_name": "Currency Exchange",
        "category_slug": "travel",
        "priority": 983
    },
    {
        "pattern": "baggage fee",
        "merchant_name": "Baggage Fee",
        "category_slug": "travel",
        "priority": 989
    },
    {
        "pattern": "payroll",
        "merchant_name": "Payroll",
        "category_slug": "income",
        "priority": 993
    },
    {
        "pattern": "direct deposit",
        "merchant_name": "Direct Deposit",
        "category_slug": "income",
        "priority": 986
    },
    {
        "pattern": "salary",
        "merchant_name": "Salary",
        "category_slug": "income",
        "priority": 994
    },
    {
        "pattern": "paycheck",
        "merchant_name": "Paycheck",
        "category_slug": "income",
        "priority": 992
    },
    {
        "pattern": "employer",
        "merchant_name": "Employer",
        "category_slug": "income",
        "priority": 992
    },
    {
        "pattern": "adp payroll",
        "merchant_name": "Adp Payroll",
        "category_slug": "income",
        "priority": 989
    },
    {
        "pattern": "gusto",
        "merchant_name": "Gusto",
        "category_slug": "income",
        "priority": 995
    },
    {
        "pattern": "paychex",
        "merchant_name": "Paychex",
        "category_slug": "income",
        "priority": 993
    },
    {
        "pattern": "workday payroll",
        "merchant_name": "Workday Payroll",
        "category_slug": "income",
        "priority": 985
    },
    {
        "pattern": "interest paid",
        "merchant_name": "Interest Paid",
        "category_slug": "income",
        "priority": 987
    },
    {
        "pattern": "dividend",
        "merchant_name": "Dividend",
        "category_slug": "income",
        "priority": 992
    },
    {
        "pattern": "refund",
        "merchant_name": "Refund",
        "category_slug": "income",
        "priority": 994
    },
    {
        "pattern": "reimbursement",
        "merchant_name": "Reimbursement",
        "category_slug": "income",
        "priority": 987
    },
    {
        "pattern": "tax refund",
        "merchant_name": "Tax Refund",
        "category_slug": "income",
        "priority": 990
    },
    {
        "pattern": "irs treas",
        "merchant_name": "Irs Treas",
        "category_slug": "income",
        "priority": 991
    },
    {
        "pattern": "stripe payout",
        "merchant_name": "Stripe Payout",
        "category_slug": "income",
        "priority": 987
    },
    {
        "pattern": "paypal transfer in",
        "merchant_name": "Paypal Transfer In",
        "category_slug": "income",
        "priority": 982
    },
    {
        "pattern": "venmo",
        "merchant_name": "Venmo",
        "category_slug": "transfers",
        "priority": 995
    },
    {
        "pattern": "zelle",
        "merchant_name": "Zelle",
        "category_slug": "transfers",
        "priority": 995
    },
    {
        "pattern": "cash app",
        "merchant_name": "Cash App",
        "category_slug": "transfers",
        "priority": 992
    },
    {
        "pattern": "paypal",
        "merchant_name": "Paypal",
        "category_slug": "transfers",
        "priority": 994
    },
    {
        "pattern": "transfer to savings",
        "merchant_name": "Transfer To Savings",
        "category_slug": "transfers",
        "priority": 981
    },
    {
        "pattern": "transfer from",
        "merchant_name": "Transfer From",
        "category_slug": "transfers",
        "priority": 987
    },
    {
        "pattern": "online transfer",
        "merchant_name": "Online Transfer",
        "category_slug": "transfers",
        "priority": 985
    },
    {
        "pattern": "internal transfer",
        "merchant_name": "Internal Transfer",
        "category_slug": "transfers",
        "priority": 983
    },
    {
        "pattern": "credit card payment",
        "merchant_name": "Credit Card Payment",
        "category_slug": "transfers",
        "priority": 981
    },
    {
        "pattern": "autopay",
        "merchant_name": "Autopay",
        "category_slug": "transfers",
        "priority": 993
    },
    {
        "pattern": "card payment thank you",
        "merchant_name": "Card Payment Thank You",
        "category_slug": "transfers",
        "priority": 978
    },
    {
        "pattern": "wire transfer",
        "merchant_name": "Wire Transfer",
        "category_slug": "transfers",
        "priority": 987
    },
    {
        "pattern": "ach transfer",
        "merchant_name": "Ach Transfer",
        "category_slug": "transfers",
        "priority": 988
    },
    {
        "pattern": "robinhood",
        "merchant_name": "Robinhood",
        "category_slug": "transfers",
        "priority": 991
    },
    {
        "pattern": "coinbase",
        "merchant_name": "Coinbase",
        "category_slug": "transfers",
        "priority": 992
    },
    {
        "pattern": "fidelity",
        "merchant_name": "Fidelity",
        "category_slug": "transfers",
        "priority": 992
    },
    {
        "pattern": "vanguard",
        "merchant_name": "Vanguard",
        "category_slug": "transfers",
        "priority": 992
    },
    {
        "pattern": "schwab",
        "merchant_name": "Schwab",
        "category_slug": "transfers",
        "priority": 994
    },
    {
        "pattern": "etrade",
        "merchant_name": "Etrade",
        "category_slug": "transfers",
        "priority": 994
    },
    {
        "pattern": "betterment",
        "merchant_name": "Betterment",
        "category_slug": "transfers",
        "priority": 990
    },
    {
        "pattern": "wealthfront",
        "merchant_name": "Wealthfront",
        "category_slug": "transfers",
        "priority": 989
    },
    {
        "pattern": "acorns",
        "merchant_name": "Acorns",
        "category_slug": "transfers",
        "priority": 994
    },
    {
        "pattern": "sofi invest",
        "merchant_name": "Sofi Invest",
        "category_slug": "transfers",
        "priority": 989
    },
    {
        "pattern": "ally bank transfer",
        "merchant_name": "Ally Bank Transfer",
        "category_slug": "transfers",
        "priority": 982
    },
    {
        "pattern": "overdraft fee",
        "merchant_name": "Overdraft Fee",
        "category_slug": "fees",
        "priority": 987
    },
    {
        "pattern": "service charge",
        "merchant_name": "Service Charge",
        "category_slug": "fees",
        "priority": 986
    },
    {
        "pattern": "monthly maintenance fee",
        "merchant_name": "Monthly Maintenance Fee",
        "category_slug": "fees",
        "priority": 977
    },
    {
        "pattern": "atm fee",
        "merchant_name": "Atm Fee",
        "category_slug": "fees",
        "priority": 993
    },
    {
        "pattern": "foreign transaction fee",
        "merchant_name": "Foreign Transaction Fee",
        "category_slug": "fees",
        "priority": 977
    },
    {
        "pattern": "late fee",
        "merchant_name": "Late Fee",
        "category_slug": "fees",
        "priority": 992
    },
    {
        "pattern": "annual fee",
        "merchant_name": "Annual Fee",
        "category_slug": "fees",
        "priority": 990
    },
    {
        "pattern": "interest charge",
        "merchant_name": "Interest Charge",
        "category_slug": "fees",
        "priority": 985
    },
    {
        "pattern": "finance charge",
        "merchant_name": "Finance Charge",
        "category_slug": "fees",
        "priority": 986
    },
    {
        "pattern": "returned item fee",
        "merchant_name": "Returned Item Fee",
        "category_slug": "fees",
        "priority": 983
    },
    {
        "pattern": "wire fee",
        "merchant_name": "Wire Fee",
        "category_slug": "fees",
        "priority": 992
    },
    {
        "pattern": "nsf fee",
        "merchant_name": "Nsf Fee",
        "category_slug": "fees",
        "priority": 993
    },
    {
        "pattern": "cash advance fee",
        "merchant_name": "Cash Advance Fee",
        "category_slug": "fees",
        "priority": 984
    },
    {
        "pattern": "account fee",
        "merchant_name": "Account Fee",
        "category_slug": "fees",
        "priority": 989
    },
    {
        "pattern": "minimum balance fee",
        "merchant_name": "Minimum Balance Fee",
        "category_slug": "fees",
        "priority": 981
    }
]


def upgrade() -> None:
    bind = op.get_bind()

    # ON CONFLICT DO NOTHING against the partial unique index makes this safe to
    # re-run and safe against a database that was seeded by hand.
    for category in SYSTEM_CATEGORIES:
        bind.execute(
            sa.text(
                """
                INSERT INTO categories
                    (id, user_id, name, slug, color, icon, is_system, sort_order,
                     created_at, updated_at)
                VALUES
                    (:id, NULL, :name, :slug, :color, :icon, TRUE, :sort_order,
                     NOW(), NOW())
                ON CONFLICT (slug) WHERE user_id IS NULL DO NOTHING
                """
            ),
            {"id": uuid.uuid4(), **category},
        )

    for rule in MERCHANT_RULES:
        bind.execute(
            sa.text(
                """
                INSERT INTO merchant_rules
                    (id, pattern, merchant_name, category_slug, priority,
                     created_at, updated_at)
                VALUES
                    (:id, :pattern, :merchant_name, :category_slug, :priority,
                     NOW(), NOW())
                ON CONFLICT (pattern) DO NOTHING
                """
            ),
            {"id": uuid.uuid4(), **rule},
        )


def downgrade() -> None:
    bind = op.get_bind()
    slugs = [c["slug"] for c in SYSTEM_CATEGORIES]
    patterns = [r["pattern"] for r in MERCHANT_RULES]

    # Detach transactions first. Dropping a referenced category would either
    # fail or cascade depending on the FK, and neither is a decision a
    # downgrade should make silently.
    bind.execute(
        sa.text(
            """
            UPDATE transactions SET category_id = NULL
            WHERE category_id IN (
                SELECT id FROM categories
                WHERE user_id IS NULL AND slug = ANY(:slugs)
            )
            """
        ),
        {"slugs": slugs},
    )
    bind.execute(
        sa.text(
            "DELETE FROM categories WHERE user_id IS NULL AND slug = ANY(:slugs)"
        ),
        {"slugs": slugs},
    )
    bind.execute(
        sa.text("DELETE FROM merchant_rules WHERE pattern = ANY(:patterns)"),
        {"patterns": patterns},
    )
