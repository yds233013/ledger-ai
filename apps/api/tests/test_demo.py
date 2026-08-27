"""Ephemeral demo accounts: provisioning, isolation, expiry and cleanup.

The demo is the one place where an anonymous caller can create state, so the
properties worth pinning are the ones that would let it become a liability:
one visitor reaching another's data, a retry doubling the work, a half-built
account being handed out, a session that never expires, or a cleanup sweep that
reaches a real user.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ledgerai.models import (
    Account,
    Alert,
    AnalysisRun,
    Category,
    ProcessingJob,
    Receipt,
    Transaction,
    TransactionCorrection,
    Upload,
    User,
)
from ledgerai.security.jwt import create_access_token
from ledgerai.security.ratelimit import DEMO_SESSION_LIMIT
from ledgerai.services.demo import (
    DEMO_EMAIL_DOMAIN,
    DEMO_LIFETIME_HOURS,
    cleanup_expired_demo_users,
    delete_demo_user,
    demo_has_expired,
    expired_demo_user_ids,
    is_ephemeral_demo,
    new_request_key,
    provision_demo_user,
)
from ledgerai.services.demo_data import DEMO_MONTHS_OF_HISTORY

# asyncio_mode = "auto" in pyproject.toml, so async tests need no marker and a
# blanket one would wrongly tag the many synchronous tests in this file.

# "Approximately 250" — a range, not a magic number, so tuning the generator's
# density does not silently break the product claim or the test.
EXPECTED_MIN_TRANSACTIONS = 200
EXPECTED_MAX_TRANSACTIONS = 320


def owned_counts(session: Session, user_id: uuid.UUID) -> dict[str, int]:
    """Every table that carries this user's rows."""
    models = (
        Account, Transaction, Alert, AnalysisRun, Upload,
        ProcessingJob, Receipt, TransactionCorrection, Category,
    )
    return {
        model.__tablename__: int(
            session.execute(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            ).scalar_one()
        )
        for model in models
    }


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------


class TestProvisioning:
    def test_it_creates_a_usable_populated_account(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        info = provision_demo_user(sync_factory, request_key=new_request_key())

        assert info.transaction_count >= EXPECTED_MIN_TRANSACTIONS
        assert info.account_count == 3
        assert info.reused is False

    def test_it_seeds_approximately_250_transactions(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        info = provision_demo_user(sync_factory, request_key=new_request_key())

        assert EXPECTED_MIN_TRANSACTIONS <= info.transaction_count <= EXPECTED_MAX_TRANSACTIONS

    def test_it_covers_about_eight_months(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        info = provision_demo_user(sync_factory, request_key=new_request_key())

        months = sync_db.execute(
            select(func.distinct(func.date_trunc("month", Transaction.posted_date)))
            .where(Transaction.user_id == info.user_id)
        ).scalars().all()
        assert len(months) == DEMO_MONTHS_OF_HISTORY

    def test_the_data_is_marked_synthetic(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        """Three independent markers, so no screenshot can be mistaken for real."""
        info = provision_demo_user(sync_factory, request_key=new_request_key())

        user = sync_db.execute(select(User).where(User.id == info.user_id)).scalar_one()
        assert "Synthetic" in user.display_name
        assert user.is_demo is True

        accounts = sync_db.execute(
            select(Account.name).where(Account.user_id == info.user_id)
        ).scalars().all()
        assert all(name.startswith("SANDBOX") for name in accounts)

        descriptions = sync_db.execute(
            select(Transaction.raw_description).where(Transaction.user_id == info.user_id)
        ).scalars().all()
        assert all("[SYNTHETIC]" in text for text in descriptions)

    def test_the_dashboard_has_alerts_to_show(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        """A demo whose alerts panel is empty demonstrates nothing."""
        info = provision_demo_user(sync_factory, request_key=new_request_key())
        assert info.alert_count > 0

    def test_transactions_are_categorized(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        info = provision_demo_user(sync_factory, request_key=new_request_key())

        categorized = sync_db.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == info.user_id,
                Transaction.category_id.is_not(None),
            )
        ).scalar_one()
        assert categorized > info.transaction_count * 0.7

    def test_the_account_expires_in_24_hours(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        before = datetime.now(UTC)
        info = provision_demo_user(sync_factory, request_key=new_request_key())

        expected = before + timedelta(hours=DEMO_LIFETIME_HOURS)
        assert abs((info.expires_at - expected).total_seconds()) < 60

    def test_the_address_cannot_collide_with_a_real_signup(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        info = provision_demo_user(sync_factory, request_key=new_request_key())
        assert info.email.endswith(f"@{DEMO_EMAIL_DOMAIN}")

    def test_the_seeded_development_password_is_never_reused(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        """A demo account must not be sign-in-able with the documented password."""
        from ledgerai.config import settings
        from ledgerai.security.passwords import verify_password

        info = provision_demo_user(sync_factory, request_key=new_request_key())
        user = sync_db.execute(select(User).where(User.id == info.user_id)).scalar_one()

        assert not verify_password(settings.demo_user_password, user.password_hash)
        assert settings.demo_user_password not in user.password_hash

    def test_two_visitors_get_different_datasets(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        """Independent seeds — one visitor's totals must not be another's."""
        first = provision_demo_user(sync_factory, request_key=new_request_key())
        second = provision_demo_user(sync_factory, request_key=new_request_key())

        assert first.user_id != second.user_id

        def total(user_id: uuid.UUID) -> int:
            return int(
                sync_db.execute(
                    select(func.coalesce(func.sum(Transaction.amount_cents), 0))
                    .where(Transaction.user_id == user_id)
                ).scalar_one()
            )

        assert total(first.user_id) != total(second.user_id)


class TestIdempotency:
    def test_the_same_request_key_returns_the_same_account(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        key = new_request_key()
        first = provision_demo_user(sync_factory, request_key=key)
        second = provision_demo_user(sync_factory, request_key=key)

        assert second.user_id == first.user_id
        assert second.reused is True

    def test_a_retry_does_not_double_the_dataset(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        key = new_request_key()
        first = provision_demo_user(sync_factory, request_key=key)
        provision_demo_user(sync_factory, request_key=key)

        rows = sync_db.execute(
            select(func.count(Transaction.id)).where(Transaction.user_id == first.user_id)
        ).scalar_one()
        assert rows == first.transaction_count

    def test_a_retry_creates_no_second_user(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        key = new_request_key()
        provision_demo_user(sync_factory, request_key=key)
        provision_demo_user(sync_factory, request_key=key)

        users = sync_db.execute(
            select(func.count(User.id)).where(User.demo_request_key == key)
        ).scalar_one()
        assert users == 1

    def test_different_keys_create_different_accounts(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        first = provision_demo_user(sync_factory, request_key=new_request_key())
        second = provision_demo_user(sync_factory, request_key=new_request_key())
        assert first.user_id != second.user_id


class TestConcurrency:
    def test_concurrent_requests_with_one_key_produce_one_account(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        """Real threads against real Postgres — the unique index is the referee.

        Whichever thread flushes first claims the key; the other collides,
        rolls back and re-reads. Neither may end up with a partial account.
        """
        key = new_request_key()
        results: list[object] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(provision_demo_user(sync_factory, request_key=key))
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        assert errors == [], f"provisioning raised under concurrency: {errors}"
        assert len(results) == 4

        user_ids = {result.user_id for result in results}  # type: ignore[attr-defined]
        assert len(user_ids) == 1, "concurrent requests created more than one account"

        rows = sync_db.execute(
            select(func.count(User.id)).where(User.demo_request_key == key)
        ).scalar_one()
        assert rows == 1

    def test_the_surviving_account_is_complete_not_partial(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        key = new_request_key()
        threads = [
            threading.Thread(
                target=lambda: provision_demo_user(sync_factory, request_key=key)
            )
            for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        user = sync_db.execute(
            select(User).where(User.demo_request_key == key)
        ).scalar_one()
        counts = owned_counts(sync_db, user.id)

        assert counts["accounts"] == 3
        assert counts["transactions"] >= EXPECTED_MIN_TRANSACTIONS
        assert counts["alerts"] > 0


class TestPartialFailureIsRecoverable:
    def test_a_failure_during_seeding_leaves_no_user_behind(
        self, sync_factory: sessionmaker[Session], sync_db: Session, monkeypatch
    ) -> None:
        """All-or-nothing: a half-built account must never be handed out."""
        from ledgerai.services import demo as demo_module

        key = new_request_key()

        def explode(*args, **kwargs):
            raise RuntimeError("seeding failed half way")

        monkeypatch.setattr(demo_module, "_seed_dataset", explode)

        with pytest.raises(RuntimeError):
            provision_demo_user(sync_factory, request_key=key)

        leftover = sync_db.execute(
            select(func.count(User.id)).where(User.demo_request_key == key)
        ).scalar_one()
        assert leftover == 0, "a failed provisioning left a user row behind"

    def test_retrying_after_a_failure_succeeds(
        self, sync_factory: sessionmaker[Session], sync_db: Session, monkeypatch
    ) -> None:
        """The failed attempt must not poison the key for the retry."""
        from ledgerai.services import demo as demo_module

        key = new_request_key()
        original = demo_module._seed_dataset

        def explode(*args, **kwargs):
            raise RuntimeError("transient failure")

        monkeypatch.setattr(demo_module, "_seed_dataset", explode)
        with pytest.raises(RuntimeError):
            provision_demo_user(sync_factory, request_key=key)

        monkeypatch.setattr(demo_module, "_seed_dataset", original)
        info = provision_demo_user(sync_factory, request_key=key)

        assert info.transaction_count >= EXPECTED_MIN_TRANSACTIONS
        assert info.reused is False


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


class TestIsolation:
    async def test_one_visitor_cannot_see_anothers_transactions(
        self, client: AsyncClient, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        first = provision_demo_user(sync_factory, request_key=new_request_key())
        second = provision_demo_user(sync_factory, request_key=new_request_key())

        headers = {"Authorization": f"Bearer {create_access_token(first.user_id, first.email)}"}
        response = await client.get(
            "/api/transactions", headers=headers, params={"limit": 200}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == first.transaction_count

        second_ids = {
            str(row)
            for row in sync_db.execute(
                select(Transaction.id).where(Transaction.user_id == second.user_id)
            ).scalars().all()
        }
        returned = {item["id"] for item in body["items"]}
        assert returned.isdisjoint(second_ids)

    async def test_one_visitor_cannot_see_anothers_dashboard(
        self, client: AsyncClient, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        first = provision_demo_user(sync_factory, request_key=new_request_key())
        second = provision_demo_user(sync_factory, request_key=new_request_key())

        async def dashboard(info) -> dict:  # noqa: ANN001
            headers = {
                "Authorization": f"Bearer {create_access_token(info.user_id, info.email)}"
            }
            response = await client.get("/api/dashboard", headers=headers)
            assert response.status_code == 200
            return response.json()

        first_board = await dashboard(first)
        second_board = await dashboard(second)

        # Each visitor sees only their own three sandbox accounts.
        assert first_board["account_count"] == 3
        assert second_board["account_count"] == 3

        # Independently seeded datasets: the totals cannot coincide.
        assert first_board["total_spend_cents"] != second_board["total_spend_cents"]

        # And no row of one appears in the other's recent list.
        second_ids = {
            str(row)
            for row in sync_db.execute(
                select(Transaction.id).where(Transaction.user_id == second.user_id)
            ).scalars().all()
        }
        assert {row["id"] for row in first_board["recent"]}.isdisjoint(second_ids)

    async def test_one_visitor_cannot_modify_anothers_transaction(
        self, client: AsyncClient, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        first = provision_demo_user(sync_factory, request_key=new_request_key())
        second = provision_demo_user(sync_factory, request_key=new_request_key())

        victim = sync_db.execute(
            select(Transaction.id).where(Transaction.user_id == second.user_id).limit(1)
        ).scalar_one()

        headers = {"Authorization": f"Bearer {create_access_token(first.user_id, first.email)}"}
        response = await client.patch(
            f"/api/transactions/{victim}",
            headers=headers,
            json={"merchant": "Hijacked"},
        )
        # 404, not 403 — another user's row must not be shown to exist.
        assert response.status_code == 404

    async def test_deleting_one_visitors_data_leaves_the_other_intact(
        self, client: AsyncClient, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        first = provision_demo_user(sync_factory, request_key=new_request_key())
        second = provision_demo_user(sync_factory, request_key=new_request_key())

        headers = {"Authorization": f"Bearer {create_access_token(first.user_id, first.email)}"}
        response = await client.post(
            "/api/settings/delete-data",
            headers=headers,
            json={"confirmation": "DELETE", "dry_run": False},
        )
        assert response.status_code == 200

        sync_db.expire_all()
        assert owned_counts(sync_db, first.user_id)["transactions"] == 0
        assert (
            owned_counts(sync_db, second.user_id)["transactions"]
            == second.transaction_count
        )

    async def test_a_demo_visitor_cannot_see_the_development_account(
        self, client: AsyncClient, sync_factory: sessionmaker[Session], demo_data: dict
    ) -> None:
        """The pre-existing seeded user's data must not leak into a demo."""
        info = provision_demo_user(sync_factory, request_key=new_request_key())
        headers = {"Authorization": f"Bearer {create_access_token(info.user_id, info.email)}"}

        response = await client.get(
            "/api/transactions", headers=headers, params={"search": "OTHER USER SECRET"}
        )
        assert response.json()["total"] == 0


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------


class TestExpiry:
    def test_a_fresh_account_has_not_expired(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        info = provision_demo_user(sync_factory, request_key=new_request_key())
        user = sync_db.execute(select(User).where(User.id == info.user_id)).scalar_one()
        assert demo_has_expired(user) is False

    def test_a_permanent_account_never_expires(self, sync_db: Session, demo_data: dict) -> None:
        """is_demo without a deadline is the permanent development account."""
        user = demo_data["user"]
        assert user.is_demo is True
        assert user.demo_expires_at is None
        assert is_ephemeral_demo(user) is False
        assert demo_has_expired(user) is False

    async def test_an_expired_account_is_refused(
        self, client: AsyncClient, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        info = provision_demo_user(sync_factory, request_key=new_request_key())
        user = sync_db.execute(select(User).where(User.id == info.user_id)).scalar_one()
        user.demo_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        sync_db.commit()

        headers = {"Authorization": f"Bearer {create_access_token(info.user_id, info.email)}"}
        response = await client.get("/api/transactions", headers=headers)

        assert response.status_code == 401
        assert "demo session has ended" in response.json()["detail"].lower()

    async def test_a_fresh_token_cannot_extend_an_expired_demo(
        self, client: AsyncClient, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        """The regression that matters: refreshing the page must not renew it.

        The browser mints a new short-lived token whenever the old one nears
        expiry. If expiry lived in the token, keeping the tab open would extend
        the demo indefinitely.
        """
        info = provision_demo_user(sync_factory, request_key=new_request_key())
        user = sync_db.execute(select(User).where(User.id == info.user_id)).scalar_one()
        user.demo_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        sync_db.commit()

        # A brand-new, entirely valid, long-lived token.
        fresh = create_access_token(info.user_id, info.email, ttl_minutes=600)
        response = await client.get(
            "/api/transactions", headers={"Authorization": f"Bearer {fresh}"}
        )
        assert response.status_code == 401

    async def test_an_unexpired_demo_still_works(
        self, client: AsyncClient, sync_factory: sessionmaker[Session]
    ) -> None:
        """Positive control for the two tests above."""
        info = provision_demo_user(sync_factory, request_key=new_request_key())
        headers = {"Authorization": f"Bearer {create_access_token(info.user_id, info.email)}"}
        response = await client.get("/api/transactions", headers=headers)
        assert response.status_code == 200


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------


class TestCleanup:
    def _expire(self, sync_db: Session, user_id: uuid.UUID) -> None:
        user = sync_db.execute(select(User).where(User.id == user_id)).scalar_one()
        user.demo_expires_at = datetime.now(UTC) - timedelta(hours=1)
        sync_db.commit()

    def test_an_expired_account_is_removed_entirely(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        info = provision_demo_user(sync_factory, request_key=new_request_key())
        self._expire(sync_db, info.user_id)

        report = cleanup_expired_demo_users(sync_db)
        sync_db.commit()

        assert report.users_removed == 1
        assert all(count == 0 for count in owned_counts(sync_db, info.user_id).values())
        assert sync_db.execute(
            select(User).where(User.id == info.user_id)
        ).scalar_one_or_none() is None

    def test_every_owned_table_is_emptied(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        """Positive control: the account really did own rows before cleanup."""
        info = provision_demo_user(sync_factory, request_key=new_request_key())
        before = owned_counts(sync_db, info.user_id)
        assert before["accounts"] > 0
        assert before["transactions"] > 0
        assert before["alerts"] > 0

        self._expire(sync_db, info.user_id)
        cleanup_expired_demo_users(sync_db)
        sync_db.commit()

        assert all(count == 0 for count in owned_counts(sync_db, info.user_id).values())

    def test_cleanup_is_idempotent(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        info = provision_demo_user(sync_factory, request_key=new_request_key())
        self._expire(sync_db, info.user_id)

        first = cleanup_expired_demo_users(sync_db)
        sync_db.commit()
        second = cleanup_expired_demo_users(sync_db)
        sync_db.commit()

        assert first.users_removed == 1
        assert second.users_removed == 0

    def test_an_unexpired_account_survives(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        info = provision_demo_user(sync_factory, request_key=new_request_key())

        report = cleanup_expired_demo_users(sync_db)
        sync_db.commit()

        assert report.users_removed == 0
        assert sync_db.execute(
            select(User).where(User.id == info.user_id)
        ).scalar_one_or_none() is not None

    def test_cleanup_removes_only_the_expired_one(
        self, sync_factory: sessionmaker[Session], sync_db: Session
    ) -> None:
        stale = provision_demo_user(sync_factory, request_key=new_request_key())
        live = provision_demo_user(sync_factory, request_key=new_request_key())
        self._expire(sync_db, stale.user_id)

        cleanup_expired_demo_users(sync_db)
        sync_db.commit()

        assert owned_counts(sync_db, stale.user_id)["transactions"] == 0
        assert owned_counts(sync_db, live.user_id)["transactions"] == live.transaction_count


class TestCleanupSafety:
    """The sweep must be structurally incapable of reaching a real account."""

    def test_an_ordinary_user_is_never_selected(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        ordinary = demo_data["other"]
        ordinary.is_demo = False
        ordinary.demo_expires_at = datetime.now(UTC) - timedelta(days=365)
        sync_db.commit()

        assert ordinary.id not in expired_demo_user_ids(sync_db)

    def test_an_ordinary_user_is_never_deleted(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        """Even with a long-past date, a non-demo account stays put."""
        ordinary = demo_data["other"]
        ordinary.is_demo = False
        ordinary.demo_expires_at = datetime.now(UTC) - timedelta(days=365)
        sync_db.commit()
        before = owned_counts(sync_db, ordinary.id)

        cleanup_expired_demo_users(sync_db)
        sync_db.commit()

        assert sync_db.execute(
            select(User).where(User.id == ordinary.id)
        ).scalar_one_or_none() is not None
        assert owned_counts(sync_db, ordinary.id) == before

    def test_the_permanent_development_demo_user_is_never_selected(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        """is_demo=True with no deadline — the seeded local account."""
        permanent = demo_data["user"]
        assert permanent.is_demo is True
        assert permanent.demo_expires_at is None

        assert permanent.id not in expired_demo_user_ids(sync_db)

        cleanup_expired_demo_users(sync_db)
        sync_db.commit()
        assert sync_db.execute(
            select(User).where(User.id == permanent.id)
        ).scalar_one_or_none() is not None

    def test_direct_deletion_refuses_a_non_demo_user(
        self, sync_db: Session, demo_data: dict
    ) -> None:
        """Belt and braces: even called directly it will not delete a real user."""
        ordinary = demo_data["other"]
        ordinary.is_demo = False
        sync_db.commit()

        with pytest.raises(ValueError, match="ephemeral demo"):
            delete_demo_user(sync_db, ordinary.id)

        assert sync_db.execute(
            select(User).where(User.id == ordinary.id)
        ).scalar_one_or_none() is not None

    def test_direct_deletion_of_a_missing_user_is_a_no_op(
        self, sync_db: Session
    ) -> None:
        report = delete_demo_user(sync_db, uuid.uuid4())
        assert report.users_removed == 0


# --------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------


class TestDemoSessionEndpoint:
    async def test_it_provisions_without_authentication(
        self, client: AsyncClient
    ) -> None:
        response = await client.post("/api/auth/demo-session", json={})

        assert response.status_code == 200
        body = response.json()
        assert body["transaction_count"] >= EXPECTED_MIN_TRANSACTIONS
        assert body["user"]["is_demo"] is True
        assert body["reused"] is False

    async def test_the_returned_token_works(self, client: AsyncClient) -> None:
        body = (await client.post("/api/auth/demo-session", json={})).json()
        headers = {"Authorization": f"Bearer {body['access_token']}"}

        me = await client.get("/api/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["id"] == body["user"]["id"]

    async def test_the_token_never_outlives_the_demo_account(
        self, client: AsyncClient
    ) -> None:
        body = (await client.post("/api/auth/demo-session", json={})).json()
        assert body["expires_in"] <= body["demo_expires_in_seconds"] + 60

    async def test_it_reports_when_the_demo_ends(self, client: AsyncClient) -> None:
        body = (await client.post("/api/auth/demo-session", json={})).json()

        assert body["demo_expires_in_seconds"] > 0
        assert body["user"]["demo_expires_at"] is not None
        assert "synthetic" in body["notice"].lower()

    async def test_it_never_returns_a_password(self, client: AsyncClient) -> None:
        raw = (await client.post("/api/auth/demo-session", json={})).text
        from ledgerai.config import settings

        assert "password" not in raw.lower()
        assert settings.demo_user_password not in raw

    async def test_the_request_key_is_honoured(self, client: AsyncClient) -> None:
        key = new_request_key()
        first = (await client.post("/api/auth/demo-session", json={"request_key": key})).json()
        second = (await client.post("/api/auth/demo-session", json={"request_key": key})).json()

        assert second["user"]["id"] == first["user"]["id"]
        assert second["reused"] is True

    async def test_a_malformed_request_key_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/auth/demo-session", json={"request_key": "has spaces and $"}
        )
        assert response.status_code == 422

    async def test_provisioning_is_rate_limited(self, client: AsyncClient) -> None:
        """An unmetered endpoint that writes 250 rows per call is a lever."""
        codes = [
            (await client.post("/api/auth/demo-session", json={})).status_code
            for _ in range(DEMO_SESSION_LIMIT.times + 1)
        ]
        assert codes[-1] == 429
        assert codes.count(200) == DEMO_SESSION_LIMIT.times

    async def test_the_throttled_response_says_when_to_retry(
        self, client: AsyncClient
    ) -> None:
        response = None
        for _ in range(DEMO_SESSION_LIMIT.times + 2):
            response = await client.post("/api/auth/demo-session", json={})
            if response.status_code == 429:
                break

        assert response is not None
        assert response.status_code == 429
        assert response.headers["retry-after"] == str(DEMO_SESSION_LIMIT.seconds)

    async def test_the_profile_reports_demo_expiry(self, client: AsyncClient) -> None:
        body = (await client.post("/api/auth/demo-session", json={})).json()
        headers = {"Authorization": f"Bearer {body['access_token']}"}

        profile = (await client.get("/api/settings/profile", headers=headers)).json()
        assert profile["is_ephemeral_demo"] is True
        assert profile["demo_expires_at"] is not None
        assert profile["demo_notice"]

    async def test_a_permanent_account_is_not_marked_ephemeral(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        profile = (await client.get("/api/settings/profile", headers=auth_headers)).json()
        assert profile["is_ephemeral_demo"] is False
        assert profile["demo_expires_at"] is None
        assert profile["demo_notice"] is None
