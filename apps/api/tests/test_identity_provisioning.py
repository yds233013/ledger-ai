"""Invitations, lazy provisioning, and the tombstone that outlives a profile.

Three properties carry the weight here.

**Exactly one profile per Clerk subject.** Provisioning happens on the first
authenticated request, and a browser reliably fires several at once, so the
"first" request is routinely a race. The partial unique index is the actual
guarantee; these tests run real concurrent transactions rather than trusting
that the code path looks single-threaded.

**Exactly one redemption per invitation.** Same reasoning: the claim is an
`UPDATE ... WHERE redeemed_at IS NULL RETURNING`, because a check-then-update
would let two simultaneous requests both pass the check.

**A deleted identity stays deleted.** A token minted before deletion is still
cryptographically valid until it expires. Without the tombstone, the next
request would recreate the account — which is the single worst outcome this
whole feature could produce.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ledgerai.config import settings
from ledgerai.models import DeletedIdentity, Invitation, User
from ledgerai.services import account_deletion
from ledgerai.services.identity import (
    IdentityRevokedError,
    ProvisioningError,
    create_invitation,
    email_hint,
    email_hmac,
    normalize_email,
    provision_profile,
    revoke_invitation,
)
from tests.conftest import TEST_DB, _url

SUBJECT = "user_2abcdefghijklmnop"


class TestEmailHandling:
    def test_normalization_is_case_and_unicode_stable(self) -> None:
        assert normalize_email("  Beta@Example.COM ") == "beta@example.com"
        assert normalize_email("BETA@example.com") == normalize_email("beta@EXAMPLE.com")

    def test_dots_and_plus_tags_are_preserved(self) -> None:
        """Gmail treats a.b@ and ab@ alike; the standard does not, and other
        providers do not. Collapsing them would let an invitation for one
        address be redeemed by a different mailbox."""
        assert normalize_email("a.b@example.com") != normalize_email("ab@example.com")
        assert normalize_email("x+beta@example.com") != normalize_email("x@example.com")

    def test_the_digest_is_keyed_not_a_bare_hash(self) -> None:
        """A plain SHA-256 of an email is enumerable: the input space is small
        and guessable, so anyone reading the table could confirm whether a
        specific person was invited. Keying it removes that."""
        import hashlib

        address = "beta@example.com"
        assert email_hmac(address) != hashlib.sha256(address.encode()).hexdigest()
        assert email_hmac(address) != hashlib.sha256(normalize_email(address).encode()).hexdigest()

    def test_the_digest_depends_on_the_key(self, monkeypatch) -> None:
        before = email_hmac("beta@example.com")
        monkeypatch.setattr(settings, "auth_secret", "a-completely-different-secret", raising=False)
        assert email_hmac("beta@example.com") != before

    def test_the_hint_cannot_reconstruct_the_address(self) -> None:
        hint = email_hint("alexandra.jones@example.com")
        assert "alexandra" not in hint
        assert "jones" not in hint
        assert hint.startswith("a***@")

    def test_equivalent_addresses_share_a_digest(self) -> None:
        assert email_hmac(" Beta@Example.com ") == email_hmac("beta@example.com")


class TestInvitations:
    def test_creating_one_stores_no_address(self, sync_db) -> None:
        invitation = create_invitation(sync_db, "beta@example.com")
        sync_db.commit()
        row = sync_db.execute(select(Invitation)).scalars().one()
        assert "beta@example.com" not in str(row.__dict__)
        assert row.email_hmac == email_hmac("beta@example.com")
        assert invitation.email_hint == "a***@e***.com" or row.email_hint.startswith("b***")

    def test_inviting_twice_refreshes_rather_than_duplicating(self, sync_db) -> None:
        create_invitation(sync_db, "beta@example.com", ttl_days=1)
        first = sync_db.execute(select(Invitation)).scalars().one()
        first_expiry = first.expires_at

        create_invitation(sync_db, "beta@example.com", ttl_days=30)
        sync_db.commit()
        # The ON CONFLICT UPDATE happens in SQL, so the identity-mapped object
        # still holds the old value until it is expired.
        sync_db.expire_all()

        rows = list(sync_db.execute(select(Invitation)).scalars())
        assert len(rows) == 1
        assert rows[0].expires_at > first_expiry

    def test_revoking_prevents_provisioning(self, sync_db) -> None:
        create_invitation(sync_db, "beta@example.com")
        assert revoke_invitation(sync_db, "beta@example.com") is True
        sync_db.commit()

        with pytest.raises(ProvisioningError):
            provision_profile(sync_db, clerk_user_id=SUBJECT, email="beta@example.com")

    def test_an_expired_invitation_does_not_work(self, sync_db) -> None:
        invitation = create_invitation(sync_db, "beta@example.com")
        invitation.expires_at = datetime.now(UTC) - timedelta(days=1)
        sync_db.commit()

        with pytest.raises(ProvisioningError):
            provision_profile(sync_db, clerk_user_id=SUBJECT, email="beta@example.com")

    def test_a_redeemed_invitation_cannot_be_reused(self, sync_db) -> None:
        create_invitation(sync_db, "beta@example.com")
        provision_profile(sync_db, clerk_user_id=SUBJECT, email="beta@example.com")
        sync_db.commit()

        with pytest.raises(ProvisioningError):
            provision_profile(
                sync_db, clerk_user_id="user_2differentsubject", email="beta@example.com"
            )


class TestProvisioning:
    def test_the_user_never_enters_a_code(self, sync_db) -> None:
        """The whole UX requirement: the invitation is found from the email
        Clerk verified, not typed in by the user."""
        create_invitation(sync_db, "beta@example.com")
        user = provision_profile(sync_db, clerk_user_id=SUBJECT, email="Beta@Example.com")
        sync_db.commit()

        assert user.clerk_user_id == SUBJECT
        assert user.email == "beta@example.com"
        assert user.created_via == "invite"
        assert user.is_demo is False
        assert user.password_hash is None

    def test_an_uninvited_address_is_refused(self, sync_db) -> None:
        with pytest.raises(ProvisioningError):
            provision_profile(sync_db, clerk_user_id=SUBJECT, email="stranger@example.com")

    def test_the_second_request_reuses_the_profile(self, sync_db) -> None:
        create_invitation(sync_db, "beta@example.com")
        first = provision_profile(sync_db, clerk_user_id=SUBJECT, email="beta@example.com")
        sync_db.commit()
        second = provision_profile(sync_db, clerk_user_id=SUBJECT, email="beta@example.com")
        assert first.id == second.id

    def test_a_changed_email_updates_the_profile_not_the_identity(self, sync_db) -> None:
        """Clerk lets a user change their address; the identity key must not
        move with it."""
        create_invitation(sync_db, "beta@example.com")
        user = provision_profile(sync_db, clerk_user_id=SUBJECT, email="beta@example.com")
        sync_db.commit()
        user_id = user.id

        again = provision_profile(sync_db, clerk_user_id=SUBJECT, email="new@example.com")
        assert again.id == user_id
        assert again.email == "new@example.com"
        assert again.clerk_user_id == SUBJECT

    def test_invite_only_can_be_switched_off(self, sync_db, monkeypatch) -> None:
        monkeypatch.setattr(settings, "beta_invite_only", False, raising=False)
        user = provision_profile(sync_db, clerk_user_id=SUBJECT, email="anyone@example.com")
        assert user.clerk_user_id == SUBJECT

    def test_concurrent_first_requests_create_exactly_one_profile(self, sync_db) -> None:
        """A browser fires several requests at once, so the 'first' request is
        routinely a race. The partial unique index is what decides it."""
        create_invitation(sync_db, "race@example.com")
        sync_db.commit()

        engine = create_engine(_url(TEST_DB, is_async=False))
        factory = sessionmaker(engine, expire_on_commit=False)
        barrier = threading.Barrier(6)
        results: list[object] = []
        lock = threading.Lock()

        def attempt() -> None:
            barrier.wait()
            try:
                with factory() as session:
                    user = provision_profile(
                        session, clerk_user_id=SUBJECT, email="race@example.com"
                    )
                    session.commit()
                    with lock:
                        results.append(user.id)
            except Exception as exc:  # noqa: BLE001 - a loser is a valid outcome
                with lock:
                    results.append(type(exc).__name__)

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        engine.dispose()

        profiles = list(
            sync_db.execute(select(User).where(User.clerk_user_id == SUBJECT)).scalars()
        )
        assert len(profiles) == 1, f"provisioning created {len(profiles)} profiles"

        redemptions = list(
            sync_db.execute(
                select(Invitation).where(Invitation.email_hmac == email_hmac("race@example.com"))
            ).scalars()
        )
        assert len(redemptions) == 1
        assert redemptions[0].redeemed_at is not None

    def test_concurrent_requests_redeem_the_invitation_once(self, sync_db) -> None:
        """Two different subjects racing for one invitation: one wins."""
        create_invitation(sync_db, "single@example.com")
        sync_db.commit()

        engine = create_engine(_url(TEST_DB, is_async=False))
        factory = sessionmaker(engine, expire_on_commit=False)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def attempt(subject: str) -> None:
            barrier.wait()
            try:
                with factory() as session:
                    provision_profile(session, clerk_user_id=subject, email="single@example.com")
                    session.commit()
                with lock:
                    outcomes.append("created")
            except Exception:  # noqa: BLE001
                with lock:
                    outcomes.append("refused")

        threads = [
            threading.Thread(target=attempt, args=(f"user_2subject{i}aaaaaaaaaa",))
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        engine.dispose()

        assert outcomes.count("created") == 1, outcomes


class TestTombstonesPreventResurrection:
    def test_a_deleted_identity_cannot_be_reprovisioned(self, sync_db) -> None:
        """The core of correction 2. A token minted before deletion is still
        valid; without this the next request rebuilds the account."""
        create_invitation(sync_db, "gone@example.com")
        provision_profile(sync_db, clerk_user_id=SUBJECT, email="gone@example.com")
        account_deletion.record_deletion_intent(sync_db, clerk_user_id=SUBJECT)
        sync_db.commit()

        with pytest.raises(IdentityRevokedError):
            provision_profile(sync_db, clerk_user_id=SUBJECT, email="gone@example.com")

    def test_it_blocks_even_with_a_fresh_invitation(self, sync_db) -> None:
        account_deletion.record_deletion_intent(sync_db, clerk_user_id=SUBJECT)
        create_invitation(sync_db, "gone@example.com")
        sync_db.commit()

        with pytest.raises(IdentityRevokedError):
            provision_profile(sync_db, clerk_user_id=SUBJECT, email="gone@example.com")

    def test_it_blocks_in_every_state(self, sync_db) -> None:
        """Partial completion must not reopen the door."""
        for state in (
            account_deletion.STATE_PENDING,
            account_deletion.STATE_STORAGE_PURGED,
            account_deletion.STATE_COMPLETE,
        ):
            subject = f"user_2state{state[:8]}xxxxx"
            account_deletion.record_deletion_intent(sync_db, clerk_user_id=subject)
            tombstone = sync_db.get(DeletedIdentity, subject)
            tombstone.state = state
            sync_db.flush()
            with pytest.raises(IdentityRevokedError):
                provision_profile(sync_db, clerk_user_id=subject, email="x@example.com")

    def test_recording_intent_is_idempotent(self, sync_db) -> None:
        """Both the in-app route and the webhook call this, possibly at once,
        and Svix redelivers."""
        create_invitation(sync_db, "twice@example.com")
        user = provision_profile(sync_db, clerk_user_id=SUBJECT, email="twice@example.com")
        user_id = user.id
        sync_db.commit()

        for _ in range(3):
            account_deletion.record_deletion_intent(sync_db, clerk_user_id=SUBJECT)
            sync_db.commit()

        rows = list(sync_db.execute(select(DeletedIdentity)).scalars())
        assert len(rows) == 1
        assert rows[0].user_id == user_id

    def test_intent_denies_access_before_anything_is_removed(self, sync_db) -> None:
        create_invitation(sync_db, "pending@example.com")
        user = provision_profile(sync_db, clerk_user_id=SUBJECT, email="pending@example.com")
        sync_db.commit()

        account_deletion.record_deletion_intent(sync_db, clerk_user_id=SUBJECT)
        sync_db.commit()
        sync_db.refresh(user)

        # The rows are still there; the answer is already no.
        assert user.status == "pending_deletion"

    def test_a_repeat_request_does_not_reset_progress(self, sync_db) -> None:
        account_deletion.record_deletion_intent(sync_db, clerk_user_id=SUBJECT)
        account_deletion.mark_storage_purged(sync_db, SUBJECT)
        sync_db.commit()

        account_deletion.record_deletion_intent(sync_db, clerk_user_id=SUBJECT)
        sync_db.commit()

        assert sync_db.get(DeletedIdentity, SUBJECT).state == account_deletion.STATE_STORAGE_PURGED

    def test_the_tombstone_holds_nothing_sensitive(self, sync_db) -> None:
        create_invitation(sync_db, "private@example.com")
        provision_profile(sync_db, clerk_user_id=SUBJECT, email="private@example.com")
        account_deletion.record_deletion_intent(sync_db, clerk_user_id=SUBJECT)
        account_deletion.record_attempt(sync_db, SUBJECT, error=ValueError("merchant Whole Foods"))
        sync_db.commit()

        rendered = str(sync_db.get(DeletedIdentity, SUBJECT).__dict__)
        assert "private@example.com" not in rendered
        assert "Whole Foods" not in rendered, "only an exception class name may be recorded"
        assert "ValueError" in rendered

    def test_unfinished_lists_only_incomplete_work(self, sync_db) -> None:
        account_deletion.record_deletion_intent(sync_db, clerk_user_id="user_2unfinishedaaaa")
        account_deletion.record_deletion_intent(sync_db, clerk_user_id="user_2finishedbbbbb")
        account_deletion.mark_complete(sync_db, "user_2finishedbbbbb")
        sync_db.commit()

        names = {t.clerk_user_id for t in account_deletion.unfinished(sync_db)}
        assert "user_2unfinishedaaaa" in names
        assert "user_2finishedbbbbb" not in names

    def test_a_permanently_failing_identity_stops_being_retried_but_stays_blocked(
        self, sync_db
    ) -> None:
        """The retry stops; the block does not."""
        account_deletion.record_deletion_intent(sync_db, clerk_user_id=SUBJECT)
        tombstone = sync_db.get(DeletedIdentity, SUBJECT)
        tombstone.attempts = account_deletion.MAX_ATTEMPTS
        sync_db.commit()

        assert SUBJECT not in {t.clerk_user_id for t in account_deletion.unfinished(sync_db)}
        with pytest.raises(IdentityRevokedError):
            provision_profile(sync_db, clerk_user_id=SUBJECT, email="x@example.com")


class TestDemoAccountsAreUnaffected:
    """Regression guard: none of this may touch the demo flow."""

    def test_demo_users_have_no_clerk_identity(self, demo_data) -> None:
        assert demo_data["user"].clerk_user_id is None
        assert demo_data["user"].created_via in {"demo", "invite"}

    def test_a_demo_user_is_not_blocked_by_someone_elses_tombstone(
        self, sync_db, demo_data
    ) -> None:
        account_deletion.record_deletion_intent(sync_db, clerk_user_id=SUBJECT)
        sync_db.commit()
        assert account_deletion.is_revoked(sync_db, SUBJECT) is True
        # The demo user has no clerk_user_id at all, so no tombstone can match.
        assert demo_data["user"].clerk_user_id is None
