"""initial schema: meta + instruments (scrip master cache)

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "bridge"


def upgrade() -> None:
    # Nothing in "bridge" is for PUBLIC; the app connects as the schema owner.
    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")

    op.create_table(
        "meta",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "instruments",
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("exchange_segment", sa.Text(), primary_key=True),
        sa.Column("security_id", sa.Text(), primary_key=True),
        sa.Column("trading_symbol", sa.Text(), nullable=False),
        sa.Column("instrument", sa.Text(), nullable=False),
        sa.Column("series", sa.Text(), nullable=False, server_default=""),
        sa.Column("lot_size", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tick_size", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        schema=SCHEMA,
    )
    op.create_index(
        "idx_instruments_symbol", "instruments", ["exchange", "trading_symbol"], schema=SCHEMA
    )


def downgrade() -> None:
    raise NotImplementedError("expand-only: roll back by restoring the pre-deploy backup")
