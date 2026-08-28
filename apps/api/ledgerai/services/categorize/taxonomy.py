"""The canonical system taxonomy, and the one place it is defined.

`categories.yaml` and `merchant_rules.yaml` are the source of truth. Everything
that needs the taxonomy — the seed script, the Alembic data migration, the
health probe — reads it through here rather than parsing the YAML again, so
there is exactly one definition of what a system category is.

**Why the migration still embeds a frozen copy.** A migration has to produce the
same rows in 2030 that it produced the day it was written; one that imported
this module would instead apply whatever the taxonomy happens to say then, and
two environments migrated a year apart would diverge. So the migration carries a
snapshot, and `TAXONOMY_FINGERPRINT` ties the two together:
`tests/test_taxonomy_migration.py` asserts the snapshot still matches this
module. Editing the YAML therefore fails CI until a new migration is written for
the change, which is the intended workflow rather than an obstacle.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache

from ..ingest import load_category_definitions, load_merchant_rule_definitions

# The slug every fallback path writes. Present in the taxonomy so the review
# queue has a real category row to point at rather than a NULL.
UNCATEGORIZED_SLUG = "uncategorized"


@dataclass(frozen=True, slots=True)
class SystemCategory:
    slug: str
    name: str
    color: str
    icon: str
    sort_order: int


@dataclass(frozen=True, slots=True)
class SystemMerchantRule:
    pattern: str
    merchant_name: str
    category_slug: str
    priority: int


def _rule_priority(pattern: str) -> int:
    """Longer patterns are more specific, so they must win.

    Kept here rather than at each call site because the migration and the seed
    script have to agree: a rule inserted with one priority and re-inserted with
    another would categorize differently depending on which ran.
    """
    return 1000 - len(pattern)


@lru_cache
def system_categories() -> tuple[SystemCategory, ...]:
    return tuple(
        SystemCategory(
            slug=d["slug"],
            name=d["name"],
            color=d["color"],
            icon=d["icon"],
            sort_order=d["sort_order"],
        )
        for d in load_category_definitions()
    )


@lru_cache
def system_merchant_rules() -> tuple[SystemMerchantRule, ...]:
    return tuple(
        SystemMerchantRule(
            pattern=pattern,
            merchant_name=pattern.title(),
            category_slug=slug,
            priority=_rule_priority(pattern),
        )
        for pattern, slug in load_merchant_rule_definitions()
    )


def taxonomy_payload() -> dict[str, list[dict]]:
    """The taxonomy as plain data — what a migration snapshot embeds."""
    return {
        "categories": [
            {
                "slug": c.slug,
                "name": c.name,
                "color": c.color,
                "icon": c.icon,
                "sort_order": c.sort_order,
            }
            for c in system_categories()
        ],
        "merchant_rules": [
            {
                "pattern": r.pattern,
                "merchant_name": r.merchant_name,
                "category_slug": r.category_slug,
                "priority": r.priority,
            }
            for r in system_merchant_rules()
        ],
    }


def fingerprint(payload: dict[str, list[dict]] | None = None) -> str:
    """Stable digest of the taxonomy, for drift detection.

    Sorted keys and a canonical separator, so the digest depends on the content
    and not on dict ordering or formatting.
    """
    data = taxonomy_payload() if payload is None else payload
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


TAXONOMY_FINGERPRINT = fingerprint()
