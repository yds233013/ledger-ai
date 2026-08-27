"""ephemeral demo users

Adds the two columns that make a per-visitor demo account safe to hand out and
safe to delete:

  * demo_expires_at   — when this ephemeral account stops working. NULL means
                        "not ephemeral", which is what keeps the permanent
                        development demo user and every real account outside
                        the reach of the cleanup sweep.
  * demo_request_key  — UNIQUE idempotency key. Two concurrent provisioning
                        requests carrying the same key collide here rather than
                        both building a dataset, and a retried request returns
                        the account the first attempt created.

Revision ID: b8d41c7e2f90
Revises: a67da4364dca
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d41c7e2f90"
down_revision: str | None = "a67da4364dca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("demo_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("demo_request_key", sa.String(length=64), nullable=True),
    )
    # The sweep filters on this column, and it stays small: only ephemeral rows
    # are non-NULL.
    op.create_index(
        "ix_users_demo_expires_at", "users", ["demo_expires_at"], unique=False
    )
    op.create_unique_constraint(
        "uq_users_demo_request_key", "users", ["demo_request_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_users_demo_request_key", "users", type_="unique")
    op.drop_index("ix_users_demo_expires_at", table_name="users")
    op.drop_column("users", "demo_request_key")
    op.drop_column("users", "demo_expires_at")
