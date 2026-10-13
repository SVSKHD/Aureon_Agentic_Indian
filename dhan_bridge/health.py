"""Token health monitoring, the ``/health`` payload, and a probe CLI.

The token is verified at startup, every ``token_check_interval_s`` (hourly by
default) and immediately after the secrets file changes.

Probe CLI (used by the Docker HEALTHCHECK, the CI smoke test and deploy gates)::

    python -m dhan_bridge.health probe [--url URL] [--strict] [--expect-mode replay]
                                       [--expect-version 0.1.0]

``--strict`` additionally requires ``db_ok`` and, in live mode, ``token_ok == true``
(regardless of market hours); in replay mode ``token_ok`` must be ``null``.
Exit 0 when healthy, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dhan_bridge.calendar import is_market_open, now_ist
from dhan_bridge.dhan_client import MarketDataClient, TokenCheck

log = logging.getLogger(__name__)

FEED_NOT_STARTED = "not_started"
DEFAULT_PROBE_URL = "http://127.0.0.1:8000/health"


@dataclass
class TokenState:
    ok: bool | None = None
    detail: str = "not checked yet"
    checked_at: datetime | None = None


@dataclass
class TokenMonitor:
    """Tracks whether the current Dhan token works. ``client_getter`` returns the live client."""

    client_getter: Callable[[], MarketDataClient | None]
    state: TokenState = field(default_factory=TokenState)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check(self) -> TokenState:
        async with self._lock:
            client = self.client_getter()
            if client is None:
                result = TokenCheck(False, "no Dhan client (secrets not loaded)")
            else:
                try:
                    result = await client.verify_token()
                except Exception as exc:  # never let a health probe crash the app
                    result = TokenCheck(False, f"token check raised {type(exc).__name__}")
            self.state = TokenState(result.ok, result.detail, now_ist())
            level = logging.INFO if result.ok else logging.WARNING
            log.log(level, "token check: ok=%s detail=%s", result.ok, result.detail)
            return self.state

    async def run_forever(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await self.check()


def db_ok(db_path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            conn.execute("SELECT 1 FROM schema_migrations LIMIT 1")
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return True


def health_payload(
    *,
    version: str,
    mode: str,
    token_ok: bool | None,
    database_ok: bool,
    now: datetime | None = None,
    feed: str = FEED_NOT_STARTED,
) -> dict[str, Any]:
    return {
        "version": version,
        "mode": mode,
        "token_ok": token_ok,
        "feed": feed,
        "market_open": is_market_open(now or now_ist()),
        "db_ok": database_ok,
    }


# --------------------------------------------------------------------------- probe CLI


def evaluate(
    status_code: int,
    body: dict[str, Any],
    *,
    strict: bool = False,
    expect_mode: str | None = None,
    expect_version: str | None = None,
) -> list[str]:
    """Return a list of problems; empty means healthy."""
    problems: list[str] = []
    if status_code != 200:
        problems.append(f"HTTP {status_code}")
    for key in ("version", "mode", "token_ok", "feed", "market_open", "db_ok"):
        if key not in body:
            problems.append(f"missing key {key}")
    if expect_mode and body.get("mode") != expect_mode:
        problems.append(f"mode={body.get('mode')!r}, expected {expect_mode!r}")
    if expect_version and body.get("version") != expect_version:
        problems.append(f"version={body.get('version')!r}, expected {expect_version!r}")
    if strict:
        if body.get("db_ok") is not True:
            problems.append("db_ok is not true")
        mode = body.get("mode")
        if mode == "live":
            # Regardless of market hours: a deploy with a dead token fails the gate.
            if body.get("token_ok") is not True:
                problems.append("live mode requires token_ok == true")
        elif mode == "replay":
            if body.get("token_ok") is not None:
                problems.append("replay mode requires token_ok == null")
        else:
            problems.append(f"unknown mode {mode!r}")
    return problems


def fetch(url: str, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local URL
            return int(response.status), json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dhan_bridge.health")
    parser.add_argument("command", choices=["probe"])
    parser.add_argument("--url", default=DEFAULT_PROBE_URL)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--expect-mode")
    parser.add_argument("--expect-version")
    args = parser.parse_args(argv)
    try:
        status_code, body = fetch(args.url)
    except (OSError, ValueError) as exc:
        print(f"UNHEALTHY: {args.url} unreachable: {exc}")
        return 1
    problems = evaluate(
        status_code,
        body,
        strict=args.strict,
        expect_mode=args.expect_mode,
        expect_version=args.expect_version,
    )
    if problems:
        print(f"UNHEALTHY: {'; '.join(problems)} body={json.dumps(body)}")
        return 1
    print(f"HEALTHY: {json.dumps(body)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
