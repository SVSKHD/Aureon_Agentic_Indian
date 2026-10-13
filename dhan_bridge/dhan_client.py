"""Thin, read-only wrapper around the ``dhanhq`` SDK (verified against dhanhq 2.3.0).

Only market-data / account-read classes may be imported here. Every
order-capable SDK class (and the legacy all-in-one client class) is deliberately
never imported; ``tests/test_no_trade.py`` enforces this.

dhanhq >= 2.1 API used:

* ``dhanhq.DhanContext(client_id, access_token)`` - holds credentials + HTTP session.
* ``dhanhq.Funds(context).get_fund_limits()`` - ``GET /v2/fundlimit``; a cheap
  authenticated read used to verify the token. Like every SDK call it returns
  ``{"status": "success"|"failure", "remarks": ..., "data": ...}`` and never raises.
* ``dhanhq.marketfeed.MarketFeed`` - WebSocket feed, wired up in a later release.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from dhan_bridge.config import DhanSecrets


@dataclass(frozen=True)
class TokenCheck:
    ok: bool
    detail: str


class MarketDataClient(Protocol):
    async def verify_token(self) -> TokenCheck: ...


class DhanSdkClient:
    """Real client backed by the installed dhanhq SDK."""

    def __init__(self, secrets: DhanSecrets) -> None:
        from dhanhq import DhanContext, Funds  # imported lazily: tests never need the SDK

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
