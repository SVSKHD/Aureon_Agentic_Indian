from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from dhan_bridge import migrations as m

V1 = m.Migration(1, "initial", "CREATE TABLE a (x INTEGER);")
V2 = m.Migration(
    2, "add_b", "CREATE TABLE b (y TEXT DEFAULT 'semi;colon');\nINSERT INTO b VALUES ('q');"
)


def tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_shipped_migrations_apply_cleanly(tmp_path: Path) -> None:
    db = tmp_path / "bridge.db"
    result = m.migrate(db, tmp_path / "backups", "0.1.0")
    assert result.applied == [mig.version for mig in m.discover()]
    assert result.backup is None  # fresh database: nothing to back up
    assert {"meta", "instruments", "schema_migrations"} <= tables(db)
    assert m.migrate(db, tmp_path / "backups", "0.1.0").applied == []


def test_backup_is_taken_before_applying(tmp_path: Path) -> None:
    db = tmp_path / "bridge.db"
    backups = tmp_path / "backups"
    m.migrate(db, backups, "0.1.0", migrations=[V1])
    result = m.migrate(db, backups, "0.2.0", migrations=[V1, V2])

    assert result.applied == [2]
    assert result.backup is not None
    assert result.backup.parent == backups
    assert result.backup.name.startswith("0.2.0-")
    assert result.backup.suffix == ".db"
    # the backup holds the pre-migration schema; the live db has the new one
    assert "b" not in tables(result.backup)
    assert "b" in tables(db)


def test_no_backup_when_nothing_pending(tmp_path: Path) -> None:
    db = tmp_path / "bridge.db"
    m.migrate(db, tmp_path / "backups", "0.1.0", migrations=[V1])
    assert m.migrate(db, tmp_path / "backups", "0.1.0", migrations=[V1]).backup is None
    assert not (tmp_path / "backups").exists()


def test_keeps_only_last_30_backups(tmp_path: Path) -> None:
    db = tmp_path / "bridge.db"
    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(35):
        (backups / f"0.0.{i}-20260101T0000{i:02d}.000000Z.db").write_bytes(b"")
    m.migrate(db, backups, "0.1.0", migrations=[V1])
    result = m.migrate(db, backups, "0.2.0", migrations=[V1, V2])

    remaining = sorted(p.name for p in backups.glob("*.db"))
    assert len(remaining) == 30
    assert result.backup is not None
    assert result.backup.name in remaining
    # the six oldest (by timestamp) were pruned
    for i in range(6):
        assert f"0.0.{i}-20260101T0000{i:02d}.000000Z.db" not in remaining


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    db = tmp_path / "bridge.db"
    bad = m.Migration(2, "bad", "CREATE TABLE c (z INTEGER);\nINSERT INTO nope VALUES (1);")
    m.migrate(db, tmp_path / "backups", "0.1.0", migrations=[V1])
    with pytest.raises(sqlite3.OperationalError):
        m.migrate(db, tmp_path / "backups", "0.2.0", migrations=[V1, bad])
    assert "c" not in tables(db)
    conn = sqlite3.connect(db)
    assert m.applied_versions(conn) == {1}
    conn.close()


def test_discover_validates_filenames(tmp_path: Path) -> None:
    (tmp_path / "0001_ok.sql").write_text("SELECT 1;", encoding="utf-8")
    assert [x.name for x in m.discover(tmp_path)] == ["ok"]
    (tmp_path / "bad-name.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="bad migration filename"):
        m.discover(tmp_path)


def test_discover_rejects_duplicates(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0001_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        m.discover(tmp_path)


def test_incomplete_statement_rejected() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        m._split_statements("CREATE TABLE x (a INTEGER)")


# --------------------------------------------------------------------------- expand-only rule

DESTRUCTIVE = re.compile(r"\b(DROP|RENAME)\b", re.IGNORECASE)
ALLOW = re.compile(r"^\s*--\s*allow-destructive:\s*\S", re.IGNORECASE | re.MULTILINE)
MIGRATIONS_DIR = Path(m.__file__).parent


def destructive_violations(directory: Path) -> list[str]:
    """Migrations must be expand-only so the previous release keeps working after a rollback."""
    bad: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        code = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
        if DESTRUCTIVE.search(code) and not ALLOW.search(sql):
            bad.append(path.name)
    return bad


def test_shipped_migrations_are_expand_only() -> None:
    assert destructive_violations(MIGRATIONS_DIR) == [], (
        "DROP/RENAME in a migration breaks rollback to the previous release. "
        "If unavoidable, add a line '-- allow-destructive: <reason>' (see README 'Migrations')."
    )


@pytest.mark.parametrize(
    ("sql", "violates"),
    [
        ("DROP TABLE a;", True),
        ("ALTER TABLE a RENAME COLUMN x TO y;", True),
        ("alter table a rename to b;", True),
        ("DROP INDEX i;\n-- allow-destructive:\n", True),  # reason is mandatory
        ("-- allow-destructive: table unused since 0.3.0\nDROP TABLE old;", False),
        ("-- we never DROP anything here\nCREATE TABLE a (x INTEGER);", False),
        ("ALTER TABLE a ADD COLUMN dropped_at TEXT;", False),
    ],
)
def test_destructive_checker(tmp_path: Path, sql: str, violates: bool) -> None:
    (tmp_path / "0001_x.sql").write_text(sql, encoding="utf-8")
    assert bool(destructive_violations(tmp_path)) is violates
