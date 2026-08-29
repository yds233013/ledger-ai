"""Clerk webhooks. Currently one event: user.deleted.

Clerk's own documentation says webhook delivery is not guaranteed, so this
endpoint is deliberately *not* the guarantee. It records intent — a tombstone —
and returns. A sweep on the worker does the actual work and retries until it
succeeds, which means a dropped, delayed or duplicated delivery costs time
rather than correctness.

Three properties the implementation is built around:

**Signature over the raw body.** The bytes are verified exactly as received,
before any parsing. Verifying a re-serialized payload would check a different
string than the one that was signed, and any difference in key order or spacing
would either break valid requests or, worse, let a modified body pass.

**Retryable failures must look retryable.** If the tombstone cannot be written,
this returns 500 so Svix retries. Returning 200 on a failure would silently
drop a deletion request, which is the one outcome that must not happen.

**Duplicates are free.** `record_deletion_intent` is idempotent, so a redelivery
of an event already processed changes nothing.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from svix.webhooks import Webhook, WebhookVerificationError

from ..config import settings
from ..deps import SyncSessionFactory
from ..services.account_deletion import record_deletion_intent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

# Clerk sends many event types; this handles the one that has a consequence.
HANDLED_EVENTS = {"user.deleted"}


@router.post("/clerk", include_in_schema=False)
async def clerk_webhook(
    request: Request, response: Response, factory: SyncSessionFactory
) -> dict[str, str]:
    if not settings.clerk_webhook_signing_secret:
        # Unconfigured must not mean "accept unsigned callbacks".
        logger.warning("clerk_webhook.not_configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Unavailable")

    # The exact bytes that were signed. Read before anything looks at them.
    payload = await request.body()

    try:
        # Raises unless the signature over these exact bytes checks out. The
        # return value is not used: the verified payload is the bytes we
        # already hold, and parsing them ourselves keeps "what was signed" and
        # "what we act on" provably the same object.
        Webhook(settings.clerk_webhook_signing_secret).verify(payload, dict(request.headers))
    except WebhookVerificationError as exc:
        # 400, not 500: the request is wrong, and Svix should not retry it.
        logger.warning("clerk_webhook.signature_rejected")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - malformed body, bad header shape
        logger.warning("clerk_webhook.unverifiable error=%s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature"
        ) from exc

    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed payload"
        ) from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed payload")

    event_type = str(event.get("type", ""))
    if event_type not in HANDLED_EVENTS:
        # Acknowledged so Svix stops retrying something we will never act on.
        logger.info("clerk_webhook.ignored event_type=%s", event_type)
        return {"status": "ignored"}

    data = event.get("data")
    clerk_user_id = str((data or {}).get("id") or "") if isinstance(data, dict) else ""
    if not clerk_user_id:
        logger.warning("clerk_webhook.missing_subject event_type=%s", event_type)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed payload")

    try:
        # The injected factory, not a module-level session: a route that can
        # only ever reach one database cannot be tested against another, and
        # the rest of this codebase already takes it as a dependency.
        with factory() as session:
            record_deletion_intent(session, clerk_user_id=clerk_user_id)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - the retry IS the recovery
        # 500 so Svix retries. Acknowledging a deletion we failed to record
        # would lose it, and nothing else would notice.
        logger.exception("clerk_webhook.intent_not_recorded error=%s", type(exc).__name__)
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Retry"
        ) from exc

    logger.info("clerk_webhook.accepted event_type=%s", event_type)
    return {"status": "accepted"}
