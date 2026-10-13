"""Backups of schema ``bridge``: consistent dump, verification, restore, deploy records.

Runs on the deploy HOST (via ``deploy/ops.sh backup ...``), not in the bridge
container: pg_dump/pg_restore run in ``postgres:<major>-alpine`` containers
(:mod:`dhan_bridge.pgtools`), with <major> taken from the server.

Commands::

    now [--record-deploy --previous-tag T --target-tag T] [--prefix P]
    verify <file>             restore into a throwaway plain postgres, compare fingerprints
    restore <file> [--force]  replace schema bridge (refuses while the bridge runs)
    rollback-plan             KEY=VALUE lines for remote-deploy.sh rollback
    prune [--keep 30]
    latest                    newest dump path

Every dump ``<name>.dump`` has a sidecar ``<name>.dump.fingerprint.json`` taken
INSIDE the dump's snapshot: a REPEATABLE READ, READ ONLY transaction exports its
snapshot (``pg_export_snapshot()``), the fingerprint is computed in that
transaction, and ``pg_dump --snapshot=<id>`` dumps exactly the same data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from dhan_bridge.config import DatabaseSecrets, Settings, read_db_secrets_file
from dhan_bridge.db import libpq_url, make_engine, server_major
from dhan_bridge.dbverify import alembic_revision, compare, fingerprint
from dhan_bridge.models import SCHEMA
from dhan_bridge.pgtools import DockerHost, PgTools, ThrowawayPostgres

log = logging.getLogger(__name__)

LAST_DEPLOY = "last-deploy.json"
SIDECAR_SUFFIX = ".fingerprint.json"
DEFAULT_KEEP = 30
EXIT_NO_SCHEMA = 3  # `now`: the database has no bridge schema yet
TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
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


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Dump:
    path: Path
    meta: dict[str, Any]

    @property
    def fingerprint(self) -> dict[str, Any]:
        fp: dict[str, Any] = self.meta["fingerprint"]
        return fp

    @property
    def sidecar(self) -> Path:
        return sidecar_for(self.path)


def sidecar_for(dump: Path) -> Path:
    return dump.with_name(dump.name + SIDECAR_SUFFIX)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tools_for(engine: Engine, settings: Settings) -> PgTools:
    with engine.connect() as conn:
        major = server_major(conn)
    return PgTools(major, settings.pg_tools_image, settings.pg_tools_network)


# --------------------------------------------------------------------------- dump


def consistent_dump(
    secrets: DatabaseSecrets,
    out_dir: Path,
    settings: Settings,
    *,
    prefix: str = "bridge",
) -> Dump:
    """pg_dump of schema bridge + a fingerprint of exactly the same snapshot."""
    out_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(secrets.url, settings.db_sslmode)
    try:
        tools = tools_for(engine, settings)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = out_dir / f"{prefix}-{stamp}.dump"
        partial = path.with_name(path.name + ".partial")
        with engine.connect() as conn:
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
            with conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                snapshot = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
                fp = fingerprint(conn)
                with partial.open("wb") as handle:
                    tools.run(
                        "pg_dump",
                        ["-Fc", f"--schema={SCHEMA}", f"--snapshot={snapshot}"],
                        libpq_url(secrets.url, settings.db_sslmode),
                        stdout=handle,
                    )
        partial.replace(path)
    finally:
        engine.dispose()
    meta = {
        "dump": path.name,
        "dump_sha256": sha256_file(path),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": secrets.host,
        "fingerprint": fp,
    }
    sidecar_for(path).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    log.info(
        "dump %s (%d bytes, revision %s)", path.name, path.stat().st_size, fp["alembic_revision"]
    )
    return Dump(path, meta)


def load_dump(path: Path) -> Dump:
    sidecar = sidecar_for(path)
    if not path.is_file():
        raise BackupError(f"{path} does not exist")
    if not sidecar.is_file():
        raise BackupError(f"{sidecar.name} is missing: cannot verify {path.name}")
    meta = json.loads(sidecar.read_text())
    actual = sha256_file(path)
    if actual != meta.get("dump_sha256"):
        raise BackupError(f"{path.name}: sha256 mismatch (file changed or truncated)")
    return Dump(path, meta)


def resolve_dump(name: str, backups_dir: Path) -> Path:
    path = Path(name)
    if path.is_file():
        return path
    candidate = backups_dir / path.name
    if candidate.is_file():
        return candidate
    raise BackupError(f"backup {name} not found (looked in {backups_dir})")


def list_dumps(backups_dir: Path) -> list[Path]:
    return sorted(backups_dir.glob("*.dump"), key=lambda p: (p.stat().st_mtime, p.name))


def prune(backups_dir: Path, keep: int = DEFAULT_KEEP) -> list[Path]:
    dumps_ = list_dumps(backups_dir)
    doomed = dumps_[:-keep] if keep > 0 else dumps_
    for path in doomed:
        path.unlink()
        sidecar_for(path).unlink(missing_ok=True)
    return doomed


# --------------------------------------------------------------------------- restore


def schema_exists(url: str, settings: Settings) -> bool:
    engine = make_engine(url, settings.db_sslmode)
    try:
        with engine.connect() as conn:
            return bool(
                conn.execute(
                    text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
                ).scalar()
            )
    finally:
        engine.dispose()


def restore_into(
    dump: Dump, target_url: str, settings: Settings, *, replace_existing: bool
) -> list[str]:
    """Restore ``dump`` into ``target_url`` and return fingerprint differences.

    The existing schema (if any) is renamed aside first, so a failed restore can
    be undone and tables created after the dump cannot survive it. On success
    the aside copy is dropped; on any failure the original schema is put back.
    """
    engine = make_engine(target_url, settings.db_sslmode)
    aside = f"{SCHEMA}_pre_restore_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    renamed = False
    try:
        tools = tools_for(engine, settings)
        if tools.major < int(dump.fingerprint["server_major"]):
            raise BackupError(
                f"target server {tools.major} is older than the dump's source "
                f"({dump.fingerprint['server_major']})"
            )
        with engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
            ).scalar()
            if exists:
                if not replace_existing:
                    raise BackupError(f'schema "{SCHEMA}" already exists on the target')
                conn.execute(text(f"ALTER SCHEMA {SCHEMA} RENAME TO {aside}"))
                renamed = True
        try:
            with dump.path.open("rb") as handle:
                tools.run(
                    "pg_restore",
                    ["--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction"],
                    libpq_url(target_url, settings.db_sslmode),
                    stdin=handle,
                )
            with engine.begin() as conn:
                conn.execute(text(ENABLE_RLS_ON_ALL))
            with engine.connect() as conn:
                diffs = compare(dump.fingerprint, fingerprint(conn), ignore=["server_major"])
            if diffs:
                raise BackupError(
                    "restored data does not match the dump's fingerprint: " + "; ".join(diffs[:10])
                )
        except BaseException:
            if renamed:
                with engine.begin() as conn:
                    conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
                    conn.execute(text(f"ALTER SCHEMA {aside} RENAME TO {SCHEMA}"))
                log.error("restore failed; original schema put back")
            raise
        if renamed:
            with engine.begin() as conn:
                conn.execute(text(f"DROP SCHEMA {aside} CASCADE"))
        return diffs
    finally:
        engine.dispose()


def verify_dump(dump: Dump, settings: Settings) -> list[str]:
    """Restore into a throwaway plain postgres:<major>-alpine and compare fingerprints."""
    major = int(dump.fingerprint["server_major"])
    with ThrowawayPostgres(major, settings.pg_tools_image) as url:
        engine = make_engine(url, "disable")
        try:
            tools = PgTools(major, settings.pg_tools_image, settings.pg_tools_network)
            with dump.path.open("rb") as handle:
                tools.run(
                    "pg_restore",
                    ["--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction"],
                    libpq_url(url, "disable"),
                    stdin=handle,
                )
            with engine.connect() as conn:
                return compare(dump.fingerprint, fingerprint(conn))
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- deploy record


def record_deploy(backups_dir: Path, *, dump: Dump, previous_tag: str, target_tag: str) -> Path:
    for tag in (previous_tag, target_tag):
        if not TAG_RE.match(tag):
            raise BackupError(f"invalid image tag {tag!r}")
    payload = {
        "previous_tag": previous_tag,
        "target_tag": target_tag,
        "alembic_revision": dump.fingerprint["alembic_revision"],
        "backup": dump.path.name,
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
    recorded_revision: str
    current_revision: str
    backup: str

    @property
    def needs_restore(self) -> bool:
        return self.current_revision != self.recorded_revision


def rollback_plan(secrets: DatabaseSecrets, backups_dir: Path, settings: Settings) -> RollbackPlan:
    path = backups_dir / LAST_DEPLOY
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BackupError(f"{path} not found: no deploy was recorded, cannot roll back") from None
    previous = str(data.get("previous_tag", ""))
    backup = str(data.get("backup", ""))
    if not TAG_RE.match(previous) or not backup or "/" in backup:
        raise BackupError(f"{path} is malformed")
    engine = make_engine(secrets.url, settings.db_sslmode)
    try:
        with engine.connect() as conn:
            current = alembic_revision(conn) or ""
    finally:
        engine.dispose()
    plan = RollbackPlan(previous, str(data.get("alembic_revision") or ""), current, backup)
    if plan.needs_restore and not (backups_dir / backup).is_file():
        raise BackupError(f"schema changed but pre-deploy backup {backup} is missing")
    return plan


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None, host: DockerHost | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dhan_bridge.backup")
    sub = parser.add_subparsers(dest="command", required=True)
    now = sub.add_parser("now", help="consistent dump of schema bridge + fingerprint")
    now.add_argument("--record-deploy", action="store_true")
    now.add_argument("--previous-tag")
    now.add_argument("--target-tag")
    now.add_argument("--prefix", default="bridge")
    ver = sub.add_parser("verify", help="restore into a throwaway postgres and compare")
    ver.add_argument("file")
    res = sub.add_parser("restore", help="replace schema bridge with a dump")
    res.add_argument("file")
    res.add_argument("--force", action="store_true", help="restore even if the bridge runs")
    sub.add_parser("rollback-plan", help="KEY=VALUE lines for remote-deploy.sh rollback")
    pr = sub.add_parser("prune")
    pr.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    sub.add_parser("latest")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()
    backups_dir = settings.backups_dir
    host = host or DockerHost(settings.bridge_container)
    try:
        if args.command == "latest":
            dumps_ = list_dumps(backups_dir)
            if not dumps_:
                raise BackupError(f"no dumps in {backups_dir}")
            print(dumps_[-1])
            return 0
        if args.command == "prune":
            for path in prune(backups_dir, args.keep):
                print(f"pruned {path.name}")
            return 0
        if args.command == "verify":
            dump = load_dump(resolve_dump(args.file, backups_dir))
            diffs = verify_dump(dump, settings)
            for line in diffs:
                print(f"DIFF {line}")
            print(("FAILED: " if diffs else "OK: ") + f"{dump.path.name} verified in a throwaway "
                  f"postgres:{dump.fingerprint['server_major']}")  # fmt: skip
            return 1 if diffs else 0

        secrets = read_db_secrets_file(settings.db_secrets_file)
        if args.command == "now":
            if args.record_deploy and (not args.previous_tag or not args.target_tag):
                parser.error("--record-deploy needs --previous-tag and --target-tag")
            if not schema_exists(secrets.url, settings):
                print(f'database has no schema "{SCHEMA}" yet: nothing to back up')
                if args.record_deploy:  # never leave a stale record for a later rollback
                    (backups_dir / LAST_DEPLOY).unlink(missing_ok=True)
                return EXIT_NO_SCHEMA
            dump = consistent_dump(secrets, backups_dir, settings, prefix=args.prefix)
            print(f"backup={dump.path.name} revision={dump.fingerprint['alembic_revision']}")
            if args.record_deploy:
                record_deploy(
                    backups_dir,
                    dump=dump,
                    previous_tag=args.previous_tag,
                    target_tag=args.target_tag,
                )
                print(f"recorded {backups_dir / LAST_DEPLOY}")
        elif args.command == "restore":
            if host.is_running() and not args.force:
                raise BackupError(
                    f"{host.container} is running; stop it first "
                    "(docker compose stop bridge) or pass --force"
                )
            dump = load_dump(resolve_dump(args.file, backups_dir))
            if schema_exists(secrets.url, settings):
                safety = consistent_dump(secrets, backups_dir, settings, prefix="pre-restore")
                print(f"safety backup of the current data: {safety.path.name}")
            restore_into(dump, secrets.url, settings, replace_existing=True)
            print(f"restored {dump.path.name}; fingerprint verified")
        else:
            plan = rollback_plan(secrets, backups_dir, settings)
            print(f"PREVIOUS_TAG={plan.previous_tag}")
            print(f"RECORDED_REVISION={plan.recorded_revision}")
            print(f"CURRENT_REVISION={plan.current_revision}")
            print(f"BACKUP={plan.backup}")
            print(f"NEEDS_RESTORE={1 if plan.needs_restore else 0}")
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
