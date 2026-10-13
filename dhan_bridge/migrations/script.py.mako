"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Rules (tests enforce them): schema "bridge" only, standard Postgres types,
no extensions, no vendor-specific schemas, expand-only (no drop_*/rename
without a "# -- allow-destructive: <reason>" line).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

SCHEMA = "bridge"


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    raise NotImplementedError("expand-only: roll back by restoring the pre-deploy backup")
