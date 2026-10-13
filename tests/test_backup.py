"""Backups with real psql/pg_dump/pg_restore inside postgres containers (never on the host)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from dhan_bridge import backup, pgtools
from dhan_bridge.config import Settings, read_db_secrets_file
from dhan_bridge.db import Database
from dhan_bridge.instruments import InstrumentStore
from dhan_bridge.pgtools import ExecRunner, PgRunner, PgToolError
from tests.conftest import FakeHost, export_ops_env


@pytest.fixture
def env(
    make_settings: Callable[..., Settings], scrip_csv: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[Settings, Database]:
    settings = make_settings()
    export_ops_env(monkeypatch, settings)
    db = Database(read_db_secrets_file(settings.db_secrets_file), "disable")
    db.migrate()
    InstrumentStore(db).load_csv(scrip_csv, date(2026, 10, 13))
    yield settings, db  # type: ignore[misc]
    db.dispose()


def count(db: Database, table: str = "instruments") -> int:
    with db.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM bridge.{table}")).scalar_one())


def run(args: list[str], host: FakeHost | None = None) -> int:
    return backup.main(args, host=host or FakeHost(running=False))


def latest(settings: Settings) -> Path:
    return backup.list_dumps(settings.backups_dir)[-1]


def test_now_writes_dump_and_fingerprint_sidecar(env: tuple[Settings, Database]) -> None:
    settings, db = env
    assert run(["now"]) == 0
    dump = latest(settings)
    meta = json.loads(backup.sidecar_for(dump).read_text())
    assert dump.name.startswith("bridge-")
    assert dump.suffix == ".dump"
    assert meta["fingerprint"]["alembic_revision"] == db.head
    assert meta["fingerprint"]["tables"]["instruments"]["rows"] == 10
    assert meta["dump_sha256"] == backup.sha256_file(dump)
    assert "password" not in json.dumps(meta).lower()
    assert db_url_password(settings) not in json.dumps(meta)


def db_url_password(settings: Settings) -> str:
    return read_db_secrets_file(settings.db_secrets_file).password


def own_runner(settings: Settings) -> PgRunner:
    return pgtools.runner_for(read_db_secrets_file(settings.db_secrets_file).url, settings)


def test_own_database_uses_exec_runner(env: tuple[Settings, Database]) -> None:
    settings, _ = env
    runner = own_runner(settings)
    assert isinstance(runner, ExecRunner)  # docker exec into the server: no password needed
    assert "@/" in runner.url  # local socket, no host
    assert ":" not in runner.url.split("//", 1)[1].split("@", 1)[0]  # and no password


def test_dump_is_a_consistent_snapshot(
    env: tuple[Settings, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writes committed after the snapshot is exported are in neither the dump nor its
    fingerprint, even though they land before the fingerprint and pg_dump run."""
    settings, db = env
    concurrent = "\\! psql -X -q --dbname=\"$PGDBURL\" -c 'DELETE FROM bridge.instruments'\n"
    marker = "\\setenv PGSNAP :snap\n"
    assert marker in pgtools.DUMP_PSQL
    monkeypatch.setattr(
        pgtools, "DUMP_PSQL", pgtools.DUMP_PSQL.replace(marker, marker + concurrent)
    )
    dump = backup.consistent_dump(
        read_db_secrets_file(settings.db_secrets_file), settings.backups_dir, settings
    )
    assert count(db) == 0  # the delete really happened (another session, committed)
    assert dump.fingerprint["tables"]["instruments"]["rows"] == 10
    assert backup.verify_dump(dump, settings) == []  # ...and the dump holds the 10 rows


def test_verify_ok_and_detects_tampering(
    env: tuple[Settings, Database], capsys: pytest.CaptureFixture[str]
) -> None:
    settings, _ = env
    assert run(["now"]) == 0
    dump = latest(settings)
    assert run(["verify", dump.name]) == 0
    assert "OK:" in capsys.readouterr().out

    # a sidecar that no longer describes the dump -> non-zero
    sidecar = backup.sidecar_for(dump)
    meta = json.loads(sidecar.read_text())
    meta["fingerprint"]["tables"]["instruments"]["rows"] = 11
    sidecar.write_text(json.dumps(meta))
    assert run(["verify", dump.name]) == 1
    assert "DIFF tables.instruments.rows: 11 != 10" in capsys.readouterr().out

    # a modified dump file -> sha mismatch -> refused before restoring anything
    with dump.open("ab") as handle:
        handle.write(b"x")
    assert run(["verify", dump.name]) == 2
    assert "sha256 mismatch" in capsys.readouterr().err


def test_restore_refuses_while_bridge_runs(
    env: tuple[Settings, Database], capsys: pytest.CaptureFixture[str]
) -> None:
    settings, _ = env
    assert run(["now"]) == 0
    assert run(["restore", latest(settings).name], host=FakeHost(running=True)) == 2
    assert "is running" in capsys.readouterr().err


def test_restore_replaces_schema_completely(env: tuple[Settings, Database]) -> None:
    settings, db = env
    assert run(["now"]) == 0
    snapshot = latest(settings)
    with db.begin() as conn:
        conn.execute(text("DELETE FROM bridge.instruments WHERE security_id <> '2885'"))
        conn.execute(text("CREATE TABLE bridge.added_later (x int)"))  # e.g. a newer revision
    assert count(db) == 1

    assert run(["restore", snapshot.name]) == 0
    assert count(db) == 10
    with db.connect() as conn:
        tables = set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='bridge'")
            ).scalars()
        )
        aside = conn.execute(
            text("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'bridge_pre_restore_%'")
        ).scalar()
    assert "added_later" not in tables  # nothing newer than the dump survives a restore
    assert aside == 0
    assert any(p.name.startswith("pre-restore-") for p in backup.list_dumps(settings.backups_dir))


def test_failed_restore_puts_original_schema_back(
    env: tuple[Settings, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, db = env
    assert run(["now"]) == 0
    snapshot = latest(settings)
    with db.begin() as conn:
        conn.execute(text("DELETE FROM bridge.instruments WHERE security_id <> '2885'"))

    def half_restore_then_fail(runner: PgRunner, dump_path: Path, pre_sql: str = "") -> None:
        pgtools.psql(runner, pre_sql + " CREATE TABLE bridge.partial (x int);")
        raise PgToolError("simulated pg_restore failure")

    monkeypatch.setattr(backup, "restore", half_restore_then_fail)
    dump = backup.load_dump(snapshot)
    with pytest.raises(PgToolError):
        backup.restore_into(own_runner(settings), dump, replace_existing=True)
    assert count(db) == 1  # untouched current data is back in place
    assert db.ok()


def test_record_deploy_and_rollback_plan(
    env: tuple[Settings, Database], capsys: pytest.CaptureFixture[str]
) -> None:
    settings, db = env
    assert run(["now", "--record-deploy", "--previous-tag", "0.0.9", "--target-tag", "0.1.0"]) == 0
    record = json.loads((settings.backups_dir / backup.LAST_DEPLOY).read_text())
    assert record["previous_tag"] == "0.0.9"
    assert record["alembic_revision"] == db.head
    capsys.readouterr()

    assert run(["rollback-plan"]) == 0
    plan = dict(line.split("=", 1) for line in capsys.readouterr().out.split())
    assert plan["NEEDS_RESTORE"] == "0"
    assert plan["PREVIOUS_TAG"] == "0.0.9"

    # the new release migrated (simulated by moving the revision marker)
    with db.begin() as conn:
        conn.execute(text("UPDATE bridge.alembic_version SET version_num = '0099_future'"))
    assert run(["rollback-plan"]) == 0
    plan = dict(line.split("=", 1) for line in capsys.readouterr().out.split())
    assert plan["NEEDS_RESTORE"] == "1"
    assert plan["CURRENT_REVISION"] == "0099_future"
    assert (settings.backups_dir / plan["BACKUP"]).is_file()


def test_rollback_plan_errors(
    env: tuple[Settings, Database], capsys: pytest.CaptureFixture[str]
) -> None:
    settings, _ = env
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    assert run(["rollback-plan"]) == 2
    assert "no deploy was recorded" in capsys.readouterr().err
    (settings.backups_dir / backup.LAST_DEPLOY).write_text(
        json.dumps({"previous_tag": "$(id)", "backup": "a.dump"})
    )
    assert run(["rollback-plan"]) == 2
    assert "malformed" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run(["now", "--record-deploy"])


def test_prune_keeps_newest(
    env: tuple[Settings, Database], capsys: pytest.CaptureFixture[str]
) -> None:
    settings, _ = env
    for _ in range(3):
        assert run(["now"]) == 0
    assert run(["prune", "--keep", "1"]) == 0
    remaining = backup.list_dumps(settings.backups_dir)
    assert len(remaining) == 1
    assert [p.name for p in settings.backups_dir.glob("*.fingerprint.json")] == [
        remaining[0].name + ".fingerprint.json"
    ]
    capsys.readouterr()
    assert run(["latest"]) == 0
    assert capsys.readouterr().out.strip().endswith(remaining[0].name)


def test_restore_refuses_older_server(env: tuple[Settings, Database]) -> None:
    settings, _ = env
    assert run(["now"]) == 0
    dump = backup.load_dump(latest(settings))
    runner = own_runner(settings)
    runner.major = 15  # pretend the target is older than the dump's source
    with pytest.raises(backup.BackupError, match="older"):
        backup.restore_into(runner, dump, replace_existing=True)


def test_now_without_schema_exits_3(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings()  # fresh database, never migrated
    export_ops_env(monkeypatch, settings)
    assert run(["now"]) == backup.EXIT_NO_SCHEMA
    assert not list(settings.backups_dir.glob("*.dump"))


# --------------------------------------------------------------------------- off-site targets


def test_load_targets(tmp_path: Path) -> None:
    path = tmp_path / "targets.yaml"
    assert backup.load_targets(path) == []  # no file: nothing configured
    path.write_text(
        "targets:\n"
        "  - {name: nas, type: rsync, host: backup@nas, path: /srv/b/, ssh_key: /k}\n"
        "  - {type: rclone, remote: 'b2:bucket/dhan'}\n"
    )
    assert backup.load_targets(path) == [
        backup.Target("nas", "rsync", "backup@nas", "/srv/b/", "/k"),
        backup.Target("target-1", "rclone", remote="b2:bucket/dhan"),
    ]
    path.write_text("targets:\n  - {type: ftp, host: x}\n")
    with pytest.raises(backup.BackupError, match="unknown type"):
        backup.load_targets(path)
    path.write_text("targets:\n  - {type: rsync, host: x}\n")
    with pytest.raises(backup.BackupError, match="needs host and path"):
        backup.load_targets(path)


def test_shipped_targets_config_is_an_all_commented_example() -> None:
    from tests.conftest import REPO_ROOT

    shipped = REPO_ROOT / "config" / "backup-targets.yaml"
    assert shipped.is_file()
    assert backup.load_targets(shipped) == []  # real targets are configured per host


def test_offsite_copies_to_every_target_and_reports_failures(
    env: tuple[Settings, Database],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings, _ = env
    assert run(["now"]) == 0
    calls: list[list[str]] = []

    class Proc:
        def __init__(self, code: int) -> None:
            self.returncode, self.stderr = code, "boom"

    def fake_run(cmd: list[str], **kw: object) -> Proc:
        calls.append(cmd)
        return Proc(1 if cmd[0] == "rclone" else 0)

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    targets = tmp_path / "t.yaml"
    targets.write_text(
        "targets:\n"
        "  - {name: nas, type: rsync, host: b@nas, path: /srv/b, ssh_key: /keys/id}\n"
        "  - {name: cloud, type: rclone, remote: 'b2:x'}\n"
    )
    assert run(["offsite", latest(settings).name, "--targets", str(targets)]) == 1
    assert "1/2 target(s) ok" in capsys.readouterr().out
    rsync = calls[0]
    assert rsync[0] == "rsync"
    assert rsync[-1] == "b@nas:/srv/b/"
    assert "-i /keys/id" in rsync[rsync.index("-e") + 1]
    assert {Path(a).name for a in rsync if a.endswith(".dump")} == {latest(settings).name}
    assert any(a.endswith(".fingerprint.json") for a in rsync)
    assert calls[1][:2] == ["rclone", "copy"]

    calls.clear()
    assert run(["offsite", latest(settings).name, "--targets", str(tmp_path / "none.yaml")]) == 0
    assert calls == []
    assert "no off-site targets" in capsys.readouterr().out
