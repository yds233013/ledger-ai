"""Revoking a Clerk identity, and the deletion lifecycle around it.

Every branch is driven through an injected transport, so no test needs a
network, a real key, or a real user. The properties under test are the ones
that decide whether somebody's account actually goes away:

  * success and "already gone" both finish the deletion;
  * every failure leaves the tombstone unfinished so the sweep retries;
  * the identity stays locally blocked throughout, whatever Clerk says;
  * a duplicated `user.deleted` webhook changes nothing;
  * the secret never appears in a log record.
"""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest

from ledgerai.config import settings
from ledgerai.jobs.account_reconcile import reconcile_deletions
from ledgerai.services import account_deletion
from ledgerai.services.clerk_admin import RevocationOutcome, revoke_identity

SUBJECT = "user_2revocationsubject"
# Not a real credential: a shape-correct fake so the "never logged" assertions
# have something distinctive to search for.
FAKE_SECRET = "sk_live_" + "z9" * 20


@pytest.fixture(autouse=True)
def clerk_secret(monkeypatch):
    monkeypatch.setattr(settings, "clerk_secret_key", FAKE_SECRET, raising=False)
    monkeypatch.setattr(settings, "clerk_api_base", "https://api.clerk.test/v1", raising=False)
    monkeypatch.setattr(settings, "clerk_http_timeout_seconds", 1.0, raising=False)


def client_returning(*statuses: int) -> httpx.Client:
    """A client that yields the given statuses in order, repeating the last."""
    seen: list[httpx.Request] = []
    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        code = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(code, json={"id": SUBJECT, "email": "leak@example.com"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.recorded = seen  # type: ignore[attr-defined]
    return client


def client_raising(exc: Exception) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestOutcomes:
    def test_a_deleted_identity_succeeds(self) -> None:
        assert revoke_identity(SUBJECT, client=client_returning(200)).succeeded

    @pytest.mark.parametrize("code", [200, 202, 204])
    def test_every_success_status_counts(self, code) -> None:
        assert revoke_identity(SUBJECT, client=client_returning(code)).succeeded

    def test_404_is_success_because_already_gone_is_the_goal(self) -> None:
        outcome = revoke_identity(SUBJECT, client=client_returning(404))
        assert outcome is RevocationOutcome.ALREADY_ABSENT
        assert outcome.succeeded

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (401, RevocationOutcome.UNAUTHORIZED),
            (403, RevocationOutcome.UNAUTHORIZED),
            (418, RevocationOutcome.UNEXPECTED_STATUS),
        ],
    )
    def test_failures_do_not_succeed(self, code, expected) -> None:
        outcome = revoke_identity(SUBJECT, client=client_returning(code))
        assert outcome is expected
        assert not outcome.succeeded

    def test_a_timeout_is_a_failure_not_an_exception(self) -> None:
        outcome = revoke_identity(SUBJECT, client=client_raising(httpx.ReadTimeout("slow")))
        assert outcome is RevocationOutcome.TIMEOUT
        assert not outcome.succeeded

    def test_a_network_error_is_a_failure_not_an_exception(self) -> None:
        outcome = revoke_identity(SUBJECT, client=client_raising(httpx.ConnectError("down")))
        assert outcome is RevocationOutcome.NETWORK
        assert not outcome.succeeded

    def test_missing_secret_is_a_failure_not_a_silent_success(self, monkeypatch) -> None:
        """Enabled-but-unconfigured must not read as 'deletion complete'."""
        monkeypatch.setattr(settings, "clerk_secret_key", "", raising=False)
        outcome = revoke_identity(SUBJECT, client=client_returning(200))
        assert outcome is RevocationOutcome.NOT_CONFIGURED
        assert not outcome.succeeded


class TestRetries:
    def test_a_transient_error_is_retried_and_can_succeed(self) -> None:
        client = client_returning(503, 503, 204)
        assert revoke_identity(SUBJECT, client=client).succeeded
        assert len(client.recorded) == 3  # type: ignore[attr-defined]

    def test_retries_are_bounded(self) -> None:
        client = client_returning(503)
        outcome = revoke_identity(SUBJECT, client=client)
        assert outcome is RevocationOutcome.TRANSIENT
        assert len(client.recorded) == 3  # type: ignore[attr-defined]

    def test_a_permanent_failure_is_not_retried(self) -> None:
        """401 will not fix itself; hammering Clerk with a bad key is pointless."""
        client = client_returning(401)
        revoke_identity(SUBJECT, client=client)
        assert len(client.recorded) == 1  # type: ignore[attr-defined]


class TestTheRequestItself:
    def test_it_deletes_by_id_and_never_by_email(self) -> None:
        client = client_returning(204)
        revoke_identity(SUBJECT, client=client)
        request = client.recorded[0]  # type: ignore[attr-defined]
        assert request.method == "DELETE"
        assert str(request.url).endswith(f"/users/{SUBJECT}")
        assert "@" not in str(request.url)

    def test_the_secret_is_sent_as_a_bearer_token(self) -> None:
        client = client_returning(204)
        revoke_identity(SUBJECT, client=client)
        assert client.recorded[0].headers["authorization"] == f"Bearer {FAKE_SECRET}"  # type: ignore[attr-defined]


class TestNothingSensitiveIsLogged:
    def test_no_secret_header_email_or_body_reaches_the_logs(self, caplog) -> None:
        caplog.set_level(logging.DEBUG)
        for client in (
            client_returning(204),
            client_returning(404),
            client_returning(401),
            client_returning(503),
        ):
            revoke_identity(SUBJECT, client=client)
        revoke_identity(SUBJECT, client=client_raising(httpx.ConnectError("down")))

        written = "\n".join(r.getMessage() for r in caplog.records)
        # The requirement: never the secret, the header, the address, or the body.
        assert FAKE_SECRET not in written
        assert "Bearer" not in written
        assert "authorization" not in written.lower()
        assert "leak@example.com" not in written
        assert '"id"' not in written and "{" not in written

        # And our own records carry the outcome alone — not even the subject.
        ours = "\n".join(
            r.getMessage() for r in caplog.records if r.name == "ledgerai.services.clerk_admin"
        )
        assert SUBJECT not in ours
        assert ours.strip(), "expected the module to log something"


class TestTheDeletionLifecycle:
    """The reconciler's contract, with revocation wired in."""

    def _tombstone(self, sync_db, clerk_user_id: str):
        account_deletion.record_deletion_intent(sync_db, clerk_user_id=clerk_user_id)
        sync_db.commit()

    @staticmethod
    def _row(sync_db, clerk_user_id: str):
        from ledgerai.models import DeletedIdentity

        return sync_db.get(DeletedIdentity, clerk_user_id)

    def test_a_failed_revocation_leaves_the_tombstone_unfinished(
        self, sync_db, sync_factory, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "clerk_enabled", True, raising=False)
        monkeypatch.setattr(
            "ledgerai.jobs.account_reconcile.revoke_identity",
            lambda _id: RevocationOutcome.TIMEOUT,
        )
        cid = f"user_2fail{uuid.uuid4().hex[:8]}"
        self._tombstone(sync_db, cid)

        reconcile_deletions(session_factory=sync_factory)

        sync_db.expire_all()
        row = self._row(sync_db, cid)
        assert row is not None
        assert row.state != account_deletion.STATE_COMPLETE
        assert account_deletion.is_revoked(sync_db, cid) is True

    def test_a_successful_revocation_completes_it(
        self, sync_db, sync_factory, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "clerk_enabled", True, raising=False)
        monkeypatch.setattr(
            "ledgerai.jobs.account_reconcile.revoke_identity",
            lambda _id: RevocationOutcome.DELETED,
        )
        cid = f"user_2ok{uuid.uuid4().hex[:8]}"
        self._tombstone(sync_db, cid)

        reconcile_deletions(session_factory=sync_factory)

        sync_db.expire_all()
        row = self._row(sync_db, cid)
        assert row is not None
        assert row.state == account_deletion.STATE_COMPLETE
        # Still blocked: completion is not un-deletion.
        assert account_deletion.is_revoked(sync_db, cid) is True

    def test_an_already_absent_identity_completes_it(
        self, sync_db, sync_factory, monkeypatch
    ) -> None:
        """The duplicate-webhook case, end to end."""
        monkeypatch.setattr(settings, "clerk_enabled", True, raising=False)
        monkeypatch.setattr(
            "ledgerai.jobs.account_reconcile.revoke_identity",
            lambda _id: RevocationOutcome.ALREADY_ABSENT,
        )
        cid = f"user_2dup{uuid.uuid4().hex[:8]}"
        self._tombstone(sync_db, cid)
        # Deliver the same event again before the sweep runs.
        self._tombstone(sync_db, cid)

        reconcile_deletions(session_factory=sync_factory)

        sync_db.expire_all()
        row = self._row(sync_db, cid)
        assert row is not None
        assert row.state == account_deletion.STATE_COMPLETE

    def test_a_repeated_sweep_over_a_completed_tombstone_is_harmless(
        self, sync_db, sync_factory, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "clerk_enabled", True, raising=False)
        calls: list[str] = []

        def _revoke(identifier: str) -> RevocationOutcome:
            calls.append(identifier)
            return RevocationOutcome.DELETED

        monkeypatch.setattr("ledgerai.jobs.account_reconcile.revoke_identity", _revoke)
        cid = f"user_2rep{uuid.uuid4().hex[:8]}"
        self._tombstone(sync_db, cid)

        reconcile_deletions(session_factory=sync_factory)
        first = len(calls)
        reconcile_deletions(session_factory=sync_factory)

        # A finished tombstone is not picked up again.
        assert len(calls) == first
        sync_db.expire_all()
        assert account_deletion.is_revoked(sync_db, cid) is True

    def test_clerk_disabled_needs_no_provider_call(
        self, sync_db, sync_factory, monkeypatch
    ) -> None:
        """With Clerk off there is no identity to revoke, so the local purge is
        the whole deletion and it completes without touching the network."""
        monkeypatch.setattr(settings, "clerk_enabled", False, raising=False)

        def _explode(_id: str) -> RevocationOutcome:  # pragma: no cover
            raise AssertionError("must not call Clerk while disabled")

        monkeypatch.setattr("ledgerai.jobs.account_reconcile.revoke_identity", _explode)
        cid = f"user_2off{uuid.uuid4().hex[:8]}"
        self._tombstone(sync_db, cid)

        reconcile_deletions(session_factory=sync_factory)

        sync_db.expire_all()
        row = self._row(sync_db, cid)
        assert row is not None
        assert row.state == account_deletion.STATE_COMPLETE
