"""Dhan scrip master cache and symbol resolution.

The compact scrip master CSV is downloaded at most once per IST calendar day and
cached in PostgreSQL (``bridge.instruments``; equity and index rows only). The
cache is replaced in ONE transaction with a bulk ``COPY``; if a download fails
the previous (stale) cache stays in use. Symbols are written
``EXCHANGE:TRADING_SYMBOL`` (e.g. ``NSE:RELIANCE``) and resolve to the
``(exchange_segment, security_id)`` pair the Dhan feed expects.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dhan_bridge.db import Database
from dhan_bridge.models import instruments, meta

log = logging.getLogger(__name__)

Fetcher = Callable[[], str]

# Compact scrip master columns (https://images.dhan.co/api-data/api-scrip-master.csv)
COL_EXCHANGE = "SEM_EXM_EXCH_ID"
COL_SEGMENT = "SEM_SEGMENT"
COL_SECURITY_ID = "SEM_SMST_SECURITY_ID"
COL_INSTRUMENT = "SEM_INSTRUMENT_NAME"
COL_SYMBOL = "SEM_TRADING_SYMBOL"
COL_LOT = "SEM_LOT_UNITS"
COL_TICK = "SEM_TICK_SIZE"
COL_SERIES = "SEM_SERIES"
COL_NAME = "SM_SYMBOL_NAME"
REQUIRED_COLUMNS = (COL_EXCHANGE, COL_SEGMENT, COL_SECURITY_ID, COL_INSTRUMENT, COL_SYMBOL)

# (exchange, segment code) -> Dhan exchange_segment
SEGMENTS = {
    ("NSE", "E"): "NSE_EQ",
    ("BSE", "E"): "BSE_EQ",
    ("NSE", "I"): "IDX_I",
    ("BSE", "I"): "IDX_I",
}
META_KEY_DATE = "scrip_master_date"


class UnknownSymbolsError(ValueError):
    def __init__(self, symbols: list[str], source: str) -> None:
        self.symbols = symbols
        super().__init__(
            f"{len(symbols)} symbol(s) in {source} are not in the Dhan scrip master: "
            f"{', '.join(symbols)}. Fix the universe file or check the trading symbol."
        )


@dataclass(frozen=True)
class Instrument:
    symbol: str  # canonical "NSE:RELIANCE"
    exchange_segment: str  # "NSE_EQ"
    security_id: str  # "2885"
    lot_size: int = 1
    tick_size: float = 0.0
    name: str = ""


def http_fetcher(url: str, timeout: float = 60.0) -> Fetcher:
    def fetch() -> str:
        log.info("downloading scrip master from %s", url)
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed https URL
            data: bytes = response.read()
        return data.decode("utf-8-sig")

    return fetch


def normalise_symbol(raw: str, default_exchange: str = "NSE") -> str:
    text = raw.strip()
    exchange, sep, symbol = text.partition(":")
    if not sep:
        exchange, symbol = default_exchange, text
    exchange, symbol = exchange.strip().upper(), symbol.strip().upper()
    if not exchange or not symbol:
        raise ValueError(f"invalid symbol {raw!r}; expected EXCHANGE:SYMBOL")
    return f"{exchange}:{symbol}"


def load_universe(path: Path) -> list[str]:
    """Read the universe file: one symbol per line, ``#`` comments, duplicates removed."""
    symbols: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            symbol = normalise_symbol(line)
            if symbol not in symbols:
                symbols.append(symbol)
    if not symbols:
        raise ValueError(f"universe file {path} contains no symbols")
    return symbols


Row = tuple[str, str, str, str, str, str, int, Decimal, str]
COPY_COLUMNS = (
    "exchange",
    "exchange_segment",
    "security_id",
    "trading_symbol",
    "instrument",
    "series",
    "lot_size",
    "tick_size",
    "name",
)


def parse_scrip_master(text: str) -> Iterator[Row]:
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"scrip master is missing columns: {', '.join(missing)}")
    for row in reader:
        exchange = (row.get(COL_EXCHANGE) or "").strip().upper()
        segment = SEGMENTS.get((exchange, (row.get(COL_SEGMENT) or "").strip().upper()))
        if segment is None:
            continue
        yield (
            exchange,
            segment,
            (row.get(COL_SECURITY_ID) or "").strip(),
            (row.get(COL_SYMBOL) or "").strip().upper(),
            (row.get(COL_INSTRUMENT) or "").strip().upper(),
            (row.get(COL_SERIES) or "").strip().upper(),
            _to_int(row.get(COL_LOT), 1),
            _to_decimal(row.get(COL_TICK)),
            (row.get(COL_NAME) or "").strip(),
        )


def _to_int(value: str | None, default: int) -> int:
    try:
        return int(float(value)) if value else default
    except ValueError:
        return default


def _to_decimal(value: str | None) -> Decimal:
    try:
        return Decimal(value).quantize(Decimal("0.0001")) if value else Decimal(0)
    except (InvalidOperation, ValueError):
        return Decimal(0)


class InstrumentStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def cached_date(self) -> date | None:
        with self.db.connect() as conn:
            value = conn.execute(select(meta.c.value).where(meta.c.key == META_KEY_DATE)).scalar()
        return date.fromisoformat(value) if value else None

    def count(self) -> int:
        with self.db.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(instruments)).scalar_one())

    def load_csv(self, text: str, as_of: date) -> int:
        """Replace the cache atomically: DELETE + COPY + meta update in one transaction."""
        unique: dict[tuple[str, str], Row] = {}
        for row in parse_scrip_master(text):
            unique[(row[1], row[2])] = row  # (exchange_segment, security_id) is the key
        if not unique:
            raise ValueError("scrip master contained no equity/index rows")
        with self.db.begin() as conn:
            conn.execute(delete(instruments))
            raw: Any = conn.connection.driver_connection
            with raw.cursor() as cur:
                columns = ", ".join(COPY_COLUMNS)
                with cur.copy(f"COPY bridge.instruments ({columns}) FROM STDIN") as copy:
                    for row in unique.values():
                        copy.write_row(row)
            stmt = pg_insert(meta).values(key=META_KEY_DATE, value=as_of.isoformat())
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[meta.c.key],
                    set_={"value": stmt.excluded.value, "updated_at": func.now()},
                )
            )
        log.info("scrip master cached: %d rows as of %s", len(unique), as_of)
        return len(unique)

    def refresh_if_stale(self, today: date, fetch: Fetcher) -> bool:
        """Download once per day. Falls back to a stale cache if the download fails.

        Returns True if a download happened. Raises if there is no usable cache.
        """
        if self.cached_date() == today and self.count() > 0:
            return False
        try:
            self.load_csv(fetch(), today)  # all-or-nothing: a failed load keeps the old cache
            return True
        except Exception as exc:
            if self.count() > 0:
                log.warning(
                    "scrip master refresh failed (%s); using cache from %s", exc, self.cached_date()
                )
                return False
            raise RuntimeError(f"scrip master unavailable and no cache: {exc}") from exc

    def resolve(self, symbol: str) -> Instrument | None:
        canonical = normalise_symbol(symbol)
        exchange, _, trading_symbol = canonical.partition(":")
        cols = (
            instruments.c.exchange_segment,
            instruments.c.security_id,
            instruments.c.lot_size,
            instruments.c.tick_size,
            instruments.c.name,
        )
        if exchange == "IDX":
            stmt = select(*cols).where(
                instruments.c.exchange_segment == "IDX_I",
                instruments.c.trading_symbol == trading_symbol,
            )
        else:
            # Prefer the regular EQ series when a symbol trades in several series.
            rank = case(
                (instruments.c.series == "EQ", 0), (instruments.c.series == "A", 1), else_=2
            )
            stmt = (
                select(*cols)
                .where(
                    instruments.c.exchange == exchange,
                    instruments.c.trading_symbol == trading_symbol,
                    instruments.c.instrument == "EQUITY",
                )
                .order_by(rank, instruments.c.security_id)
            )
        with self.db.connect() as conn:
            row = conn.execute(stmt.limit(1)).first()
        if row is None:
            return None
        return Instrument(canonical, row[0], str(row[1]), int(row[2]), float(row[3]), row[4])

    def reverse(self, exchange_segment: str, security_id: str) -> str | None:
        """(exchange_segment, security_id) -> canonical symbol."""
        stmt = select(instruments.c.exchange, instruments.c.trading_symbol).where(
            instruments.c.exchange_segment == exchange_segment,
            instruments.c.security_id == str(security_id),
        )
        with self.db.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        exchange = "IDX" if exchange_segment == "IDX_I" else row[0]
        return f"{exchange}:{row[1]}"

    def resolve_all(self, symbols: Iterable[str], source: str = "universe") -> list[Instrument]:
        resolved: list[Instrument] = []
        unknown: list[str] = []
        for symbol in symbols:
            instrument = self.resolve(symbol)
            if instrument is None:
                unknown.append(normalise_symbol(symbol))
            else:
                resolved.append(instrument)
        if unknown:
            raise UnknownSymbolsError(unknown, source)
        return resolved
