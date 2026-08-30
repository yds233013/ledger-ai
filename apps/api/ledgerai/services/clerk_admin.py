"""Revoking a Clerk identity, over Clerk's Backend API.

This is the one place in the codebase that holds `CLERK_SECRET_KEY`, and the
rules it follows are worth stating because a mistake here leaks a credential
that can delete every user in the instance:

  * The key is read from settings at call time and placed in an Authorization
    header. It is never logged, never interpolated into a message, never
    attached to an exception, and never returned.
  * Response bodies are never logged either. Clerk echoes user attributes —
    including the email address — in both success and error payloads, and this
    application has no reason to write those anywhere.
  * Errors are reported as a small closed set of outcome strings. Nothing
    derived from the wire is propagated.

**Identity, not address.** Deletion is always by `clerk_user_id`. Clerk's own
`sub` claim is the only stable handle; an email address can be changed by the
user, can be reassigned, and is exactly the wrong key for an irreversible
operation.

**404 is success.** The desired end state is "this identity does not exist".
An identity that is already gone has reached that state, so a 404 completes the
deletion rather than failing it. That is what makes a delayed or duplicated
`user.deleted` webhook harmless: the second delivery finds nothing and reports
done.

**Failure keeps the tombstone.** Every non-success returns False rather than
raising. The caller leaves the identity blocked locally and the worker sweep
retries later. Nothing here decides that a deletion is finished.
"""

from __future__ import annotations

import logging
from enum import StrEnum

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

# Transient conditions worth one immediate re-attempt inside a single sweep
# tick. Anything else is left to the sweep, which retries on a slower cadence
# and counts attempts durably.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS_PER_CALL = 3


class RevocationOutcome(StrEnum):
    """Why a revocation attempt ended. Carries no provider text."""

    DELETED = "deleted"
    ALREADY_ABSENT = "already_absent"
    NOT_CONFIGURED = "not_configured"
    UNAUTHORIZED = "unauthorized"
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    NETWORK = "network"
    UNEXPECTED_STATUS = "unexpected_status"

    @property
    def succeeded(self) -> bool:
        return self in (RevocationOutcome.DELETED, RevocationOutcome.ALREADY_ABSENT)


def revoke_identity(
    clerk_user_id: str,
    *,
    client: httpx.Client | None = None,
) -> RevocationOutcome:
    """Delete one Clerk user by id. Never raises, never logs the secret.

    The client is injectable so tests can drive every branch — success, 404,
    401, timeout, transient-then-success — without a network or a real key.
    """
    if not clerk_user_id:
        return RevocationOutcome.NOT_CONFIGURED

    secret = settings.clerk_secret_key
    if not secret:
        # Enabled but unconfigured is a deployment error, not a completed
        # deletion. Reported as a failure so the tombstone survives.
        logger.warning("clerk_admin.secret_key_missing")
        return RevocationOutcome.NOT_CONFIGURED

    url = f"{settings.clerk_api_base.rstrip('/')}/users/{clerk_user_id}"
    headers = {
        "Authorization": f"Bearer {secret}",
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(settings.clerk_http_timeout_seconds)

    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        outcome = RevocationOutcome.TRANSIENT
        for attempt in range(1, _MAX_ATTEMPTS_PER_CALL + 1):
            try:
                response = http.request("DELETE", url, headers=headers, timeout=timeout)
            except httpx.TimeoutException:
                outcome = RevocationOutcome.TIMEOUT
            except httpx.HTTPError:
                # Connection reset, DNS failure, TLS problem. The exception text
                # can contain the URL but never the header, and we log neither.
                outcome = RevocationOutcome.NETWORK
            else:
                outcome = _classify(response.status_code)
                if outcome is not RevocationOutcome.TRANSIENT:
                    break

            if attempt < _MAX_ATTEMPTS_PER_CALL and outcome in (
                RevocationOutcome.TRANSIENT,
                RevocationOutcome.TIMEOUT,
                RevocationOutcome.NETWORK,
            ):
                continue
            break

        # Our record carries the outcome and nothing else: no secret, no
        # Authorization header, no response body, no email address. (httpx's
        # own INFO log line does include the request URL, and therefore the
        # clerk_user_id — the same identifier already stored in the
        # tombstone. It is not a secret and not financial data.)
        logger.info("clerk_admin.revoke outcome=%s", outcome.value)
        return outcome
    finally:
        if owns_client:
            http.close()


def _classify(status_code: int) -> RevocationOutcome:
    if status_code in (200, 202, 204):
        return RevocationOutcome.DELETED
    if status_code == 404:
        # Already gone is the end state we wanted. Idempotent by construction.
        return RevocationOutcome.ALREADY_ABSENT
    if status_code in (401, 403):
        # A bad or revoked key. Retrying will not help, but the tombstone must
        # still survive, so this is a failure rather than an exception.
        return RevocationOutcome.UNAUTHORIZED
    if status_code in _RETRY_STATUSES:
        return RevocationOutcome.TRANSIENT
    return RevocationOutcome.UNEXPECTED_STATUS
