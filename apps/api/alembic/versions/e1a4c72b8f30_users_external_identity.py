"""users: external identity and account lifecycle

Revision ID: e1a4c72b8f30
Revises: d2f81b6c9a37
Create Date: 2026-08-29 00:10:00.000000

First of four migrations for the private beta. Additive and independently
reversible, so a bad release can roll back one step rather than all four.

`clerk_user_id` is the permanent identity key for a persistent account. Not the
email address: Clerk lets a user change theirs, and an identity that can be
edited by its holder is not an identity. Demo users keep NULL here.

The uniqueness guarantee is a PARTIAL index. Postgres treats NULLs as distinct,
so a plain UNIQUE would permit two rows for the same Clerk subject once demo
rows (all NULL) are in the table. The partial index constrains only real
identities, and it is what makes ON CONFLICT provisioning race-safe rather than
merely usually-correct.

`password_hash` becomes nullable because a Clerk-backed account has no password
of ours. The downgrade REFUSES rather than corrupting if NULLs exist by then —
see the note there.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a4c72b8f30"
down_revision: str | None = "d2f81b6c9a37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("clerk_user_id", sa.String(length=64), nullable=True))
    # server_default so the ALTER can run against a populated table without a
    # rewrite pass that needs every row supplied up front.
    op.add_column(
        "users",
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
    )
    op.add_column(
        "users",
        sa.Column("created_via", sa.String(length=24), nullable=False, server_default="demo"),
    )
    op.add_column("users", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column(
        "users", "password_hash", existing_type=sa.VARCHAR(length=255), nullable=True
    )
    op.create_index(
        "uq_users_clerk_user_id",
        "users",
        ["clerk_user_id"],
        unique=True,
        postgresql_where=sa.text("clerk_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Restoring NOT NULL on password_hash is only possible if nothing relies on
    # it being NULL. Failing loudly beats inventing a placeholder hash, which
    # would leave rows that look like password accounts and are not.
    nulls = bind.execute(
        sa.text("SELECT COUNT(*) FROM users WHERE password_hash IS NULL")
    ).scalar_one()
    if int(nulls) > 0:
        raise RuntimeError(
            f"Refusing to downgrade: {nulls} user(s) have no password_hash. "
            "These are Clerk-backed accounts; removing them or assigning a "
            "placeholder is a decision this migration must not make. Delete or "
            "migrate those accounts first."
        )

    op.drop_index("uq_users_clerk_user_id", table_name="users")
    op.alter_column(
        "users", "password_hash", existing_type=sa.VARCHAR(length=255), nullable=False
    )
    op.drop_column("users", "last_seen_at")
    op.drop_column("users", "created_via")
    op.drop_column("users", "status")
    op.drop_column("users", "clerk_user_id")
