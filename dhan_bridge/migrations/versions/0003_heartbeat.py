"""heartbeat: one row, rewritten daily (keeps a free Supabase project awake)

Revision ID: 0003_heartbeat
Revises: 0002_api_keys
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_heartbeat"
down_revision = "0002_api_keys"
branch_labels = None
depends_on = None

SCHEMA = "bridge"


def upgrade() -> None:
    op.create_table(
        "heartbeat",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="heartbeat_single_row"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    raise NotImplementedError("expand-only: roll back by restoring the pre-deploy backup")
