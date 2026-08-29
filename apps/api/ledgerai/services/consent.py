"""Versioned consent records.

Recording *which* document someone agreed to is the whole value: "accepted the
terms" is not a meaningful statement without a version, and a beta whose terms
change needs to know who has seen which text. Each acceptance is a row, so the
history survives a version bump instead of being overwritten by it.

Nothing financial is stored — a consent type, a version string, a timestamp and
a request id.

Enforcement is deliberately narrow. Reading your own data, exporting it and
deleting it are never gated: withholding somebody's own records until they
accept a new document would be a hostage-taking, not a consent flow. Upload is
gated, because that is the point at which new financial data enters the system.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ..config import settings
from ..models import User, UserConsent

logger = logging.getLogger(__name__)

TERMS = "terms"
PRIVACY = "privacy"
UPLOAD = "upload"

#: Consent type -> the version currently required.
REQUIRED_VERSIONS = {
    TERMS: lambda: settings.terms_version,
    PRIVACY: lambda: settings.privacy_version,
    UPLOAD: lambda: settings.upload_consent_version,
}

#: What a persistent account must have accepted before uploading anything.
UPLOAD_PREREQUISITES = (TERMS, PRIVACY, UPLOAD)


def required_version(consent_type: str) -> str:
    resolver = REQUIRED_VERSIONS.get(consent_type)
    return resolver() if resolver else ""


def record_consent(
    session: Session,
    *,
    user_id: uuid.UUID,
    consent_type: str,
    request_id: str = "",
    now: datetime | None = None,
) -> UserConsent:
    """Record acceptance of the currently required version."""
    consent = UserConsent(
        id=uuid.uuid4(),
        user_id=user_id,
        consent_type=consent_type,
        document_version=required_version(consent_type),
        accepted_at=now or datetime.now(UTC),
        request_id=request_id[:64],
    )
    session.add(consent)
    session.flush()
    logger.info(
        "consent.recorded type=%s version=%s", consent_type, consent.document_version
    )
    return consent


async def accepted_versions(session: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    """The latest accepted version per consent type."""
    rows = (
        await session.execute(
            select(UserConsent.consent_type, UserConsent.document_version, UserConsent.accepted_at)
            .where(UserConsent.user_id == user_id)
            .order_by(UserConsent.accepted_at)
        )
    ).all()
    # Later rows win, which is what "latest accepted" means.
    return {row.consent_type: row.document_version for row in rows}


async def missing_consents(session: AsyncSession, user: User) -> list[str]:
    """Which prerequisites this user has not accepted at the current version.

    Demo accounts are exempt. They upload only synthetic data into an account
    that deletes itself within a day, and interposing a legal wall in front of
    a one-click demo would cost the demo without protecting anybody.
    """
    if user.is_demo:
        return []
    accepted = await accepted_versions(session, user.id)
    return [
        consent_type
        for consent_type in UPLOAD_PREREQUISITES
        if accepted.get(consent_type) != required_version(consent_type)
    ]
