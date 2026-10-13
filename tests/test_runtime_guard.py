"""Runtime half of the no-trade invariant (the static half is test_no_trade.py)."""

from __future__ import annotations

import importlib

import pytest

import dhan_bridge.dhan_client as dc
from dhan_bridge.dhan_client import NoTradeViolation

ORDER_CLASSES = ["Order", "ForeverOrder", "SuperOrder", "ConditionalOrder", "TraderControl"]


@pytest.mark.parametrize("name", [*ORDER_CLASSES, "dhanhq"])
def test_order_capable_package_attributes_raise(name: str) -> None:
    sdk = importlib.import_module("dhanhq")
    stub = getattr(sdk, name)
    with pytest.raises(NoTradeViolation):
        stub(object())
    with pytest.raises(NoTradeViolation):
        stub.place_order  # noqa: B018 - attribute access itself must raise


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("dhanhq._order", "Order"),
        ("dhanhq._forever_order", "ForeverOrder"),
        ("dhanhq._super_order", "SuperOrder"),
        ("dhanhq._conditional_order", "ConditionalOrder"),
        ("dhanhq._trader_control", "TraderControl"),
        ("dhanhq.dhanhq", "dhanhq"),
    ],
)
def test_private_module_imports_are_disarmed_too(module: str, name: str) -> None:
    cls = getattr(importlib.import_module(module), name)
    with pytest.raises(NoTradeViolation):
        cls(object())


@pytest.mark.parametrize(
    "endpoint",
    ["/orders", "/orders/123", "/super/orders", "/forever/orders", "/alerts/orders",
     "/positions/convert", "/killswitch?killSwitchStatus=ACTIVATE", "/pnlExit", "/edis/tpin"],
)  # fmt: skip
def test_http_layer_refuses_order_endpoints(endpoint: str) -> None:
    sdk = importlib.import_module("dhanhq")
    http = sdk.DhanHTTP("client", "token")
    for call in (lambda: http.post(endpoint, {}), lambda: http.put(endpoint, {})):
        with pytest.raises(NoTradeViolation):
            call()
    with pytest.raises(NoTradeViolation):
        http.delete(endpoint)


def test_read_endpoints_still_reach_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = importlib.import_module("dhanhq")
    http = sdk.DhanHTTP("client", "token")
    calls: list[str] = []
    monkeypatch.setattr(http.session, "get", lambda url, **kw: calls.append(url) or _Resp())
    http.get("/fundlimit")
    http.get("/orders-history-not-a-real-endpoint")  # prefix match only on whole segments
    assert len(calls) == 2


def test_guard_is_idempotent_and_reapplied_after_reimport() -> None:
    dc.install_no_trade_guard()
    dc.install_no_trade_guard()
    with pytest.raises(NoTradeViolation):
        importlib.import_module("dhanhq").Order(object())


class _Resp:
    status_code = 200
    content = b"{}"
