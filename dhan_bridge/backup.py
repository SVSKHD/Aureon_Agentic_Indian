"""SQLite backups, restore and deploy bookkeeping.

CLI (run inside the bridge image so file ownership and paths match)::

    python -m dhan_bridge.backup now [--record-deploy --previous-tag T --target-tag T]
    python -m dhan_bridge.backup verify <file>
    python -m dhan_bridge.backup restore <file> [--force]
    python -m dhan_bridge.backup rollback-plan

Backups use SQLite's online backup API, so ``now`` is safe while the app runs.
``restore`` refuses while the app is running (it holds ``data/bridge.lock``)
unless ``--force`` is given. ``<file>`` may be a path or a name inside
``data/backups/``.

``now --record-deploy`` also writes ``data/backups/last-deploy.json``::

    {"previous_tag": ..., "target_tag": ..., "schema_version": N,
     "backup": "<file name>", "created_at": ...}

which ``remote-deploy.sh rollback`` consumes through ``rollback-plan``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from dhan_bridge.config import VERSION
from dhan_bridge.migrations import DEFAULT_KEEP_BACKUPS, backup_database, prune_backups

EXIT_NO_DATABASE = 3  # `now`: nothing to back up yet (the app never created its db)
LOCK_NAME = "bridge.lock"
LAST_DEPLOY = "last-deploy.json"
TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class BackupError(RuntimeError):
    pass


# --------------------------------------------------------------------------- instance lock


def acquire_instance_lock(data_dir: Path) -> IO[str]:
    """Hold an exclusive lock for the app's lifetime. Raises if another instance holds it.

    Works across containers sharing the data volume (same kernel, same inode).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = (data_dir / LOCK_NAME).open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise BackupError(
            f"another dhan_bridge instance holds {data_dir / LOCK_NAME}; "
            "only one instance may run per data directory"
        ) from None
    return handle


def is_app_running(data_dir: Path) -> bool:
    lock_path = data_dir / LOCK_NAME
    if not lock_path.exists():
        return False
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


# --------------------------------------------------------------------------- primitives


def schema_version(db_path: Path) -> int:
    """Highest applied migration version (0 for a missing or unmigrated database)."""
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()
    return int(row[0]) if row and row[0] is not None else 0


def verify(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"{path} does not exist"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return False, f"not a usable SQLite database: {exc}"
    result = "; ".join(str(r[0]) for r in rows)
    return result == "ok", result


def take_backup(db_path: Path, backups_dir: Path, keep: int = DEFAULT_KEEP_BACKUPS) -> Path:
    if not db_path.exists():
        raise BackupError(f"database {db_path} does not exist")
    target = backup_database(db_path, backups_dir, VERSION)
    prune_backups(backups_dir, keep)
    return target


def resolve_backup(name: str, backups_dir: Path) -> Path:
    path = Path(name)
    if path.is_file():
        return path
    candidate = backups_dir / path.name
    if candidate.is_file():
        return candidate
    raise BackupError(f"backup {name} not found (looked in {backups_dir})")


def restore(
    source: Path, db_path: Path, backups_dir: Path, data_dir: Path, force: bool = False
) -> Path | None:
    """Replace the live database with ``source``. Returns the safety copy of the old db."""
    if is_app_running(data_dir) and not force:
        raise BackupError(
            "dhan_bridge is running; stop it first (docker compose stop bridge) or pass --force"
        )
    ok, detail = verify(source)
    if not ok:
        raise BackupError(f"refusing to restore {source}: integrity check failed: {detail}")
    safety = take_backup(db_path, backups_dir) if db_path.exists() else None
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(db_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return safety


def record_deploy(
    backups_dir: Path, *, backup: Path, previous_tag: str, target_tag: str, version: int
) -> Path:
    for tag in (previous_tag, target_tag):
        if not TAG_RE.match(tag):
            raise BackupError(f"invalid image tag {tag!r}")
    payload = {
        "previous_tag": previous_tag,
        "target_tag": target_tag,
        "schema_version": version,
        "backup": backup.name,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    target = backups_dir / LAST_DEPLOY
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


@dataclass(frozen=True)
class RollbackPlan:
    previous_tag: str
    recorded_schema: int
    current_schema: int
    backup: str

    @property
    def needs_restore(self) -> bool:
        return self.current_schema != self.recorded_schema


def rollback_plan(db_path: Path, backups_dir: Path) -> RollbackPlan:
    path = backups_dir / LAST_DEPLOY
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BackupError(f"{path} not found: no deploy was recorded, cannot roll back") from None
    previous = str(data.get("previous_tag", ""))
    backup = str(data.get("backup", ""))
    if not TAG_RE.match(previous) or not backup or "/" in backup:
        raise BackupError(f"{path} is malformed")
    plan = RollbackPlan(previous, int(data["schema_version"]), schema_version(db_path), backup)
    if plan.needs_restore and not (backups_dir / backup).is_file():
        raise BackupError(f"schema changed but pre-deploy backup {backup} is missing")
    return plan


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dhan_bridge.backup")
    sub = parser.add_subparsers(dest="command", required=True)
    now = sub.add_parser("now", help="online backup of the live database")
    now.add_argument("--record-deploy", action="store_true")
    now.add_argument("--previous-tag")
    now.add_argument("--target-tag")
    ver = sub.add_parser("verify", help="PRAGMA integrity_check on a backup")
    ver.add_argument("file")
    res = sub.add_parser("restore", help="replace the live database with a backup")
    res.add_argument("file")
    res.add_argument("--force", action="store_true", help="restore even if the app is running")
    sub.add_parser("rollback-plan", help="print KEY=VALUE lines for remote-deploy.sh rollback")
    args = parser.parse_args(argv)

    from dhan_bridge.config import Settings

    settings = Settings()
    db_path, backups_dir, data_dir = settings.db_path, settings.backups_dir, settings.data_dir
    try:
        if args.command == "now":
            if not db_path.exists():
                print(f"no database at {db_path} yet: nothing to back up")
                if args.record_deploy:
                    # never leave a stale record that a later rollback could act on
                    (backups_dir / LAST_DEPLOY).unlink(missing_ok=True)
                return EXIT_NO_DATABASE
            path = take_backup(db_path, backups_dir)
            version = schema_version(path)
            print(f"backup={path.name} schema_version={version}")
            if args.record_deploy:
                if not args.previous_tag or not args.target_tag:
                    parser.error("--record-deploy needs --previous-tag and --target-tag")
                record_deploy(
                    backups_dir,
                    backup=path,
                    previous_tag=args.previous_tag,
                    target_tag=args.target_tag,
                    version=version,
                )
                print(f"recorded {backups_dir / LAST_DEPLOY}")
        elif args.command == "verify":
            ok, detail = verify(resolve_backup(args.file, backups_dir))
            print(("OK: " if ok else "CORRUPT: ") + detail)
            return 0 if ok else 1
        elif args.command == "restore":
            source = resolve_backup(args.file, backups_dir)
            safety = restore(source, db_path, backups_dir, data_dir, force=args.force)
            print(f"restored {source.name} -> {db_path}")
            if safety:
                print(f"previous database saved as {safety.name}")
        else:
            plan = rollback_plan(db_path, backups_dir)
            print(f"PREVIOUS_TAG={plan.previous_tag}")
            print(f"RECORDED_SCHEMA={plan.recorded_schema}")
            print(f"CURRENT_SCHEMA={plan.current_schema}")
            print(f"BACKUP={plan.backup}")
            print(f"NEEDS_RESTORE={1 if plan.needs_restore else 0}")
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
