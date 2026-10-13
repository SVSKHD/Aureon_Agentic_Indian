"""CORS: exact allowed origins for /api, and an Origin check on the WebSocket handshake."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.testclient import WebSocketDenialResponse

from dhan_bridge import keys
from dhan_bridge.api import create_app
from dhan_bridge.auth import WS_APP_PROTOCOL, authenticate_websocket
from dhan_bridge.config import Settings
from dhan_bridge.cors import origin_allowed
from dhan_bridge.db import Database
from tests.conftest import FakeClient

ALLOWED = "https://bridge.example.org"
UNKNOWN = "https://evil.example.net"


@pytest.fixture
def api(
    make_settings: Callable[..., Settings], scrip_csv: str
) -> tuple[TestClient, dict[str, str]]:
    settings = make_settings(allowed_origins=f"{ALLOWED}, http://localhost:5173")
    app = create_app(settings, client_factory=FakeClient, fetcher=lambda: scrip_csv)
    client = TestClient(app, client=("203.0.113.7", 50000))
    client.__enter__()
    _, key = keys.create_key(app.state.bridge.db, "vue")
    yield client, {"Authorization": f"Bearer {key}"}  # type: ignore[misc]
    client.__exit__(None, None, None)


def test_allowed_origin_gets_cors_headers(api: tuple[TestClient, dict[str, str]]) -> None:
    client, auth = api
    resp = client.get("/api/universe", headers={**auth, "Origin": ALLOWED})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED
    assert "access-control-allow-credentials" not in resp.headers  # credentials disabled

    pre = client.options(
        "/api/universe",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == ALLOWED
    assert "authorization" in pre.headers["access-control-allow-headers"].lower()


def test_unknown_origin_is_rejected_on_rest(api: tuple[TestClient, dict[str, str]]) -> None:
    client, auth = api
    pre = client.options(
        "/api/universe",
        headers={"Origin": UNKNOWN, "Access-Control-Request-Method": "GET"},
    )
    assert pre.status_code == 400  # preflight refused: the browser never sends the request
    assert "access-control-allow-origin" not in pre.headers
    resp = client.get("/api/universe", headers={**auth, "Origin": UNKNOWN})
    assert "access-control-allow-origin" not in resp.headers  # the browser hides the response


def test_cors_applies_only_to_api(api: tuple[TestClient, dict[str, str]]) -> None:
    client, _ = api
    resp = client.get("/health", headers={"Origin": ALLOWED})
    assert "access-control-allow-origin" not in resp.headers


# --------------------------------------------------------------------------- WebSocket


@pytest.fixture
def ws_client(db: Database) -> tuple[TestClient, str, list[str]]:
    _, key = keys.create_key(db, "ws")
    seen: list[str] = []
    app = FastAPI()
    app.state.allowed_origins = [ALLOWED]
    app.state.verify_api_key = lambda k: seen.append(k) or keys.verify_key(db, k)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if await authenticate_websocket(websocket, timeout_s=0.5) is None:
            return
        await websocket.send_json({"type": "hello"})
        await websocket.close()

    return TestClient(app), key, seen


def test_ws_allowed_origin_passes(ws_client: tuple[TestClient, str, list[str]]) -> None:
    client, key, _ = ws_client
    with client.websocket_connect(
        "/ws",
        subprotocols=[WS_APP_PROTOCOL, f"bearer.{key}"],
        headers={"Origin": ALLOWED},
    ) as ws:
        assert ws.receive_json() == {"type": "hello"}


def test_ws_unknown_origin_gets_403_before_auth(
    ws_client: tuple[TestClient, str, list[str]],
) -> None:
    client, key, seen = ws_client
    with (
        pytest.raises(WebSocketDenialResponse) as err,
        client.websocket_connect(
            "/ws",
            subprotocols=[WS_APP_PROTOCOL, f"bearer.{key}"],  # a VALID key does not help
            headers={"Origin": UNKNOWN},
        ),
    ):
        pass
    assert err.value.status_code == 403  # an HTTP response, the socket is never accepted
    assert seen == []  # the key was never looked at


# --------------------------------------------------------------------------- settings


def test_origin_parsing_and_validation(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(allowed_origins=" https://a.example ,http://localhost:5173,")
    assert settings.allowed_origins == ["https://a.example", "http://localhost:5173"]
    for bad in ("https://*.example.org", "https://a.example/", "a.example", "ftp://a.example",
                "https://a.example/path"):  # fmt: skip
        with pytest.raises(ValidationError):
            make_settings(allowed_origins=bad)


def test_wildcard_refused_in_live_mode(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="not allowed in live mode"):
        make_settings(bridge_mode="live", allowed_origins="*")
    assert make_settings(bridge_mode="replay", allowed_origins="*").allowed_origins == ["*"]


def test_origin_allowed_helper() -> None:
    assert origin_allowed(None, [])  # non-browser clients send no Origin
    assert origin_allowed(ALLOWED, [ALLOWED])
    assert not origin_allowed(UNKNOWN, [ALLOWED])
    assert not origin_allowed(ALLOWED + ".evil.net", [ALLOWED])  # exact match only
    assert not origin_allowed(ALLOWED, [])
    assert origin_allowed(UNKNOWN, ["*"])
