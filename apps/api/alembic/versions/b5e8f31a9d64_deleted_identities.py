"""deleted_identities: tombstones that survive the profile

Revision ID: b5e8f31a9d64
Revises: a7c2d94e6b18
Create Date: 2026-08-29 00:13:00.000000

Lazy provisioning creates a profile on the first authenticated request. Without
this table, a token minted before deletion — or a Clerk identity whose removal
failed halfway through — would silently recreate the account the user asked to
erase. The tombstone is what refuses that, and it has to outlive the profile,
which is why it is its own table rather than a column on `users`.

It also carries the state a retry needs, because deletion spans four systems
(Postgres, Redis, the queue, R2) plus Clerk and cannot be atomic across them.
`user_id` is kept only until cleanup finishes so a retry knows what to purge.

Deliberately absent: email, name, and anything financial. An opaque provider
id, timestamps, a state and a retry count are the minimum needed to refuse a
subject and to finish a partial deletion. `last_error` holds an exception CLASS
NAME, never a message, because a message can quote data.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e8f31a9d64"
down_revision: str | None = "a7c2d94e6b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deleted_identities",
        sa.Column("clerk_user_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("clerk_user_id"),
    )
    # The reconciliation sweep scans for unfinished work; without this it is a
    # sequential scan on every tick.
    op.create_index(
        "ix_deleted_identities_state", "deleted_identities", ["state"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_deleted_identities_state", table_name="deleted_identities")
    op.drop_table("deleted_identities")
