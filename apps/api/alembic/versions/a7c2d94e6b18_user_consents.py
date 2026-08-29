"""user_consents: versioned consent records

Revision ID: a7c2d94e6b18
Revises: f3b95e01c7a2
Create Date: 2026-08-29 00:12:00.000000

An event log rather than a set of boolean columns. "Accepted the terms" is not
a useful fact without knowing WHICH terms, so each row carries the document
version; bumping a version re-prompts, and the history of what somebody agreed
to survives the change.

Nothing financial is stored here — a consent type, a version string, a
timestamp and a request id.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c2d94e6b18"
down_revision: str | None = "f3b95e01c7a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_consents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("consent_type", sa.String(length=40), nullable=False),
        sa.Column("document_version", sa.String(length=40), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_consents_user_id", "user_consents", ["user_id"], unique=False)
    op.create_index(
        "ix_user_consents_user_type", "user_consents", ["user_id", "consent_type"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_user_consents_user_type", table_name="user_consents")
    op.drop_index("ix_user_consents_user_id", table_name="user_consents")
    op.drop_table("user_consents")
