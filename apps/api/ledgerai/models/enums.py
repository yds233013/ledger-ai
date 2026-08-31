"""String enums shared by the ORM models and the API schemas.

These are stored as VARCHAR with a CHECK-style application constraint rather
than native Postgres ENUMs: adding a value later is a code change, not a
migration that locks the table.
"""

from __future__ import annotations

from enum import StrEnum


class UploadKind(StrEnum):
    CSV = "csv"
    IMAGE = "image"
    # A bank statement PDF, read through its text layer. Distinct from IMAGE so
    # a PDF receipt keeps the OCR path and its five-page cap: the two are the
    # same file format asking for completely different treatment, and the
    # uploader says which rather than the server guessing.
    STATEMENT_PDF = "statement_pdf"


class StatementImportStatus(StrEnum):
    """Where an import has got to.

    Rows are inert in every state but COMMITTED — a parsed statement is an
    inference, and nothing derived from it reaches the ledger until a person
    says so.
    """

    PARSING = "parsing"
    NEEDS_REVIEW = "needs_review"
    COMMITTED = "committed"
    FAILED = "failed"


class UploadStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"
    DUPLICATE = "duplicate"  # identical bytes already ingested for this user


class JobStage(StrEnum):
    """The user-visible pipeline. Order matters — the UI renders it as steps."""

    QUEUED = "queued"
    EXTRACTING = "extracting"
    NORMALIZING = "normalizing"
    CATEGORIZING = "categorizing"
    ANALYZING = "analyzing"
    COMPLETE = "complete"
    FAILED = "failed"


JOB_STAGE_ORDER: list[JobStage] = [
    JobStage.QUEUED,
    JobStage.EXTRACTING,
    JobStage.NORMALIZING,
    JobStage.CATEGORIZING,
    JobStage.ANALYZING,
    JobStage.COMPLETE,
]


class CorrectionField(StrEnum):
    MERCHANT = "merchant"
    CATEGORY = "category"


class CorrectionScope(StrEnum):
    """How a correction was applied.

    Individual corrections are protected from later bulk changes: if the user
    deliberately set one row to something different, a subsequent
    "apply to all matching" must not silently overwrite that decision.
    """

    INDIVIDUAL = "individual"
    BULK = "bulk"


class AlertType(StrEnum):
    DUPLICATE = "duplicate"
    NEAR_DUPLICATE = "near_duplicate"
    UNUSUAL_AMOUNT = "unusual_amount"
    NEW_MERCHANT = "new_merchant"
    LARGE_FOR_MERCHANT = "large_for_merchant"


class AlertSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AlertStatus(StrEnum):
    OPEN = "open"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"


class ReceiptStatus(StrEnum):
    """A receipt never becomes a transaction on its own — confirming is an
    explicit user action, so `needs_review` is a resting state, not an error."""

    PENDING = "pending"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class ReceiptLinkMode(StrEnum):
    """How a confirmed receipt produced its transaction."""

    CREATED = "created"   # a new transaction was created from the receipt
    LINKED = "linked"     # the receipt was attached to an existing transaction


class AnalysisStatus(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AnalysisStepName(StrEnum):
    UNDERSTAND = "understand"
    SELECT = "select"
    AGGREGATE = "aggregate"
    VISUALIZE = "visualize"
    EXPLAIN = "explain"


class StepStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class PlannerKind(StrEnum):
    RULES = "rules"
    LLM = "llm"


class NarratorKind(StrEnum):
    TEMPLATE = "template"
    LLM = "llm"
