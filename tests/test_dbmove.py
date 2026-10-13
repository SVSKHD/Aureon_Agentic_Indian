"""db-move between real throwaway PostgreSQL servers.

The source is "our" database (docker exec into its container, like the compose
service); targets are other servers reached by a postgres:<major>-alpine tools
container. Nothing connects from the host except the tests' own assertions.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from dhan_bridge import backup, dbmove, pgtools
from dhan_bridge.config import DatabaseSecrets, Settings, read_db_secrets_file
from dhan_bridge.db import Database
from dhan_bridge.instruments import InstrumentStore
from dhan_bridge.keys import create_key, verify_key
from tests.conftest import FakeHost, export_ops_env, write_db_secrets
from tests.pg import PgServer

ALLOWED = lambda: 0  # noqa: E731 - market-hours guard says "allowed"
BLOCKED = lambda: 3  # noqa: E731


@pytest.fixture(scope="session")
def pg17() -> Iterator[PgServer]:
    server = PgServer.start(17)
    yield server
    server.stop()


@pytest.fixture(scope="session")
def pg15() -> Iterator[PgServer]:
    server = PgServer.start(15)
    yield server
    server.stop()


@pytest.fixture
def source(
    make_settings: Callable[..., Settings], scrip_csv: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Settings, Database, str]]:
    settings = make_settings()
    export_ops_env(monkeypatch, settings)
    db = Database(read_db_secrets_file(settings.db_secrets_file), "disable")
    db.migrate()
    InstrumentStore(db).load_csv(scrip_csv, date(2026, 10, 13))
    _, key = create_key(db, "vue")
    yield settings, db, key
    db.dispose()


def target_file(tmp_path: Path, url: str) -> Path:
    path = tmp_path / "target-db.env"
    write_db_secrets(path, url)
    return path


def move(args: list[str], host: FakeHost, guard: Callable[[], int] = ALLOWED) -> int:
    return dbmove.main(args, host=host, guard=guard, health_timeout_s=1)


def logs(settings: Settings) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(settings.migrations_log_dir.glob("*.json"))]


# --------------------------------------------------------------------------- preflight


def test_preflight_rejects_older_target(source, pg15: PgServer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    settings, _, _ = source
    host = FakeHost()
    code = move(["--target-secrets", str(target_file(tmp_path, pg15.create_database()))], host)
    assert code == dbmove.EXIT_PREFLIGHT
    assert host.calls == []  # nothing stopped, nothing changed
    assert "older than source" in logs(settings)[-1]["steps"][-1]["reason"]


def test_preflight_rejects_non_empty_target(source, pg17: PgServer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    settings, _, _ = source
    url = pg17.create_database()
    other = Database(DatabaseSecrets(url), "disable")
    other.migrate()  # target already has a bridge schema with tables
    other.dispose()
    host = FakeHost()
    assert (
        move(["--target-secrets", str(target_file(tmp_path, url))], host) == dbmove.EXIT_PREFLIGHT
    )
    assert "not empty" in logs(settings)[-1]["steps"][-1]["reason"]
    assert host.calls == []


def test_preflight_market_hours(source, pg17: PgServer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    settings, _, _ = source
    target = str(target_file(tmp_path, pg17.create_database()))
    host = FakeHost()
    assert move(["--target-secrets", target], host, guard=BLOCKED) == dbmove.EXIT_PREFLIGHT
    assert "09:00-15:45" in logs(settings)[-1]["steps"][-1]["reason"]
    # forcing without a reason is refused too
    assert move(["--target-secrets", target, "--force-market-hours"], host, guard=BLOCKED) == 2
    assert "requires a non-empty --reason" in logs(settings)[-1]["steps"][-1]["reason"]
    assert host.calls == []
    # with a reason the guard is skipped (dry run so nothing else changes)
    code = move(
        [
            "--target-secrets",
            target,
            "--dry-run",
            "--force-market-hours",
            "--reason",
            "primary host failing",
        ],
        host,
        guard=BLOCKED,
    )
    assert code == dbmove.EXIT_OK
    assert logs(settings)[-1]["steps"][0] == {
        **logs(settings)[-1]["steps"][0],
        "reason": "primary host failing",
    }


def test_preflight_rejects_same_database(source, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    settings, _, _ = source
    same = target_file(tmp_path, read_db_secrets_file(settings.db_secrets_file).url)
    assert move(["--target-secrets", str(same)], FakeHost()) == dbmove.EXIT_PREFLIGHT


# --------------------------------------------------------------------------- dry run


def test_dry_run_never_touches_db_env_or_stops_bridge(
    source, pg17: PgServer, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    settings, _, _ = source
    db_env_before = settings.db_secrets_file.read_bytes()
    url = pg17.create_database()
    host = FakeHost(running=True)
    assert move(["--target-secrets", str(target_file(tmp_path, url)), "--dry-run"], host) == 0
    assert host.calls == []  # never stopped, started or restarted
    assert settings.db_secrets_file.read_bytes() == db_env_before
    assert not settings.db_secrets_file.with_name("db.env.previous").exists()
    record = logs(settings)[-1]
    assert record["result"] == "dry_run_ok"
    assert record["fingerprints"]["source"]["tables"]["instruments"]["rows"] == 10
    # the verified copy was removed, so the real move can start from an empty target
    target = Database(DatabaseSecrets(url), "disable")
    with target.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM pg_namespace WHERE nspname='bridge'")).scalar()
            == 0
        )
    target.dispose()


# --------------------------------------------------------------------------- full move


def test_full_move_succeeds_with_identical_fingerprints(
    source, pg17: PgServer, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    settings, src_db, key = source
    src_db_env = settings.db_secrets_file.read_text()
    url = pg17.create_database()
    host = FakeHost(running=True, healthy=[True])
    assert move(["--target-secrets", str(target_file(tmp_path, url))], host) == dbmove.EXIT_OK
    assert host.calls == ["stop", "start", "health"]

    # db.env now points at the target; the old one is kept for --revert
    assert read_db_secrets_file(settings.db_secrets_file).url == url
    assert settings.db_secrets_file.with_name("db.env.previous").read_text() == src_db_env

    record = logs(settings)[-1]
    assert record["result"] == "moved"
    fps = record["fingerprints"]
    assert fps["source"]["server_major"] == 16
    assert fps["target"]["server_major"] == 17
    assert {k: v for k, v in fps["source"].items() if k != "server_major"} == {
        k: v for k, v in fps["target"].items() if k != "server_major"
    }
    assert "password" not in json.dumps(record).lower()
    assert read_db_secrets_file(settings.db_secrets_file).password not in json.dumps(record)

    # the data really is there, keys still work, and the source is untouched
    target = Database(DatabaseSecrets(url), "disable")
    try:
        assert target.ok()
        assert verify_key(target, key) is not None
    finally:
        target.dispose()
    assert InstrumentStore(src_db).count() == 10
    assert verify_key(src_db, key) is not None


def test_corrupted_restore_falls_back_to_source(
    source,
    pg17: PgServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    settings, src_db, _ = source
    db_env_before = settings.db_secrets_file.read_bytes()
    url = pg17.create_database()
    real_restore = backup.restore

    def corrupting_restore(runner, dump_path, pre_sql=""):  # type: ignore[no-untyped-def]
        real_restore(runner, dump_path, pre_sql)
        if isinstance(runner, pgtools.DockerRunner) and runner.url == url:  # target only
            pgtools.psql(runner, "DELETE FROM bridge.instruments WHERE security_id = '2885';")

    monkeypatch.setattr(backup, "restore", corrupting_restore)
    host = FakeHost(running=True)
    assert (
        move(["--target-secrets", str(target_file(tmp_path, url))], host)
        == dbmove.EXIT_FAILED_RESTORED
    )
    assert host.calls == ["stop", "start", "health"]  # bridge back up on the source
    assert settings.db_secrets_file.read_bytes() == db_env_before
    assert not settings.db_secrets_file.with_name("db.env.previous").exists()
    record = logs(settings)[-1]
    assert record["result"] == "failed_source_restored"
    assert any("instruments.rows" in s.get("reason", "") for s in record["steps"])
    assert InstrumentStore(src_db).count() == 10  # source never modified
    target = Database(DatabaseSecrets(url), "disable")
    with target.connect() as conn:  # the bad copy was removed from the target
        assert (
            conn.execute(text("SELECT count(*) FROM pg_namespace WHERE nspname='bridge'")).scalar()
            == 0
        )
    target.dispose()


def test_health_gate_failure_reverts_db_env(source, pg17: PgServer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    settings, _, _ = source
    db_env_before = settings.db_secrets_file.read_bytes()
    host = FakeHost(running=True, healthy=[False, True])
    url = pg17.create_database()
    assert (
        move(["--target-secrets", str(target_file(tmp_path, url))], host)
        == dbmove.EXIT_FAILED_RESTORED
    )
    assert host.calls == ["stop", "start", "health", "restart", "health"]
    assert settings.db_secrets_file.read_bytes() == db_env_before
    assert logs(settings)[-1]["result"] == "failed_reverted"


def test_revert(source, pg17: PgServer, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    settings, _, _ = source
    source_url = read_db_secrets_file(settings.db_secrets_file).url
    host = FakeHost(running=True)
    assert move(["--revert"], host) == dbmove.EXIT_PREFLIGHT  # nothing to revert yet
    url = pg17.create_database()
    assert move(["--target-secrets", str(target_file(tmp_path, url))], host) == 0
    assert read_db_secrets_file(settings.db_secrets_file).url == url

    host.calls.clear()
    assert move(["--revert"], host) == dbmove.EXIT_OK
    assert host.calls == ["restart", "health"]
    assert read_db_secrets_file(settings.db_secrets_file).url == source_url
    assert read_db_secrets_file(settings.db_secrets_file.with_name("db.env.moved")).url == url
    assert "NOT copied back" in capsys.readouterr().err
    assert logs(settings)[-1]["result"] == "reverted"


def test_switched_db_env_keeps_owner_group_and_mode(tmp_path: Path) -> None:
    """The bridge reads db.env via its group: a switch must not reset owner/group/mode."""
    import os
    import stat

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    db_env = secrets / "db.env"
    write_db_secrets(db_env, "postgresql://a:b@old-host:5432/db")
    os.chmod(db_env, 0o440)
    if os.geteuid() == 0:
        os.chown(db_env, 10001, 10001)  # as on the host: readable by the container's group
    target = tmp_path / "target.env"
    write_db_secrets(target, "postgresql://a:b@new-host:5432/db")
    os.chmod(target, 0o600)

    ref = db_env.stat()
    previous = dbmove.switch_secrets(db_env, target)
    for path in (db_env, previous):
        st = path.stat()
        assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (
            ref.st_uid,
            ref.st_gid,
            0o440,
        ), path.name
    assert "new-host" in db_env.read_text()
    dbmove.restore_previous(db_env)
    st = db_env.stat()
    assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (ref.st_uid, ref.st_gid, 0o440)
    assert "old-host" in db_env.read_text()
