"""Re-categorize transactions that were imported while the taxonomy was empty.

Production ran for a while with no system categories and no merchant rules, so
the deterministic categorizer had nothing to match against and every row fell
through to Uncategorized. Inserting the taxonomy fixes new imports; it does not
retroactively fix rows that were already written.

This runs the **real categorizer** — the same `build_categorizer()` the upload
pipeline uses, over the same `CategorizationContext` — rather than a SQL
approximation of it. That is deliberately not done inside the Alembic migration:
a migration that imports ORM models and service code breaks the moment either
changes shape, and this one would then fail on a database it was supposed to
repair. Reference data belongs in the migration; behaviour belongs here.

**What it will not touch.** Only rows the engine itself gave up on are
eligible:

* `is_corrected = TRUE` is excluded outright — the user told us that answer,
  and `corrections.py` sets that flag on every manual assignment.
* Anything already pointing at a real category is excluded, so an earlier
  successful categorization is never overwritten.

Note what is deliberately NOT in that filter: `categorized_by`. The affected
rows do not say "none". `build_context()` falls back to the YAML when
`merchant_rules` is empty, so the engine *did* produce answers — it wrote
`categorized_by="rule"` and `confidence=0.90` — but `ingest_rows` resolves the
slug through `resolve_category_ids()`, which returned an empty dict, so
`category_id` fell through to NULL. Filtering on `categorized_by = "none"`
would therefore skip exactly the rows that need repair. A NULL category with
`is_corrected = FALSE` is the honest signal.

Running it twice is a no-op the second time: the first run gives those rows a
real `category_id`, which takes them out of the eligible set.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ..db import sync_session
from ..models import Transaction
from ..services.categorize import build_categorizer
from ..services.categorize.base import TransactionCandidate
from ..services.categorize.taxonomy import UNCATEGORIZED_SLUG
from ..services.ingest import build_context, resolve_category_ids

logger = logging.getLogger(__name__)

# Committed in batches so a large table does not run in one long transaction.
BATCH_SIZE = 500


@dataclass(slots=True)
class BackfillReport:
    scanned: int = 0
    recategorized: int = 0
    still_uncategorized: int = 0
    users: int = 0
    by_source: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "recategorized": self.recategorized,
            "still_uncategorized": self.still_uncategorized,
            "users": self.users,
            "by_source": dict(sorted(self.by_source.items())),
        }


def _eligible(session: Session, user_id: uuid.UUID, uncategorized_id: uuid.UUID | None):
    """Rows the engine gave up on, and nothing else. See the module docstring."""
    condition: ColumnElement[bool] = Transaction.category_id.is_(None)
    if uncategorized_id is not None:
        condition = or_(condition, Transaction.category_id == uncategorized_id)
    return session.execute(
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.is_corrected.is_(False),
            condition,
        )
        .order_by(Transaction.id)
    ).scalars()


def backfill_categories(session: Session, *, user_id: uuid.UUID | None = None) -> BackfillReport:
    """Re-run categorization for eligible rows. Returns counts only."""
    report = BackfillReport()

    slug_to_id = resolve_category_ids(session)
    if not slug_to_id:
        # Nothing to categorize *into*. Refusing here rather than rewriting
        # every row to NULL is the difference between a no-op and damage.
        logger.error("backfill.aborted reason=no_system_categories")
        return report

    uncategorized_id = slug_to_id.get(UNCATEGORIZED_SLUG)
    categorizer = build_categorizer()

    user_ids = (
        [user_id]
        if user_id is not None
        else list(session.execute(select(Transaction.user_id).distinct()).scalars())
    )
    report.users = len(user_ids)

    for uid in user_ids:
        # Per user: correction memory and merchant history are user-scoped, so
        # the context cannot be shared across accounts.
        context = build_context(session, uid)
        pending = 0

        for transaction in _eligible(session, uid, uncategorized_id):
            report.scanned += 1
            suggestion = categorizer.categorize(
                TransactionCandidate(
                    merchant=transaction.merchant or "",
                    merchant_key=transaction.merchant_key or "",
                    normalized_description=transaction.normalized_description or "",
                    amount_cents=transaction.amount_cents,
                    posted_date=transaction.posted_date,
                ),
                context,
            )

            slug = suggestion.category_slug
            if slug == UNCATEGORIZED_SLUG or slug not in slug_to_id:
                # Genuinely unknown merchant. Point it at the Uncategorized row
                # so it lands in the review queue with a real category rather
                # than a NULL the UI has to special-case.
                if uncategorized_id is not None and transaction.category_id is None:
                    transaction.category_id = uncategorized_id
                    transaction.needs_review = True
                report.still_uncategorized += 1
                continue

            transaction.category_id = slug_to_id[slug]
            transaction.confidence = suggestion.confidence
            transaction.categorized_by = suggestion.source
            transaction.needs_review = suggestion.needs_review
            # is_corrected is deliberately left alone: this is the engine's
            # answer, not the user's, and a later correction must still win.
            report.recategorized += 1
            report.by_source[suggestion.source] = report.by_source.get(suggestion.source, 0) + 1

            pending += 1
            if pending >= BATCH_SIZE:
                session.flush()
                pending = 0

        session.flush()

    session.commit()
    logger.info(
        "backfill.completed users=%d scanned=%d recategorized=%d still_uncategorized=%d",
        report.users,
        report.scanned,
        report.recategorized,
        report.still_uncategorized,
    )
    return report


def run_category_backfill() -> dict[str, object]:
    """Entry point. Returns counts, never raises into a caller."""
    with sync_session() as session:
        return backfill_categories(session).as_dict()
