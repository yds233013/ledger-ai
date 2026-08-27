"""merchant key and correction scope

Adds the stored normalized merchant used by "apply to all matching", and the
scope flag that protects individually-corrected rows from later bulk changes.

Both columns are added nullable, backfilled, then made NOT NULL, so the
migration is safe against a table that already has rows.

Revision ID: 6653fa455298
Revises: 3f5a019a2d69
Create Date: 2026-08-26 18:30:58.020595
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '6653fa455298'
down_revision: str | None = '3f5a019a2d69'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors services.normalize.merchant_key exactly: lower-case, replace every
# character outside [a-z0-9 ] with a space, collapse runs of whitespace, trim.
MERCHANT_KEY_SQL = (
    "btrim("
    "  regexp_replace("
    "    regexp_replace(lower(merchant), '[^a-z0-9 ]', ' ', 'g'),"
    "    '\\s+', ' ', 'g'"
    "  )"
    ")"
)


def upgrade() -> None:
    op.add_column(
        "transaction_corrections",
        sa.Column("scope", sa.String(length=12), nullable=True),
    )
    # Every correction that predates this column was made one row at a time.
    op.execute("UPDATE transaction_corrections SET scope = 'individual' WHERE scope IS NULL")
    op.alter_column(
        "transaction_corrections",
        "scope",
        existing_type=sa.String(length=12),
        nullable=False,
        server_default="individual",
    )

    op.add_column(
        "transactions", sa.Column("merchant_key", sa.String(length=200), nullable=True)
    )
    op.execute(f"UPDATE transactions SET merchant_key = {MERCHANT_KEY_SQL}")
    op.alter_column(
        "transactions",
        "merchant_key",
        existing_type=sa.String(length=200),
        nullable=False,
    )
    op.create_index(
        "ix_transactions_user_merchant_key",
        "transactions",
        ["user_id", "merchant_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_user_merchant_key", table_name="transactions")
    op.drop_column("transactions", "merchant_key")
    op.drop_column("transaction_corrections", "scope")
