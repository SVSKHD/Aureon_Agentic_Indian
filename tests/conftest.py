from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dhan_bridge.config import DatabaseSecrets, DhanSecrets, Settings
from dhan_bridge.db import Database
from dhan_bridge.dhan_client import TokenCheck
from tests.pg import PgServer, from_env_or_start

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


def _is_local(address: Any) -> bool:
    if isinstance(address, (str, bytes)):  # AF_UNIX path
        return True
    try:
        return ipaddress.ip_address(address[0]).is_loopback
    except (ValueError, TypeError, IndexError):
        return address[0] == "localhost"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach the network. Loopback (the throwaway Postgres) is allowed.

    (libpq/psycopg connects from C, below this guard; tests only ever point it at
    127.0.0.1 test servers.)
    """
    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if not _is_local(address):
            raise RuntimeError(f"network access attempted in a test: {address!r}")
        return real_connect(self, address)

    def guarded_create(address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_local(address):
            raise RuntimeError(f"network access attempted in a test: {address!r}")
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create)


# --------------------------------------------------------------------------- postgres


@pytest.fixture(scope="session")
def pg_server() -> Iterator[PgServer]:
    server = from_env_or_start(16)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def db_url(pg_server: PgServer) -> str:
    """A fresh, empty database (libpq URL) for one test."""
    return pg_server.create_database()


@pytest.fixture
def db(db_url: str) -> Iterator[Database]:
    """A migrated Database on a fresh test database."""
    database = Database(DatabaseSecrets(db_url), "disable")
    database.migrate()
    try:
        yield database
    finally:
        database.dispose()


PG_TOOLS_IMAGE = os.environ.get("PG_TEST_IMAGE", "postgres:{major}-alpine")


class FakeHost:
    """Stands in for pgtools.DockerHost (no real bridge container in tests)."""

    def __init__(self, running: bool = True, healthy: list[bool] | None = None) -> None:
        self.container = "dhan_bridge"
        self.running = running
        self.healthy = list(healthy) if healthy else []
        self.calls: list[str] = []

    def is_running(self) -> bool:
        return self.running

    def exists(self) -> bool:
        return True

    def stop(self) -> None:
        self.calls.append("stop")
        self.running = False

    def start(self) -> None:
        self.calls.append("start")
        self.running = True

    def restart(self) -> None:
        self.calls.append("restart")
        self.running = True

    def health_gate(self, timeout_s: float = 120, strict: bool = True) -> bool:
        self.calls.append("health")
        return self.healthy.pop(0) if self.healthy else True


def write_db_secrets(path: Path, url: str) -> None:
    path.write_text(f"DATABASE_URL={url}\n", encoding="utf-8")


@pytest.fixture
def scrip_csv() -> str:
    return (FIXTURES / "scrip_master_sample.csv").read_text(encoding="utf-8")


def write_secrets(path: Path, client_id: str, token: str) -> None:
    path.write_text(f"DHAN_CLIENT_ID={client_id}\nDHAN_ACCESS_TOKEN={token}\n", encoding="utf-8")


class FakeClient:
    """Stands in for DhanSdkClient; records which secrets it was built with."""

    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, secrets: DhanSecrets, ok: bool = True) -> None:
        self.secrets = secrets
        self.ok = ok
        self.calls = 0
        FakeClient.instances.append(self)

    async def verify_token(self) -> TokenCheck:
        self.calls += 1
        return TokenCheck(self.ok, "fake")


@pytest.fixture
def fake_clients() -> list[FakeClient]:
    FakeClient.instances = []
    return FakeClient.instances


@pytest.fixture
def make_settings(tmp_path: Path, db_url: str) -> Callable[..., Settings]:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    write_db_secrets(secrets_dir / "db.env", db_url)
    write_secrets(secrets_dir / "dhan.env", "1100223344", "eyJ-first-token-abcdef")
    (secrets_dir / "admin.key").write_text("correct-horse-battery\n", encoding="utf-8")
    universe = tmp_path / "universe.txt"
    universe.write_text("NSE:RELIANCE\nTCS\n# comment\nNSE:M&M\n", encoding="utf-8")

    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "bridge_mode": "live",
            "universe_file": universe,
            "secrets_file": secrets_dir / "dhan.env",
            "db_secrets_file": secrets_dir / "db.env",
            "db_sslmode": "disable",
            "heartbeat_enabled": False,
            "pg_tools_image": PG_TOOLS_IMAGE,
            "admin_key_file": secrets_dir / "admin.key",
            "data_dir": tmp_path / "data",
            "secrets_poll_interval_s": 0.05,
            "_env_file": None,
        }
        values.update(overrides)
        return Settings(**values)

    return factory
