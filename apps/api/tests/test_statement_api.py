"""Statement PDFs at the upload boundary, and through review to commit.

Two properties carry most of the weight here. A PDF is a statement or a receipt
because the uploader said so — never because the server guessed — and nothing
parsed out of one reaches the ledger until a person confirms it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from ledgerai.config import settings
from ledgerai.models import (
    StatementImport,
    StatementImportRow,
    StatementImportStatus,
    Transaction,
    Upload,
    UploadKind,
    UploadStatus,
    User,
)
from ledgerai.security.filenames import build_storage_key
from ledgerai.security.jwt import create_access_token
from ledgerai.services import consent, quota, statements
from ledgerai.services.statements import extract_pages, parse_statement, verify_text_layer
from ledgerai.services.storage import StorageError, get_storage
from tests.synthetic_pdf import DEFAULT_ROWS, Run, transaction_page, write_pdf

STATEMENT = write_pdf([transaction_page(DEFAULT_ROWS)])


@pytest.fixture
def beta_user(sync_db: Session) -> User:
    user = User(
        email="beta-statements@test.local",
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


def _upload(client: AsyncClient, headers: dict, data: bytes, kind: str | None):
    files = {"file": ("august.pdf", data, "application/pdf")}
    payload = {"kind": kind} if kind else None
    return client.post("/api/uploads", headers=headers, files=files, data=payload)


def _stage(sync_db: Session, user: User, data: bytes = STATEMENT) -> StatementImport:
    """Put a parsed statement in front of review, as the worker would."""
    upload = Upload(
        user_id=user.id,
        filename="august.pdf",
        original_filename="august.pdf",
        content_hash=uuid.uuid4().hex,
        kind=UploadKind.STATEMENT_PDF,
        content_type="application/pdf",
        size_bytes=len(data),
        storage_key="",
        status=UploadStatus.PROCESSING,
    )
    sync_db.add(upload)
    sync_db.flush()
    pages = extract_pages(data)
    parsed = parse_statement(pages)
    record = statements.stage(
        sync_db,
        user_id=user.id,
        upload_id=upload.id,
        parsed=parsed,
        verification=verify_text_layer(b"", []),
    )
    sync_db.commit()
    return record


class TestUploadRouting:
    async def test_a_pdf_without_a_declared_kind_is_refused(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        response = await _upload(client, beta_headers, STATEMENT, None)
        assert response.status_code == 422
        assert "statement or a receipt" in response.json()["detail"]

    async def test_a_declared_statement_is_stored_as_one(
        self, client: AsyncClient, beta_headers: dict, sync_db: Session
    ) -> None:
        response = await _upload(client, beta_headers, STATEMENT, "statement")
        assert response.status_code == 201, response.text

        sync_db.expire_all()
        upload = sync_db.execute(select(Upload)).scalars().one()
        assert upload.kind == UploadKind.STATEMENT_PDF

    async def test_a_declared_receipt_pdf_keeps_the_receipt_path(
        self, client: AsyncClient, beta_headers: dict, sync_db: Session
    ) -> None:
        """The existing OCR path and its five-page cap must be untouched."""
        response = await _upload(client, beta_headers, STATEMENT, "receipt")
        assert response.status_code == 201

        sync_db.expire_all()
        upload = sync_db.execute(select(Upload)).scalars().one()
        assert upload.kind == UploadKind.IMAGE

    async def test_a_csv_needs_no_kind(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        csv = b"Date,Description,Amount\n2026-08-01,SANDBOX SHOP,-4.50\n"
        response = await client.post(
            "/api/uploads", headers=beta_headers, files={"file": ("s.csv", csv, "text/csv")}
        )
        assert response.status_code == 201


class TestStatementScreening:
    async def test_a_scanned_pdf_is_refused_with_a_route_out(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        response = await _upload(client, beta_headers, write_pdf([[]]), "statement")
        assert response.status_code == 422
        assert "csv" in response.json()["detail"].lower()

    async def test_an_unmasked_account_number_refuses_the_file(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        page = transaction_page(DEFAULT_ROWS)
        page.append(Run(58, 660, "Account Number 021000021"))
        response = await _upload(client, beta_headers, write_pdf([page]), "statement")

        assert response.status_code == 422
        assert response.headers["x-rejected-categories"] == "us_routing"

    async def test_the_refusal_names_no_page_or_value(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        page = transaction_page(DEFAULT_ROWS)
        page.append(Run(58, 660, "Account Number 021000021"))
        response = await _upload(client, beta_headers, write_pdf([page]), "statement")

        body = response.text + repr(dict(response.headers))
        for leak in ("021000021", "page 1", "SANDBOX GROCERS", "1,904.55"):
            assert leak not in body, leak

    async def test_a_rejected_statement_is_never_stored(
        self, client: AsyncClient, beta_headers: dict, sync_db: Session
    ) -> None:
        page = transaction_page(DEFAULT_ROWS)
        page.append(Run(58, 660, "Account Number 021000021"))
        await _upload(client, beta_headers, write_pdf([page]), "statement")

        sync_db.expire_all()
        assert sync_db.execute(select(Upload)).scalars().all() == []

    async def test_a_masked_account_number_is_accepted(
        self, client: AsyncClient, beta_headers: dict
    ) -> None:
        # Every real statement prints its own masked account line.
        page = transaction_page(DEFAULT_ROWS)
        page.append(Run(58, 660, "Account Number ****4821"))
        response = await _upload(client, beta_headers, write_pdf([page]), "statement")
        assert response.status_code == 201


class TestReview:
    async def test_rows_are_listed_with_confidence_and_direction(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        body = (await client.get(f"/api/statements/{record.id}", headers=beta_headers)).json()

        assert body["row_count"] == 5
        assert body["status"] == "needs_review"
        assert {r["direction"] for r in body["rows"]} == {"debit", "credit"}
        assert all(r["confidence"] == 1.0 for r in body["rows"])

    async def test_nothing_is_in_the_ledger_before_confirmation(
        self, client: AsyncClient, beta_user: User, sync_db: Session
    ) -> None:
        _stage(sync_db, beta_user)
        sync_db.expire_all()
        mine = sync_db.execute(
            select(Transaction).where(Transaction.user_id == beta_user.id)
        ).scalars().all()
        assert mine == []

    async def test_a_row_can_be_corrected(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        body = (await client.get(f"/api/statements/{record.id}", headers=beta_headers)).json()
        rows = body["rows"]

        response = await client.patch(
            f"/api/statements/{record.id}/rows/{rows[0]['id']}",
            headers=beta_headers,
            json={"description": "SANDBOX GROCERS CORRECTED", "amount_cents": -5000},
        )
        assert response.status_code == 200
        assert response.json()["description"] == "SANDBOX GROCERS CORRECTED"
        assert response.json()["edited"] is True
        assert response.json()["confidence"] == 1.0

    async def test_a_row_can_be_excluded(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        body = (await client.get(f"/api/statements/{record.id}", headers=beta_headers)).json()
        rows = body["rows"]
        await client.patch(
            f"/api/statements/{record.id}/rows/{rows[0]['id']}",
            headers=beta_headers,
            json={"excluded": True},
        )
        confirmed = await client.post(
            f"/api/statements/{record.id}/confirm", headers=beta_headers, json={}
        )
        assert "4 transaction(s) imported" in confirmed.json()["message"]


class TestConfirm:
    async def test_confirming_creates_the_transactions(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        response = await client.post(
            f"/api/statements/{record.id}/confirm", headers=beta_headers, json={}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "committed"

        sync_db.expire_all()
        rows = sync_db.execute(
            select(Transaction).where(Transaction.user_id == beta_user.id)
        ).scalars().all()
        assert len(rows) == 5
        assert sorted(t.amount_cents for t in rows) == [-4210, -3036, -675, -275, 180000]

    async def test_confirming_twice_imports_nothing_the_second_time(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        await client.post(f"/api/statements/{record.id}/confirm", headers=beta_headers, json={})
        second = await client.post(
            f"/api/statements/{record.id}/confirm", headers=beta_headers, json={}
        )
        assert "already imported" in second.json()["message"]

        sync_db.expire_all()
        mine = sync_db.execute(
            select(Transaction).where(Transaction.user_id == beta_user.id)
        ).scalars().all()
        assert len(mine) == 5

    async def test_a_low_confidence_row_stays_flagged_after_import(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        rows = list(DEFAULT_ROWS)
        rows[2] = ("14 Aug", "SANDBOX GROCERS 0042", -3036, 187_999)
        record = _stage(sync_db, beta_user, write_pdf([transaction_page(rows)]))
        await client.post(f"/api/statements/{record.id}/confirm", headers=beta_headers, json={})

        sync_db.expire_all()
        flagged = sync_db.execute(
            select(Transaction).where(
                Transaction.user_id == beta_user.id, Transaction.needs_review.is_(True)
            )
        ).scalars().all()
        assert flagged, "a row the parser doubted must not blend into the ledger"

    async def test_the_original_pdf_is_purged_on_commit(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        upload_id = record.upload_id
        key = build_storage_key(beta_user.id, "august.pdf")
        get_storage().put(key, b"%PDF-1.4 synthetic", "application/pdf")
        sync_db.get(Upload, upload_id).storage_key = key
        sync_db.commit()

        await client.post(f"/api/statements/{record.id}/confirm", headers=beta_headers, json={})

        sync_db.expunge_all()
        assert sync_db.get(Upload, upload_id).storage_key == ""
        with pytest.raises(StorageError):
            get_storage().get(key)


class TestOwnership:
    async def test_another_account_cannot_see_the_import(
        self, client: AsyncClient, auth_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        for path in (f"/api/statements/{record.id}",):
            response = await client.get(path, headers=auth_headers)
            # 404 rather than 403: a 403 would confirm the import exists.
            assert response.status_code == 404

    async def test_another_account_cannot_confirm_it(
        self, client: AsyncClient, auth_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        response = await client.post(
            f"/api/statements/{record.id}/confirm", headers=auth_headers, json={}
        )
        assert response.status_code == 404

    async def test_unauthenticated_access_is_refused(
        self, client: AsyncClient, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        assert (await client.get(f"/api/statements/{record.id}")).status_code == 401


class TestDiscardAndExpiry:
    async def test_discarding_removes_everything(
        self, client: AsyncClient, beta_headers: dict, beta_user: User, sync_db: Session
    ) -> None:
        record = _stage(sync_db, beta_user)
        response = await client.delete(f"/api/statements/{record.id}", headers=beta_headers)
        assert response.status_code == 204

        # The row is gone on another connection; drop this session's copy of it
        # rather than asking it about a row it still thinks it holds.
        sync_db.expunge_all()
        assert sync_db.get(StatementImport, record.id) is None
        remaining = sync_db.execute(
            select(StatementImportRow).where(StatementImportRow.user_id == beta_user.id)
        ).scalars().all()
        assert remaining == []

    def test_an_abandoned_import_is_swept(self, sync_db: Session, beta_user: User) -> None:
        from ledgerai.services.lifecycle import retention_sweep

        record = _stage(sync_db, beta_user)
        record.expires_at = datetime.now(UTC) - timedelta(hours=1)
        sync_db.commit()

        report = retention_sweep(sync_db)
        sync_db.commit()

        assert report.statement_imports_expired == 1
        assert sync_db.get(StatementImport, record.id) is None

    def test_a_live_import_survives_the_sweep(
        self, sync_db: Session, beta_user: User
    ) -> None:
        from ledgerai.services.lifecycle import retention_sweep

        record = _stage(sync_db, beta_user)
        sync_db.commit()
        assert retention_sweep(sync_db).statement_imports_expired == 0
        assert sync_db.get(StatementImport, record.id) is not None

    def test_a_committed_import_is_never_swept(
        self, sync_db: Session, beta_user: User
    ) -> None:
        from ledgerai.services.lifecycle import retention_sweep

        record = _stage(sync_db, beta_user)
        record.status = StatementImportStatus.COMMITTED
        record.expires_at = datetime.now(UTC) - timedelta(days=30)
        sync_db.commit()

        assert retention_sweep(sync_db).statement_imports_expired == 0


class TestQuotas:
    def test_pages_are_reserved_as_well_as_bytes(
        self, sync_db: Session, beta_user: User
    ) -> None:
        reservation = quota.reserve_upload(sync_db, beta_user.id, 1024, pages=12)
        assert reservation.pages_reserved == 12

    def _spend_pages(self, sync_db: Session, user: User, pages: int) -> None:
        """Commit pages the way a finished import does, without holding a claim."""
        upload = Upload(
            user_id=user.id,
            filename="prior.pdf",
            original_filename="prior.pdf",
            content_hash=uuid.uuid4().hex,
            kind=UploadKind.STATEMENT_PDF,
            content_type="application/pdf",
            size_bytes=1024,
            storage_key="",
            status=UploadStatus.COMPLETE,
        )
        sync_db.add(upload)
        sync_db.flush()
        sync_db.add(
            StatementImport(
                user_id=user.id,
                upload_id=upload.id,
                status=StatementImportStatus.COMMITTED,
                page_count=pages,
                expires_at=datetime.now(UTC) + timedelta(hours=72),
                notes={},
            )
        )
        sync_db.flush()

    def test_the_daily_page_budget_is_enforced(
        self, sync_db: Session, beta_user: User
    ) -> None:
        self._spend_pages(sync_db, beta_user, settings.quota_statement_pages_per_day - 10)
        with pytest.raises(quota.QuotaExceededError) as exc:
            quota.reserve_upload(sync_db, beta_user.id, 1024, pages=40)
        assert exc.value.quota == "statement_pages_per_day"

    def test_a_page_budget_refusal_says_when_it_resets(
        self, sync_db: Session, beta_user: User
    ) -> None:
        self._spend_pages(sync_db, beta_user, settings.quota_statement_pages_per_day)
        with pytest.raises(quota.QuotaExceededError) as exc:
            quota.reserve_upload(sync_db, beta_user.id, 1024, pages=1)
        assert "midnight UTC" in exc.value.detail

    def test_a_statement_within_the_budget_is_allowed(
        self, sync_db: Session, beta_user: User
    ) -> None:
        self._spend_pages(sync_db, beta_user, 40)
        assert quota.reserve_upload(sync_db, beta_user.id, 1024, pages=40) is not None

    def test_a_csv_reserves_no_pages(self, sync_db: Session, beta_user: User) -> None:
        assert quota.reserve_upload(sync_db, beta_user.id, 1024).pages_reserved == 0
