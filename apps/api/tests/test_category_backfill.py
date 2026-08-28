"""Repairing rows that were imported while the taxonomy was missing.

The taxonomy migration fixes new imports. It does nothing for the transactions
already written with a NULL category, and those are the ones a user is looking
at. This backfill re-runs the real categorizer over exactly the rows the engine
could not place, and over nothing else.

The population is subtler than "categorized_by = none". `build_context()` falls
back to the YAML when `merchant_rules` is empty, so the affected rows carry
`categorized_by="rule"` and `confidence=0.90` — a confident answer that had
nowhere to land, because `resolve_category_ids()` returned an empty mapping and
`category_id` fell through to NULL. Several tests below construct that exact
shape, because a filter on `categorized_by` would silently skip it.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text

from ledgerai.jobs.backfill import backfill_categories
from ledgerai.models import Category, Transaction
from tests.conftest import make_account, make_user


def _category_id(session, slug: str):
    return session.execute(
        select(Category.id).where(Category.slug == slug, Category.user_id.is_(None))
    ).scalar_one_or_none()


def _add_transaction(
    session,
    user,
    account,
    *,
    merchant: str,
    merchant_key: str,
    description: str = "",
    amount_cents: int = -1500,
    category_id=None,
    categorized_by: str = "rule",
    confidence: str = "0.90",
    is_corrected: bool = False,
    needs_review: bool = False,
) -> Transaction:
    transaction = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        upload_id=None,
        posted_date=date(2026, 8, 1),
        amount_cents=amount_cents,
        currency="USD",
        raw_description=description or merchant,
        normalized_description=description or merchant,
        merchant=merchant,
        merchant_key=merchant_key,
        category_id=category_id,
        confidence=Decimal(confidence),
        categorized_by=categorized_by,
        needs_review=needs_review,
        is_corrected=is_corrected,
        dedupe_hash=uuid.uuid4().hex,
        source_row_index=0,
    )
    session.add(transaction)
    session.flush()
    return transaction


class TestKnownMerchantsGetCategorized:
    def test_a_seeded_merchant_is_recategorized(self, sync_db) -> None:
        """The production shape: a confident rule answer with a NULL category."""
        user = make_user(sync_db, "backfill@test.local")
        account = make_account(sync_db, user)
        transaction = _add_transaction(
            sync_db, user, account, merchant="Whole Foods MKT", merchant_key="whole foods"
        )
        assert transaction.category_id is None

        report = backfill_categories(sync_db, user_id=user.id)

        sync_db.refresh(transaction)
        assert transaction.category_id == _category_id(sync_db, "groceries")
        assert report.recategorized == 1

    def test_several_merchants_land_in_their_own_categories(self, sync_db) -> None:
        user = make_user(sync_db, "multi@test.local")
        account = make_account(sync_db, user)
        cases = {
            "chipotle": ("Chipotle Online 8811", "dining"),
            "uber": ("Uber", "transport"),
            "amazon": ("Amazon.com", "shopping"),
            "netflix": ("Netflix", "subscriptions"),
        }
        made = {
            key: _add_transaction(sync_db, user, account, merchant=name, merchant_key=key)
            for key, (name, _) in cases.items()
        }

        backfill_categories(sync_db, user_id=user.id)

        for key, (_, expected_slug) in cases.items():
            sync_db.refresh(made[key])
            assert made[key].category_id == _category_id(sync_db, expected_slug), key

    def test_the_source_and_confidence_are_recorded(self, sync_db) -> None:
        user = make_user(sync_db, "source@test.local")
        account = make_account(sync_db, user)
        transaction = _add_transaction(
            sync_db, user, account, merchant="Trader Joes", merchant_key="trader joes"
        )

        backfill_categories(sync_db, user_id=user.id)

        sync_db.refresh(transaction)
        assert transaction.categorized_by == "rule"
        assert transaction.confidence == Decimal("0.90")
        assert transaction.needs_review is False


class TestUnknownMerchantsStayReviewable:
    def test_an_unknown_merchant_is_not_invented_into_a_category(self, sync_db) -> None:
        """Refusing to guess is the whole point of the review queue."""
        user = make_user(sync_db, "unknown@test.local")
        account = make_account(sync_db, user)
        transaction = _add_transaction(
            sync_db,
            user,
            account,
            merchant="Zorblax Quantum Widgets LLC",
            merchant_key="zorblax quantum widgets llc",
            description="ZORBLAX QUANTUM WIDGETS LLC",
        )

        report = backfill_categories(sync_db, user_id=user.id)

        sync_db.refresh(transaction)
        assert transaction.category_id == _category_id(sync_db, "uncategorized")
        assert transaction.needs_review is True
        assert report.still_uncategorized == 1
        assert report.recategorized == 0

    def test_it_points_at_a_real_row_rather_than_leaving_null(self, sync_db) -> None:
        """A NULL category is what the UI could not render in the first place."""
        user = make_user(sync_db, "notnull@test.local")
        account = make_account(sync_db, user)
        transaction = _add_transaction(
            sync_db, user, account, merchant="Wat", merchant_key="wat", description="WAT"
        )

        backfill_categories(sync_db, user_id=user.id)

        sync_db.refresh(transaction)
        assert transaction.category_id is not None


class TestUserWorkIsPreserved:
    def test_a_corrected_row_is_never_touched(self, sync_db) -> None:
        """`is_corrected` means the user told us the answer."""
        user = make_user(sync_db, "corrected@test.local")
        account = make_account(sync_db, user)
        travel = _category_id(sync_db, "travel")
        # A grocery merchant the user deliberately filed under Travel.
        transaction = _add_transaction(
            sync_db,
            user,
            account,
            merchant="Whole Foods MKT",
            merchant_key="whole foods",
            category_id=travel,
            categorized_by="correction",
            confidence="1.00",
            is_corrected=True,
        )

        backfill_categories(sync_db, user_id=user.id)

        sync_db.refresh(transaction)
        assert transaction.category_id == travel
        assert transaction.categorized_by == "correction"
        assert transaction.is_corrected is True

    def test_a_corrected_row_with_a_null_category_is_still_skipped(self, sync_db) -> None:
        """`is_corrected` wins even when the row looks eligible otherwise."""
        user = make_user(sync_db, "corrected-null@test.local")
        account = make_account(sync_db, user)
        transaction = _add_transaction(
            sync_db,
            user,
            account,
            merchant="Whole Foods MKT",
            merchant_key="whole foods",
            category_id=None,
            is_corrected=True,
        )

        backfill_categories(sync_db, user_id=user.id)

        sync_db.refresh(transaction)
        assert transaction.category_id is None

    def test_a_row_already_categorized_is_left_alone(self, sync_db) -> None:
        user = make_user(sync_db, "already@test.local")
        account = make_account(sync_db, user)
        housing = _category_id(sync_db, "housing")
        transaction = _add_transaction(
            sync_db,
            user,
            account,
            merchant="Whole Foods MKT",
            merchant_key="whole foods",
            category_id=housing,
        )

        report = backfill_categories(sync_db, user_id=user.id)

        sync_db.refresh(transaction)
        assert transaction.category_id == housing
        assert report.scanned == 0

    def test_correction_memory_wins_over_the_seeded_rule(self, sync_db) -> None:
        """A prior correction for the same merchant should teach the backfill."""
        user = make_user(sync_db, "memory@test.local")
        account = make_account(sync_db, user)
        entertainment = _category_id(sync_db, "entertainment")
        # The correction rows the user's earlier edit would have produced. It
        # has to hang off a real transaction: transaction_id is NOT NULL.
        earlier = _add_transaction(
            sync_db,
            user,
            account,
            merchant="Whole Foods MKT",
            merchant_key="whole foods",
            category_id=entertainment,
            categorized_by="correction",
            confidence="1.00",
            is_corrected=True,
        )
        sync_db.execute(
            text(
                """
                INSERT INTO transaction_corrections
                    (id, user_id, transaction_id, field, merchant_key, old_value,
                     new_value, scope, created_at, updated_at)
                VALUES (:id, :uid, :tid, 'category', 'whole foods', 'groceries',
                        'entertainment', 'merchant', NOW(), NOW())
                """
            ),
            {"id": uuid.uuid4(), "uid": user.id, "tid": earlier.id},
        )
        sync_db.flush()
        transaction = _add_transaction(
            sync_db, user, account, merchant="Whole Foods MKT", merchant_key="whole foods"
        )

        backfill_categories(sync_db, user_id=user.id)

        sync_db.refresh(transaction)
        assert transaction.category_id == entertainment
        assert transaction.categorized_by == "correction"


class TestIdempotency:
    def test_running_twice_changes_nothing_the_second_time(self, sync_db) -> None:
        user = make_user(sync_db, "twice@test.local")
        account = make_account(sync_db, user)
        _add_transaction(
            sync_db, user, account, merchant="Whole Foods MKT", merchant_key="whole foods"
        )

        first = backfill_categories(sync_db, user_id=user.id)
        second = backfill_categories(sync_db, user_id=user.id)

        assert first.recategorized == 1
        assert second.recategorized == 0
        assert second.scanned == 0

    def test_an_unknown_merchant_is_not_rescanned_forever(self, sync_db) -> None:
        """After the first pass it points at Uncategorized, so it leaves the
        eligible set rather than being re-examined on every run."""
        user = make_user(sync_db, "unknown-twice@test.local")
        account = make_account(sync_db, user)
        _add_transaction(
            sync_db, user, account, merchant="Zzz Unknown", merchant_key="zzz unknown"
        )

        backfill_categories(sync_db, user_id=user.id)
        second = backfill_categories(sync_db, user_id=user.id)

        # It is still eligible by category (it points at Uncategorized), but the
        # outcome is stable — no churn, no repeated writes.
        assert second.recategorized == 0


class TestItRefusesToRunWithoutATaxonomy:
    def test_no_categories_means_no_changes(self, sync_db) -> None:
        """The failure this whole change exists to fix must not be made worse.

        With no categories to resolve into, rewriting rows would only replace
        one wrong answer with another.
        """
        user = make_user(sync_db, "empty@test.local")
        account = make_account(sync_db, user)
        transaction = _add_transaction(
            sync_db, user, account, merchant="Whole Foods MKT", merchant_key="whole foods"
        )
        sync_db.execute(text("UPDATE transactions SET category_id = NULL"))
        sync_db.execute(text("DELETE FROM categories WHERE user_id IS NULL"))
        sync_db.flush()

        report = backfill_categories(sync_db, user_id=user.id)

        sync_db.refresh(transaction)
        assert report.recategorized == 0
        assert report.scanned == 0
        assert transaction.category_id is None


class TestAllUsers:
    def test_it_covers_every_user_when_none_is_named(self, sync_db) -> None:
        groceries = _category_id(sync_db, "groceries")
        made = []
        for i in range(3):
            user = make_user(sync_db, f"multi-user-{i}@test.local")
            account = make_account(sync_db, user)
            made.append(
                _add_transaction(
                    sync_db, user, account, merchant="Trader Joes", merchant_key="trader joes"
                )
            )

        report = backfill_categories(sync_db)

        assert report.users >= 3
        for transaction in made:
            sync_db.refresh(transaction)
            assert transaction.category_id == groceries
