"""enable row level security on every bridge table, with no policies

With RLS on and no policies, any role other than the table owner (e.g. Supabase's
anon / authenticated API roles, should the schema ever be exposed) sees nothing.
The bridge connects as the owner, which bypasses RLS. Later revisions must
enable RLS on the tables they create (tests/test_db_schema.py checks).

Revision ID: 0004_enable_rls
Revises: 0003_heartbeat
"""

from __future__ import annotations

from alembic import op

revision = "0004_enable_rls"
down_revision = "0003_heartbeat"
branch_labels = None
depends_on = None


ENABLE_RLS_ON_ALL = """
DO $$
DECLARE t record;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'bridge' LOOP
        EXECUTE format('ALTER TABLE bridge.%I ENABLE ROW LEVEL SECURITY', t.tablename);
    END LOOP;
END
$$
"""


def upgrade() -> None:
    op.execute(ENABLE_RLS_ON_ALL)


def downgrade() -> None:
    raise NotImplementedError("expand-only: roll back by restoring the pre-deploy backup")
