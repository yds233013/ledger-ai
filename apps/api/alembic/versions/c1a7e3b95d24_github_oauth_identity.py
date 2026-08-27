"""github oauth identity

Adds users.github_id — GitHub's immutable numeric account id — as the single
key a GitHub sign-in is resolved by.

Deliberately NOT an email column: an email address, even one a provider claims
to have verified, is not proof of ownership of a Ledger AI account. Resolving
on it would let anyone who can set their provider address to a known user's
address inherit that user's data. UNIQUE so one GitHub account maps to exactly
one Ledger AI account, and NULL for every account that does not use GitHub.

Revision ID: c1a7e3b95d24
Revises: b8d41c7e2f90
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1a7e3b95d24"
down_revision: str | None = "b8d41c7e2f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("github_id", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_users_github_id", "users", ["github_id"])


def downgrade() -> None:
    op.drop_constraint("uq_users_github_id", "users", type_="unique")
    op.drop_column("users", "github_id")
