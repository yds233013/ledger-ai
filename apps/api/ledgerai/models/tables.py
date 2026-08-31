"""Ledger AI ORM models.

Conventions enforced here:
  * Money is ALWAYS integer cents (BigInteger). No floats anywhere.
    Sign convention: negative = outflow/spend, positive = inflow/credit.
  * Every user-owned row carries user_id and is indexed on it. The API layer
    applies the user predicate in a single shared selectable so no route can
    forget it (see services/scoping.py).
  * Idempotency is enforced by the database, not by application checks:
    uploads.(user_id, content_hash) and transactions.dedupe_hash are unique.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, uuid_pk
from .enums import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    AnalysisStatus,
    AnalysisStepName,
    CorrectionField,
    CorrectionScope,
    JobStage,
    NarratorKind,
    PlannerKind,
    ReceiptLinkMode,
    ReceiptStatus,
    StatementImportStatus,
    StepStatus,
    UploadKind,
    UploadStatus,
)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    # Nullable since Clerk-backed accounts have no password of ours to store.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # True for any account holding synthetic demo data — including the seeded
    # local development account, which is permanent.
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Set ONLY on ephemeral per-visitor demo accounts, and the single marker the
    # cleanup sweep selects on. The permanent development demo user has
    # is_demo=True with this left NULL, so the sweep can never reach it, and a
    # real account can never be reached because it has neither.
    demo_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Idempotency key for demo provisioning. UNIQUE, so two concurrent requests
    # carrying the same key cannot both create a user: the loser collides on
    # this index and re-reads the winner's row instead of building a second
    # dataset. NULL for every non-demo account (Postgres treats NULLs as
    # distinct, so the constraint does not serialise ordinary sign-ups).
    demo_request_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    # GitHub's immutable numeric account id, when this account signs in with
    # GitHub. UNIQUE, and the ONLY key an OAuth identity is resolved by — an
    # email address is not proof of ownership even when the provider says it
    # verified it, so it is never used to find an existing account.
    github_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    # The Clerk `sub` claim. THE permanent identity key for a persistent
    # account — never the email address, which Clerk lets a user change.
    # NULL for demo users, so the uniqueness guarantee is a partial index.
    clerk_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    created_via: Mapped[str] = mapped_column(String(24), default="demo", nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Ledger AI does not convert between currencies. Aggregates are restricted
    # to this currency and anything else is disclosed, never silently summed.
    base_currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    __table_args__ = (
        # Postgres treats NULLs as distinct, so a plain UNIQUE would not stop a
        # second row for the same Clerk subject — and every demo user has NULL
        # here. The partial index constrains only real Clerk identities, and it
        # is what makes ON CONFLICT provisioning race-safe.
        Index(
            "uq_users_clerk_user_id",
            "clerk_user_id",
            unique=True,
            postgresql_where=text("clerk_user_id IS NOT NULL"),
        ),
    )

    accounts: Mapped[list[Account]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Account(Base, TimestampMixin):
    """A synthetic bank/card account. No real institution is ever contacted."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    institution: Mapped[str] = mapped_column(String(120), nullable=False)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)  # checking|credit|savings
    mask: Mapped[str] = mapped_column(String(4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    # True for the "Cash / Receipt Purchases" holding account. A receipt-created
    # transaction is never silently attached to a real bank account, so the
    # fallback destination has to be visibly distinct.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="accounts")

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_accounts_user_name"),)


class Category(Base, TimestampMixin):
    """System categories have user_id NULL; user-created ones are scoped."""

    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    color: Mapped[str] = mapped_column(String(9), nullable=False)  # #RRGGBB
    icon: Mapped[str] = mapped_column(String(40), nullable=False, default="tag")
    is_system: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_categories_user_slug"),
        # Postgres treats NULLs as distinct in a UNIQUE constraint, so the
        # constraint above does NOT prevent duplicate system categories
        # (user_id IS NULL). This partial index does.
        Index(
            "uq_categories_system_slug",
            "slug",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
    )


class Invitation(Base, TimestampMixin):
    """A local, email-bound invitation. The second gate behind Clerk's own.

    Clerk's Restricted mode is the real barrier — without an invitation there is
    no Clerk account and therefore no token. This table exists so the beta also
    has an audit trail, revocation after a Clerk invite has been sent, and a
    kill-switch that does not depend on dashboard state.

    **The address is stored as a keyed HMAC, never in plaintext.** It has to be
    matchable, because provisioning looks up the invitation by the email Clerk
    verified — the user is never asked to type a code. A plain SHA-256 of an
    email is trivially enumerable (the input space is small and guessable), so
    the digest is keyed: without the key, the column cannot be searched for a
    known address. `email_hint` is a deliberately lossy display string so an
    administrator can tell invitations apart.
    """

    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = uuid_pk()
    email_hmac: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    email_hint: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeemed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class UserConsent(Base, TimestampMixin):
    """One row per accepted document version. An event log, not a flag.

    Recording the version is the point: "accepted the terms" is not a useful
    fact without knowing *which* terms. Bumping a version re-prompts, and the
    history of what someone agreed to survives the change.
    """

    __tablename__ = "user_consents"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    consent_type: Mapped[str] = mapped_column(String(40), nullable=False)
    document_version: Mapped[str] = mapped_column(String(40), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (
        Index("ix_user_consents_user_type", "user_id", "consent_type"),
    )


class DeletedIdentity(Base, TimestampMixin):
    """Tombstone: a Clerk identity that must never be provisioned again.

    Lazy provisioning creates a profile on first authenticated request, which
    means a token issued before deletion — or a Clerk identity whose removal
    failed halfway — would otherwise silently recreate the account the user
    asked to erase. This row is what denies that, and it outlives the profile
    on purpose.

    It holds an opaque provider id, timestamps, a state and a retry count.
    No email, no name, and nothing financial: the minimum needed to refuse a
    subject and to finish a partial deletion.
    """

    __tablename__ = "deleted_identities"

    clerk_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # pending -> storage_purged -> complete. `clerk_pending` means our data is
    # gone but the provider identity has not been revoked yet.
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # An exception CLASS NAME only — never a message, which could quote data.
    last_error: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    # The local profile id, kept only until cleanup finishes so a retry knows
    # what to purge. Cleared on completion.
    user_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    __table_args__ = (
        # The reconciliation sweep scans for unfinished work every tick;
        # without this it is a sequential scan on each pass.
        Index("ix_deleted_identities_state", "state"),
    )


class UsageReservation(Base, TimestampMixin):
    """A claim on a user's budget, held while work is in flight.

    Quotas cannot be enforced by counting committed rows alone: between the
    check and the insert, a concurrent request can pass the same check. A
    reservation is taken inside the same transaction as the check, so the
    database — not a race — decides who gets the last slot.

    Rows are short-lived. Each is released on rejection, terminal failure or
    cancellation, or converted to committed usage once the upload and its
    stored object are both known to exist. A reconciliation sweep clears any
    that outlive their work, so a crash between reserve and commit costs a
    little headroom for one sweep interval rather than a permanently lost slot.
    """

    __tablename__ = "usage_reservations"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The upload this reservation is for, once one exists. Null between the
    # reservation and the insert that creates it.
    upload_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("uploads.id", ondelete="CASCADE"), nullable=True
    )
    bytes_reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Statement PDFs cost pages, not bytes: a long statement is a small file
    # that keeps the single worker busy for minutes. Zero for everything else.
    pages_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # UTC date the reservation counts against, so a daily window cannot be
    # reset by a client clock.
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_usage_reservations_user_date", "user_id", "usage_date"),
        Index("ix_usage_reservations_expires_at", "expires_at"),
    )


class UserUsage(Base, TimestampMixin):
    """Committed usage for one persistent account on one UTC day.

    Postgres is the authority. Redis handles burst rate limiting and nothing
    durable — a Redis restart must not hand anybody a fresh daily allowance.

    Only the daily counters are stored. Lifetime totals — stored bytes,
    transaction rows, receipts — are counted from the authoritative tables at
    check time rather than accumulated here: a stored total drifts the moment
    anything is deleted outside the quota path, and a drifted counter locks
    somebody out of their own account with no visible cause. Counting costs a
    little more and cannot drift.
    """

    __tablename__ = "user_usage"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    uploads_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_today: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("user_id", "usage_date", name="uq_user_usage_user_date"),
    )


class Upload(Base, TimestampMixin):
    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Sanitized, generated storage name. The user's original name is retained
    # as display data only and is never used to build a filesystem/S3 path.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[UploadKind] = mapped_column(String(16), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[UploadStatus] = mapped_column(
        String(16), default=UploadStatus.RECEIVED, nullable=False
    )

    jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="upload", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # File-level idempotency: identical bytes can only be ingested once
        # per user. Enforced by the database, not by a read-then-write check.
        UniqueConstraint("user_id", "content_hash", name="uq_uploads_user_content_hash"),
        CheckConstraint("size_bytes > 0", name="ck_uploads_size_positive"),
    )


class ProcessingJob(Base, TimestampMixin):
    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    upload_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("uploads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rq_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stage: Mapped[JobStage] = mapped_column(String(20), default=JobStage.QUEUED, nullable=False)
    progress: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    rows_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    upload: Mapped[Upload] = relationship(back_populates="jobs")

    __table_args__ = (
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_jobs_progress_range"),
    )


class Transaction(Base, TimestampMixin):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    upload_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("uploads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    posted_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Negative = money out, positive = money in. Integer cents, never float.
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    raw_description: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_description: Mapped[str] = mapped_column(String(512), nullable=False)
    merchant: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    # Normalized merchant, stored so "apply to all matching" is an exact,
    # indexed predicate rather than a LIKE over a display string.
    merchant_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), default=0, nullable=False)
    categorized_by: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_corrected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Row-level idempotency: a retried or re-uploaded row collides here and is
    # dropped by ON CONFLICT DO NOTHING rather than duplicating spend.
    dedupe_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_row_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    category: Mapped[Category | None] = relationship()
    account: Mapped[Account] = relationship()

    __table_args__ = (
        Index("ix_transactions_user_posted", "user_id", "posted_date"),
        Index("ix_transactions_user_category", "user_id", "category_id"),
        Index("ix_transactions_user_review", "user_id", "needs_review"),
        Index("ix_transactions_user_merchant", "user_id", "merchant"),
        Index("ix_transactions_user_merchant_key", "user_id", "merchant_key"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_tx_confidence_range"),
    )


class TransactionCorrection(Base, TimestampMixin):
    """Audit trail of manual edits — also the highest-priority categorization
    signal, so a correction teaches every future import."""

    __tablename__ = "transaction_corrections"

    id: Mapped[uuid.UUID] = uuid_pk()
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field: Mapped[CorrectionField] = mapped_column(String(20), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    new_value: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[CorrectionScope] = mapped_column(
        String(12), default=CorrectionScope.INDIVIDUAL, nullable=False
    )
    # Normalized merchant at time of correction — the lookup key for the
    # correction-memory stage of the categorizer.
    merchant_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)


class Alert(Base, TimestampMixin):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alert_type: Mapped[AlertType] = mapped_column(String(24), nullable=False)
    severity: Mapped[AlertSeverity] = mapped_column(String(10), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[AlertStatus] = mapped_column(
        String(12), default=AlertStatus.OPEN, nullable=False
    )

    __table_args__ = (
        Index("ix_alerts_user_status", "user_id", "status"),
        UniqueConstraint("transaction_id", "alert_type", name="uq_alerts_tx_type"),
    )


class Receipt(Base, TimestampMixin):
    """One uploaded receipt and everything OCR extracted from it.

    A receipt is inert until confirmed: it holds extracted values but owns no
    transaction. `upload_id` is UNIQUE, so a retried job cannot produce a
    second receipt for the same file.
    """

    __tablename__ = "receipts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    upload_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("uploads.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    link_mode: Mapped[ReceiptLinkMode | None] = mapped_column(String(10), nullable=True)
    status: Mapped[ReceiptStatus] = mapped_column(
        String(16), default=ReceiptStatus.PENDING, nullable=False
    )

    page_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    ocr_engine: Mapped[str] = mapped_column(String(40), default="tesseract", nullable=False)
    # Mean per-word confidence across the whole document, 0.00–1.00.
    ocr_confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=0, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    merchant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    posted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Extracted values stay POSITIVE here. The outflow sign is applied when the
    # transaction is created, so the receipt keeps what was printed on it.
    subtotal_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tax_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tip_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    # {field: 0.0-1.0} rather than a column per field, so adding a field later
    # is not a migration.
    field_confidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    parse_notes: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    upload: Mapped[Upload] = relationship()

    __table_args__ = (
        Index("ix_receipts_user_status", "user_id", "status"),
        CheckConstraint(
            "ocr_confidence >= 0 AND ocr_confidence <= 1", name="ck_receipts_conf_range"
        ),
    )


class StatementImport(Base, TimestampMixin):
    """One statement PDF, parsed and awaiting review.

    The original file and everything derived from it are temporary. `expires_at`
    is the promise: unconfirmed, the whole import — rows, renderings and the PDF
    in object storage — is purged automatically. A statement is the most
    sensitive artefact this product handles, so it is not kept on the chance it
    might be useful later.

    The extracted text is deliberately absent. Receipts keep `raw_text` because
    a receipt is a page; the same column here would be a verbatim second copy of
    a bank statement sitting in the primary database and in every backup. Only
    normalised rows and provenance survive parsing.
    """

    __tablename__ = "statement_imports"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    upload_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("uploads.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # Chosen at confirmation, not at parse time: the user says where these
    # transactions belong.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[StatementImportStatus] = mapped_column(
        String(16), default=StatementImportStatus.PARSING, nullable=False
    )

    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    table_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_lines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    # The strongest correctness signal available, and free: where balances are
    # printed, consecutive deltas must equal the parsed amounts.
    balance_chain_checked: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    balance_chain_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Text-layer versus rendered-page cross-check.
    verified_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verified_mismatches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Safe tokens only — "balance_chain_broken", never statement content.
    notes: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(400), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    upload: Mapped[Upload] = relationship()

    __table_args__ = (
        Index("ix_statement_imports_user_status", "user_id", "status"),
        Index("ix_statement_imports_expires_at", "expires_at"),
    )


class StatementImportRow(Base, TimestampMixin):
    """One parsed transaction, inert until the import is confirmed.

    `source_page` and `source_line` are provenance: they locate the row in a
    document the user already holds, and they make re-parsing the same file
    produce the same idempotency keys. They are not statement content.
    """

    __tablename__ = "statement_import_rows"

    id: Mapped[uuid.UUID] = uuid_pk()
    import_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("statement_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    source_page: Mapped[int] = mapped_column(Integer, nullable=False)
    source_line: Mapped[int] = mapped_column(Integer, nullable=False)

    posted_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=0, nullable=False)
    notes: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # The user's decisions during review.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("ix_statement_rows_import_page", "import_id", "source_page"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_statement_rows_conf_range"
        ),
    )


class ReceiptMatchRejection(Base, TimestampMixin):
    """A candidate the user rejected for this receipt.

    Persisted rather than held in session state: a rejected suggestion should
    not reappear after a page reload, and persistence is simpler to reason
    about than client-side state that has to survive navigation.
    """

    __tablename__ = "receipt_match_rejections"

    id: Mapped[uuid.UUID] = uuid_pk()
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint("receipt_id", "transaction_id", name="uq_receipt_rejection"),
    )


class AnalysisRun(Base, TimestampMixin):
    """One Ask Ledger question. `plan` and `result` are persisted so a cached
    replay renders exactly the same inspectable steps as a live run."""

    __tablename__ = "analysis_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    planner: Mapped[PlannerKind] = mapped_column(String(10), default=PlannerKind.RULES)
    narrator: Mapped[NarratorKind] = mapped_column(String(10), default=NarratorKind.TEMPLATE)
    status: Mapped[AnalysisStatus] = mapped_column(
        String(12), default=AnalysisStatus.RUNNING, nullable=False
    )
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    chart_spec: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    narration: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cache_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    served_from_cache: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    steps: Mapped[list[AnalysisStep]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AnalysisStep.seq",
    )

    __table_args__ = (Index("ix_analysis_runs_user_cache", "user_id", "cache_key"),)


class AnalysisStep(Base, TimestampMixin):
    __tablename__ = "analysis_steps"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[AnalysisStepName] = mapped_column(String(20), nullable=False)
    status: Mapped[StepStatus] = mapped_column(String(12), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    # The inspectable body the UI expands: resolved plan, filter summary,
    # aggregation description + rows, chart spec, or narration source.
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    run: Mapped[AnalysisRun] = relationship(back_populates="steps")

    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_steps_run_seq"),)


class MerchantRule(Base, TimestampMixin):
    """Seeded merchant-pattern -> category mapping (deterministic stage 2)."""

    __tablename__ = "merchant_rules"

    id: Mapped[uuid.UUID] = uuid_pk()
    pattern: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    merchant_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category_slug: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
