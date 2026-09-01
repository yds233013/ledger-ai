"""Alert detection on confirmed statement imports.

A statement enters the ledger through a different door than a CSV: the worker
stages rows for review and a person confirms them later. Detection has to run
behind that second door, and only behind it — staged rows are an inference
about a document, not yet a record, and alerting on them would tell someone
about a duplicate they have not agreed to import.

The properties under test are the ones that make running it inline defensible:
alerts commit with the transactions that caused them, a failure leaves the
import staged and confirmable again rather than half-imported, and confirming
twice produces the alerts once.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ledgerai.models import (
    Alert,
    AlertType,
    StatementImport,
    StatementImportStatus,
    Transaction,
    Upload,
    UploadKind,
    UploadStatus,
    User,
)
from ledgerai.security.jwt import create_access_token
from ledgerai.services import consent, statements
from ledgerai.services.ingest import resolve_account
from ledgerai.services.normalize import extract_merchant, merchant_key
from ledgerai.services.statements import extract_pages, parse_statement, verify_text_layer
from tests.synthetic_pdf import transaction_page, write_pdf

# One repeated charge inside a single statement, and one that a CSV import will
# also have produced. The first must not alert; the second must.
REPEATED = ("14 Aug", "SANDBOX COFFEE BAR", -450, None)
SHARED = ("12 Aug", "SANDBOX GROCERS 0042", -4210, None)

ROWS = [
    SHARED,
    ("13 Aug", "SANDBOX TRANSIT AUTHORITY", -275, None),
    REPEATED,
    REPEATED,
    ("16 Aug", "SANDBOX PAYROLL DEPOSIT", 180_000, None),
]

STATEMENT = write_pdf([transaction_page(ROWS)])


@pytest.fixture
def beta_user(sync_db: Session) -> User:
    user = User(
        email="beta-statement-alerts@test.local",
        password_hash=None,
        display_name="Beta",
        is_demo=False,
        clerk_user_id=f"user_{uuid.uuid4().hex}",
    )
    sync_db.add(user)
    sync_db.flush()
    for consent_type in consent.UPLOAD_PREREQUISITES:
        consent.record_consent(sync_db, user_id=user.id, consent_type=consent_type)
    sync_db.commit()
    return user


@pytest.fixture
def beta_headers(beta_user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(beta_user.id, beta_user.email)}"}


def _upload_row(
    sync_db: Session, user: User, kind: UploadKind = UploadKind.STATEMENT_PDF
) -> Upload:
    upload = Upload(
        user_id=user.id,
        filename="august.pdf",
        original_filename="august.pdf",
        content_hash=uuid.uuid4().hex,
        kind=kind,
        content_type="application/pdf",
        size_bytes=len(STATEMENT),
        storage_key="",
        status=UploadStatus.PROCESSING,
    )
    sync_db.add(upload)
    sync_db.flush()
    return upload


def _stage(sync_db: Session, user: User, data: bytes = STATEMENT) -> StatementImport:
    """Put a parsed statement in front of review, as the worker would."""
    upload = _upload_row(sync_db, user)
    record = statements.stage(
        sync_db,
        user_id=user.id,
        upload_id=upload.id,
        parsed=parse_statement(extract_pages(data)),
        verification=verify_text_layer(b"", []),
    )
    sync_db.commit()
    return record


def _earlier_csv_charge(sync_db: Session, user: User, account_id: uuid.UUID) -> Upload:
    """A charge that already reached the ledger from a different file."""
    upload = _upload_row(sync_db, user, kind=UploadKind.CSV)
    sync_db.add(
        Transaction(
            user_id=user.id,
            account_id=account_id,
            upload_id=upload.id,
            posted_date=date(2026, 8, 12),
            amount_cents=SHARED[2],
            currency="GBP",
            # A bank's CSV and its PDF describe the same charge differently, so
            # the dedupe hash does not collapse them and both rows survive.
            raw_description=f"{SHARED[1]} CARD PAYMENT",
            normalized_description=f"{SHARED[1]} card payment".lower(),
            merchant=extract_merchant(SHARED[1]),
            merchant_key=merchant_key(extract_merchant(SHARED[1])),
            needs_review=False,
            dedupe_hash=uuid.uuid4().hex,
        )
    )
    sync_db.commit()
    return upload


def _alerts(sync_db: Session, user: User) -> list[Alert]:
    sync_db.expire_all()
    return list(
        sync_db.execute(select(Alert).where(Alert.user_id == user.id)).scalars().all()
    )


async def _confirm(client: AsyncClient, headers: dict, record_id: uuid.UUID):
    return await client.post(f"/api/statements/{record_id}/confirm", headers=headers, json={})


class TestStagedRowsAreNotAnalyzed:
    async def test_staging_a_statement_creates_no_alerts(
        self, client: AsyncClient, beta_user: User, sync_db: Session
    ) -> None:
        """Detection belongs behind confirmation, not in the worker.

        Staged rows are an inference about a document. Telling someone about a
        duplicate in rows they have not agreed to import — and may still edit or
        discard — would be reporting on something that is not in their ledger.
        """
        _stage(sync_db, beta_user)
        assert _alerts(sync_db, beta_user) == []


class TestConfirmationRunsDetection:
    async def test_a_charge_from_another_upload_raises_a_duplicate(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        """The alert a statement import could not previously raise.

        The same charge reaches the ledger twice through two different files.
        The dedupe hash does not collapse them — the descriptions differ, as
        they do between a bank's CSV and its PDF — so both rows exist and the
        second must be flagged against the first.
        """
        account = resolve_account(sync_db, beta_user.id, None)
        _earlier_csv_charge(sync_db, beta_user, account.id)

        record = _stage(sync_db, beta_user)
        assert (await _confirm(client, beta_headers, record.id)).status_code == 200

        duplicates = [a for a in _alerts(sync_db, beta_user) if a.alert_type == AlertType.DUPLICATE]
        assert duplicates, "a charge already present from a different upload must be flagged"

    async def test_the_same_charge_twice_in_one_statement_is_not_a_duplicate(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        """Two identical coffees on one day are two coffees.

        Both rows come from the same upload, which is what separates a genuine
        repeat from the same charge arriving twice through different files.
        """
        record = _stage(sync_db, beta_user)
        assert (await _confirm(client, beta_headers, record.id)).status_code == 200

        upload_id = sync_db.get(StatementImport, record.id).upload_id
        same_file = [
            a
            for a in _alerts(sync_db, beta_user)
            if a.alert_type == AlertType.DUPLICATE
            and sync_db.get(Transaction, a.transaction_id).upload_id == upload_id
        ]
        assert same_file == []


class TestIdempotence:
    async def test_confirming_twice_produces_the_alerts_once(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        account = resolve_account(sync_db, beta_user.id, None)
        _earlier_csv_charge(sync_db, beta_user, account.id)

        record = _stage(sync_db, beta_user)
        assert (await _confirm(client, beta_headers, record.id)).status_code == 200
        after_first = len(_alerts(sync_db, beta_user))
        assert after_first > 0

        repeat = await _confirm(client, beta_headers, record.id)
        assert repeat.status_code == 200
        assert "already imported" in repeat.json()["message"]
        assert len(_alerts(sync_db, beta_user)) == after_first

    async def test_a_second_confirmation_imports_no_further_rows(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        await _confirm(client, beta_headers, record.id)
        before = sync_db.execute(
            select(func.count()).select_from(Transaction).where(Transaction.user_id == beta_user.id)
        ).scalar_one()

        await _confirm(client, beta_headers, record.id)
        sync_db.expire_all()
        after = sync_db.execute(
            select(func.count()).select_from(Transaction).where(Transaction.user_id == beta_user.id)
        ).scalar_one()
        assert after == before


class TestAtomicity:
    async def test_a_failure_in_detection_imports_nothing_and_leaves_it_retryable(
        self,
        client: AsyncClient,
        beta_headers: dict,
        beta_user: User,
        sync_db: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The reason inline analysis is safe to run inside the commit.

        Alerts and the transactions that caused them share one transaction, so a
        failure anywhere in detection rolls back both. What must not happen is a
        half-imported statement: rows in the ledger, no alerts, and an import
        marked committed that nobody can confirm again.
        """
        record = _stage(sync_db, beta_user)

        def _boom(*args: object, **kwargs: object) -> int:
            raise RuntimeError("detector exploded")

        monkeypatch.setattr("ledgerai.routers.statements.analyze_upload", _boom)
        with pytest.raises(RuntimeError):
            await _confirm(client, beta_headers, record.id)

        sync_db.expire_all()
        assert sync_db.execute(
            select(func.count()).select_from(Transaction).where(Transaction.user_id == beta_user.id)
        ).scalar_one() == 0
        assert _alerts(sync_db, beta_user) == []

        # Still staged, so the user can simply try again.
        again = sync_db.get(StatementImport, record.id)
        assert again.status == StatementImportStatus.NEEDS_REVIEW
        assert again.committed_at is None

        monkeypatch.undo()
        retried = await _confirm(client, beta_headers, record.id)
        assert retried.status_code == 200
        sync_db.expire_all()
        assert sync_db.execute(
            select(func.count()).select_from(Transaction).where(Transaction.user_id == beta_user.id)
        ).scalar_one() == len(ROWS)

    async def test_the_original_pdf_survives_a_failed_confirmation(
        self,
        client: AsyncClient,
        beta_headers: dict,
        beta_user: User,
        sync_db: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Purging the stored file is the one step a rollback cannot undo.

        It therefore runs after everything that can still fail. If detection
        raises, the file must still be there for the retry.
        """
        record = _stage(sync_db, beta_user)
        purged: list[uuid.UUID] = []
        real_purge = statements.purge_original

        def _record_purge(session: Session, local: StatementImport) -> None:
            purged.append(local.id)
            real_purge(session, local)

        monkeypatch.setattr("ledgerai.routers.statements.statements.purge_original", _record_purge)
        monkeypatch.setattr(
            "ledgerai.routers.statements.analyze_upload",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("detector exploded")),
        )
        with pytest.raises(RuntimeError):
            await _confirm(client, beta_headers, record.id)
        assert purged == [], "the file must not be purged by an attempt that failed"


class TestConcurrentConfirmation:
    async def test_two_confirmations_at_once_import_one_set_of_rows(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        """The row lock, exercised.

        Both requests read the import as reviewable before either commits. The
        lock serialises them so the loser sees "already imported" rather than a
        unique-constraint error, and the ledger gets one copy of the rows.
        """
        import anyio

        record = _stage(sync_db, beta_user)
        responses: list[int] = []

        async def _go() -> None:
            response = await _confirm(client, beta_headers, record.id)
            responses.append(response.status_code)

        async with anyio.create_task_group() as group:
            group.start_soon(_go)
            group.start_soon(_go)

        assert responses == [200, 200], responses
        sync_db.expire_all()
        assert sync_db.execute(
            select(func.count()).select_from(Transaction).where(Transaction.user_id == beta_user.id)
        ).scalar_one() == len(ROWS)


class TestOwnershipIsolation:
    async def test_detection_never_reaches_another_users_ledger(
        self,
        client: AsyncClient,
        beta_headers: dict,
        beta_user: User,
        sync_db: Session,
        demo_data: dict,
    ) -> None:
        other = demo_data["user"]
        before = len(_alerts(sync_db, other))
        record = _stage(sync_db, beta_user)
        assert (await _confirm(client, beta_headers, record.id)).status_code == 200
        assert len(_alerts(sync_db, other)) == before
