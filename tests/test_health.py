from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest

from dhan_bridge import health
from dhan_bridge.calendar import IST
from dhan_bridge.dhan_client import TokenCheck

GOOD: dict[str, Any] = {
    "version": "0.1.0",
    "mode": "live",
    "token_ok": True,
    "feed": "not_started",
    "market_open": True,
    "db_ok": True,
}


def test_payload_shape() -> None:
    payload = health.health_payload(
        version="0.1.0",
        mode="replay",
        token_ok=None,
        database_ok=True,
        now=datetime(2026, 10, 13, 10, 0, tzinfo=IST),
    )
    assert payload == {**GOOD, "mode": "replay", "token_ok": None}


def test_evaluate_ok() -> None:
    assert health.evaluate(200, GOOD, strict=True, expect_mode="live", expect_version="0.1.0") == []


@pytest.mark.parametrize(
    ("status", "body", "kwargs", "fragment"),
    [
        (503, GOOD, {}, "HTTP 503"),
        (200, {"mode": "live"}, {}, "missing key"),
        (200, GOOD, {"expect_mode": "replay"}, "mode="),
        (200, GOOD, {"expect_version": "9.9.9"}, "version="),
        (200, {**GOOD, "db_ok": False}, {"strict": True}, "db_ok"),
        (200, {**GOOD, "token_ok": False}, {"strict": True}, "token_ok"),
    ],
)
def test_evaluate_problems(
    status: int, body: dict[str, Any], kwargs: dict[str, Any], fragment: str
) -> None:
    assert any(fragment in p for p in health.evaluate(status, body, **kwargs))


def test_strict_live_requires_token_even_when_market_closed() -> None:
    body = {**GOOD, "token_ok": False, "market_open": False}
    assert any("token_ok" in p for p in health.evaluate(200, body, strict=True))
    assert health.evaluate(200, {**body, "token_ok": None}, strict=True) != []
    assert health.evaluate(200, body, strict=False) == []  # non-strict: liveness only


def test_strict_replay_requires_null_token() -> None:
    replay = {**GOOD, "mode": "replay", "token_ok": None}
    assert health.evaluate(200, replay, strict=True) == []
    problems = health.evaluate(200, {**replay, "token_ok": True}, strict=True)
    assert any("null" in p for p in problems)


def test_strict_rejects_unknown_mode() -> None:
    problems = health.evaluate(200, {**GOOD, "mode": "x"}, strict=True)
    assert any("unknown mode" in p for p in problems)


def test_reduced_public_payload_fails_probe() -> None:
    assert health.evaluate(200, {"status": "ok", "version": "0.1.0"}) != []


def test_db_ok(db_url: str) -> None:
    from dhan_bridge.config import DatabaseSecrets
    from dhan_bridge.db import Database

    database = Database(DatabaseSecrets(db_url), "disable")
    try:
        assert asyncio.run(health.db_ok(database)) is False  # reachable but not migrated
        database.migrate()
        assert asyncio.run(health.db_ok(database)) is True
    finally:
        database.dispose()
    assert asyncio.run(health.db_ok(None)) is False


def test_monitor_handles_missing_and_crashing_clients() -> None:
    class Boom:
        async def verify_token(self) -> TokenCheck:
            raise ConnectionError("down")

    monitor = health.TokenMonitor(client_getter=lambda: None)
    assert asyncio.run(monitor.check()).ok is False
    monitor = health.TokenMonitor(client_getter=lambda: Boom())
    state = asyncio.run(monitor.check())
    assert state.ok is False
    assert "ConnectionError" in state.detail


def test_probe_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(health, "fetch", lambda url: (200, GOOD))
    assert health.main(["probe", "--strict", "--expect-mode", "live"]) == 0
    assert health.main(["probe", "--expect-mode", "replay"]) == 1
    assert "UNHEALTHY" in capsys.readouterr().out

    def unreachable(url: str) -> tuple[int, dict[str, Any]]:
        raise OSError("connection refused")

    monkeypatch.setattr(health, "fetch", unreachable)
    assert health.main(["probe"]) == 1


def test_fetch_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class Resp:
        status = 200

        def read(self) -> bytes:
            return json.dumps(GOOD).encode()

        def __enter__(self) -> "Resp":  # noqa: UP037
            return self

        def __exit__(self, *a: object) -> None:
            return None

    monkeypatch.setattr(health.urllib.request, "urlopen", lambda url, timeout: Resp())
    assert health.fetch("http://x/health") == (200, GOOD)
