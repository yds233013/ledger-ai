"""statement_imports and rows: text-layer statement PDF import

Revision ID: d7f2c48a91b3
Revises: c4e7a1f92b58
Create Date: 2026-08-31 00:00:00.000000

Two tables plus a page counter on the existing reservation row.

`statement_imports` holds one parsed statement awaiting review. It carries no
extracted text: a receipt keeps `raw_text` because a receipt is a page, but the
same column here would put a verbatim bank statement in the primary database
and in every backup. Only normalised rows and provenance survive parsing.

`expires_at` is indexed because the sweep that purges unconfirmed imports —
rows, renderings and the stored PDF together — queries on it exclusively.

`usage_reservations.pages_reserved` exists because pages, not bytes, are what a
statement costs: a long statement is a small file that occupies the single
worker for a long time.

Both tables cascade from users, so account deletion takes them with it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d7f2c48a91b3"
down_revision: str | None = "c4e7a1f92b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "usage_reservations",
        sa.Column("pages_reserved", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "statement_imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("table_pages", sa.Integer(), nullable=False),
        sa.Column("skipped_lines", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("balance_chain_checked", sa.Boolean(), nullable=False),
        sa.Column("balance_chain_ok", sa.Boolean(), nullable=False),
        sa.Column("verified_pages", sa.Integer(), nullable=False),
        sa.Column("verified_mismatches", sa.Integer(), nullable=False),
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_message", sa.String(length=400), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["upload_id"], ["uploads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("upload_id"),
    )
    op.create_index("ix_statement_imports_user_id", "statement_imports", ["user_id"])
    op.create_index("ix_statement_imports_account_id", "statement_imports", ["account_id"])
    op.create_index(
        "ix_statement_imports_user_status", "statement_imports", ["user_id", "status"]
    )
    op.create_index("ix_statement_imports_expires_at", "statement_imports", ["expires_at"])

    op.create_table(
        "statement_import_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_page", sa.Integer(), nullable=False),
        sa.Column("source_line", sa.Integer(), nullable=False),
        sa.Column("posted_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("balance_cents", sa.BigInteger(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("edited", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_statement_rows_conf_range"
        ),
        sa.ForeignKeyConstraint(["import_id"], ["statement_imports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_statement_import_rows_import_id", "statement_import_rows", ["import_id"])
    op.create_index("ix_statement_import_rows_user_id", "statement_import_rows", ["user_id"])
    op.create_index(
        "ix_statement_rows_import_page", "statement_import_rows", ["import_id", "source_page"]
    )


def downgrade() -> None:
    op.drop_index("ix_statement_rows_import_page", table_name="statement_import_rows")
    op.drop_index("ix_statement_import_rows_user_id", table_name="statement_import_rows")
    op.drop_index("ix_statement_import_rows_import_id", table_name="statement_import_rows")
    op.drop_table("statement_import_rows")
    op.drop_index("ix_statement_imports_expires_at", table_name="statement_imports")
    op.drop_index("ix_statement_imports_user_status", table_name="statement_imports")
    op.drop_index("ix_statement_imports_account_id", table_name="statement_imports")
    op.drop_index("ix_statement_imports_user_id", table_name="statement_imports")
    op.drop_table("statement_imports")
    op.drop_column("usage_reservations", "pages_reserved")
