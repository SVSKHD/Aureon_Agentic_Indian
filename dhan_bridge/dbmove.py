"""Move schema ``bridge`` to another PostgreSQL host with no code change.

Usage (on the deploy host, via ``deploy/db-move.sh``)::

    db-move.sh --target-secrets /path/target-db.env [--dry-run] [--overwrite-target]
               [--force-market-hours --reason "..."]
    db-move.sh --revert

Phases: preflight -> copy -> verify -> switch, each logged to
``data/migrations-log/<timestamp>.json``. The SOURCE database is only ever read
(a READ ONLY snapshot); this tool never modifies or deletes it.

* preflight (nothing changes on failure): market-hours guard; target reachable;
  target major >= source major; target schema "bridge" absent or empty (else
  ``--overwrite-target``); enough local disk for the dump; source != target.
* copy: stop the bridge (not in --dry-run, which dumps a live snapshot), consistent
  dump (backup.consistent_dump), restore into the target, re-enable RLS.
* verify: fingerprint(target) == the dump's fingerprint (server_major excluded:
  the target may be newer) and alembic current == head on the target. On any
  mismatch: restart the bridge on the source, report, exit 1.
* switch (not in --dry-run): db.env -> db.env.previous, atomically mv the target's
  db.env into place, start the bridge, strict health gate (120 s). On failure:
  put db.env.previous back, restart, health gate again, exit 1 (3 if the
  fallback is unhealthy too).

Exit codes: 0 moved (or dry run passed), 1 failed but source restored and healthy,
2 preflight refused (nothing changed), 3 manual action required.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from dhan_bridge.backup import ENABLE_RLS_ON_ALL, consistent_dump, restore_into
from dhan_bridge.config import (
    DatabaseSecrets,
    SecretsError,
    Settings,
    read_db_secrets_file,
)
from dhan_bridge.db import head_revision, make_engine, server_major
from dhan_bridge.dbverify import alembic_revision, compare, fingerprint
from dhan_bridge.models import SCHEMA
from dhan_bridge.pgtools import DockerHost

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAILED_RESTORED, EXIT_PREFLIGHT, EXIT_MANUAL = 0, 1, 2, 3
HEALTH_TIMEOUT_S = 120.0
DISK_MARGIN_BYTES = 64 * 1024 * 1024

Guard = Callable[[], int]


class PreflightError(RuntimeError):
    pass


def calendar_guard() -> int:
    """``python -m dhan_bridge.calendar deploy-guard``: 0 allowed, 3 blocked."""
    return subprocess.run(
        [sys.executable, "-m", "dhan_bridge.calendar", "deploy-guard"], check=False
    ).returncode


@dataclass
class MoveOptions:
    target_secrets: Path
    dry_run: bool = False
    force_market_hours: bool = False
    reason: str = ""
    overwrite_target: bool = False


@dataclass
class MoveLog:
    started: float = field(default_factory=time.monotonic)
    record: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)

    def step(self, name: str, **info: Any) -> None:
        entry = {"step": name, "at": datetime.now(UTC).isoformat(timespec="seconds"), **info}
        self.steps.append(entry)
        log.info("%s %s", name, json.dumps(info, default=str) if info else "")

    def write(self, directory: Path, result: str, **extra: Any) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{stamp}.json"
        payload = {
            **self.record,
            **extra,
            "result": result,
            "duration_s": round(time.monotonic() - self.started, 1),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "steps": self.steps,
        }
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        return path


# --------------------------------------------------------------------------- preflight


def _server_info(secrets: DatabaseSecrets, sslmode: str) -> dict[str, Any]:
    engine = make_engine(secrets.url, sslmode)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            tables = conn.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = :s"
                ),
                {"s": SCHEMA},
            ).scalar_one()
            schema = conn.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
            ).scalar()
            size = conn.execute(
                text(
                    "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0)"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = :s AND c.relkind IN ('r', 'm')"
                ),
                {"s": SCHEMA},
            ).scalar_one()
            identity = conn.execute(
                text("SELECT current_database(), inet_server_addr()::text, inet_server_port()")
            ).one()
            return {
                "host": secrets.host,
                "server_major": server_major(conn),
                "schema_exists": bool(schema),
                "schema_objects": int(tables),
                "schema_bytes": int(size),
                "identity": [str(v) for v in identity],
            }
    finally:
        engine.dispose()


def preflight(
    settings: Settings,
    opts: MoveOptions,
    source: DatabaseSecrets,
    target: DatabaseSecrets,
    guard: Guard,
    mlog: MoveLog,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if opts.force_market_hours:
        if not opts.reason.strip():
            raise PreflightError("--force-market-hours requires a non-empty --reason")
        mlog.step("market_hours_override", reason=opts.reason.strip())
    else:
        code = guard()
        if code == 3:
            raise PreflightError(
                "blocked: 09:00-15:45 IST on a trading day "
                '(use --force-market-hours --reason "..." to override)'
            )
        if code != 0:
            raise PreflightError(f"deploy-guard failed with exit code {code}")
        mlog.step("market_hours_ok")

    try:
        src = _server_info(source, settings.db_sslmode)
    except SQLAlchemyError as exc:
        raise PreflightError(f"source {source.host} unreachable: {type(exc).__name__}") from None
    try:
        dst = _server_info(target, settings.db_sslmode)
    except SQLAlchemyError as exc:
        raise PreflightError(f"target {target.host} unreachable: {type(exc).__name__}") from None
    mlog.step("reachable", source=src["host"], target=dst["host"])

    if src["identity"] == dst["identity"] and source.host == target.host:
        raise PreflightError("source and target are the same database")
    if dst["server_major"] < src["server_major"]:
        raise PreflightError(
            f"target PostgreSQL {dst['server_major']} is older than source {src['server_major']}"
        )
    if not src["schema_exists"]:
        raise PreflightError(f'source has no schema "{SCHEMA}" to move')
    if opts.dry_run and dst["schema_objects"] > 0:
        raise PreflightError("--dry-run needs an empty target (it removes its copy afterwards)")
    if dst["schema_objects"] > 0 and not opts.overwrite_target:
        raise PreflightError(
            f'target schema "{SCHEMA}" is not empty ({dst["schema_objects"]} objects); '
            "pass --overwrite-target to replace it"
        )
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(settings.backups_dir).free
    needed = int(src["schema_bytes"] * 1.2) + DISK_MARGIN_BYTES
    if free < needed:
        raise PreflightError(f"not enough disk in {settings.backups_dir}: {free} < {needed} bytes")
    mlog.step(
        "preflight_ok",
        source_major=src["server_major"],
        target_major=dst["server_major"],
        target_schema_objects=dst["schema_objects"],
        free_bytes=free,
        needed_bytes=needed,
    )
    return src, dst


# --------------------------------------------------------------------------- secrets swap


def _atomic_write_like(reference: Path, content: str, dest: Path) -> None:
    """Write ``content`` to ``dest`` atomically with ``reference``'s mode, owner and group.

    The container reads db.env as uid/gid 10001: a file rewritten by root (or by the
    deploy user) must keep the original group, or the bridge can't read its own
    database URL after the switch.
    """
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    if reference.exists():
        st = reference.stat()
        try:
            os.chown(tmp, st.st_uid if os.geteuid() == 0 else -1, st.st_gid)
        except PermissionError:
            log.warning("could not set group %s on %s; check its permissions", st.st_gid, dest)
        shutil.copymode(reference, tmp)
    os.replace(tmp, dest)


def switch_secrets(db_env: Path, target_secrets: Path) -> Path:
    previous = db_env.with_name(db_env.name + ".previous")
    _atomic_write_like(db_env, db_env.read_text(encoding="utf-8"), previous)
    _atomic_write_like(db_env, target_secrets.read_text(encoding="utf-8"), db_env)
    return previous


def restore_previous(db_env: Path) -> None:
    previous = db_env.with_name(db_env.name + ".previous")
    _atomic_write_like(db_env, previous.read_text(encoding="utf-8"), db_env)


def drop_target_schema(target: DatabaseSecrets, settings: Settings) -> None:
    """Dry run only: remove the verified copy so the real move starts from empty."""
    engine = make_engine(target.url, settings.db_sslmode)
    try:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- move


def run_move(
    settings: Settings,
    opts: MoveOptions,
    host: DockerHost,
    guard: Guard = calendar_guard,
    health_timeout_s: float = HEALTH_TIMEOUT_S,
) -> int:
    mlog = MoveLog()
    db_env = settings.db_secrets_file
    mlog.record = {"kind": "move", "dry_run": opts.dry_run, "db_env": str(db_env)}
    try:
        source = read_db_secrets_file(db_env)
        target = read_db_secrets_file(opts.target_secrets)
    except SecretsError as exc:
        print(f"preflight refused: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT
    mlog.record.update(source={"host": source.host}, target={"host": target.host})

    try:
        src, dst = preflight(settings, opts, source, target, guard, mlog)
    except PreflightError as exc:
        mlog.step("preflight_refused", reason=str(exc))
        path = mlog.write(settings.migrations_log_dir, "preflight_refused")
        print(f"preflight refused: {exc}\nlog: {path}", file=sys.stderr)
        return EXIT_PREFLIGHT
    mlog.record["source"].update(server_major=src["server_major"])
    mlog.record["target"].update(server_major=dst["server_major"])

    stopped = False
    if not opts.dry_run and host.is_running():
        host.stop()
        stopped = True
        mlog.step("bridge_stopped")

    def fallback(reason: str, **extra: Any) -> int:
        mlog.step("fallback", reason=reason)
        if stopped:
            host.start()
            healthy = host.health_gate(health_timeout_s)
            mlog.step("bridge_restarted_on_source", healthy=healthy)
            result = "failed_source_restored" if healthy else "failed_source_unhealthy"
            code = EXIT_FAILED_RESTORED if healthy else EXIT_MANUAL
        else:
            result, code = "failed_source_untouched", EXIT_FAILED_RESTORED
        path = mlog.write(settings.migrations_log_dir, result, **extra)
        print(f"MOVE FAILED: {reason}\nsource left untouched; log: {path}", file=sys.stderr)
        return code

    # copy
    try:
        dump = consistent_dump(source, settings.backups_dir, settings, prefix="move")
        mlog.step("dumped", dump=dump.path.name, revision=dump.fingerprint["alembic_revision"])
        mlog.record["fingerprints"] = {"source": dump.fingerprint}
        # preflight guaranteed the target schema is absent/empty unless --overwrite-target
        restore_into(dump, target.url, settings, replace_existing=True)
    except Exception as exc:
        return fallback(f"copy failed: {exc}")
    mlog.step("restored")

    # verify (independently of restore_into's own check)
    engine = make_engine(target.url, settings.db_sslmode)
    try:
        with engine.begin() as conn:
            conn.execute(text(ENABLE_RLS_ON_ALL))
        with engine.connect() as conn:
            target_fp = fingerprint(conn)
            target_rev = alembic_revision(conn)
    finally:
        engine.dispose()
    mlog.record["fingerprints"]["target"] = target_fp
    diffs = compare(dump.fingerprint, target_fp, ignore=["server_major"])
    head = head_revision()
    if target_rev != head:
        diffs.append(f"alembic_revision on target {target_rev!r} != head {head!r}")
    if diffs:
        return fallback("target does not match the source", differences=diffs)
    mlog.step("verified", revision=target_rev)

    if opts.dry_run:
        drop_target_schema(target, settings)
        mlog.step("dry_run_target_cleaned")
        path = mlog.write(settings.migrations_log_dir, "dry_run_ok")
        print(
            f"DRY RUN OK: target verified identical (copy removed); db.env untouched; log: {path}"
        )
        return EXIT_OK

    # switch
    previous = switch_secrets(db_env, opts.target_secrets)
    mlog.step("db_env_switched", previous=previous.name)
    if stopped:
        host.start()
    else:
        host.restart()
    if host.health_gate(health_timeout_s):
        mlog.step("health_ok")
        path = mlog.write(settings.migrations_log_dir, "moved")
        print(
            f"MOVED to {target.host}. Keep the old host for 2 weeks "
            f"(db-move.sh --revert is possible meanwhile). log: {path}"
        )
        return EXIT_OK

    mlog.step("health_failed_reverting")
    restore_previous(db_env)
    host.restart()
    healthy = host.health_gate(health_timeout_s)
    mlog.step("reverted_to_source", healthy=healthy)
    result = "failed_reverted" if healthy else "failed_revert_unhealthy"
    path = mlog.write(settings.migrations_log_dir, result)
    print(
        f"MOVE FAILED at the health gate; db.env reverted to the source. log: {path}",
        file=sys.stderr,
    )
    return EXIT_FAILED_RESTORED if healthy else EXIT_MANUAL


def run_revert(
    settings: Settings, host: DockerHost, health_timeout_s: float = HEALTH_TIMEOUT_S
) -> int:
    db_env = settings.db_secrets_file
    previous = db_env.with_name(db_env.name + ".previous")
    if not previous.is_file():
        print(f"nothing to revert: {previous} does not exist", file=sys.stderr)
        return EXIT_PREFLIGHT
    mlog = MoveLog()
    current = read_db_secrets_file(db_env)
    back = read_db_secrets_file(previous)
    mlog.record = {"kind": "revert", "from": {"host": current.host}, "to": {"host": back.host}}
    print(
        f"WARNING: writes made on the new host since the move are NOT copied back to {back.host}.",
        file=sys.stderr,
    )
    moved = db_env.with_name(db_env.name + ".moved")
    _atomic_write_like(db_env, db_env.read_text(encoding="utf-8"), moved)
    restore_previous(db_env)
    mlog.step("db_env_reverted", saved_as=moved.name)
    host.restart()
    healthy = host.health_gate(health_timeout_s)
    mlog.step("health", healthy=healthy)
    path = mlog.write(settings.migrations_log_dir, "reverted" if healthy else "revert_unhealthy")
    print(f"{'REVERTED' if healthy else 'REVERTED but UNHEALTHY'} to {back.host}; log: {path}")
    return EXIT_OK if healthy else EXIT_MANUAL


def main(
    argv: Sequence[str] | None = None,
    host: DockerHost | None = None,
    guard: Guard = calendar_guard,
    health_timeout_s: float = HEALTH_TIMEOUT_S,
) -> int:
    parser = argparse.ArgumentParser(prog="db-move.sh")
    parser.add_argument("--target-secrets", type=Path, help="db.env for the new host")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite-target", action="store_true")
    parser.add_argument("--force-market-hours", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--revert", action="store_true", help="switch db.env back (fallback)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()
    host = host or DockerHost(settings.bridge_container)
    if args.revert:
        return run_revert(settings, host, health_timeout_s)
    if args.target_secrets is None:
        parser.error("--target-secrets is required (or --revert)")
    opts = MoveOptions(
        target_secrets=args.target_secrets,
        dry_run=args.dry_run,
        force_market_hours=args.force_market_hours,
        reason=args.reason,
        overwrite_target=args.overwrite_target,
    )
    return run_move(settings, opts, host, guard, health_timeout_s)


if __name__ == "__main__":
    sys.exit(main())
