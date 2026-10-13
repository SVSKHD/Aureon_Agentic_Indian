"""Database resilience: fail fast at live startup, survive outages afterwards."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from dhan_bridge.api import create_app, write_heartbeat
from dhan_bridge.config import DatabaseSecrets, Settings
from dhan_bridge.db import Database, retry
from dhan_bridge.keys import create_key
from tests.conftest import FakeClient, write_db_secrets
from tests.pg import PgServer

LOOPBACK = ("127.0.0.1", 50000)
DEAD_URL = "postgresql://u:p@127.0.0.1:1/x?sslmode=disable&connect_timeout=1"


@pytest.fixture
def own_server() -> Iterator[PgServer]:
    """A private server we can pause to simulate an outage."""
    server = PgServer.start(16)
    yield server
    subprocess.run(["docker", "unpause", server.container or ""], capture_output=True, check=False)
    server.stop()


def app_client(settings: Settings, scrip_csv: str) -> TestClient:
    app = create_app(settings, client_factory=FakeClient, fetcher=lambda: scrip_csv)
    return TestClient(app, client=LOOPBACK)


def test_live_startup_fails_fast_without_database(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> None:
    settings = make_settings()
    write_db_secrets(settings.db_secrets_file, DEAD_URL)
    with (
        pytest.raises(RuntimeError, match=r"database 127\.0\.0\.1:1/x unavailable"),
        app_client(settings, scrip_csv),
    ):
        pass


def test_live_startup_requires_db_env(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> None:
    settings = make_settings()
    settings.db_secrets_file.unlink()
    with (
        pytest.raises(RuntimeError, match=r"DATABASE_URL|cannot read"),
        app_client(settings, scrip_csv),
    ):
        pass


def test_replay_starts_without_database(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> None:
    settings = make_settings(bridge_mode="replay")
    write_db_secrets(settings.db_secrets_file, DEAD_URL)
    with app_client(settings, scrip_csv) as client:
        body = client.get("/health").json()
    assert body["db_ok"] is False
    assert body["mode"] == "replay"


def test_outage_after_startup_does_not_crash(
    make_settings: Callable[..., Settings], scrip_csv: str, own_server: PgServer
) -> None:
    url = own_server.create_database()
    settings = make_settings()
    write_db_secrets(settings.db_secrets_file, url)
    with app_client(settings, scrip_csv) as client:
        assert client.get("/health").json()["db_ok"] is True
        db = Database(DatabaseSecrets(url), "disable")
        _, key = create_key(db, "vue")
        db.dispose()
        auth = {"Authorization": f"Bearer {key}"}
        assert client.get("/api/universe", headers=auth).status_code == 200

        subprocess.run(
            ["docker", "pause", own_server.container or ""], check=True, capture_output=True
        )
        started = time.monotonic()
        response = client.get("/health")
        assert response.status_code == 200  # the app is still up...
        assert response.json()["db_ok"] is False  # ...and says the database is not
        assert time.monotonic() - started < 6  # SELECT 1 is bounded (3 s)

        subprocess.run(
            ["docker", "unpause", own_server.container or ""], check=True, capture_output=True
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not client.get("/health").json()["db_ok"]:
            time.sleep(0.5)
        assert client.get("/health").json()["db_ok"] is True  # recovered without a restart
        assert client.get("/api/universe", headers=auth).status_code == 200


def test_api_answers_503_not_401_when_database_is_down(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> None:
    settings = make_settings(bridge_mode="replay")
    write_db_secrets(settings.db_secrets_file, DEAD_URL)
    with app_client(settings, scrip_csv) as client:
        response = client.get("/api/universe", headers={"Authorization": "Bearer dbk_" + "x" * 43})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"


def test_db_env_rotation_switches_database_without_restart(
    make_settings: Callable[..., Settings], scrip_csv: str, pg_server: PgServer
) -> None:
    settings = make_settings()
    other_url = pg_server.create_database()
    other = Database(DatabaseSecrets(other_url), "disable")
    other.migrate()
    _, other_key = create_key(other, "on-the-other-db")
    other.dispose()
    auth = {"Authorization": f"Bearer {other_key}"}
    with app_client(settings, scrip_csv) as client:
        assert client.get("/api/universe", headers=auth).status_code == 401
        tmp = settings.db_secrets_file.with_suffix(".tmp")
        write_db_secrets(tmp, other_url)
        tmp.replace(settings.db_secrets_file)
        deadline = time.monotonic() + 5
        while (
            time.monotonic() < deadline
            and client.get("/api/universe", headers=auth).status_code != 200
        ):
            time.sleep(0.05)
        assert client.get("/api/universe", headers=auth).status_code == 200


def test_retry_backs_off_then_succeeds() -> None:
    sleeps: list[float] = []
    attempts = iter(
        [
            OperationalError("x", {}, Exception("down")),
            OperationalError("x", {}, Exception("down")),
            "ok",
        ]
    )

    def flaky() -> str:
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    assert retry(flaky, what="test write", sleep=sleeps.append, base_delay_s=1) == "ok"
    assert sleeps == [1, 2]


def test_retry_gives_up_and_raises(caplog: pytest.LogCaptureFixture) -> None:
    def down() -> None:
        raise OperationalError("x", {}, Exception("down"))

    with pytest.raises(OperationalError):
        retry(down, what="scrip refresh", attempts=3, sleep=lambda _: None)
    assert "scrip refresh failed after 3 attempts" in caplog.text


def test_heartbeat_upserts_one_row(db: Database) -> None:
    write_heartbeat(db, "test")
    write_heartbeat(db, "test 2")
    with db.connect() as conn:
        rows = conn.execute(text("SELECT id, source FROM bridge.heartbeat")).all()
    assert rows == [(1, "test 2")]


def test_heartbeat_task_runs_at_startup(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> None:
    settings = make_settings(heartbeat_enabled=True)
    with app_client(settings, scrip_csv) as client:
        deadline = time.monotonic() + 5
        db = client.app.state.bridge.db  # type: ignore[attr-defined]
        rows = 0
        while time.monotonic() < deadline and rows == 0:
            with db.connect() as conn:
                rows = conn.execute(text("SELECT count(*) FROM bridge.heartbeat")).scalar_one()
            time.sleep(0.05)
    assert rows == 1


def test_database_url_is_masked_everywhere(db_url: str) -> None:
    secrets = DatabaseSecrets(db_url)
    db = Database(secrets, "disable")
    try:
        for text_ in (repr(secrets), str(secrets), repr(db), repr(db.engine.url)):
            assert secrets.password not in text_
    finally:
        db.dispose()
