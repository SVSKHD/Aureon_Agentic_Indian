from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dhan_bridge.config import DhanSecrets
from dhan_bridge.dhan_client import DhanSdkClient, interpret_response, make_client


@pytest.mark.parametrize(
    ("response", "ok", "fragment"),
    [
        ({"status": "success", "remarks": "", "data": {"availabelBalance": 1}}, True, "ok"),
        (
            {
                "status": "failure",
                "remarks": {"error_code": "DH-901", "error_message": "Invalid token"},
                "data": "",
            },
            False,
            "DH-901",
        ),
        ({"status": "failure", "remarks": "timed out", "data": ""}, False, "timed out"),
        ("nonsense", False, "unexpected"),
    ],
)
def test_interpret_response(response: Any, ok: bool, fragment: str) -> None:
    result = interpret_response(response)
    assert result.ok is ok
    assert fragment in result.detail


def test_sdk_client_uses_fund_limits_without_leaking(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = DhanSecrets("1100223344", "eyJ-real-looking-token")
    client = make_client(secrets)
    assert isinstance(client, DhanSdkClient)
    assert "eyJ" not in repr(client)
    calls: list[str] = []

    def fake_get(endpoint: str) -> dict[str, Any]:
        calls.append(endpoint)
        return {"status": "success", "remarks": "", "data": {}}

    monkeypatch.setattr(client._context.get_dhan_http(), "get", fake_get)
    assert asyncio.run(client.verify_token()).ok is True
    assert calls == ["/fundlimit"]
