"""Resolving a verified email address for a Clerk subject, and provisioning on it.

The bug these exist to prevent already happened once in production, and it is
worth naming precisely: the first invited user signed in successfully, reached
the dashboard, and was refused an account because the code read `email` from
the session token. A Clerk session token has no such claim — its defaults are
azp, exp, fva, iat, iss, jti, nbf, sid, sub, v, pla, fea and sts. The earlier
tests passed because they minted their own tokens *with* an email claim, so the
fixture disagreed with reality and the suite could not see it.

Two rules follow, and everything here checks one of them:

  * the address comes from Clerk's Backend API, which is the only source that
    reports whether it was verified;
  * nothing short of one unambiguous verified address creates an account, and
    every other outcome fails closed while leaving the invitation unspent.
"""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest

from ledgerai.config import settings
from ledgerai.services import account_deletion
from ledgerai.services.clerk_admin import EmailOutcome, fetch_verified_email
from ledgerai.services.identity import (
    ProvisioningError,
    create_invitation,
    provision_profile,
)

SUBJECT = "user_2emaillookupsubject"
ADDRESS = "invited@example.com"
OTHER = "someone-else@example.com"
FAKE_SECRET = "sk_live_" + "q4" * 20


@pytest.fixture(autouse=True)
def clerk_configured(monkeypatch):
    monkeypatch.setattr(settings, "clerk_secret_key", FAKE_SECRET, raising=False)
    monkeypatch.setattr(settings, "clerk_api_base", "https://api.clerk.test/v1", raising=False)
    monkeypatch.setattr(settings, "clerk_http_timeout_seconds", 1.0, raising=False)
    monkeypatch.setattr(settings, "beta_invite_only", True, raising=False)


def user_payload(*addresses, primary_id: str | None = None) -> dict:
    """Clerk's user shape. `addresses` are (id, address, status) triples."""
    return {
        "id": SUBJECT,
        "primary_email_address_id": primary_id,
        "email_addresses": [
            {"id": i, "email_address": a, "verification": {"status": s}}
            for i, a, s in addresses
        ],
    }


def client_json(payload, status: int = 200) -> httpx.Client:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.recorded = seen  # type: ignore[attr-defined]
    return client


def client_status(*statuses: int) -> httpx.Client:
    seen: list[httpx.Request] = []
    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        code = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(code, json={"errors": [{"message": "nope"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.recorded = seen  # type: ignore[attr-defined]
    return client


def client_raising(exc: Exception) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestTheTokenCarriesNoAddress:
    def test_a_standard_session_token_has_no_email_claim(self) -> None:
        """The regression, stated as a fact about Clerk rather than our code.

        If this ever stops being true the lookup is merely redundant, not wrong
        — but the whole reason for the Backend API call is this claim's absence.
        """
        from ledgerai.security.clerk import ClerkIdentity

        default_claims = {
            "azp", "exp", "fva", "iat", "iss", "jti",
            "nbf", "sid", "sub", "v", "pla", "fea", "sts",
        }
        assert "email" not in default_claims
        # And our identity type tolerates its absence rather than assuming it.
        identity = ClerkIdentity(subject=SUBJECT, session_id="s", email=None, issued_at=None)
        assert identity.email is None


class TestSelectingTheAddress:
    def test_one_verified_address_resolves(self) -> None:
        client = client_json(user_payload(("e1", ADDRESS, "verified")))
        assert fetch_verified_email(SUBJECT, client=client) == (
            EmailOutcome.RESOLVED,
            ADDRESS,
        )

    def test_an_unverified_address_is_refused(self) -> None:
        """Anyone can type someone else's address into a sign-up form."""
        client = client_json(user_payload(("e1", ADDRESS, "unverified")))
        outcome, email = fetch_verified_email(SUBJECT, client=client)
        assert outcome is EmailOutcome.NO_VERIFIED_EMAIL
        assert email is None

    @pytest.mark.parametrize("status", ["unverified", "failed", "expired", "transferable", None])
    def test_only_the_exact_verified_status_counts(self, status) -> None:
        client = client_json(user_payload(("e1", ADDRESS, status)))
        outcome, email = fetch_verified_email(SUBJECT, client=client)
        assert outcome is EmailOutcome.NO_VERIFIED_EMAIL
        assert email is None

    def test_no_addresses_at_all_is_refused(self) -> None:
        outcome, email = fetch_verified_email(SUBJECT, client=client_json(user_payload()))
        assert outcome is EmailOutcome.NO_VERIFIED_EMAIL
        assert email is None

    def test_the_verified_primary_wins_when_several_are_verified(self) -> None:
        client = client_json(
            user_payload(
                ("e1", OTHER, "verified"),
                ("e2", ADDRESS, "verified"),
                primary_id="e2",
            )
        )
        assert fetch_verified_email(SUBJECT, client=client) == (
            EmailOutcome.RESOLVED,
            ADDRESS,
        )

    def test_several_verified_with_no_primary_is_ambiguous(self) -> None:
        client = client_json(
            user_payload(("e1", OTHER, "verified"), ("e2", ADDRESS, "verified"))
        )
        outcome, email = fetch_verified_email(SUBJECT, client=client)
        assert outcome is EmailOutcome.AMBIGUOUS
        assert email is None

    def test_several_verified_with_an_unverified_primary_is_ambiguous(self) -> None:
        """The primary is only a tiebreak among addresses already verified."""
        client = client_json(
            user_payload(
                ("e1", OTHER, "verified"),
                ("e2", ADDRESS, "verified"),
                ("e3", "primary@example.com", "unverified"),
                primary_id="e3",
            )
        )
        outcome, email = fetch_verified_email(SUBJECT, client=client)
        assert outcome is EmailOutcome.AMBIGUOUS
        assert email is None

    def test_an_unverified_primary_alongside_one_verified_still_resolves(self) -> None:
        client = client_json(
            user_payload(
                ("e1", ADDRESS, "verified"),
                ("e2", OTHER, "unverified"),
                primary_id="e2",
            )
        )
        assert fetch_verified_email(SUBJECT, client=client) == (
            EmailOutcome.RESOLVED,
            ADDRESS,
        )

    @pytest.mark.parametrize(
        "payload",
        [
            "not a dict",
            {"email_addresses": "not a list"},
            {},
            {"email_addresses": [None, 3, "x"]},
            {"email_addresses": [{"email_address": ADDRESS}]},
        ],
    )
    def test_malformed_payloads_never_yield_an_address(self, payload) -> None:
        outcome, email = fetch_verified_email(SUBJECT, client=client_json(payload))
        assert email is None
        assert outcome is not EmailOutcome.RESOLVED


class TestApiFailuresFailClosed:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, EmailOutcome.UNAUTHORIZED),
            (403, EmailOutcome.UNAUTHORIZED),
            (404, EmailOutcome.NOT_FOUND),
            (418, EmailOutcome.UNEXPECTED_STATUS),
        ],
    )
    def test_error_statuses(self, status, expected) -> None:
        outcome, email = fetch_verified_email(SUBJECT, client=client_status(status))
        assert outcome is expected
        assert email is None

    def test_a_429_is_retried_then_gives_up(self) -> None:
        client = client_status(429)
        outcome, email = fetch_verified_email(SUBJECT, client=client)
        assert outcome is EmailOutcome.TRANSIENT
        assert email is None
        assert len(client.recorded) == 3  # type: ignore[attr-defined]

    def test_a_5xx_can_recover_on_retry(self) -> None:
        seen: list[httpx.Request] = []
        queue = [503, 200]

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            code = queue.pop(0) if len(queue) > 1 else queue[0]
            if code == 200:
                return httpx.Response(200, json=user_payload(("e1", ADDRESS, "verified")))
            return httpx.Response(code, json={})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert fetch_verified_email(SUBJECT, client=client) == (
            EmailOutcome.RESOLVED,
            ADDRESS,
        )

    def test_a_timeout_fails_closed(self) -> None:
        outcome, email = fetch_verified_email(
            SUBJECT, client=client_raising(httpx.ReadTimeout("slow"))
        )
        assert outcome is EmailOutcome.TIMEOUT
        assert email is None

    def test_a_network_error_fails_closed(self) -> None:
        outcome, email = fetch_verified_email(
            SUBJECT, client=client_raising(httpx.ConnectError("down"))
        )
        assert outcome is EmailOutcome.NETWORK
        assert email is None

    def test_a_missing_secret_fails_closed(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "clerk_secret_key", "", raising=False)
        outcome, email = fetch_verified_email(
            SUBJECT, client=client_json(user_payload(("e1", ADDRESS, "verified")))
        )
        assert outcome is EmailOutcome.NOT_CONFIGURED
        assert email is None

    def test_it_reads_by_id_and_never_by_address(self) -> None:
        client = client_json(user_payload(("e1", ADDRESS, "verified")))
        fetch_verified_email(SUBJECT, client=client)
        request = client.recorded[0]  # type: ignore[attr-defined]
        assert request.method == "GET"
        assert str(request.url).endswith(f"/users/{SUBJECT}")
        assert "@" not in str(request.url)


class TestNothingSensitiveIsLogged:
    def test_no_address_secret_header_or_body_reaches_our_logs(self, caplog) -> None:
        caplog.set_level(logging.DEBUG)
        fetch_verified_email(SUBJECT, client=client_json(user_payload(("e1", ADDRESS, "verified"))))
        fetch_verified_email(SUBJECT, client=client_status(401))
        fetch_verified_email(SUBJECT, client=client_raising(httpx.ConnectError("x")))

        ours = "\n".join(
            r.getMessage() for r in caplog.records if r.name == "ledgerai.services.clerk_admin"
        )
        assert ours.strip(), "expected the module to log something"
        for forbidden in (FAKE_SECRET, "Bearer", ADDRESS, SUBJECT, "email_addresses"):
            assert forbidden not in ours


class TestProvisioningThroughTheResolver:
    """The end-to-end contract, against a real database."""

    def _invite(self, sync_db, address: str) -> None:
        create_invitation(sync_db, address, ttl_days=30, note="test")
        sync_db.commit()

    def test_a_verified_address_matching_an_invitation_creates_one_profile(
        self, sync_db
    ) -> None:
        subject = f"user_2ok{uuid.uuid4().hex[:8]}"
        address = f"ok-{uuid.uuid4().hex[:8]}@example.com"
        self._invite(sync_db, address)

        user = provision_profile(
            sync_db, clerk_user_id=subject, resolve_email=lambda: address
        )
        sync_db.commit()

        assert user.clerk_user_id == subject
        assert user.is_demo is False
        assert user.created_via == "invite"

    def test_the_resolver_is_not_called_for_a_returning_user(self, sync_db) -> None:
        """A profile that exists costs no call to Clerk at all."""
        subject = f"user_2ret{uuid.uuid4().hex[:8]}"
        address = f"ret-{uuid.uuid4().hex[:8]}@example.com"
        self._invite(sync_db, address)
        provision_profile(sync_db, clerk_user_id=subject, resolve_email=lambda: address)
        sync_db.commit()

        calls: list[int] = []

        def _resolver() -> str:
            calls.append(1)
            return address

        provision_profile(sync_db, clerk_user_id=subject, resolve_email=_resolver)
        sync_db.commit()
        assert calls == []

    def test_retrying_after_a_failed_lookup_succeeds_and_spends_nothing(
        self, sync_db
    ) -> None:
        """Exactly the production situation: first attempt refused, retry works."""
        subject = f"user_2retry{uuid.uuid4().hex[:8]}"
        address = f"retry-{uuid.uuid4().hex[:8]}@example.com"
        self._invite(sync_db, address)

        def _failing() -> str:
            raise ProvisioningError("Could not confirm your email address with Clerk.")

        with pytest.raises(ProvisioningError):
            provision_profile(sync_db, clerk_user_id=subject, resolve_email=_failing)
        sync_db.rollback()

        # The invitation was never spent, so the retry is a clean first attempt.
        user = provision_profile(
            sync_db, clerk_user_id=subject, resolve_email=lambda: address
        )
        sync_db.commit()
        assert user.clerk_user_id == subject

    def test_an_address_with_no_invitation_is_refused(self, sync_db) -> None:
        subject = f"user_2noinv{uuid.uuid4().hex[:8]}"
        with pytest.raises(ProvisioningError):
            provision_profile(
                sync_db,
                clerk_user_id=subject,
                resolve_email=lambda: f"uninvited-{uuid.uuid4().hex[:8]}@example.com",
            )
        sync_db.rollback()

    def test_a_mismatched_address_does_not_claim_someone_elses_invitation(
        self, sync_db
    ) -> None:
        invited = f"invited-{uuid.uuid4().hex[:8]}@example.com"
        self._invite(sync_db, invited)
        with pytest.raises(ProvisioningError):
            provision_profile(
                sync_db,
                clerk_user_id=f"user_2mis{uuid.uuid4().hex[:8]}",
                resolve_email=lambda: f"other-{uuid.uuid4().hex[:8]}@example.com",
            )
        sync_db.rollback()

    def test_a_revoked_invitation_is_refused(self, sync_db) -> None:
        from ledgerai.services.identity import revoke_invitation

        address = f"rev-{uuid.uuid4().hex[:8]}@example.com"
        self._invite(sync_db, address)
        revoke_invitation(sync_db, address)
        sync_db.commit()

        with pytest.raises(ProvisioningError):
            provision_profile(
                sync_db,
                clerk_user_id=f"user_2rev{uuid.uuid4().hex[:8]}",
                resolve_email=lambda: address,
            )
        sync_db.rollback()

    def test_an_expired_invitation_is_refused(self, sync_db) -> None:
        address = f"exp-{uuid.uuid4().hex[:8]}@example.com"
        create_invitation(sync_db, address, ttl_days=-1, note="already expired")
        sync_db.commit()

        with pytest.raises(ProvisioningError):
            provision_profile(
                sync_db,
                clerk_user_id=f"user_2exp{uuid.uuid4().hex[:8]}",
                resolve_email=lambda: address,
            )
        sync_db.rollback()

    def test_an_invitation_is_redeemed_only_once(self, sync_db) -> None:
        """A second subject cannot ride in on the same invitation."""
        address = f"once-{uuid.uuid4().hex[:8]}@example.com"
        self._invite(sync_db, address)

        provision_profile(
            sync_db,
            clerk_user_id=f"user_2first{uuid.uuid4().hex[:8]}",
            resolve_email=lambda: address,
        )
        sync_db.commit()

        with pytest.raises(ProvisioningError):
            provision_profile(
                sync_db,
                clerk_user_id=f"user_2second{uuid.uuid4().hex[:8]}",
                resolve_email=lambda: address,
            )
        sync_db.rollback()

    def test_a_tombstoned_subject_cannot_be_rebuilt(self, sync_db) -> None:
        """A token minted before deletion stays valid until it expires."""
        from ledgerai.services.identity import IdentityRevokedError

        subject = f"user_2tomb{uuid.uuid4().hex[:8]}"
        address = f"tomb-{uuid.uuid4().hex[:8]}@example.com"
        self._invite(sync_db, address)
        account_deletion.record_deletion_intent(sync_db, clerk_user_id=subject)
        sync_db.commit()

        with pytest.raises(IdentityRevokedError):
            provision_profile(
                sync_db, clerk_user_id=subject, resolve_email=lambda: address
            )
        sync_db.rollback()

    def test_a_subject_cannot_be_rebound_to_another_profile(self, sync_db) -> None:
        """The identity key never moves, even when the address changes."""
        subject = f"user_2bind{uuid.uuid4().hex[:8]}"
        first = f"first-{uuid.uuid4().hex[:8]}@example.com"
        second = f"second-{uuid.uuid4().hex[:8]}@example.com"
        self._invite(sync_db, first)
        self._invite(sync_db, second)

        original = provision_profile(
            sync_db, clerk_user_id=subject, resolve_email=lambda: first
        )
        sync_db.commit()
        original_id = original.id

        # Same subject, different verified address: the profile follows the
        # address, but it is the same profile and no second one appears.
        again = provision_profile(
            sync_db, clerk_user_id=subject, email=second
        )
        sync_db.commit()
        assert again.id == original_id
