"""Finishing deletions that were interrupted, and the ones a webhook never
delivered.

Deletion spans Postgres, Redis, the queue, R2 and Clerk. No transaction covers
those, so any of them can fail after the others have succeeded. This sweep is
the thing that makes the process eventually complete rather than
usually-complete: it picks up every tombstone that is not `complete` and pushes
it one step further.

It runs on the existing maintenance scheduler inside the worker, alongside the
demo and retention sweeps, so it inherits their guarantees — one runner at a
time via a Redis lock, state in Redis rather than memory, and a failure that is
logged and retried rather than propagated.

**Ordering.** Our data goes first, the provider identity second. If our cleanup
fails the tombstone stays `pending`, and nothing can sign in meanwhile because
the tombstone already denies provisioning. If our cleanup succeeded and the
Clerk revocation failed, the tombstone sits at `storage_purged` — still
denying — and only the revocation is retried. Neither order avoids partial
failure; this one keeps the account unusable throughout it.

Clerk revocation is a no-op stub until `CLERK_ENABLED` is turned on and a
secret key exists. That is deliberate for this phase: the tombstone and the
local purge are what protect the user's data, and calling an unconfigured API
would only manufacture failures.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any

from ..config import settings
from ..db import sync_session
from ..models import User
from ..services import account_deletion
from ..services.clerk_admin import revoke_identity
from ..services.storage import StorageError, get_storage

logger = logging.getLogger(__name__)


def _purge_local_data(session: Any, tombstone: Any) -> dict[str, int]:
    """Remove everything this deployment holds for the account.

    Postgres cascades from `users.id`; R2 is purged by prefix. Redis analysis
    caches and queued jobs hang off the same user id and are removed with the
    row, so the counts here are the ones worth recording.
    """
    counts: dict[str, int] = {"storage_objects_removed": 0, "profiles_removed": 0}

    if tombstone.user_id is None:
        # Either the profile never existed, or a previous attempt removed it.
        return counts

    user = session.get(User, tombstone.user_id)
    if user is None:
        return counts

    # Object storage first: deleting the row first would lose the id the
    # prefix is built from, stranding the files with nothing pointing at them.
    try:
        counts["storage_objects_removed"] = get_storage().delete_prefix(f"users/{user.id}/")
    except StorageError:
        # Re-raised so the tombstone stays unfinished and the next tick retries.
        # Swallowing it here would mark the deletion done with files still in
        # the bucket.
        raise

    session.delete(user)
    counts["profiles_removed"] = 1
    session.flush()
    return counts


def _revoke_clerk_identity(clerk_user_id: str) -> bool:
    """Revoke the identity at the provider.

    Returns True when the identity is gone or there is nothing to do.

    With Clerk disabled there is no provider to call and no identity that could
    exist, so there is nothing outstanding — the local purge is the whole of the
    deletion and this reports done. With Clerk enabled the call is real, and
    anything short of "gone" leaves the tombstone unfinished for the next tick.
    """
    if not settings.clerk_enabled:
        return True
    return revoke_identity(clerk_user_id).succeeded


def reconcile_deletions(
    limit: int = 25,
    session_factory: Callable[[], AbstractContextManager[Any]] | None = None,
) -> Mapping[str, object]:
    """One pass. Never raises — the caller is the worker's scheduler thread.

    The session factory is injectable because the default one binds to
    DATABASE_URL, which is the deployment's database and not necessarily the
    one a caller means. A function that can only ever talk to one database is
    a function that cannot be tested against another.
    """
    factory = session_factory or sync_session
    processed = 0
    completed = 0
    failed = 0
    totals: dict[str, int] = {}

    with factory() as session:
        pending = account_deletion.unfinished(session, limit=limit)
        for tombstone in pending:
            clerk_user_id = tombstone.clerk_user_id
            processed += 1
            try:
                if tombstone.state == account_deletion.STATE_PENDING:
                    counts = _purge_local_data(session, tombstone)
                    for key, value in counts.items():
                        totals[key] = totals.get(key, 0) + value
                    account_deletion.mark_storage_purged(session, clerk_user_id)
                    tombstone.user_id = None

                if _revoke_clerk_identity(clerk_user_id):
                    account_deletion.mark_complete(session, clerk_user_id)
                    completed += 1
                else:
                    # Our data is gone and the identity stays blocked; only the
                    # provider call is outstanding.
                    account_deletion.record_attempt(session, clerk_user_id)
                session.commit()
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
                session.rollback()
                failed += 1
                with factory() as recording:
                    account_deletion.record_attempt(recording, clerk_user_id, error=exc)
                    recording.commit()
                logger.warning(
                    "account_reconcile.attempt_failed error=%s", type(exc).__name__
                )

    report = {
        "tombstones_processed": processed,
        "completed": completed,
        "failed": failed,
        **totals,
    }
    logger.info(
        "account_reconcile.completed processed=%d completed=%d failed=%d",
        processed,
        completed,
        failed,
    )
    return report


def run_account_reconcile() -> Mapping[str, object]:
    """Scheduler entry point."""
    return reconcile_deletions()
