"""The deletion preview must describe the deletion that actually runs.

Review found the preview and the delete step iterating two separately
maintained lists, which had drifted in both directions: the preview counted
`accounts` (which data-only deletion deliberately keeps) and omitted
`categories` (which it removes). For an irreversible operation whose entire
purpose is informed consent, that is the preview lying about both what goes
and what stays.

The fix is one shared tuple. These tests assert the property rather than the
tuple's contents, so they keep holding when a table is added later.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ledgerai.models import (
    Account,
    Alert,
    AlertSeverity,
    AlertStatus,
    AlertType,
    AnalysisRun,
    Category,
    CorrectionField,
    JobStage,
    ProcessingJob,
    Receipt,
    ReceiptStatus,
    TransactionCorrection,
    Upload,
    UploadKind,
    UploadStatus,
    User,
)
from ledgerai.services.lifecycle import (
    DATA_ONLY_MODELS,
    DATA_ONLY_RETAINED,
    TABLE_LABELS,
    delete_user_data,
)
from tests.conftest import make_transaction

pytestmark = pytest.mark.asyncio


@pytest.fixture
def populated(sync_db: Session, demo_data: dict) -> dict:
    """Give the user at least one row in every data-only table.

    A preview cannot be shown to match a deletion for a table that is empty in
    both, so every table has to be non-empty for the assertions to bite.
    """
    user, account = demo_data["user"], demo_data["account"]

    transaction = make_transaction(
        sync_db, user, account, posted=date(2026, 7, 5), cents=-3_300,
        description="SANDBOX BOOKS", merchant="Sandbox Books",
        category_slug="shopping", index=500,
    )

    upload = Upload(
        user_id=user.id, filename="statement.csv", original_filename="statement.csv",
        content_hash="a" * 64, kind=UploadKind.CSV, content_type="text/csv",
        size_bytes=1024, storage_key=f"users/{user.id}/uploads/{uuid.uuid4()}/statement.csv",
        status=UploadStatus.COMPLETE,
    )
    sync_db.add(upload)
    sync_db.flush()

    sync_db.add_all([
        ProcessingJob(
            upload_id=upload.id, user_id=user.id,
            stage=JobStage.COMPLETE, progress=100,
        ),
        Receipt(user_id=user.id, upload_id=upload.id, status=ReceiptStatus.PENDING),
        Alert(
            user_id=user.id, transaction_id=transaction.id,
            alert_type=AlertType.NEW_MERCHANT, severity=AlertSeverity.LOW,
            message="First charge here.", evidence={}, status=AlertStatus.OPEN,
        ),
        AnalysisRun(
            user_id=user.id, question="How much did I spend?",
            normalized_question="how much did i spend",
        ),
        TransactionCorrection(
            transaction_id=transaction.id, user_id=user.id,
            field=CorrectionField.CATEGORY, old_value="shopping",
            new_value="groceries", merchant_key="sandbox books",
        ),
        # A category the user created — the table the preview used to omit.
        Category(
            user_id=user.id, name="Hobby Supplies", slug="hobby-supplies",
            color="#8855ff", icon="tag", is_system=False, sort_order=500,
        ),
    ])
    sync_db.commit()
    return {"user": user, "transaction": transaction}


async def counts_for(session: AsyncSession, user_id) -> dict[str, int]:
    """Live row counts for every data-only table, plus accounts."""
    out: dict[str, int] = {}
    for model in (*DATA_ONLY_MODELS, Account):
        out[model.__tablename__] = int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(model.user_id == user_id)
                )
            ).scalar_one()
        )
    return out


async def load_user(session: AsyncSession, user_id) -> User:
    return (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one()


class TestDataOnlyPreviewMatchesDeletion:
    async def test_the_fixture_populates_every_previewed_table(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        """Positive control.

        Every assertion below compares preview against reality per table. If a
        table were empty in both, that comparison would pass vacuously and hide
        a real mismatch — so the fixture is checked first.
        """
        live = await counts_for(async_db, populated["user"].id)
        for model in DATA_ONLY_MODELS:
            assert live[model.__tablename__] > 0, (
                f"{model.__tablename__} is empty, so it proves nothing"
            )

    async def test_preview_counts_equal_the_live_counts(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        user = await load_user(async_db, populated["user"].id)
        live = await counts_for(async_db, user.id)

        report = await delete_user_data(async_db, user, delete_account=False, dry_run=True)

        for model in DATA_ONLY_MODELS:
            table = model.__tablename__
            assert report.rows_by_table[table] == live[table]

    async def test_every_previewed_row_is_actually_deleted(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        """The core property: preview in, zero out, table by table."""
        user = await load_user(async_db, populated["user"].id)
        preview = await delete_user_data(
            async_db, user, delete_account=False, dry_run=True
        )

        await delete_user_data(async_db, user, delete_account=False)
        await async_db.commit()

        after = await counts_for(async_db, user.id)
        for table, previewed in preview.rows_by_table.items():
            assert previewed > 0
            assert after[table] == 0, f"{table} was previewed but survived deletion"

    async def test_nothing_is_deleted_that_the_preview_did_not_mention(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        """The other direction — the one that lost user categories silently."""
        user = await load_user(async_db, populated["user"].id)
        before = await counts_for(async_db, user.id)
        preview = await delete_user_data(
            async_db, user, delete_account=False, dry_run=True
        )

        await delete_user_data(async_db, user, delete_account=False)
        await async_db.commit()
        after = await counts_for(async_db, user.id)

        for table, before_count in before.items():
            removed = before_count - after[table]
            if removed:
                assert table in preview.rows_by_table, (
                    f"{removed} row(s) were deleted from {table}, which the "
                    "preview never mentioned"
                )

    async def test_categories_are_previewed_and_removed(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        """Pins the specific omission review found."""
        user = await load_user(async_db, populated["user"].id)
        preview = await delete_user_data(
            async_db, user, delete_account=False, dry_run=True
        )
        assert preview.rows_by_table["categories"] >= 1

    async def test_system_categories_survive(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        """Only the user's own categories go; the shared vocabulary stays."""
        user = await load_user(async_db, populated["user"].id)
        await delete_user_data(async_db, user, delete_account=False)
        await async_db.commit()

        system = int(
            (
                await async_db.execute(
                    select(func.count())
                    .select_from(Category)
                    .where(Category.user_id.is_(None))
                )
            ).scalar_one()
        )
        assert system > 0


class TestDataOnlyPreviewDoesNotOverstate:
    async def test_accounts_are_not_previewed(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        """Pins the specific overstatement review found."""
        user = await load_user(async_db, populated["user"].id)
        report = await delete_user_data(
            async_db, user, delete_account=False, dry_run=True
        )
        assert "accounts" not in report.rows_by_table

    async def test_accounts_actually_survive(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        user = await load_user(async_db, populated["user"].id)
        before = (await counts_for(async_db, user.id))["accounts"]

        await delete_user_data(async_db, user, delete_account=False)
        await async_db.commit()

        after = (await counts_for(async_db, user.id))["accounts"]
        assert before > 0
        assert after == before, "data-only deletion must keep the user's accounts"

    async def test_the_total_excludes_untouched_tables(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        """total_rows drove the confirmation copy, and was inflated by accounts."""
        user = await load_user(async_db, populated["user"].id)
        report = await delete_user_data(
            async_db, user, delete_account=False, dry_run=True
        )
        assert report.total_rows == sum(report.rows_by_table.values())

        accounts = (await counts_for(async_db, user.id))["accounts"]
        assert accounts > 0
        assert report.total_rows == sum(
            count for table, count in report.rows_by_table.items() if table != "accounts"
        )

    async def test_what_is_retained_is_stated(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        user = await load_user(async_db, populated["user"].id)
        report = await delete_user_data(
            async_db, user, delete_account=False, dry_run=True
        )
        assert report.retained == list(DATA_ONLY_RETAINED)
        assert any("account" in item for item in report.retained)


class TestFullAccountDeletion:
    async def test_accounts_are_previewed_when_the_account_goes(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        """Deleting the account DOES reach accounts, so the preview must say so."""
        user = await load_user(async_db, populated["user"].id)
        report = await delete_user_data(
            async_db, user, delete_account=True, dry_run=True
        )
        assert report.rows_by_table["accounts"] >= 1

    async def test_nothing_is_retained_when_the_account_goes(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        user = await load_user(async_db, populated["user"].id)
        report = await delete_user_data(
            async_db, user, delete_account=True, dry_run=True
        )
        assert report.retained == []

    async def test_the_user_and_every_table_are_gone(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        user = await load_user(async_db, populated["user"].id)
        user_id = user.id

        await delete_user_data(async_db, user, delete_account=True)
        await async_db.commit()

        after = await counts_for(async_db, user_id)
        assert all(count == 0 for count in after.values())
        assert (
            await async_db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none() is None

    async def test_a_dry_run_removes_nothing(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        user = await load_user(async_db, populated["user"].id)
        before = await counts_for(async_db, user.id)

        await delete_user_data(async_db, user, delete_account=True, dry_run=True)
        await async_db.commit()

        assert await counts_for(async_db, user.id) == before


class TestReportLabels:
    async def test_every_previewed_table_has_a_readable_label(
        self, async_db: AsyncSession, populated: dict
    ) -> None:
        user = await load_user(async_db, populated["user"].id)
        report = await delete_user_data(
            async_db, user, delete_account=True, dry_run=True
        )
        for table in report.rows_by_table:
            assert table in TABLE_LABELS, f"{table} has no human-readable label"
            assert TABLE_LABELS[table]

    async def test_isolation_another_user_is_untouched(
        self, async_db: AsyncSession, populated: dict, demo_data: dict
    ) -> None:
        other_id = demo_data["other"].id
        before = await counts_for(async_db, other_id)
        assert before["transactions"] > 0

        user = await load_user(async_db, populated["user"].id)
        await delete_user_data(async_db, user, delete_account=True)
        await async_db.commit()

        assert await counts_for(async_db, other_id) == before
