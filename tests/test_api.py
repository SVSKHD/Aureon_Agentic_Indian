from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dhan_bridge.api import create_app
from dhan_bridge.config import VERSION, DhanSecrets, Settings
from dhan_bridge.dhan_client import MarketDataClient
from tests.conftest import FakeClient, write_secrets

HEALTH_KEYS = {"version", "mode", "token_ok", "feed", "market_open", "db_ok"}
ADMIN = {"X-Admin-Key": "correct-horse-battery"}


def app_for(
    settings: Settings,
    scrip_csv: str,
    factory: Callable[[DhanSecrets], MarketDataClient] = FakeClient,
) -> TestClient:
    return TestClient(create_app(settings, client_factory=factory, fetcher=lambda: scrip_csv))


def test_health_live_mode(
    make_settings: Callable[..., Settings], scrip_csv: str, fake_clients: list[FakeClient]
) -> None:
    with app_for(make_settings(), scrip_csv) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert set(body) == HEALTH_KEYS
    assert body["version"] == VERSION
    assert body["mode"] == "live"
    assert body["token_ok"] is True
    assert body["feed"] == "not_started"
    assert isinstance(body["market_open"], bool)
    assert body["db_ok"] is True
    assert fake_clients[0].calls == 1  # verified at startup


def test_health_reports_bad_token(make_settings: Callable[..., Settings], scrip_csv: str) -> None:
    def bad(secrets: DhanSecrets) -> MarketDataClient:
        return FakeClient(secrets, ok=False)

    with app_for(make_settings(), scrip_csv, bad) as client:
        body = client.get("/health").json()
    assert body["token_ok"] is False


def test_health_replay_mode(
    make_settings: Callable[..., Settings], scrip_csv: str, fake_clients: list[FakeClient]
) -> None:
    def must_not_fetch() -> str:
        raise AssertionError("replay mode must not download the scrip master")

    app = create_app(make_settings(bridge_mode="replay"), FakeClient, fetcher=must_not_fetch)
    with TestClient(app) as client:
        response = client.get("/health")
    body = response.json()
    assert response.status_code == 200
    assert set(body) == HEALTH_KEYS
    assert body["mode"] == "replay"
    assert body["token_ok"] is None
    assert body["db_ok"] is True
    assert fake_clients == []  # no Dhan client in replay mode


def test_replay_mode_starts_without_secrets(
    make_settings: Callable[..., Settings], tmp_path: Path, scrip_csv: str
) -> None:
    settings = make_settings(bridge_mode="replay", secrets_file=tmp_path / "absent.env")
    with app_for(settings, scrip_csv) as client:
        assert client.get("/health").status_code == 200


def test_live_mode_requires_secrets(
    make_settings: Callable[..., Settings], tmp_path: Path, scrip_csv: str
) -> None:
    settings = make_settings(secrets_file=tmp_path / "absent.env")
    with pytest.raises(RuntimeError, match="startup aborted"), app_for(settings, scrip_csv):
        pass


def test_startup_fails_on_unknown_universe_symbol(
    make_settings: Callable[..., Settings], tmp_path: Path, scrip_csv: str
) -> None:
    universe = tmp_path / "bad_universe.txt"
    universe.write_text("NSE:RELIANCE\nNSE:NOTREAL\n", encoding="utf-8")
    with (
        pytest.raises(ValueError, match="NSE:NOTREAL"),
        app_for(make_settings(universe_file=universe), scrip_csv),
    ):
        pass


def test_universe_endpoint(make_settings: Callable[..., Settings], scrip_csv: str) -> None:
    with app_for(make_settings(), scrip_csv) as client:
        body = client.get("/api/universe").json()
    assert body["symbols"] == ["NSE:RELIANCE", "NSE:TCS", "NSE:M&M"]
    assert body["resolved"][0] == {
        "symbol": "NSE:RELIANCE",
        "exchange_segment": "NSE_EQ",
        "security_id": "2885",
    }


def test_admin_reload_rejects_bad_or_missing_key(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> None:
    with app_for(make_settings(), scrip_csv) as client:
        assert (
            client.post("/admin/reload-token", headers={"X-Admin-Key": "wrong"}).status_code == 401
        )
        assert client.post("/admin/reload-token").status_code == 401


def test_admin_reload_disabled_without_key_file(
    make_settings: Callable[..., Settings], tmp_path: Path, scrip_csv: str
) -> None:
    settings = make_settings(admin_key_file=tmp_path / "no.key")
    with app_for(settings, scrip_csv) as client:
        assert client.post("/admin/reload-token", headers=ADMIN).status_code == 503


def test_admin_reload_swaps_token_without_restart(
    make_settings: Callable[..., Settings], scrip_csv: str, fake_clients: list[FakeClient]
) -> None:
    settings = make_settings(secrets_poll_interval_s=3600)  # isolate the endpoint from the watcher
    with app_for(settings, scrip_csv) as client:
        unchanged = client.post("/admin/reload-token", headers=ADMIN).json()
        assert unchanged["changed"] is False

        write_secrets(settings.secrets_file, "1100223344", "eyJ-second-token-zzzzzz")
        response = client.post("/admin/reload-token", headers=ADMIN)
        assert response.status_code == 200
        body = response.json()
        assert body["changed"] is True
        assert body["token_ok"] is True
        assert "eyJ-second-token" not in response.text
        assert fake_clients[-1].secrets.access_token == "eyJ-second-token-zzzzzz"
        assert client.get("/health").json()["token_ok"] is True


def test_admin_reload_reports_malformed_file(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> None:
    settings = make_settings(secrets_poll_interval_s=3600)
    with app_for(settings, scrip_csv) as client:
        settings.secrets_file.write_text("DHAN_CLIENT_ID=1\n", encoding="utf-8")
        response = client.post("/admin/reload-token", headers=ADMIN)
    assert response.status_code == 422


def test_file_watch_swaps_token_without_restart(
    make_settings: Callable[..., Settings], scrip_csv: str, fake_clients: list[FakeClient]
) -> None:
    settings = make_settings()
    with app_for(settings, scrip_csv) as client:
        tmp = settings.secrets_file.with_suffix(".tmp")
        write_secrets(tmp, "1100223344", "eyJ-watched-token-yyyyyy")
        tmp.replace(settings.secrets_file)  # atomic rotation, as the runbook recommends
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if fake_clients[-1].secrets.access_token == "eyJ-watched-token-yyyyyy":
                break
            time.sleep(0.05)
        assert fake_clients[-1].secrets.access_token == "eyJ-watched-token-yyyyyy"
        while time.monotonic() < deadline and fake_clients[-1].calls == 0:
            time.sleep(0.05)
        assert fake_clients[-1].calls >= 1  # the new token was verified immediately
        assert client.get("/health").status_code == 200
