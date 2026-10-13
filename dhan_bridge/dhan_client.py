"""Thin, read-only wrapper around the ``dhanhq`` SDK (verified against dhanhq 2.3.0).

Only market-data / account-read classes may be imported here. Every
order-capable SDK class (and the legacy all-in-one client class) is deliberately
never imported; ``tests/test_no_trade.py`` enforces this statically.

Runtime guard: importing this module also disarms the SDK itself. Every
order-capable attribute of the ``dhanhq`` package (and of the private modules
that define them) is replaced with a stub that raises :class:`NoTradeViolation`
on any use, and the SDK's HTTP layer refuses order / account-mutating endpoints.
So even code that slips past the grep test cannot reach an order endpoint
through dhanhq in this process.

dhanhq >= 2.1 API used:

* ``dhanhq.DhanContext(client_id, access_token)`` - holds credentials + HTTP session.
* ``dhanhq.Funds(context).get_fund_limits()`` - ``GET /v2/fundlimit``; a cheap
  authenticated read used to verify the token. Like every SDK call it returns
  ``{"status": "success"|"failure", "remarks": ..., "data": ...}`` and never raises.
* ``dhanhq.marketfeed.MarketFeed`` - WebSocket feed, wired up in a later release.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import sys
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

import dhanhq
from dhanhq import DhanContext, Funds

from dhan_bridge.config import DhanSecrets


class NoTradeViolation(RuntimeError):
    """Raised on any attempt to use an order-capable part of the Dhan SDK."""


# (module, attribute) pairs disarmed at import. Names are strings so the static
# no-trade grep stays meaningful for real code.
_ORDER_CAPABLE: tuple[tuple[str, str], ...] = (
    ("dhanhq._order", "Order"),
    ("dhanhq._forever_order", "ForeverOrder"),
    ("dhanhq._super_order", "SuperOrder"),
    ("dhanhq._conditional_order", "ConditionalOrder"),
    ("dhanhq._trader_control", "TraderControl"),
)
# Read-mostly classes with a few mutating calls (position conversion, eDIS) are
# covered by the HTTP endpoint guard below instead of being stubbed wholesale.
_LEGACY_CLIENT = "dhanhq"  # the all-in-one class exported as dhanhq.<this name>
_BLOCKED_ENDPOINT = re.compile(
    r"^/?(orders|forever/orders|super/orders|alerts/orders|positions/convert|killswitch"
    r"|pnlExit|edis)(/|\?|$)"
)


class _ForbiddenMeta(type):
    def __getattr__(cls, name: str) -> NoReturn:
        raise NoTradeViolation(f"{cls.__name__}.{name}: order-capable Dhan SDK use is forbidden")

    def __call__(cls, *args: object, **kwargs: object) -> NoReturn:
        raise NoTradeViolation(f"{cls.__name__}(): order-capable Dhan SDK use is forbidden")


def _forbidden(name: str) -> type:
    return _ForbiddenMeta(name, (), {"__doc__": "Disarmed by dhan_bridge (no-trade invariant)."})


def install_no_trade_guard() -> None:
    """Idempotently disarm every order-capable part of the dhanhq SDK in this process."""
    for module_name, attr in _ORDER_CAPABLE:
        stub = _forbidden(attr)
        module = importlib.import_module(module_name)
        setattr(module, attr, stub)
        if hasattr(dhanhq, attr):
            setattr(dhanhq, attr, stub)
    legacy = sys.modules.get(f"dhanhq.{_LEGACY_CLIENT}")
    if legacy is not None and hasattr(legacy, _LEGACY_CLIENT):
        setattr(legacy, _LEGACY_CLIENT, _forbidden(_LEGACY_CLIENT))
    setattr(dhanhq, _LEGACY_CLIENT, _forbidden(_LEGACY_CLIENT))

    http_cls = dhanhq.DhanHTTP
    if not getattr(http_cls, "_no_trade_guarded", False):
        original = http_cls._send_request

        def guarded_send(self: Any, method: Any, endpoint: str, payload: Any = None) -> Any:
            if _BLOCKED_ENDPOINT.match(str(endpoint)):
                raise NoTradeViolation(
                    f"Dhan endpoint {endpoint} is forbidden (no-trade invariant)"
                )
            return original(self, method, endpoint, payload)

        http_cls._send_request = guarded_send
        http_cls._no_trade_guarded = True


install_no_trade_guard()


@dataclass(frozen=True)
class TokenCheck:
    ok: bool
    detail: str


class MarketDataClient(Protocol):
    async def verify_token(self) -> TokenCheck: ...


class DhanSdkClient:
    """Real client backed by the installed dhanhq SDK."""

    def __init__(self, secrets: DhanSecrets) -> None:
        self._context: Any = DhanContext(secrets.client_id, secrets.access_token)
        self._funds: Any = Funds(self._context)

    def __repr__(self) -> str:
        return "DhanSdkClient(<credentials hidden>)"

    async def verify_token(self) -> TokenCheck:
        response = await asyncio.to_thread(self._funds.get_fund_limits)
        return interpret_response(response)


def interpret_response(response: object) -> TokenCheck:
    """Map an SDK response dict onto a TokenCheck without echoing payload data."""
    if not isinstance(response, dict):
        return TokenCheck(False, "unexpected response type from dhanhq")
    if response.get("status") == "success":
        return TokenCheck(True, "ok")
    remarks = response.get("remarks")
    if isinstance(remarks, dict):
        code = remarks.get("error_code") or "unknown"
        message = remarks.get("error_message") or ""
        return TokenCheck(False, f"dhan error {code}: {message}".strip())
    return TokenCheck(False, f"request failed: {remarks}")


def make_client(secrets: DhanSecrets) -> MarketDataClient:
    return DhanSdkClient(secrets)
