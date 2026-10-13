from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dhan_bridge.config import DhanSecrets, Settings
from dhan_bridge.dhan_client import TokenCheck

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach the network."""

    def guarded_connect(self: socket.socket, address: Any) -> None:
        raise RuntimeError(f"network access attempted in a test: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_connect)


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
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    write_secrets(secrets_dir / "dhan.env", "1100223344", "eyJ-first-token-abcdef")
    (secrets_dir / "admin.key").write_text("correct-horse-battery\n", encoding="utf-8")
    universe = tmp_path / "universe.txt"
    universe.write_text("NSE:RELIANCE\nTCS\n# comment\nNSE:M&M\n", encoding="utf-8")

    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "bridge_mode": "live",
            "universe_file": universe,
            "secrets_file": secrets_dir / "dhan.env",
            "admin_key_file": secrets_dir / "admin.key",
            "data_dir": tmp_path / "data",
            "secrets_poll_interval_s": 0.05,
            "_env_file": None,
        }
        values.update(overrides)
        return Settings(**values)

    return factory
