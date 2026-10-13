from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from dhan_bridge import dbverify, pgtools
from dhan_bridge.db import Database
from dhan_bridge.instruments import InstrumentStore
from dhan_bridge.pgtools import ExecRunner
from tests.pg import PgServer


@pytest.fixture
def loaded(db: Database, scrip_csv: str) -> Database:
    from datetime import date

    InstrumentStore(db).load_csv(scrip_csv, date(2026, 10, 13))
    with db.begin() as conn:
        conn.execute(text("INSERT INTO bridge.api_keys (name, key_hash) VALUES ('a', 'h1')"))
    return db


def fp(db: Database) -> dict:
    with db.connect() as conn:
        return dbverify.fingerprint(conn)


def test_fingerprint_shape_and_discovery(loaded: Database) -> None:
    result = fp(loaded)
    assert result["alembic_revision"] == loaded.head
    assert result["server_major"] == 16
    tables = result["tables"]
    assert set(tables) == {"alembic_version", "api_keys", "instruments", "meta"}
    assert tables["instruments"]["rows"] == 10
    # NUMERIC columns are summed exactly, TIMESTAMPTZ columns get min/max (from metadata)
    assert tables["instruments"]["sum"] == {"tick_size": "75.0000"}
    assert set(tables["api_keys"]["min_max"]) == {"created_at", "revoked_at"}
    assert tables["api_keys"]["min_max"]["revoked_at"] == [None, None]
    assert tables["alembic_version"] == {"rows": 1}  # catalog-only table: row count
    json.dumps(result)  # serialisable


def test_fingerprint_is_stable(loaded: Database) -> None:
    assert fp(loaded) == fp(loaded)
    assert dbverify.compare(fp(loaded), fp(loaded)) == []


def test_detects_changed_row_count(loaded: Database) -> None:
    before = fp(loaded)
    with loaded.begin() as conn:
        conn.execute(text("DELETE FROM bridge.instruments WHERE security_id = '13'"))
    diffs = dbverify.compare(before, fp(loaded))
    assert any(d.startswith("tables.instruments.rows: 10 != 9") for d in diffs), diffs


def test_detects_changed_money_sum(loaded: Database) -> None:
    before = fp(loaded)
    with loaded.begin() as conn:
        conn.execute(
            text(
                "UPDATE bridge.instruments SET tick_size = tick_size + 0.0001"
                " WHERE security_id = '2885'"
            )
        )
    diffs = dbverify.compare(before, fp(loaded))
    assert diffs == ["tables.instruments.sum.tick_size: '75.0000' != '75.0001'"]


def test_detects_changed_timestamp(loaded: Database) -> None:
    before = fp(loaded)
    with loaded.begin() as conn:
        conn.execute(text("UPDATE bridge.api_keys SET revoked_at = now()"))
    diffs = dbverify.compare(before, fp(loaded))
    assert any("api_keys.min_max.revoked_at" in d for d in diffs)


def test_detects_different_alembic_revision(loaded: Database) -> None:
    before = fp(loaded)
    with loaded.begin() as conn:
        conn.execute(text("UPDATE bridge.alembic_version SET version_num = '0099_other'"))
    diffs = dbverify.compare(before, fp(loaded))
    assert diffs == [f"alembic_revision: {loaded.head!r} != '0099_other'"]


def test_new_tables_are_covered_automatically(loaded: Database) -> None:
    before = fp(loaded)
    with loaded.begin() as conn:
        conn.execute(text("CREATE TABLE bridge.surprise (x int)"))
    diffs = dbverify.compare(before, fp(loaded))
    assert diffs == ["tables.surprise: missing in first"]


def test_compare_ignore_and_nested() -> None:
    a = {"server_major": 16, "tables": {"t": {"rows": 1}}}
    b = {"server_major": 17, "tables": {"t": {"rows": 1}}}
    assert dbverify.compare(a, b) == ["server_major: 16 != 17"]
    assert dbverify.compare(a, b, ignore=["server_major"]) == []


def test_cli(loaded: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(dbverify.dumps(fp(loaded)))
    b.write_text(json.dumps({"dump": "x.dump", "fingerprint": fp(loaded)}))  # a backup sidecar
    assert dbverify.main(["compare", str(a), str(b)]) == 0
    other = fp(loaded)
    other["tables"]["meta"]["rows"] += 1
    b.write_text(json.dumps(other))
    assert dbverify.main(["compare", str(a), str(b)]) == 1
    assert "tables.meta.rows" in capsys.readouterr().out
    assert dbverify.main(["sql"]) == 0
    assert capsys.readouterr().out == dbverify.PSQL_SCRIPT


def test_psql_and_app_fingerprints_are_identical(
    loaded: Database, db_url: str, pg_server: PgServer
) -> None:
    """The ops tools (psql in the postgres container) and the app compute the same thing."""
    dbname = db_url.rsplit("/", 1)[1].split("?", 1)[0]
    runner = ExecRunner(["docker", "exec", "-i"], pg_server.container or "", "postgres", dbname)
    assert pgtools.fingerprint(runner) == fp(loaded)
