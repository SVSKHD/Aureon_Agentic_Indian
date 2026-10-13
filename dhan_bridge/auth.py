"""Request authentication.

* HTTP: ``Authorization: Bearer <key>`` on every ``/api/*`` route
  (:func:`require_api_key`, attached to the ``/api`` router).
* WebSocket (prepared for P2): :func:`authenticate_websocket` first rejects any
  browser Origin not in ALLOWED_ORIGINS (HTTP 403), then accepts the key
  either in ``Sec-WebSocket-Protocol`` (``bearer.<key>``, alongside
  ``dhan-bridge.v1``) or as the first message. Keys in the query string are
  refused outright: query strings end up in nginx access logs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Callable

from fastapi import HTTPException, Request, WebSocket, status
from starlette.responses import PlainTextResponse

from dhan_bridge.cors import origin_allowed
from dhan_bridge.db import DatabaseUnavailable
from dhan_bridge.keys import ApiKey

WS_APP_PROTOCOL = "dhan-bridge.v1"
WS_BEARER_PREFIX = "bearer."
WS_AUTH_TIMEOUT_S = 5.0
WS_CLOSE_UNAUTHORIZED = 4401  # application-defined close code (4000-4999)
QUERY_KEY_PARAMS = frozenset({"key", "api_key", "apikey", "token", "access_token"})

KeyVerifier = Callable[[str | None], ApiKey | None]

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="missing or invalid API key",
    headers={"WWW-Authenticate": "Bearer"},
)


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def is_loopback(host: str | None) -> bool:
    """True only for a loopback TCP peer. Proxy headers are never consulted."""
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _verifier(request: Request | WebSocket) -> KeyVerifier:
    verify: KeyVerifier = request.app.state.verify_api_key
    return verify


DB_UNAVAILABLE = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail="database unavailable",
    headers={"Retry-After": "30"},
)


def request_api_key(request: Request) -> ApiKey | None:
    """Raises DatabaseUnavailable if the key cannot be checked right now."""
    return _verifier(request)(bearer_token(request.headers.get("authorization")))


def optional_api_key(request: Request) -> ApiKey | None:
    """Like request_api_key, but a database outage just means 'not authenticated'."""
    try:
        return request_api_key(request)
    except DatabaseUnavailable:
        return None


async def require_api_key(request: Request) -> ApiKey:
    """FastAPI dependency: 401 unless a valid, unrevoked bearer key is presented
    (503 if the database cannot be asked)."""
    try:
        key = request_api_key(request)
    except DatabaseUnavailable:
        raise DB_UNAVAILABLE from None
    if key is None:
        raise UNAUTHORIZED
    request.state.api_key = key
    return key


def ws_protocol_key(header: str | None) -> str | None:
    """Extract ``<key>`` from a ``Sec-WebSocket-Protocol: dhan-bridge.v1, bearer.<key>`` header."""
    if not header:
        return None
    for item in header.split(","):
        item = item.strip()
        if item.startswith(WS_BEARER_PREFIX) and len(item) > len(WS_BEARER_PREFIX):
            return item[len(WS_BEARER_PREFIX) :]
    return None


def _first_message_key(text: str) -> str | None:
    """First message may be ``{"type": "auth", "key": "<key>"}`` or the bare key."""
    text = text.strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if isinstance(payload, dict) and payload.get("type") == "auth":
            key = payload.get("key")
            return key if isinstance(key, str) else None
        return None
    return text or None


async def authenticate_websocket(
    websocket: WebSocket, timeout_s: float = WS_AUTH_TIMEOUT_S
) -> ApiKey | None:
    """Authenticate a WebSocket before any data flows.

    On success the socket is accepted (selecting ``dhan-bridge.v1``, never echoing
    the key) and the key record is returned. On failure the socket is closed and
    None is returned; the caller must simply return.
    """
    # 1. Origin first: a browser page from an origin we don't serve is refused with
    #    HTTP 403 before any key is looked at.
    allowed: list[str] = getattr(websocket.app.state, "allowed_origins", [])
    if not origin_allowed(websocket.headers.get("origin"), allowed):
        await websocket.send_denial_response(PlainTextResponse("origin not allowed", 403))
        return None

    verify = _verifier(websocket)
    if QUERY_KEY_PARAMS & {k.lower() for k in websocket.query_params}:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="no keys in URLs")
        return None

    header = websocket.headers.get("sec-websocket-protocol")
    offered = [p.strip() for p in header.split(",")] if header else []
    subprotocol = WS_APP_PROTOCOL if WS_APP_PROTOCOL in offered else None

    header_key = ws_protocol_key(header)
    if header_key is not None:
        record = _safe_verify(verify, header_key)
        if record is None:
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="invalid API key")
            return None
        await websocket.accept(subprotocol=subprotocol)
        return record

    await websocket.accept(subprotocol=subprotocol)
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=timeout_s)
    except (TimeoutError, KeyError, RuntimeError):
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="authentication timeout")
        return None
    record = _safe_verify(verify, _first_message_key(first))
    if record is None:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="invalid API key")
        return None
    await websocket.send_json({"type": "auth_ok", "key_id": record.id})
    return record


def _safe_verify(verify: KeyVerifier, key: str | None) -> ApiKey | None:
    try:
        return verify(key)
    except DatabaseUnavailable:
        return None
