"""Backups of schema ``bridge``: consistent dump, verification, restore, off-site copy.

Runs on the HOST (``scripts/ops.sh backup ...``). The host never connects to the
database itself: every database step runs as psql / pg_dump / pg_restore inside
a postgres container (:mod:`dhan_bridge.pgtools`):

* our own database (``DATABASE_URL`` host == ``DB_SERVICE``, i.e. the compose
  service ``postgres``): ``docker compose exec -T postgres ...``
* any other host: ``docker run --rm postgres:<major>-alpine ...`` with the
  server's major version.

Commands::

    now [--record-deploy --previous-tag T --target-tag T] [--prefix P]
    verify <file>             restore into a throwaway plain postgres, compare fingerprints
    restore <file> [--force]  replace schema bridge (refuses while the bridge runs)
    offsite <file>            copy to every target in config/backup-targets.yaml
    rollback-plan             KEY=VALUE lines for remote-deploy.sh rollback
    prune [--keep N]          keep the newest N dumps (default BACKUP_KEEP=7)
    latest                    newest dump path

``now`` writes ``<name>.dump`` and ``<name>.dump.fingerprint.json``. The
fingerprint is taken INSIDE the dump's snapshot: one REPEATABLE READ, READ ONLY
transaction exports its snapshot, computes the fingerprint and runs
``pg_dump -Fc --schema=bridge --snapshot=<id>`` while it stays open.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from dhan_bridge.config import DatabaseSecrets, Settings, read_db_secrets_file
from dhan_bridge.dbverify import compare
from dhan_bridge.models import SCHEMA
from dhan_bridge.pgtools import (
    DockerHost,
    PgRunner,
    ThrowawayPostgres,
    alembic_revision,
    fingerprint,
    psql,
    restore,
    runner_for,
    schema_info,
)
from dhan_bridge.pgtools import (
    consistent_dump as tools_dump,
)

log = logging.getLogger(__name__)

LAST_DEPLOY = "last-deploy.json"
SIDECAR_SUFFIX = ".fingerprint.json"
EXIT_NO_SCHEMA = 3  # `now`: the database has no bridge schema yet
TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


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


def sidecar_for(dump: Path) -> Path:
    return dump.with_name(dump.name + SIDECAR_SUFFIX)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- dump


def consistent_dump(
    secrets: DatabaseSecrets,
    out_dir: Path,
    settings: Settings,
    *,
    prefix: str = "bridge",
    runner: PgRunner | None = None,
) -> Dump:
    """pg_dump of schema bridge + a fingerprint of exactly the same snapshot."""
    out_dir.mkdir(parents=True, exist_ok=True)
    runner = runner or runner_for(secrets.url, settings)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"{prefix}-{stamp}.dump"
    partial = path.with_name(path.name + ".partial")
    try:
        fp = tools_dump(runner, partial)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(path)
    meta = {
        "dump": path.name,
        "dump_sha256": sha256_file(path),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": secrets.host,
        "tools": runner.label,
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
    if sha256_file(path) != meta.get("dump_sha256"):
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


def prune(backups_dir: Path, keep: int) -> list[Path]:
    dumps = list_dumps(backups_dir)
    doomed = dumps[:-keep] if keep > 0 else dumps
    for path in doomed:
        path.unlink()
        sidecar_for(path).unlink(missing_ok=True)
    return doomed


# --------------------------------------------------------------------------- restore / verify


def restore_into(runner: PgRunner, dump: Dump, *, replace_existing: bool) -> None:
    """Restore ``dump`` with ``pg_restore --clean --if-exists --schema=bridge``.

    The existing schema (if any) is first renamed aside and an empty ``bridge``
    created: a ``--schema`` restore needs the schema to exist (it skips CREATE
    SCHEMA), and tables created after the dump must not survive the restore. The
    restored data must match the dump's fingerprint; on any failure the original
    schema is put back, on success the aside copy is dropped.
    """
    if runner.major < int(dump.fingerprint["server_major"]):
        raise BackupError(
            f"target server {runner.major} is older than the dump's source "
            f"({dump.fingerprint['server_major']})"
        )
    aside = f"{SCHEMA}_pre_restore_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    renamed = False
    if schema_info(runner)["exists"]:
        if not replace_existing:
            raise BackupError(f'schema "{SCHEMA}" already exists on the target')
        psql(runner, f"ALTER SCHEMA {SCHEMA} RENAME TO {aside};")
        renamed = True
    try:
        restore(runner, dump.path, pre_sql=f"CREATE SCHEMA {SCHEMA};")
        diffs = compare(dump.fingerprint, fingerprint(runner), ignore=["server_major"])
        if diffs:
            raise BackupError(
                "restored data does not match the dump's fingerprint: " + "; ".join(diffs[:10])
            )
    except BaseException:
        undo = f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;"
        if renamed:
            undo += f" ALTER SCHEMA {aside} RENAME TO {SCHEMA};"
        psql(runner, undo)
        log.error(
            "restore failed; %s", "original schema put back" if renamed else "target left empty"
        )
        raise
    if renamed:
        psql(runner, f"DROP SCHEMA {aside} CASCADE;")


def verify_dump(dump: Dump, settings: Settings) -> list[str]:
    """Restore into a throwaway plain postgres:<major>-alpine and compare fingerprints."""
    major = int(dump.fingerprint["server_major"])
    with ThrowawayPostgres(major, settings.pg_tools_image) as runner:
        restore(runner, dump.path, pre_sql=f"CREATE SCHEMA {SCHEMA};")
        return compare(dump.fingerprint, fingerprint(runner))


# --------------------------------------------------------------------------- off-site


@dataclass(frozen=True)
class Target:
    name: str
    kind: str
    host: str = ""
    path: str = ""
    ssh_key: str = ""
    remote: str = ""


def load_targets(path: Path) -> list[Target]:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    targets: list[Target] = []
    for i, item in enumerate(data.get("targets") or []):
        kind = str(item.get("type", ""))
        name = str(item.get("name") or f"target-{i}")
        if kind == "rsync":
            if not item.get("host") or not item.get("path"):
                raise BackupError(f"{path}: rsync target {name!r} needs host and path")
            targets.append(
                Target(
                    name, kind, str(item["host"]), str(item["path"]), str(item.get("ssh_key", ""))
                )
            )
        elif kind == "rclone":
            if not item.get("remote"):
                raise BackupError(f"{path}: rclone target {name!r} needs remote")
            targets.append(Target(name, kind, remote=str(item["remote"])))
        else:
            raise BackupError(f"{path}: target {name!r} has unknown type {kind!r} (rsync|rclone)")
    return targets


def copy_offsite(files: list[Path], targets: list[Target]) -> list[str]:
    """Copy ``files`` to every target. Returns the names of targets that failed."""
    failed: list[str] = []
    for target in targets:
        if target.kind == "rsync":
            ssh = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
            if target.ssh_key:
                ssh += f" -i {target.ssh_key}"
            cmd = ["rsync", "-t", "--partial", "-e", ssh, *map(str, files),
                   f"{target.host}:{target.path.rstrip('/')}/"]  # fmt: skip
            cmds = [cmd]
        else:
            cmds = [["rclone", "copy", "--no-traverse", str(f), target.remote] for f in files]
        for cmd in cmds:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
            if proc.returncode != 0:
                log.error("off-site copy to %s failed: %s", target.name, proc.stderr.strip()[-500:])
                failed.append(target.name)
                break
        else:
            log.info("copied %d file(s) to %s (%s)", len(files), target.name, target.kind)
    return failed


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


def rollback_plan(runner: PgRunner, backups_dir: Path) -> RollbackPlan:
    path = backups_dir / LAST_DEPLOY
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BackupError(f"{path} not found: no deploy was recorded, cannot roll back") from None
    previous = str(data.get("previous_tag", ""))
    backup = str(data.get("backup", ""))
    if not TAG_RE.match(previous) or not backup or "/" in backup:
        raise BackupError(f"{path} is malformed")
    current = alembic_revision(runner) or ""
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
    off = sub.add_parser("offsite", help="copy a dump to every configured off-site target")
    off.add_argument("file")
    off.add_argument("--targets", type=Path, help="default: BACKUP_TARGETS_FILE")
    sub.add_parser("rollback-plan", help="KEY=VALUE lines for remote-deploy.sh rollback")
    pr = sub.add_parser("prune")
    pr.add_argument("--keep", type=int)
    sub.add_parser("latest")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()
    backups_dir = settings.backups_dir
    host = host or DockerHost(settings.bridge_container)
    try:
        if args.command == "latest":
            dumps = list_dumps(backups_dir)
            if not dumps:
                raise BackupError(f"no dumps in {backups_dir}")
            print(dumps[-1])
            return 0
        if args.command == "prune":
            for path in prune(backups_dir, args.keep or settings.backup_keep):
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
        if args.command == "offsite":
            dump = load_dump(resolve_dump(args.file, backups_dir))
            targets = load_targets(args.targets or settings.backup_targets_file)
            if not targets:
                print("WARNING: no off-site targets configured; the dump stays on this host")
                return 0
            files = [dump.path, sidecar_for(dump.path)]
            if (backups_dir / LAST_DEPLOY).is_file():
                files.append(backups_dir / LAST_DEPLOY)
            failed = copy_offsite(files, targets)
            print(f"off-site: {len(targets) - len(failed)}/{len(targets)} target(s) ok")
            return 1 if failed else 0

        secrets = read_db_secrets_file(settings.db_secrets_file)
        runner = runner_for(secrets.url, settings)
        if args.command == "now":
            if args.record_deploy and (not args.previous_tag or not args.target_tag):
                parser.error("--record-deploy needs --previous-tag and --target-tag")
            if not schema_info(runner)["exists"]:
                print(f'database has no schema "{SCHEMA}" yet: nothing to back up')
                if args.record_deploy:  # never leave a stale record for a later rollback
                    (backups_dir / LAST_DEPLOY).unlink(missing_ok=True)
                return EXIT_NO_SCHEMA
            dump = consistent_dump(
                secrets, backups_dir, settings, prefix=args.prefix, runner=runner
            )
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
            if schema_info(runner)["exists"]:
                safety = consistent_dump(
                    secrets, backups_dir, settings, prefix="pre-restore", runner=runner
                )
                print(f"safety backup of the current data: {safety.path.name}")
            restore_into(runner, dump, replace_existing=True)
            print(f"restored {dump.path.name}; fingerprint verified")
        else:
            plan = rollback_plan(runner, backups_dir)
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
