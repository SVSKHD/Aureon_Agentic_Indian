from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dhan_bridge import backup
from dhan_bridge.migrations import Migration, migrate

V1 = Migration(1, "a", "CREATE TABLE a (x INTEGER);")
V2 = Migration(2, "b", "CREATE TABLE b (y INTEGER);")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    migrate(data / "bridge.db", data / "backups", "0.1.0", migrations=[V1])
    monkeypatch.setenv("DATA_DIR", str(data))
    return data


def rows(db: Path, table: str) -> list[tuple[int]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - test helper
    finally:
        conn.close()


def insert(db: Path, value: int) -> None:
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("INSERT INTO a VALUES (?)", (value,))
    conn.close()


def test_now_verify_and_schema_version(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert backup.main(["now"]) == 0
    out = capsys.readouterr().out
    assert "schema_version=1" in out
    name = out.split("backup=")[1].split()[0]
    assert (data_dir / "backups" / name).is_file()
    assert backup.main(["verify", name]) == 0
    assert "OK: ok" in capsys.readouterr().out


def test_now_is_safe_while_app_runs(data_dir: Path) -> None:
    lock = backup.acquire_instance_lock(data_dir)
    try:
        assert backup.is_app_running(data_dir) is True
        assert backup.main(["now"]) == 0  # online backup: allowed while running
    finally:
        lock.close()
    assert backup.is_app_running(data_dir) is False


def test_verify_detects_corruption(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 50 + b"garbage" * 200)
    assert backup.main(["verify", str(bad)]) == 1
    assert "CORRUPT" in capsys.readouterr().out
    assert backup.verify(tmp_path / "missing.db")[0] is False


def test_restore_refuses_while_running_unless_forced(data_dir: Path) -> None:
    db = data_dir / "bridge.db"
    insert(db, 1)
    snapshot = backup.take_backup(db, data_dir / "backups")
    insert(db, 2)

    lock = backup.acquire_instance_lock(data_dir)
    try:
        assert backup.main(["restore", snapshot.name]) == 2
        assert rows(db, "a") == [(1,), (2,)]
        assert backup.main(["restore", snapshot.name, "--force"]) == 0
    finally:
        lock.close()
    assert rows(db, "a") == [(1,)]


def test_restore_keeps_a_safety_copy(data_dir: Path) -> None:
    db = data_dir / "bridge.db"
    insert(db, 1)
    snapshot = backup.take_backup(db, data_dir / "backups")
    insert(db, 2)
    safety = backup.restore(snapshot, db, data_dir / "backups", data_dir)
    assert rows(db, "a") == [(1,)]
    assert safety is not None
    assert rows(safety, "a") == [(1,), (2,)]


def test_restore_refuses_corrupt_backup(data_dir: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite at all" * 100)
    with pytest.raises(backup.BackupError, match="integrity"):
        backup.restore(bad, data_dir / "bridge.db", data_dir / "backups", data_dir)
    with pytest.raises(backup.BackupError, match="not found"):
        backup.resolve_backup("nope.db", data_dir / "backups")


def test_second_instance_cannot_start(data_dir: Path) -> None:
    lock = backup.acquire_instance_lock(data_dir)
    try:
        with pytest.raises(backup.BackupError, match="another dhan_bridge instance"):
            backup.acquire_instance_lock(data_dir)
    finally:
        lock.close()


def test_record_deploy_and_rollback_plan(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        backup.main(["now", "--record-deploy", "--previous-tag", "0.0.9", "--target-tag", "0.1.0"])
        == 0
    )
    record = json.loads((data_dir / "backups" / backup.LAST_DEPLOY).read_text())
    assert record["previous_tag"] == "0.0.9"
    assert record["schema_version"] == 1
    capsys.readouterr()

    # schema unchanged -> plain redeploy of the previous tag
    assert backup.main(["rollback-plan"]) == 0
    plan = dict(line.split("=", 1) for line in capsys.readouterr().out.split())
    assert plan["PREVIOUS_TAG"] == "0.0.9"
    assert plan["NEEDS_RESTORE"] == "0"

    # the new release migrated -> restore needed
    migrate(data_dir / "bridge.db", data_dir / "backups", "0.1.0", migrations=[V1, V2])
    assert backup.main(["rollback-plan"]) == 0
    plan = dict(line.split("=", 1) for line in capsys.readouterr().out.split())
    assert plan["NEEDS_RESTORE"] == "1"
    assert plan["CURRENT_SCHEMA"] == "2"
    assert plan["RECORDED_SCHEMA"] == "1"
    assert (data_dir / "backups" / plan["BACKUP"]).is_file()


def test_rollback_plan_errors(data_dir: Path) -> None:
    backups_dir = data_dir / "backups"
    backups_dir.mkdir(exist_ok=True)
    with pytest.raises(backup.BackupError, match="no deploy was recorded"):
        backup.rollback_plan(data_dir / "bridge.db", backups_dir)
    (backups_dir / backup.LAST_DEPLOY).write_text(
        json.dumps({"previous_tag": "x; rm -rf /", "backup": "a.db", "schema_version": 1})
    )
    with pytest.raises(backup.BackupError, match="malformed"):
        backup.rollback_plan(data_dir / "bridge.db", backups_dir)
    (backups_dir / backup.LAST_DEPLOY).write_text(
        json.dumps({"previous_tag": "0.0.9", "backup": "gone.db", "schema_version": 0})
    )
    with pytest.raises(backup.BackupError, match="missing"):
        backup.rollback_plan(data_dir / "bridge.db", backups_dir)


def test_record_deploy_validates_tags(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupError, match="invalid image tag"):
        backup.record_deploy(
            tmp_path, backup=tmp_path / "x.db", previous_tag="$(id)", target_tag="1", version=1
        )


def test_now_requires_tags_with_record_deploy(data_dir: Path) -> None:
    with pytest.raises(SystemExit):
        backup.main(["now", "--record-deploy"])


def test_schema_version_of_missing_or_unmigrated(tmp_path: Path) -> None:
    assert backup.schema_version(tmp_path / "missing.db") == 0
    sqlite3.connect(tmp_path / "empty.db").close()
    assert backup.schema_version(tmp_path / "empty.db") == 0


def test_now_without_database_is_distinct_and_clears_stale_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    stale = data / "backups" / backup.LAST_DEPLOY
    stale.write_text("{}")
    monkeypatch.setenv("DATA_DIR", str(data))
    args = ["now", "--record-deploy", "--previous-tag", "a", "--target-tag", "b"]
    assert backup.main(args) == backup.EXIT_NO_DATABASE
    assert not stale.exists()
