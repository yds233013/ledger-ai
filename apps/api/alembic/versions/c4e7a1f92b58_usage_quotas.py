"""user_usage and usage_reservations: durable private-beta quotas

Revision ID: c4e7a1f92b58
Revises: b5e8f31a9d64
Create Date: 2026-08-30 00:00:00.000000

Two tables, with different lifetimes.

`user_usage` holds committed daily counters for one persistent account on one
UTC day. UTC rather than a local zone so the reset does not move when somebody
travels. The unique constraint on (user_id, usage_date) is what makes the row
lockable: concurrent requests contend for one row per user per day rather than
racing to create several.

`usage_reservations` holds claims on budget while work is in flight, and is
expected to be nearly empty. `expires_at` is indexed because the sweep that
clears reservations whose work never finished queries on it exclusively.

Both cascade from `users`, so account deletion takes the counters with it.
Nothing here records financial data — counts, byte totals and dates only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e7a1f92b58"
down_revision: str | None = "b5e8f31a9d64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("uploads_today", sa.Integer(), nullable=False),
        sa.Column("bytes_today", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "usage_date", name="uq_user_usage_user_date"),
    )
    op.create_index("ix_user_usage_user_id", "user_usage", ["user_id"], unique=False)

    op.create_table(
        "usage_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=True),
        sa.Column("bytes_reserved", sa.BigInteger(), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["upload_id"], ["uploads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_usage_reservations_user_id", "usage_reservations", ["user_id"], unique=False
    )
    op.create_index(
        "ix_usage_reservations_user_date", "usage_reservations", ["user_id", "usage_date"],
        unique=False,
    )
    op.create_index(
        "ix_usage_reservations_expires_at", "usage_reservations", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_usage_reservations_expires_at", table_name="usage_reservations")
    op.drop_index("ix_usage_reservations_user_date", table_name="usage_reservations")
    op.drop_index("ix_usage_reservations_user_id", table_name="usage_reservations")
    op.drop_table("usage_reservations")
    op.drop_index("ix_user_usage_user_id", table_name="user_usage")
    op.drop_table("user_usage")
