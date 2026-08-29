"""invitations: the local, email-bound beta gate

Revision ID: f3b95e01c7a2
Revises: e1a4c72b8f30
Create Date: 2026-08-29 00:11:00.000000

Clerk's Restricted mode is the primary gate — no invitation, no Clerk account,
no token. This table is the second one: an audit trail of who was invited,
revocation after a Clerk invite has already been sent, and a kill-switch that
does not depend on dashboard state.

The address is stored as a KEYED HMAC, never plaintext and never a bare hash.
It has to be matchable, because provisioning finds the invitation using the
email Clerk verified — the user is never asked to type a second code. A plain
SHA-256 of an email is trivially enumerable: the input space is small and
guessable, so "is alice@gmail.com invited?" would be answerable by anyone who
read the table. Keying the digest removes that.

`email_hint` is a deliberately lossy display string (a***@e***.com) so an
administrator can tell rows apart without the table holding addresses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3b95e01c7a2"
down_revision: str | None = "e1a4c72b8f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email_hmac", sa.String(length=64), nullable=False),
        sa.Column("email_hint", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["redeemed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # One live invitation per address. The UNIQUE is also what makes
        # "create an invitation" idempotent for an administrator.
        sa.UniqueConstraint("email_hmac"),
    )


def downgrade() -> None:
    op.drop_table("invitations")
