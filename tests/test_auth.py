from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dhan_bridge import keys
from dhan_bridge.api import create_app
from dhan_bridge.auth import (
    WS_APP_PROTOCOL,
    WS_CLOSE_UNAUTHORIZED,
    authenticate_websocket,
    bearer_token,
    is_loopback,
    ws_protocol_key,
)
from dhan_bridge.config import VERSION, Settings
from dhan_bridge.migrations import migrate
from tests.conftest import FakeClient

LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.7", 50000)
FULL_KEYS = {"version", "mode", "token_ok", "feed", "market_open", "db_ok"}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "bridge.db"
    migrate(path, tmp_path / "backups", "test")
    return path


def client_for(settings: Settings, scrip_csv: str, client: tuple[str, int] = REMOTE) -> TestClient:
    app = create_app(settings, client_factory=FakeClient, fetcher=lambda: scrip_csv)
    return TestClient(app, client=client)


# --------------------------------------------------------------------------- key store


def test_keys_are_stored_hashed(db: Path) -> None:
    key_id, key = keys.create_key(db, "vue")
    assert key.startswith("dbk_")
    assert len(key) > 40
    raw = db.read_bytes()
    assert key.encode() not in raw
    assert keys.hash_key(key).encode() in raw
    record = keys.verify_key(db, key)
    assert record is not None
    assert record.id == key_id
    assert record.name == "vue"


def test_revoked_unknown_and_malformed_keys_fail(db: Path) -> None:
    key_id, key = keys.create_key(db, "vue")
    assert keys.revoke_key(db, key_id) is True
    assert keys.revoke_key(db, key_id) is False  # already revoked
    assert keys.verify_key(db, key) is None
    assert keys.verify_key(db, "dbk_" + "x" * 43) is None
    assert keys.verify_key(db, "not-a-key") is None
    assert keys.verify_key(db, None) is None
    assert keys.verify_key(db.parent / "missing.db", key) is None


def test_keys_cli(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert keys.main(["--db", str(db), "create", "frontend"]) == 0
    out = capsys.readouterr().out
    key = out.strip().splitlines()[-1]
    assert keys.verify_key(db, key) is not None

    assert keys.main(["--db", str(db), "list"]) == 0
    listing = capsys.readouterr().out
    assert "frontend" in listing
    assert "active" in listing
    assert key not in listing
    assert keys.hash_key(key) not in listing

    assert keys.main(["--db", str(db), "revoke", "1"]) == 0
    assert keys.main(["--db", str(db), "revoke", "1"]) == 1
    assert keys.main(["--db", str(db), "list"]) == 0
    assert "revoked" in capsys.readouterr().out
    assert keys.main(["--db", str(db), "create", "  "]) == 2


def test_keys_cli_on_unmigrated_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert keys.main(["--db", str(tmp_path / "none.db"), "list"]) == 2
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert keys.main(["--db", str(empty), "list"]) == 2
    assert "migrate" in capsys.readouterr().err


# --------------------------------------------------------------------------- HTTP


@pytest.mark.parametrize(
    "header",
    [None, "Bearer", "Bearer dbk_wrong", "Basic dXNlcjpwYXNz", "dbk_no_scheme"],
)
def test_api_rejects_missing_or_invalid_key(
    make_settings: Callable[..., Settings], scrip_csv: str, header: str | None
) -> None:
    headers = {"Authorization": header} if header else {}
    with client_for(make_settings(), scrip_csv) as client:
        response = client.get("/api/universe", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_api_rejects_revoked_key_and_accepts_valid(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> None:
    settings = make_settings()
    with client_for(settings, scrip_csv) as client:
        key_id, key = keys.create_key(settings.db_path, "vue")
        auth = {"Authorization": f"Bearer {key}"}
        assert client.get("/api/universe", headers=auth).status_code == 200
        keys.revoke_key(settings.db_path, key_id)
        assert client.get("/api/universe", headers=auth).status_code == 401


def test_loopback_does_not_bypass_api_auth(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> None:
    with client_for(make_settings(), scrip_csv, client=LOOPBACK) as client:
        assert client.get("/api/universe").status_code == 401


def test_every_api_route_requires_a_key(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> None:
    app = create_app(make_settings(), client_factory=FakeClient, fetcher=lambda: scrip_csv)
    paths = {p: ops for p, ops in app.openapi()["paths"].items() if p.startswith("/api")}
    assert paths
    with TestClient(app, client=REMOTE) as client:
        for path, operations in paths.items():
            for method in operations:
                assert client.request(method.upper(), path).status_code == 401, path


def test_public_health_is_reduced(make_settings: Callable[..., Settings], scrip_csv: str) -> None:
    with client_for(make_settings(), scrip_csv) as client:
        response = client.get("/health")
        spoofed = client.get("/health", headers={"X-Forwarded-For": "127.0.0.1"})
        bad_key = client.get("/health", headers={"Authorization": "Bearer dbk_nope"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": VERSION}
    assert spoofed.json() == {"status": "ok", "version": VERSION}
    assert bad_key.json() == {"status": "ok", "version": VERSION}


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_loopback_gets_full_health(
    make_settings: Callable[..., Settings], scrip_csv: str, host: str
) -> None:
    with client_for(make_settings(), scrip_csv, client=(host, 1234)) as client:
        body = client.get("/health").json()
    assert set(body) == FULL_KEYS


def test_valid_key_gets_full_health(make_settings: Callable[..., Settings], scrip_csv: str) -> None:
    settings = make_settings()
    with client_for(settings, scrip_csv) as client:
        _, key = keys.create_key(settings.db_path, "monitoring")
        body = client.get("/health", headers={"Authorization": f"Bearer {key}"}).json()
    assert set(body) == FULL_KEYS


def test_helpers() -> None:
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer   abc ") == "abc"
    assert bearer_token("Token abc") is None
    assert bearer_token(None) is None
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("127.5.5.5")
    assert not is_loopback("10.0.0.1")
    assert not is_loopback("testclient")
    assert not is_loopback(None)
    assert ws_protocol_key(f"{WS_APP_PROTOCOL}, bearer.dbk_abc") == "dbk_abc"
    assert ws_protocol_key(WS_APP_PROTOCOL) is None
    assert ws_protocol_key("bearer.") is None
    assert ws_protocol_key(None) is None


# --------------------------------------------------------------------------- WebSocket helper (P2)


@pytest.fixture
def ws_app(db: Path) -> tuple[TestClient, str, int]:
    key_id, key = keys.create_key(db, "ws")
    app = FastAPI()
    app.state.verify_api_key = lambda k: keys.verify_key(db, k)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        record = await authenticate_websocket(websocket, timeout_s=0.5)
        if record is None:
            return
        await websocket.send_json({"type": "hello", "key_id": record.id})
        await websocket.close()

    return TestClient(app), key, key_id


def test_ws_key_in_protocol_header(ws_app: tuple[TestClient, str, int]) -> None:
    client, key, key_id = ws_app
    with client.websocket_connect("/ws", subprotocols=[WS_APP_PROTOCOL, f"bearer.{key}"]) as ws:
        assert ws.accepted_subprotocol == WS_APP_PROTOCOL  # the key is never echoed back
        assert ws.receive_json() == {"type": "hello", "key_id": key_id}


def test_ws_bad_key_in_protocol_header_is_rejected(ws_app: tuple[TestClient, str, int]) -> None:
    client, _, _ = ws_app
    with (
        pytest.raises(WebSocketDisconnect) as err,
        client.websocket_connect("/ws", subprotocols=[WS_APP_PROTOCOL, "bearer.dbk_bad"]),
    ):
        pass
    assert err.value.code == WS_CLOSE_UNAUTHORIZED


@pytest.mark.parametrize("as_json", [True, False])
def test_ws_key_as_first_message(ws_app: tuple[TestClient, str, int], as_json: bool) -> None:
    client, key, key_id = ws_app
    with client.websocket_connect("/ws") as ws:
        if as_json:
            ws.send_json({"type": "auth", "key": key})
        else:
            ws.send_text(key)
        assert ws.receive_json() == {"type": "auth_ok", "key_id": key_id}
        assert ws.receive_json() == {"type": "hello", "key_id": key_id}


@pytest.mark.parametrize("message", ['{"type": "auth", "key": "dbk_bad"}', "{not json", '{"x": 1}'])
def test_ws_bad_first_message_is_rejected(
    ws_app: tuple[TestClient, str, int], message: str
) -> None:
    client, _, _ = ws_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(message)
        with pytest.raises(WebSocketDisconnect) as err:
            ws.receive_json()
    assert err.value.code == WS_CLOSE_UNAUTHORIZED


def test_ws_auth_timeout(ws_app: tuple[TestClient, str, int]) -> None:
    client, _, _ = ws_app
    with client.websocket_connect("/ws") as ws, pytest.raises(WebSocketDisconnect) as err:
        ws.receive_json()
    assert err.value.code == WS_CLOSE_UNAUTHORIZED


@pytest.mark.parametrize("param", ["key", "token", "api_key"])
def test_ws_refuses_keys_in_query_string(ws_app: tuple[TestClient, str, int], param: str) -> None:
    client, key, _ = ws_app
    with pytest.raises(WebSocketDisconnect) as err, client.websocket_connect(f"/ws?{param}={key}"):
        pass
    assert err.value.code == 1008
