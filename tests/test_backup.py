"""Backups with real pg_dump/pg_restore in postgres:<major>-alpine containers."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from dhan_bridge import backup
from dhan_bridge.config import Settings, read_db_secrets_file
from dhan_bridge.db import Database
from dhan_bridge.instruments import InstrumentStore
from dhan_bridge.pgtools import PgToolError
from tests.conftest import FakeHost


@pytest.fixture
def env(
    make_settings: Callable[..., Settings], scrip_csv: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[Settings, Database]:
    settings = make_settings()
    # backup.main() builds Settings() from the environment, like on the host
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("DB_SECRETS_FILE", str(settings.db_secrets_file))
    monkeypatch.setenv("DB_SSLMODE", "disable")
    monkeypatch.setenv("PG_TOOLS_IMAGE", settings.pg_tools_image)
    db = Database(read_db_secrets_file(settings.db_secrets_file), "disable")
    db.migrate()
    InstrumentStore(db).load_csv(scrip_csv, date(2026, 10, 13))
    yield settings, db  # type: ignore[misc]
    db.dispose()


def count(db: Database, table: str = "instruments") -> int:
    with db.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM bridge.{table}")).scalar_one())  # noqa: S608


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


def test_dump_is_a_consistent_snapshot(
    env: tuple[Settings, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writes that land while pg_dump runs are in neither the dump nor its fingerprint."""
    settings, db = env
    real_run = backup.PgTools.run

    def run_and_write_concurrently(self, tool, args, url, **kw):  # type: ignore[no-untyped-def]
        if tool == "pg_dump":
            with db.begin() as conn:  # a separate connection commits mid-dump
                conn.execute(text("DELETE FROM bridge.instruments"))
        return real_run(self, tool, args, url, **kw)

    monkeypatch.setattr(backup.PgTools, "run", run_and_write_concurrently)
    dump = backup.consistent_dump(
        read_db_secrets_file(settings.db_secrets_file), settings.backups_dir, settings
    )
    monkeypatch.setattr(backup.PgTools, "run", real_run)
    assert count(db) == 0  # the delete really happened
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
        rls = conn.execute(
            text("SELECT bool_and(rowsecurity) FROM pg_tables WHERE schemaname='bridge'")
        ).scalar()
        aside = conn.execute(
            text("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'bridge_pre_restore_%'")
        ).scalar()
    assert "added_later" not in tables  # nothing newer than the dump survives a restore
    assert rls is True
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

    def broken(self, tool, args, url, **kw):  # type: ignore[no-untyped-def]
        raise PgToolError("simulated pg_restore failure")

    monkeypatch.setattr(backup.PgTools, "run", broken)
    dump = backup.load_dump(snapshot)
    with pytest.raises(PgToolError):
        backup.restore_into(
            dump,
            read_db_secrets_file(settings.db_secrets_file).url,
            settings,
            replace_existing=True,
        )
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
