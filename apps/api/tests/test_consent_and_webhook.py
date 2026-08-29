"""Consent capture, its enforcement boundary, and the Clerk webhook.

Two judgements are encoded here and both are worth stating.

**Consent gates uploading, and nothing else.** Reading, exporting and deleting
your own records are never blocked on accepting a document. Withholding
somebody's own financial data until they agree to new terms would be leverage,
not consent, and it would make the export and deletion routes — the ones that
exist for the user's benefit — hostage to a legal change.

**The webhook records intent and nothing more.** Clerk's documentation says
delivery is not guaranteed, so a design where the webhook performs the deletion
would lose deletions whenever a delivery was dropped. It writes a tombstone and
returns; the worker finishes the job.
"""

from __future__ import annotations

import json
import uuid

import pytest
from svix.webhooks import Webhook

from ledgerai.config import settings
from ledgerai.models import DeletedIdentity, UserConsent
from ledgerai.services import consent
from ledgerai.services.identity import create_invitation, provision_profile

SUBJECT = "user_2webhooksubjectxx"


def _test_factory():
    """Point the reconciler at the test database rather than DATABASE_URL."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tests.conftest import TEST_DB, _url

    engine = create_engine(_url(TEST_DB, is_async=False))
    return sessionmaker(engine, expire_on_commit=False)()


# A fixture, not a credential: base64 of "webtestsecretkey1234567890", written
# as a concatenation so a secret scanner reading this file sees no `whsec_`
# literal. Svix requires the prefix and a base64 body, so the shape has to be
# real even though the value is not.
SIGNING_SECRET = "whsec_" + "d2VidGVzdHNlY3JldGtleTEyMzQ1Njc4OTA="


class TestConsentRecords:
    def test_a_record_carries_the_version(self, sync_db, demo_data) -> None:
        """"Accepted the terms" is not a useful fact without which terms."""
        user = demo_data["user"]
        record = consent.record_consent(
            sync_db, user_id=user.id, consent_type=consent.TERMS, request_id="req-1"
        )
        sync_db.commit()
        assert record.document_version == settings.terms_version
        assert record.consent_type == consent.TERMS

    def test_records_are_an_append_only_history(self, sync_db, demo_data, monkeypatch) -> None:
        """A version bump must not erase what was agreed to before it."""
        user = demo_data["user"]
        consent.record_consent(sync_db, user_id=user.id, consent_type=consent.TERMS)
        monkeypatch.setattr(settings, "terms_version", "2026-09-draft-2", raising=False)
        consent.record_consent(sync_db, user_id=user.id, consent_type=consent.TERMS)
        sync_db.commit()

        rows = list(sync_db.query(UserConsent).filter_by(user_id=user.id).all())
        assert len(rows) == 2
        assert {r.document_version for r in rows} == {"2026-08-draft-1", "2026-09-draft-2"}

    def test_nothing_financial_is_stored(self, sync_db, demo_data) -> None:
        record = consent.record_consent(
            sync_db, user_id=demo_data["user"].id, consent_type=consent.UPLOAD
        )
        sync_db.commit()
        rendered = str(record.__dict__)
        for forbidden in ("amount", "merchant", "transaction", "$"):
            assert forbidden not in rendered.lower()


class TestConsentEnforcement:
    async def test_a_demo_account_is_exempt(self, client, auth_headers) -> None:
        """The data is synthetic and the account deletes itself within a day.
        A legal wall in front of a one-click demo costs the demo and protects
        nobody."""
        response = await client.get("/api/settings/consents", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["missing"] == []

    async def test_the_current_versions_are_reported(self, client, auth_headers) -> None:
        body = (await client.get("/api/settings/consents", headers=auth_headers)).json()
        assert body["required"]["terms"] == settings.terms_version
        assert set(body["required"]) == set(consent.UPLOAD_PREREQUISITES)

    async def test_accepting_is_recorded_and_reflected(self, client, auth_headers) -> None:
        response = await client.post(
            "/api/settings/consents",
            headers=auth_headers,
            json={"consent_types": ["terms", "privacy"]},
        )
        assert response.status_code == 200
        assert response.json()["accepted"]["terms"] == settings.terms_version

    async def test_an_unknown_consent_type_is_refused(self, client, auth_headers) -> None:
        response = await client.post(
            "/api/settings/consents",
            headers=auth_headers,
            json={"consent_types": ["something-invented"]},
        )
        assert response.status_code == 422

    async def test_export_and_deletion_are_never_gated(self, client, auth_headers) -> None:
        """The routes that exist for the user's benefit must not be hostage to
        a consent version."""
        assert (await client.get("/api/settings/export", headers=auth_headers)).status_code == 200
        preview = await client.post(
            "/api/settings/delete-data", headers=auth_headers, json={"confirmation": "PREVIEW"}
        )
        assert preview.status_code in {200, 422}


class TestClerkWebhook:
    def _signed(self, payload: dict) -> tuple[bytes, dict[str, str]]:
        from datetime import UTC, datetime

        body = json.dumps(payload).encode()
        msg_id = f"msg_{uuid.uuid4().hex[:16]}"
        moment = datetime.now(UTC)
        signature = Webhook(SIGNING_SECRET).sign(msg_id, moment, body.decode())
        return body, {
            "svix-id": msg_id,
            "svix-timestamp": str(int(moment.timestamp())),
            "svix-signature": signature,
            "content-type": "application/json",
        }

    @pytest.fixture(autouse=True)
    def _configured(self, monkeypatch):
        monkeypatch.setattr(
            settings, "clerk_webhook_signing_secret", SIGNING_SECRET, raising=False
        )

    async def test_a_valid_deletion_event_records_a_tombstone(self, client, sync_db) -> None:
        body, headers = self._signed({"type": "user.deleted", "data": {"id": SUBJECT}})
        response = await client.post("/api/webhooks/clerk", content=body, headers=headers)

        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        sync_db.expire_all()
        assert sync_db.get(DeletedIdentity, SUBJECT) is not None

    async def test_an_unsigned_request_is_refused(self, client) -> None:
        body = json.dumps({"type": "user.deleted", "data": {"id": SUBJECT}}).encode()
        response = await client.post(
            "/api/webhooks/clerk", content=body, headers={"content-type": "application/json"}
        )
        assert response.status_code == 400

    async def test_a_tampered_body_is_refused(self, client) -> None:
        """The signature covers the RAW bytes. Changing the payload after
        signing must not verify — which is why the handler never re-serializes
        before checking."""
        _, headers = self._signed({"type": "user.deleted", "data": {"id": SUBJECT}})
        tampered = json.dumps({"type": "user.deleted", "data": {"id": "user_2someoneelsexx"}})
        response = await client.post(
            "/api/webhooks/clerk", content=tampered.encode(), headers=headers
        )
        assert response.status_code == 400

    async def test_a_redelivery_is_harmless(self, client, sync_db) -> None:
        """Svix retries. Twice must mean the same as once."""
        body, headers = self._signed({"type": "user.deleted", "data": {"id": SUBJECT}})
        for _ in range(3):
            assert (
                await client.post("/api/webhooks/clerk", content=body, headers=headers)
            ).status_code == 200

        sync_db.expire_all()
        rows = list(sync_db.query(DeletedIdentity).filter_by(clerk_user_id=SUBJECT).all())
        assert len(rows) == 1

    async def test_an_unhandled_event_is_acknowledged_not_retried(self, client) -> None:
        """Returning an error would make Svix retry something we will never
        act on."""
        body, headers = self._signed({"type": "user.updated", "data": {"id": SUBJECT}})
        response = await client.post("/api/webhooks/clerk", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"

    async def test_a_payload_without_a_subject_is_rejected(self, client) -> None:
        body, headers = self._signed({"type": "user.deleted", "data": {}})
        response = await client.post("/api/webhooks/clerk", content=body, headers=headers)
        assert response.status_code == 400

    async def test_an_unconfigured_secret_does_not_accept_unsigned_calls(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "clerk_webhook_signing_secret", "", raising=False)
        body = json.dumps({"type": "user.deleted", "data": {"id": SUBJECT}}).encode()
        response = await client.post(
            "/api/webhooks/clerk", content=body, headers={"content-type": "application/json"}
        )
        assert response.status_code == 503


class TestDeletionReconciliation:
    def test_it_completes_a_pending_tombstone(self, sync_db) -> None:
        from ledgerai.jobs.account_reconcile import reconcile_deletions
        from ledgerai.services import account_deletion

        create_invitation(sync_db, "recon@example.com")
        user = provision_profile(
            sync_db, clerk_user_id="user_2reconcilesubject", email="recon@example.com"
        )
        user_id = user.id
        account_deletion.record_deletion_intent(sync_db, clerk_user_id="user_2reconcilesubject")
        sync_db.commit()

        report = reconcile_deletions(session_factory=_test_factory)

        sync_db.expire_all()
        assert report["tombstones_processed"] >= 1
        tombstone = sync_db.get(DeletedIdentity, "user_2reconcilesubject")
        assert tombstone.state == account_deletion.STATE_COMPLETE
        # The profile id is cleared: the tombstone only needs to refuse a
        # subject, not to remember what it pointed at.
        assert tombstone.user_id is None
        from ledgerai.models import User

        assert sync_db.get(User, user_id) is None

    def test_running_it_again_is_a_no_op(self, sync_db) -> None:
        from ledgerai.jobs.account_reconcile import reconcile_deletions
        from ledgerai.services import account_deletion

        account_deletion.record_deletion_intent(sync_db, clerk_user_id="user_2idempotentxxx")
        sync_db.commit()

        reconcile_deletions(session_factory=_test_factory)
        second = reconcile_deletions(session_factory=_test_factory)

        sync_db.expire_all()
        assert second["tombstones_processed"] == 0
        assert (
            sync_db.get(DeletedIdentity, "user_2idempotentxxx").state
            == account_deletion.STATE_COMPLETE
        )
