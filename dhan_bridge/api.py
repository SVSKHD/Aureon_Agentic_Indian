"""FastAPI application.

Run with exactly ONE worker - the Dhan feed must exist once per deployment::

    uvicorn --factory dhan_bridge.api:create_app --workers 1
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from typing import IO, Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request

from dhan_bridge.auth import is_loopback, request_api_key, require_api_key
from dhan_bridge.backup import acquire_instance_lock
from dhan_bridge.calendar import now_ist
from dhan_bridge.config import (
    VERSION,
    DhanSecrets,
    SecretsError,
    SecretsStore,
    Settings,
    install_redactor,
    read_admin_key,
)
from dhan_bridge.dhan_client import MarketDataClient, make_client
from dhan_bridge.health import TokenMonitor, db_ok, health_payload
from dhan_bridge.instruments import (
    Fetcher,
    Instrument,
    InstrumentStore,
    http_fetcher,
    load_universe,
)
from dhan_bridge.keys import verify_key
from dhan_bridge.migrations import migrate

log = logging.getLogger("dhan_bridge")

ClientFactory = Callable[[DhanSecrets], MarketDataClient]
SCRIP_REFRESH_CHECK_S = 3600.0


@dataclass
class BridgeState:
    settings: Settings
    secrets: SecretsStore
    instruments: InstrumentStore
    monitor: TokenMonitor
    universe: list[str] = field(default_factory=list)
    resolved: list[Instrument] = field(default_factory=list)
    client: MarketDataClient | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    lock: IO[str] | None = None

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Start a background task that removes itself from ``tasks`` when done."""
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        task.add_done_callback(self._forget)
        return task

    def _forget(self, task: asyncio.Task[None]) -> None:
        with contextlib.suppress(ValueError):
            self.tasks.remove(task)

    @property
    def live(self) -> bool:
        return self.settings.bridge_mode == "live"


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    install_redactor()


async def _poll_secrets(state: BridgeState) -> None:
    while True:
        await asyncio.sleep(state.settings.secrets_poll_interval_s)
        state.secrets.poll()


async def _refresh_scrip_master(state: BridgeState, fetch: Fetcher) -> None:
    while True:
        await asyncio.sleep(SCRIP_REFRESH_CHECK_S)
        try:
            await asyncio.to_thread(state.instruments.refresh_if_stale, now_ist().date(), fetch)
        except Exception:
            log.exception("scheduled scrip master refresh failed")


def _startup(
    settings: Settings, fetch: Fetcher, today: date, client_factory: ClientFactory
) -> BridgeState:
    # One instance per data directory: the Dhan feed must exist exactly once.
    lock = acquire_instance_lock(settings.data_dir)
    try:
        state = _startup_locked(settings, fetch, today, client_factory)
    except BaseException:
        lock.close()
        raise
    state.lock = lock
    return state


def _startup_locked(
    settings: Settings, fetch: Fetcher, today: date, client_factory: ClientFactory
) -> BridgeState:
    secrets = SecretsStore(settings.secrets_file)
    try:
        secrets.reload()
    except SecretsError as exc:
        if settings.bridge_mode == "live":
            raise RuntimeError(f"startup aborted: {exc}") from None
        log.warning("replay mode: continuing without Dhan secrets (%s)", exc)

    result = migrate(settings.db_path, settings.backups_dir, VERSION)
    if result.applied:
        log.info("applied migrations %s (backup: %s)", result.applied, result.backup)

    state = BridgeState(
        settings=settings,
        secrets=secrets,
        instruments=InstrumentStore(settings.db_path),
        monitor=TokenMonitor(client_getter=lambda: None),
    )
    state.monitor.client_getter = lambda: state.client
    state.universe = load_universe(settings.universe_file)

    if state.live:
        state.instruments.refresh_if_stale(today, fetch)
        state.resolved = state.instruments.resolve_all(state.universe, str(settings.universe_file))
        state.client = client_factory(secrets.require())
    elif state.instruments.count() > 0:
        state.resolved = state.instruments.resolve_all(state.universe, str(settings.universe_file))
    else:
        log.warning("replay mode: no cached scrip master; universe left unresolved")

    log.info(
        "dhan_bridge %s starting: mode=%s universe=%d resolved=%d",
        VERSION,
        settings.bridge_mode,
        len(state.universe),
        len(state.resolved),
    )
    return state


def create_app(
    settings: Settings | None = None,
    client_factory: ClientFactory = make_client,
    fetcher: Fetcher | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _configure_logging()
        cfg = settings or Settings()
        fetch = fetcher or http_fetcher(cfg.scrip_master_url)
        state = _startup(cfg, fetch, now_ist().date(), client_factory)
        app.state.bridge = state
        app.state.verify_api_key = partial(verify_key, cfg.db_path)

        if state.live:

            def on_new_secrets(new: DhanSecrets) -> None:
                state.client = client_factory(new)
                state.spawn(_recheck(state))

            state.secrets.add_listener(on_new_secrets)
            await state.monitor.check()
            state.spawn(state.monitor.run_forever(cfg.token_check_interval_s))
            state.spawn(_refresh_scrip_master(state, fetch))
        state.spawn(_poll_secrets(state))
        try:
            yield
        finally:
            tasks = list(state.tasks)
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if state.lock is not None:
                state.lock.close()

    app = FastAPI(title="dhan_bridge", version=VERSION, lifespan=lifespan)

    def bridge(request: Request) -> BridgeState:
        state: BridgeState = request.app.state.bridge
        return state

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        """Public: ``{status, version}``. Full payload for loopback peers or a valid API key."""
        state = bridge(request)
        client_host = request.client.host if request.client else None
        if not is_loopback(client_host) and request_api_key(request) is None:
            return {"status": "ok", "version": VERSION}
        return health_payload(
            version=VERSION,
            mode=state.settings.bridge_mode,
            token_ok=state.monitor.state.ok if state.live else None,
            database_ok=db_ok(state.settings.db_path),
        )

    @app.post("/admin/reload-token")
    async def reload_token(
        request: Request, x_admin_key: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        state = bridge(request)
        expected = read_admin_key(state.settings.admin_key_file)
        if expected is None:
            raise HTTPException(503, "admin key not configured")
        if x_admin_key is None or not hmac.compare_digest(x_admin_key.encode(), expected.encode()):
            raise HTTPException(401, "invalid admin key")
        try:
            changed = state.secrets.reload()
        except SecretsError as exc:
            raise HTTPException(422, str(exc)) from None
        token = await state.monitor.check() if state.live else None
        current = state.secrets.current
        return {
            "changed": changed,
            "fingerprint": current.fingerprint if current else None,
            "token_ok": token.ok if token else None,
            "detail": token.detail if token else "replay mode: token not checked",
        }

    # Every /api/* route goes on this router and therefore requires a bearer key.
    api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])

    @api.get("/universe")
    async def universe(request: Request) -> dict[str, Any]:
        state = bridge(request)
        return {
            "symbols": state.universe,
            "resolved": [
                {
                    "symbol": i.symbol,
                    "exchange_segment": i.exchange_segment,
                    "security_id": i.security_id,
                }
                for i in state.resolved
            ],
        }

    app.include_router(api)
    return app


async def _recheck(state: BridgeState) -> None:
    await state.monitor.check()
