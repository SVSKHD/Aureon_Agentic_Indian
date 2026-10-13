"""Alembic environment. The bridge never uses alembic.ini: dhan_bridge.db builds
the Config programmatically and passes a live connection in ``config.attributes``.
Standalone use (dev): ``python -m dhan_bridge.db upgrade``.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import text

from dhan_bridge.models import SCHEMA, metadata

config = context.config


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:  # pragma: no cover - only via `alembic` CLI
        from dhan_bridge.config import Settings, read_db_secrets_file
        from dhan_bridge.db import make_engine

        settings = Settings()
        engine = make_engine(
            read_db_secrets_file(settings.db_secrets_file).url, settings.db_sslmode
        )
        with engine.begin() as conn:
            _run(conn)
        return
    _run(connection)


def _run(connection: object) -> None:
    # The version table lives in "bridge" too, so the schema must exist first.
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))  # type: ignore[attr-defined]
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=metadata,
        version_table_schema=SCHEMA,
        include_schemas=False,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():  # pragma: no cover
    raise SystemExit("offline (--sql) migrations are not supported")
run_migrations_online()
