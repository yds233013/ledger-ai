"""Talking to Clerk's Backend API: revoking an identity, and reading its
verified email address.

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


# ---------------------------------------------------------------------------
# Reading a verified email address
# ---------------------------------------------------------------------------
#
# A Clerk session token does NOT carry the user's email. Its default claims are
# azp, exp, fva, iat, iss, jti, nbf, sid, sub, v, pla, fea and sts — nothing
# else. An address can be added with a custom JWT template, but a claim minted
# from a template is still only as trustworthy as the template, and it says
# nothing about whether the address was ever *verified*. The Backend API is the
# only source that reports verification status, so that is what this asks.


class EmailOutcome(StrEnum):
    """Why an email lookup ended. Carries no address and no provider text."""

    RESOLVED = "resolved"
    NO_VERIFIED_EMAIL = "no_verified_email"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    NOT_CONFIGURED = "not_configured"
    UNAUTHORIZED = "unauthorized"
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    NETWORK = "network"
    MALFORMED = "malformed"
    UNEXPECTED_STATUS = "unexpected_status"


def fetch_verified_email(
    clerk_user_id: str,
    *,
    client: httpx.Client | None = None,
) -> tuple[EmailOutcome, str | None]:
    """Return the one verified address Clerk holds for this subject.

    Never raises. Every failure returns an outcome and `None`, so a caller that
    forgets to check the outcome still cannot obtain an address it should not
    have — there is no path that yields a string without RESOLVED.

    Selection is deliberately narrow:

      * only addresses Clerk reports as `verified` are eligible; an unverified
        address proves nothing about who controls it, and treating one as
        identity would let anybody claim an invitation by typing someone else's
        address into a sign-up form;
      * exactly one eligible address resolves;
      * several eligible addresses resolve only to the one Clerk marks primary,
        because that is a deterministic choice Clerk already made rather than
        one this code would be inventing;
      * anything else is ambiguous and fails closed.
    """
    if not clerk_user_id:
        return EmailOutcome.NOT_CONFIGURED, None

    secret = settings.clerk_secret_key
    if not secret:
        logger.warning("clerk_admin.secret_key_missing")
        return EmailOutcome.NOT_CONFIGURED, None

    url = f"{settings.clerk_api_base.rstrip('/')}/users/{clerk_user_id}"
    headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json"}
    timeout = httpx.Timeout(settings.clerk_http_timeout_seconds)

    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        outcome = EmailOutcome.TRANSIENT
        payload: object = None
        for attempt in range(1, _MAX_ATTEMPTS_PER_CALL + 1):
            try:
                response = http.request("GET", url, headers=headers, timeout=timeout)
            except httpx.TimeoutException:
                outcome = EmailOutcome.TIMEOUT
            except httpx.HTTPError:
                outcome = EmailOutcome.NETWORK
            else:
                outcome = _classify_lookup(response.status_code)
                if outcome is EmailOutcome.RESOLVED:
                    try:
                        payload = response.json()
                    except ValueError:
                        outcome = EmailOutcome.MALFORMED
                if outcome is not EmailOutcome.TRANSIENT:
                    break

            if attempt < _MAX_ATTEMPTS_PER_CALL and outcome in (
                EmailOutcome.TRANSIENT,
                EmailOutcome.TIMEOUT,
                EmailOutcome.NETWORK,
            ):
                continue
            break

        if outcome is EmailOutcome.RESOLVED:
            outcome, email = _select_verified_email(payload)
        else:
            email = None

        # Outcome only. Never the address, the id, the body or the key.
        logger.info("clerk_admin.email_lookup outcome=%s", outcome.value)
        return outcome, email
    finally:
        if owns_client:
            http.close()


def _classify_lookup(status_code: int) -> EmailOutcome:
    if status_code == 200:
        return EmailOutcome.RESOLVED
    if status_code == 404:
        # The subject verified against Clerk's own JWKS moments ago, so a 404
        # means the identity was deleted in between. No account for it.
        return EmailOutcome.NOT_FOUND
    if status_code in (401, 403):
        return EmailOutcome.UNAUTHORIZED
    if status_code in _RETRY_STATUSES:
        return EmailOutcome.TRANSIENT
    return EmailOutcome.UNEXPECTED_STATUS


def _select_verified_email(payload: object) -> tuple[EmailOutcome, str | None]:
    """Pick the single verified address, or refuse."""
    if not isinstance(payload, dict):
        return EmailOutcome.MALFORMED, None

    entries = payload.get("email_addresses")
    if not isinstance(entries, list):
        return EmailOutcome.MALFORMED, None

    verified: list[tuple[str, str]] = []  # (id, address)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("email_address")
        verification = entry.get("verification")
        status = verification.get("status") if isinstance(verification, dict) else None
        if status == "verified" and isinstance(address, str) and address:
            verified.append((str(entry.get("id") or ""), address))

    if not verified:
        return EmailOutcome.NO_VERIFIED_EMAIL, None
    if len(verified) == 1:
        return EmailOutcome.RESOLVED, verified[0][1]

    primary_id = payload.get("primary_email_address_id")
    if isinstance(primary_id, str) and primary_id:
        for identifier, address in verified:
            if identifier == primary_id:
                return EmailOutcome.RESOLVED, address

    # Several verified addresses and no verified primary among them. Choosing
    # one would be guessing which account the invitation meant.
    return EmailOutcome.AMBIGUOUS, None
