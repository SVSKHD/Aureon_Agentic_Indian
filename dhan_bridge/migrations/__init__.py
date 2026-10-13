"""Versioned SQLite migrations.

Migrations are ``NNNN_description.sql`` files in this package, applied in
numeric order and recorded in ``schema_migrations``. Before any pending
migration is applied to an existing database, the database is copied to
``<backups_dir>/<app_version>-<UTC timestamp>.db`` using SQLite's online backup
API; only the newest ``keep`` backups are retained.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
BACKUP_TS_FORMAT = "%Y%m%dT%H%M%S.%fZ"
DEFAULT_KEEP_BACKUPS = 30


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


@dataclass(frozen=True)
class MigrationResult:
    applied: list[int]
    backup: Path | None


def discover(directory: Path | None = None) -> list[Migration]:
    """Find migrations in ``directory`` (default: this package)."""
    found: list[Migration] = []
    if directory is None:
        entries = [
            (entry.name, entry.read_text(encoding="utf-8"))
            for entry in resources.files(__package__).iterdir()
            if entry.name.endswith(".sql")
        ]
    else:
        entries = [(p.name, p.read_text(encoding="utf-8")) for p in directory.glob("*.sql")]
    for filename, sql in entries:
        match = MIGRATION_RE.match(filename)
        if not match:
            raise ValueError(f"bad migration filename: {filename}")
        found.append(Migration(int(match.group(1)), match.group(2), sql))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration versions: {versions}")
    return found


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_table(conn)
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def backup_database(db_path: Path, backups_dir: Path, app_version: str) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime(BACKUP_TS_FORMAT)
    target = backups_dir / f"{app_version}-{stamp}.db"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    log.info("database backed up to %s", target)
    return target


def _backup_sort_key(path: Path) -> tuple[str, str]:
    _, _, stamp = path.stem.rpartition("-")
    return (stamp, path.name)


def prune_backups(backups_dir: Path, keep: int = DEFAULT_KEEP_BACKUPS) -> list[Path]:
    """Delete all but the newest ``keep`` backups. Returns the deleted paths."""
    backups = sorted(backups_dir.glob("*.db"), key=_backup_sort_key)
    doomed = backups[:-keep] if keep > 0 else backups
    for path in doomed:
        path.unlink()
    return doomed


def migrate(
    db_path: Path,
    backups_dir: Path,
    app_version: str,
    keep_backups: int = DEFAULT_KEEP_BACKUPS,
    migrations: list[Migration] | None = None,
) -> MigrationResult:
    """Back up (if needed) and apply all pending migrations."""
    migrations = discover() if migrations is None else migrations
    db_path.parent.mkdir(parents=True, exist_ok=True)
    existed = db_path.exists() and db_path.stat().st_size > 0

    conn = sqlite3.connect(db_path)
    try:
        done = applied_versions(conn)
        conn.commit()
    finally:
        conn.close()

    pending = [m for m in migrations if m.version not in done]
    if not pending:
        return MigrationResult(applied=[], backup=None)

    backup = None
    if existed:
        backup = backup_database(db_path, backups_dir, app_version)
        prune_backups(backups_dir, keep_backups)

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        for migration in pending:
            log.info("applying migration %04d_%s", migration.version, migration.name)
            conn.execute("BEGIN")
            try:
                for statement in _split_statements(migration.sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, datetime.now(UTC).isoformat()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
    return MigrationResult(applied=[m.version for m in pending], backup=backup)


def _split_statements(sql: str) -> list[str]:
    """Split a script into complete statements (handles ``;`` inside string literals)."""
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip() and not all(
        ln.strip().startswith("--") or not ln.strip() for ln in buffer.splitlines()
    ):
        raise ValueError("migration ends with an incomplete SQL statement")
    return statements
