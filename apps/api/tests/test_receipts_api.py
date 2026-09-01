"""Receipt review, confirmation, linking and the currency rules, over HTTP."""

from __future__ import annotations

import io
import uuid
from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from ledgerai.models import Account, Receipt, ReceiptLinkMode, ReceiptStatus, Transaction, User
from ledgerai.services.receipts import SYNTHETIC_ACCOUNT_NAME
from tests.conftest import make_transaction, seeded_months

pytestmark = pytest.mark.asyncio

RECEIPT_TOTAL_CENTS = 3036  # $30.36


def _seeded_day() -> date:
    """A day inside the month `demo_data` fills.

    The dashboard anchors on the latest month that has data, so a receipt dated
    outside that month lands in the previous period and every "spending went
    up" assertion reads zero.
    """
    last_month, _ = seeded_months()
    return last_month + timedelta(days=5)


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def seed_receipt(  # noqa: PLR0913
    sync_db: Session,
    user: User,
    *,
    total_cents: int = RECEIPT_TOTAL_CENTS,
    currency: str = "USD",
    merchant: str = "Sandbox Grocers",
    posted: date | None = None,
    status: ReceiptStatus = ReceiptStatus.NEEDS_REVIEW,
) -> Receipt:
    """Insert an upload + receipt directly, so tests do not need Tesseract."""
    from ledgerai.models import Upload, UploadKind, UploadStatus

    upload = Upload(
        user_id=user.id,
        filename="receipt.png",
        original_filename="receipt_synthetic.png",
        content_hash=uuid.uuid4().hex,
        kind=UploadKind.IMAGE,
        content_type="image/png",
        size_bytes=1234,
        storage_key=f"users/{user.id}/uploads/{uuid.uuid4()}/receipt.png",
        status=UploadStatus.COMPLETE,
    )
    sync_db.add(upload)
    sync_db.flush()

    receipt = Receipt(
        user_id=user.id,
        upload_id=upload.id,
        status=status,
        page_count=1,
        ocr_confidence=0.93,
        raw_text="SANDBOX GROCERS\nTOTAL 30.36",
        merchant=merchant,
        posted_date=posted or _seeded_day(),
        subtotal_cents=2805,
        tax_cents=231,
        tip_cents=0,
        total_cents=total_cents,
        currency=currency,
        field_confidence={"total": 0.98, "merchant": 0.95},
        parse_notes={},
    )
    sync_db.add(receipt)
    sync_db.commit()
    return receipt


@pytest.fixture
def receipt(sync_db: Session, demo_data: dict) -> Receipt:
    return seed_receipt(sync_db, demo_data["user"])


class TestReceiptListing:
    async def test_lists_only_the_callers_receipts(
        self, client: AsyncClient, auth_headers: dict, other_headers: dict, receipt: Receipt
    ) -> None:
        mine = (await client.get("/api/receipts", headers=auth_headers)).json()
        theirs = (await client.get("/api/receipts", headers=other_headers)).json()
        assert len(mine) == 1
        assert theirs == []

    async def test_detail_includes_accounts_for_the_selector(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt
    ) -> None:
        data = (await client.get(f"/api/receipts/{receipt.id}", headers=auth_headers)).json()
        assert data["accounts"]
        assert data["default_account_name"] == SYNTHETIC_ACCOUNT_NAME

    async def test_detail_is_scoped(
        self, client: AsyncClient, other_headers: dict, receipt: Receipt
    ) -> None:
        response = await client.get(f"/api/receipts/{receipt.id}", headers=other_headers)
        assert response.status_code == 404


class TestAmountConvention:
    async def test_confirming_creates_a_negative_amount(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt
    ) -> None:
        """A $30.36 receipt is spending, so amount_cents must be -3036."""
        response = await client.post(
            f"/api/receipts/{receipt.id}/confirm",
            headers=auth_headers,
            json={"mode": "create"},
        )
        assert response.status_code == 200
        assert response.json()["amount_cents"] == -RECEIPT_TOTAL_CENTS

    async def test_confirming_increases_spending(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt
    ) -> None:
        before = (await client.get("/api/dashboard", headers=auth_headers)).json()
        await client.post(
            f"/api/receipts/{receipt.id}/confirm",
            headers=auth_headers,
            json={"mode": "create"},
        )
        after = (await client.get("/api/dashboard", headers=auth_headers)).json()

        delta = after["total_spend_cents"] - before["total_spend_cents"]
        assert delta == RECEIPT_TOTAL_CENTS  # increased, not decreased

    async def test_receipt_record_keeps_positive_extracted_values(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt
    ) -> None:
        data = (await client.get(f"/api/receipts/{receipt.id}", headers=auth_headers)).json()
        assert data["total_cents"] == RECEIPT_TOTAL_CENTS
        assert data["subtotal_cents"] > 0
        assert data["tax_cents"] >= 0


class TestAccountSelection:
    async def test_no_selection_uses_the_named_synthetic_account(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt, sync_db: Session
    ) -> None:
        """A receipt transaction is never silently attached to a bank account."""
        body = (
            await client.post(
                f"/api/receipts/{receipt.id}/confirm",
                headers=auth_headers,
                json={"mode": "create"},
            )
        ).json()

        transaction = sync_db.execute(
            select(Transaction).where(Transaction.id == uuid.UUID(body["transaction_id"]))
        ).scalar_one()
        account = sync_db.execute(
            select(Account).where(Account.id == transaction.account_id)
        ).scalar_one()

        assert account.name == SYNTHETIC_ACCOUNT_NAME
        assert account.is_synthetic is True

    async def test_chosen_account_is_honoured(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt, demo_data: dict,
        sync_db: Session,
    ) -> None:
        chosen = demo_data["account"]
        body = (
            await client.post(
                f"/api/receipts/{receipt.id}/confirm",
                headers=auth_headers,
                json={"mode": "create", "account_id": str(chosen.id)},
            )
        ).json()

        transaction = sync_db.execute(
            select(Transaction).where(Transaction.id == uuid.UUID(body["transaction_id"]))
        ).scalar_one()
        assert transaction.account_id == chosen.id

    async def test_another_users_account_cannot_be_targeted(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt, sync_db: Session,
        demo_data: dict,
    ) -> None:
        foreign = sync_db.execute(
            select(Account).where(Account.user_id == demo_data["other"].id)
        ).scalars().first()
        response = await client.post(
            f"/api/receipts/{receipt.id}/confirm",
            headers=auth_headers,
            json={"mode": "create", "account_id": str(foreign.id)},
        )
        assert response.status_code == 422


class TestIdempotency:
    async def test_confirming_twice_creates_one_transaction(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt
    ) -> None:
        first = (
            await client.post(
                f"/api/receipts/{receipt.id}/confirm",
                headers=auth_headers,
                json={"mode": "create"},
            )
        ).json()
        second = (
            await client.post(
                f"/api/receipts/{receipt.id}/confirm",
                headers=auth_headers,
                json={"mode": "create"},
            )
        ).json()

        assert first["transaction_id"] == second["transaction_id"]

        listing = (
            await client.get("/api/transactions?search=Sandbox%20Grocers", headers=auth_headers)
        ).json()
        assert listing["total"] == 1

    async def test_a_confirmed_receipt_can_no_longer_be_edited(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt
    ) -> None:
        await client.post(
            f"/api/receipts/{receipt.id}/confirm", headers=auth_headers, json={"mode": "create"}
        )
        response = await client.patch(
            f"/api/receipts/{receipt.id}", headers=auth_headers, json={"total_cents": 9999}
        )
        assert response.status_code == 409


class TestMatchCandidates:
    @pytest.fixture
    def matching_transaction(self, sync_db: Session, demo_data: dict) -> Transaction:

        transaction = make_transaction(
            sync_db, demo_data["user"], demo_data["account"],
            posted=_seeded_day(), cents=-RECEIPT_TOTAL_CENTS,
            description="SANDBOX GROCERS", merchant="Sandbox Grocers",
            category_slug="groceries", index=50,
        )
        sync_db.commit()
        return transaction

    async def test_finds_a_matching_charge_and_explains_why(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt,
        matching_transaction: Transaction,
    ) -> None:
        data = (
            await client.get(
                f"/api/receipts/{receipt.id}/match-candidates", headers=auth_headers
            )
        ).json()
        assert len(data["candidates"]) == 1

        candidate = data["candidates"][0]
        # Everything needed to tell two similar charges apart.
        for key in ("account_name", "posted_date", "merchant", "amount", "currency"):
            assert candidate[key] is not None
        assert "source_upload_id" in candidate
        names = {signal["name"] for signal in candidate["signals"]}
        assert {"amount", "date", "merchant", "currency", "account"} <= names

    async def test_a_refund_is_not_offered_as_a_match(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt,
        sync_db: Session, demo_data: dict,
    ) -> None:
        """Existing outflows are negative and the receipt total is positive, so
        a naive comparison would match a same-value refund."""

        make_transaction(
            sync_db, demo_data["user"], demo_data["account"],
            posted=_seeded_day(), cents=+RECEIPT_TOTAL_CENTS,
            description="SANDBOX GROCERS REFUND", merchant="Sandbox Grocers",
            category_slug="groceries", index=51,
        )
        sync_db.commit()

        data = (
            await client.get(
                f"/api/receipts/{receipt.id}/match-candidates", headers=auth_headers
            )
        ).json()
        assert data["candidates"] == []

    async def test_currency_mismatch_is_never_matched(
        self, client: AsyncClient, auth_headers: dict, sync_db: Session, demo_data: dict
    ) -> None:
        euro_receipt = seed_receipt(sync_db, demo_data["user"], currency="EUR")

        make_transaction(
            sync_db, demo_data["user"], demo_data["account"],
            posted=_seeded_day(), cents=-RECEIPT_TOTAL_CENTS,
            description="SANDBOX GROCERS", merchant="Sandbox Grocers",
            category_slug="groceries", index=52,
        )
        sync_db.commit()

        data = (
            await client.get(
                f"/api/receipts/{euro_receipt.id}/match-candidates", headers=auth_headers
            )
        ).json()
        assert data["candidates"] == []

    async def test_candidates_are_scoped_to_the_caller(
        self, client: AsyncClient, auth_headers: dict, other_headers: dict,
        receipt: Receipt, matching_transaction: Transaction,
    ) -> None:
        response = await client.get(
            f"/api/receipts/{receipt.id}/match-candidates", headers=other_headers
        )
        assert response.status_code == 404

    async def test_a_rejected_candidate_does_not_return(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt,
        matching_transaction: Transaction,
    ) -> None:
        await client.post(
            f"/api/receipts/{receipt.id}/reject-candidate",
            headers=auth_headers,
            json={"transaction_id": str(matching_transaction.id)},
        )
        data = (
            await client.get(
                f"/api/receipts/{receipt.id}/match-candidates", headers=auth_headers
            )
        ).json()
        assert data["candidates"] == []

    async def test_nothing_is_linked_without_an_explicit_confirm(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt,
        matching_transaction: Transaction, sync_db: Session,
    ) -> None:
        await client.get(
            f"/api/receipts/{receipt.id}/match-candidates", headers=auth_headers
        )
        sync_db.expire_all()
        stored = sync_db.execute(
            select(Receipt).where(Receipt.id == receipt.id)
        ).scalar_one()
        assert stored.transaction_id is None


class TestLinking:
    @pytest.fixture
    def existing(self, sync_db: Session, demo_data: dict) -> Transaction:

        transaction = make_transaction(
            sync_db, demo_data["user"], demo_data["account"],
            posted=_seeded_day(), cents=-RECEIPT_TOTAL_CENTS,
            description="SANDBOX GROCERS", merchant="Sandbox Grocers",
            category_slug="groceries", index=60,
        )
        sync_db.commit()
        return transaction

    async def test_linking_creates_no_transaction(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt, existing: Transaction
    ) -> None:
        before = (await client.get("/api/transactions?limit=1", headers=auth_headers)).json()
        await client.post(
            f"/api/receipts/{receipt.id}/confirm",
            headers=auth_headers,
            json={"mode": "link", "transaction_id": str(existing.id)},
        )
        after = (await client.get("/api/transactions?limit=1", headers=auth_headers)).json()
        assert after["total"] == before["total"]

    async def test_linking_preserves_the_existing_transaction(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt,
        existing: Transaction, sync_db: Session,
    ) -> None:
        before = (existing.merchant, existing.category_id, existing.amount_cents,
                  existing.is_corrected)

        await client.post(
            f"/api/receipts/{receipt.id}/confirm",
            headers=auth_headers,
            json={"mode": "link", "transaction_id": str(existing.id)},
        )
        sync_db.expire_all()
        after_row = sync_db.execute(
            select(Transaction).where(Transaction.id == existing.id)
        ).scalar_one()

        assert (after_row.merchant, after_row.category_id, after_row.amount_cents,
                after_row.is_corrected) == before

    async def test_linking_is_idempotent(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt, existing: Transaction
    ) -> None:
        first = (
            await client.post(
                f"/api/receipts/{receipt.id}/confirm",
                headers=auth_headers,
                json={"mode": "link", "transaction_id": str(existing.id)},
            )
        ).json()
        second = (
            await client.post(
                f"/api/receipts/{receipt.id}/confirm",
                headers=auth_headers,
                json={"mode": "link", "transaction_id": str(existing.id)},
            )
        ).json()
        assert first["transaction_id"] == second["transaction_id"]

    async def test_records_the_link_mode(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt,
        existing: Transaction, sync_db: Session,
    ) -> None:
        await client.post(
            f"/api/receipts/{receipt.id}/confirm",
            headers=auth_headers,
            json={"mode": "link", "transaction_id": str(existing.id)},
        )
        sync_db.expire_all()
        stored = sync_db.execute(select(Receipt).where(Receipt.id == receipt.id)).scalar_one()
        assert stored.link_mode == ReceiptLinkMode.LINKED
        assert stored.status == ReceiptStatus.CONFIRMED

    async def test_cannot_link_to_another_users_transaction(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt,
        sync_db: Session, demo_data: dict,
    ) -> None:
        foreign = sync_db.execute(
            select(Transaction).where(Transaction.user_id == demo_data["other"].id)
        ).scalars().first()
        response = await client.post(
            f"/api/receipts/{receipt.id}/confirm",
            headers=auth_headers,
            json={"mode": "link", "transaction_id": str(foreign.id)},
        )
        assert response.status_code == 422

    async def test_link_requires_a_transaction_id(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt
    ) -> None:
        response = await client.post(
            f"/api/receipts/{receipt.id}/confirm", headers=auth_headers, json={"mode": "link"}
        )
        assert response.status_code == 422


class TestCurrencyWarning:
    async def test_foreign_currency_receipt_warns_during_review(
        self, client: AsyncClient, auth_headers: dict, sync_db: Session, demo_data: dict
    ) -> None:
        euro = seed_receipt(sync_db, demo_data["user"], currency="EUR")
        data = (await client.get(f"/api/receipts/{euro.id}", headers=auth_headers)).json()

        assert data["currency_warning"] is not None
        assert "EUR" in data["currency_warning"]
        assert "does not convert" in data["currency_warning"]

    async def test_base_currency_receipt_has_no_warning(
        self, client: AsyncClient, auth_headers: dict, receipt: Receipt
    ) -> None:
        data = (await client.get(f"/api/receipts/{receipt.id}", headers=auth_headers)).json()
        assert data["currency_warning"] is None
