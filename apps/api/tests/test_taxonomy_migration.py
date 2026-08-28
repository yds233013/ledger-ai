"""The system taxonomy must exist in a deployed database, and stay correct.

Production ran with an empty `categories` table. The rows lived only in
`scripts/seed_synthetic.py`, which is not in the runtime image, and no
migration inserted them — so every transaction was written with a NULL
category and the product looked incapable of categorizing anything.

What makes that failure so quiet is worth pinning precisely: the categorizer
was working the whole time. `build_context()` falls back to the YAML when
`merchant_rules` is empty, so rows were written with `categorized_by="rule"`
and `confidence=0.90` — and a NULL `category_id`, because `resolve_category_ids()`
had nothing to resolve against. Any repair that keys off `categorized_by`
would therefore skip exactly the rows that need it.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from sqlalchemy import text

from ledgerai.services.categorize.taxonomy import (
    TAXONOMY_FINGERPRINT,
    UNCATEGORIZED_SLUG,
    fingerprint,
    system_categories,
    system_merchant_rules,
    taxonomy_payload,
)
from tests.conftest import make_user

API_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = API_ROOT / "alembic" / "versions" / "d2f81b6c9a37_seed_system_taxonomy.py"


def load_migration():
    spec = importlib.util.spec_from_file_location("_taxonomy_migration", MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheSnapshotCannotDriftFromTheCanonicalSource:
    """The migration embeds a frozen copy; this is what keeps it honest.

    A migration that imported the YAML would apply whatever the taxonomy said
    on the day it ran, so two environments migrated a year apart would end up
    with different reference data. Freezing it fixes that and creates a second
    copy — so the copy is checked against the original here. Editing the YAML
    fails this test until a new migration is written, which is the intended
    workflow.
    """

    def test_the_fingerprint_matches(self) -> None:
        assert load_migration().TAXONOMY_FINGERPRINT == TAXONOMY_FINGERPRINT

    def test_every_category_matches(self) -> None:
        snapshot = load_migration().SYSTEM_CATEGORIES
        assert snapshot == taxonomy_payload()["categories"]

    def test_every_merchant_rule_matches(self) -> None:
        snapshot = load_migration().MERCHANT_RULES
        assert snapshot == taxonomy_payload()["merchant_rules"]

    def test_the_fingerprint_actually_detects_a_change(self) -> None:
        """A drift check that cannot fail is not a drift check."""
        payload = taxonomy_payload()
        payload["categories"][0]["name"] = "Something Else"
        assert fingerprint(payload) != TAXONOMY_FINGERPRINT


class TestTheTaxonomyItself:
    def test_uncategorized_is_present(self) -> None:
        """The fallback needs a real row to point at, not a NULL."""
        assert UNCATEGORIZED_SLUG in {c.slug for c in system_categories()}

    def test_slugs_are_unique(self) -> None:
        slugs = [c.slug for c in system_categories()]
        assert len(slugs) == len(set(slugs))

    def test_patterns_are_unique(self) -> None:
        """`merchant_rules.pattern` is UNIQUE, so a duplicate would abort the
        insert of everything after it."""
        patterns = [r.pattern for r in system_merchant_rules()]
        assert len(patterns) == len(set(patterns))

    def test_every_rule_points_at_a_real_category(self) -> None:
        """A rule naming a slug with no category row silently produces NULLs —
        the same failure this whole migration exists to fix."""
        slugs = {c.slug for c in system_categories()}
        orphans = {r.category_slug for r in system_merchant_rules()} - slugs
        assert orphans == set()

    def test_longer_patterns_win(self) -> None:
        """Priority is `1000 - len(pattern)`, ascending, so the most specific
        pattern is consulted first."""
        rules = {r.pattern: r.priority for r in system_merchant_rules()}
        assert rules["whole foods"] < rules["amazon"]


class TestAgainstADatabase:
    """Applied against the real schema the test database already carries."""

    def _apply(self, session) -> None:
        migration = load_migration()
        for category in migration.SYSTEM_CATEGORIES:
            session.execute(
                text(
                    """
                    INSERT INTO categories
                        (id, user_id, name, slug, color, icon, is_system,
                         sort_order, created_at, updated_at)
                    VALUES (:id, NULL, :name, :slug, :color, :icon, TRUE,
                            :sort_order, NOW(), NOW())
                    ON CONFLICT (slug) WHERE user_id IS NULL DO NOTHING
                    """
                ),
                {"id": uuid.uuid4(), **category},
            )
        for rule in migration.MERCHANT_RULES:
            session.execute(
                text(
                    """
                    INSERT INTO merchant_rules
                        (id, pattern, merchant_name, category_slug, priority,
                         created_at, updated_at)
                    VALUES (:id, :pattern, :merchant_name, :category_slug,
                            :priority, NOW(), NOW())
                    ON CONFLICT (pattern) DO NOTHING
                    """
                ),
                {"id": uuid.uuid4(), **rule},
            )
        session.flush()

    def _system_count(self, session) -> int:
        return int(
            session.execute(
                text("SELECT COUNT(*) FROM categories WHERE user_id IS NULL")
            ).scalar_one()
        )

    def test_it_inserts_the_whole_taxonomy(self, sync_db) -> None:
        sync_db.execute(text("DELETE FROM categories WHERE user_id IS NULL"))
        sync_db.execute(text("DELETE FROM merchant_rules"))
        sync_db.flush()

        self._apply(sync_db)

        assert self._system_count(sync_db) == len(system_categories())
        rules = int(
            sync_db.execute(text("SELECT COUNT(*) FROM merchant_rules")).scalar_one()
        )
        assert rules == len(system_merchant_rules())

    def test_running_it_twice_inserts_nothing_extra(self, sync_db) -> None:
        """The partial unique index and the unique pattern are the guards."""
        self._apply(sync_db)
        after_first = self._system_count(sync_db)
        rules_first = int(
            sync_db.execute(text("SELECT COUNT(*) FROM merchant_rules")).scalar_one()
        )

        self._apply(sync_db)

        assert self._system_count(sync_db) == after_first
        assert (
            int(sync_db.execute(text("SELECT COUNT(*) FROM merchant_rules")).scalar_one())
            == rules_first
        )

    def test_it_runs_against_a_database_that_already_has_the_taxonomy(
        self, sync_db
    ) -> None:
        """The production case: rows may already exist from a manual seed."""
        self._apply(sync_db)
        before = self._system_count(sync_db)
        self._apply(sync_db)
        assert self._system_count(sync_db) == before

    def test_a_users_own_category_with_the_same_slug_survives(self, sync_db) -> None:
        """The partial index covers `user_id IS NULL` only, so a user category
        sharing a slug is a different row and must not be touched."""
        owner_id = make_user(sync_db, "owner@test.local").id
        sync_db.execute(
            text(
                """
                INSERT INTO categories
                    (id, user_id, name, slug, color, icon, is_system, sort_order,
                     created_at, updated_at)
                VALUES (:id, :uid, 'My Groceries', 'groceries', '#123456', 'tag',
                        FALSE, 5, NOW(), NOW())
                """
            ),
            {"id": uuid.uuid4(), "uid": owner_id},
        )
        sync_db.flush()

        self._apply(sync_db)

        surviving = sync_db.execute(
            text(
                "SELECT name FROM categories WHERE user_id = :uid AND slug = 'groceries'"
            ),
            {"uid": owner_id},
        ).scalar_one()
        assert surviving == "My Groceries"

    def test_the_downgrade_leaves_user_categories_alone(self, sync_db) -> None:
        """Rollback removes only what the migration is responsible for."""
        owner_id = make_user(sync_db, "downgrade-owner@test.local").id
        self._apply(sync_db)
        sync_db.execute(
            text(
                """
                INSERT INTO categories
                    (id, user_id, name, slug, color, icon, is_system, sort_order,
                     created_at, updated_at)
                VALUES (:id, :uid, 'Mine', 'my-own-thing', '#123456', 'tag',
                        FALSE, 5, NOW(), NOW())
                """
            ),
            {"id": uuid.uuid4(), "uid": owner_id},
        )
        sync_db.flush()

        migration = load_migration()
        slugs = [c["slug"] for c in migration.SYSTEM_CATEGORIES]
        sync_db.execute(
            text(
                """
                UPDATE transactions SET category_id = NULL
                WHERE category_id IN (
                    SELECT id FROM categories WHERE user_id IS NULL AND slug = ANY(:slugs)
                )
                """
            ),
            {"slugs": slugs},
        )
        sync_db.execute(
            text("DELETE FROM categories WHERE user_id IS NULL AND slug = ANY(:slugs)"),
            {"slugs": slugs},
        )
        sync_db.flush()

        assert self._system_count(sync_db) == 0
        still_there = sync_db.execute(
            text("SELECT COUNT(*) FROM categories WHERE user_id = :uid"),
            {"uid": owner_id},
        ).scalar_one()
        assert int(still_there) == 1


class TestTheMissingTaxonomyIsVisible:
    """A silent failure is what made this expensive to find.

    The API kept returning 200s, uploads kept "succeeding", and the only
    symptom was that every category read Uncategorized. Readiness now reports
    the condition — without failing the probe, because the instance really can
    still serve uploads, search, receipts and deletion. Turning a degraded
    feature into an outage would be the wrong trade.

    The probe is exercised directly against the test database. Going through
    the client would not test it: `_probe_reference_data` uses the module-level
    `async_engine`, which is bound to the development database rather than the
    one these fixtures write to, so the assertion would pass no matter what.
    """

    async def _probe_against_test_db(self, monkeypatch):
        from sqlalchemy.ext.asyncio import create_async_engine

        from ledgerai import main as main_module
        from tests.conftest import TEST_DB, _url

        engine = create_async_engine(_url(TEST_DB, is_async=True))
        monkeypatch.setattr(main_module, "async_engine", engine)
        monkeypatch.setattr(main_module, "_warned_reference_data_missing", False)
        try:
            return await main_module._probe_reference_data()
        finally:
            await engine.dispose()

    async def test_a_seeded_taxonomy_reports_ok(self, sync_db, monkeypatch) -> None:
        sync_db.commit()
        assert await self._probe_against_test_db(monkeypatch) is True

    async def test_an_empty_taxonomy_is_detected(self, sync_db, monkeypatch) -> None:
        sync_db.execute(text("UPDATE transactions SET category_id = NULL"))
        sync_db.execute(text("DELETE FROM categories WHERE user_id IS NULL"))
        sync_db.commit()

        assert await self._probe_against_test_db(monkeypatch) is False

    async def test_it_is_logged_once_not_on_every_probe(
        self, sync_db, monkeypatch, caplog
    ) -> None:
        sync_db.execute(text("UPDATE transactions SET category_id = NULL"))
        sync_db.execute(text("DELETE FROM categories WHERE user_id IS NULL"))
        sync_db.commit()

        with caplog.at_level("ERROR"):
            await self._probe_against_test_db(monkeypatch)
            # A second probe on the SAME process must not repeat itself; the
            # helper resets the flag, so call the probe again directly.
            from ledgerai import main as main_module

            await main_module._probe_reference_data()

        assert caplog.text.count("reference_data.missing") == 1

    async def test_readiness_exposes_the_field(self, client) -> None:
        """The shape of the contract, independent of which database it read."""
        body = (await client.get("/health/ready")).json()
        assert "reference_data" in body["dependencies"]
        assert body["dependencies"]["reference_data"] in {"ok", "missing"}
