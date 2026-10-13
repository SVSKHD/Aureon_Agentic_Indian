"""Database engine, Alembic integration and outage-tolerant access.

* SQLAlchemy 2 + psycopg 3, a small sync pool (3 + 2 overflow, pre-ping,
  10 s connect timeout). The URL comes ONLY from ``db.env`` (``DATABASE_URL``, a
  libpq ``postgresql://`` URI); ``sslmode`` defaults to ``DB_SSLMODE`` (prefer).
  Any PostgreSQL >= 16 host works unchanged: the compose ``postgres`` service by
  default, another host after ``scripts/db-move.sh``.
* Every connection runs ``SET search_path TO bridge`` on connect (nothing is ever
  created in ``public``). Needs a real session: a transaction-mode pooler
  (pgbouncer pool_mode=transaction) in front of the server is not supported.
* :class:`Database` survives outages after startup: ``ping()`` never raises and
  :func:`retry` re-runs failed background writes with exponential backoff.

CLI (inside the bridge image)::

    python -m dhan_bridge.db upgrade | current | heads
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from dhan_bridge.config import DatabaseSecrets, mask_url
from dhan_bridge.models import SCHEMA

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
POOL_SIZE = 3
MAX_OVERFLOW = 2
CONNECT_TIMEOUT_S = 10
PING_TIMEOUT_S = 3.0
T = TypeVar("T")


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached (outage, network, bad credentials)."""


# --------------------------------------------------------------------------- engine


def sqlalchemy_url(raw: str, default_sslmode: str = "require") -> str:
    """libpq URI -> SQLAlchemy psycopg URL, adding sslmode/connect_timeout if absent."""
    parts = urlsplit(raw)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("sslmode", default_sslmode)
    query.setdefault("connect_timeout", str(CONNECT_TIMEOUT_S))
    query.setdefault("application_name", "dhan_bridge")
    return urlunsplit(parts._replace(scheme="postgresql+psycopg", query=urlencode(query)))


def libpq_url(raw: str, default_sslmode: str = "require") -> str:
    """The same URL in the form pg_dump/psql understand (no driver suffix)."""
    return sqlalchemy_url(raw, default_sslmode).replace("postgresql+psycopg://", "postgresql://", 1)


def make_engine(raw_url: str, sslmode: str = "require") -> Engine:
    url = make_url(sqlalchemy_url(raw_url, sslmode))
    engine = create_engine(
        url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_timeout=10,
    )

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_conn: Any, _record: Any) -> None:
        # psycopg 3 starts a transaction implicitly; commit so the SET survives pooling.
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
        dbapi_conn.commit()

    return engine


def server_major(conn: Connection) -> int:
    return int(conn.execute(text("SHOW server_version_num")).scalar_one()) // 10_000


# --------------------------------------------------------------------------- alembic


def alembic_config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None, "no alembic revisions found"
    return head


def current_revision(conn: Connection) -> str | None:
    ctx = MigrationContext.configure(conn, opts={"version_table_schema": SCHEMA})
    return ctx.get_current_revision()


def upgrade(engine: Engine, target: str = "head") -> None:
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), target)


# --------------------------------------------------------------------------- resilience


def retry(
    fn: Callable[[], T],
    *,
    what: str,
    attempts: int = 5,
    base_delay_s: float = 1.0,
    max_delay_s: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``fn``; on database errors log and retry with exponential backoff."""
    delay = base_delay_s
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (SQLAlchemyError, DatabaseUnavailable) as exc:
            if attempt == attempts:
                log.error("%s failed after %d attempts: %s", what, attempts, _short(exc))
                raise
            log.warning(
                "%s failed (attempt %d/%d): %s; retrying in %.0fs",
                what,
                attempt,
                attempts,
                _short(exc),
                delay,
            )
            sleep(delay)
            delay = min(delay * 2, max_delay_s)
    raise AssertionError("unreachable")


def _short(exc: BaseException) -> str:
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        return (
            f"{type(exc.orig).__name__}: {str(exc.orig).splitlines()[0] if str(exc.orig) else ''}"
        )
    return f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"


class Database:
    """Owns the engine; swaps it when db.env changes; never raises from ``ping``."""

    def __init__(self, secrets: DatabaseSecrets, sslmode: str = "require") -> None:
        self._sslmode = sslmode
        self._lock = threading.Lock()
        self._secrets = secrets
        self._engine = make_engine(secrets.url, sslmode)
        self.head = head_revision()

    def __repr__(self) -> str:
        return f"Database({mask_url(self._secrets.url)})"

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def secrets(self) -> DatabaseSecrets:
        return self._secrets

    def swap(self, secrets: DatabaseSecrets) -> None:
        """Point at new credentials/host (db.env rotated). Old pool is disposed."""
        with self._lock:
            old = self._engine
            self._engine = make_engine(secrets.url, self._sslmode)
            self._secrets = secrets
        old.dispose()
        log.info("database engine switched to %s", mask_url(secrets.url))

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        try:
            with self._engine.begin() as conn:
                yield conn
        except DBAPIError as exc:
            if exc.connection_invalidated or _is_connectivity(exc):
                raise DatabaseUnavailable(_short(exc)) from exc
            raise

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        try:
            with self._engine.connect() as conn:
                yield conn
        except DBAPIError as exc:
            if exc.connection_invalidated or _is_connectivity(exc):
                raise DatabaseUnavailable(_short(exc)) from exc
            raise

    def migrate(self) -> list[str]:
        """Apply pending revisions. Returns the revisions that were pending."""
        with self._engine.connect() as conn:
            before = current_revision(conn)
        upgrade(self._engine)
        if before == self.head:
            return []
        log.info("database migrated: %s -> %s", before, self.head)
        return [self.head]

    def status(self) -> tuple[bool, bool]:
        """(reachable, at_head). Blocking; call through ``ping`` from async code."""
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SET LOCAL statement_timeout = 3000"))
                conn.execute(text("SELECT 1"))
                return True, current_revision(conn) == self.head
        except SQLAlchemyError as exc:
            log.warning("database ping failed: %s", _short(exc))
            return False, False

    def ok(self) -> bool:
        reachable, at_head = self.status()
        return reachable and at_head

    def dispose(self) -> None:
        self._engine.dispose()


def _is_connectivity(exc: DBAPIError) -> bool:
    import psycopg

    return isinstance(exc.orig, psycopg.OperationalError)


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dhan_bridge.db")
    parser.add_argument("command", choices=["upgrade", "current", "heads"])
    args = parser.parse_args(argv)
    if args.command == "heads":
        print(head_revision())
        return 0

    from dhan_bridge.config import Settings, read_db_secrets_file

    settings = Settings()
    db = Database(read_db_secrets_file(settings.db_secrets_file), settings.db_sslmode)
    try:
        if args.command == "upgrade":
            db.migrate()
        with db.connect() as conn:
            print(current_revision(conn) or "<empty>")
    finally:
        db.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
