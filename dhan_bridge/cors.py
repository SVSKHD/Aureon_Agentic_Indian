"""Cross-origin rules for the hosted Vue app.

* ``/api/*``: Starlette's CORSMiddleware, restricted to ``ALLOWED_ORIGINS`` (exact
  origins), credentials disabled: the frontend sends a bearer key in the
  Authorization header, never cookies. Other paths (``/health``, ``/admin``) get
  no CORS headers at all.
* WebSocket (``/ws``, P2): :func:`origin_allowed` is checked during the handshake
  before any authentication; a browser Origin not in the list gets HTTP 403.
"""

from __future__ import annotations

from collections.abc import Sequence

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

API_PREFIX = "/api"


class ApiCORSMiddleware(CORSMiddleware):
    """CORSMiddleware that only applies to HTTP requests under /api."""

    def __init__(self, app: ASGIApp, allowed_origins: Sequence[str]) -> None:
        super().__init__(
            app,
            allow_origins=list(allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )
        self.inner = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith(API_PREFIX):
            await super().__call__(scope, receive, send)
        else:
            await self.inner(scope, receive, send)


def origin_allowed(origin: str | None, allowed_origins: Sequence[str]) -> bool:
    """WebSocket handshake rule.

    No Origin header -> not a browser (browsers always send one on WebSockets), so
    cross-site WebSocket hijacking is impossible; the API key still decides.
    """
    if origin is None:
        return True
    return "*" in allowed_origins or origin in allowed_origins
