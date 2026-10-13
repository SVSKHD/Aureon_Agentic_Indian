"""Dhan scrip master cache and symbol resolution.

The compact scrip master CSV is downloaded at most once per IST calendar day and
cached in SQLite (equity and index rows only). Symbols are written
``EXCHANGE:TRADING_SYMBOL`` (e.g. ``NSE:RELIANCE``) and resolve to the
``(exchange_segment, security_id)`` pair the Dhan feed expects.
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

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


def parse_scrip_master(text: str) -> Iterator[tuple[str, str, str, str, str, str, int, float, str]]:
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
            _to_float(row.get(COL_TICK), 0.0),
            (row.get(COL_NAME) or "").strip(),
        )


def _to_int(value: str | None, default: int) -> int:
    try:
        return int(float(value)) if value else default
    except ValueError:
        return default


def _to_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value else default
    except ValueError:
        return default


class InstrumentStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:  # commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    def cached_date(self) -> date | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY_DATE,)).fetchone()
        return date.fromisoformat(row[0]) if row else None

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0])

    def load_csv(self, text: str, as_of: date) -> int:
        rows = list(parse_scrip_master(text))
        if not rows:
            raise ValueError("scrip master contained no equity/index rows")
        with self._connect() as conn:
            conn.execute("DELETE FROM instruments")
            conn.executemany(
                "INSERT OR REPLACE INTO instruments (exchange, exchange_segment, security_id,"
                " trading_symbol, instrument, series, lot_size, tick_size, name)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (META_KEY_DATE, as_of.isoformat()),
            )
        log.info("scrip master cached: %d rows as of %s", len(rows), as_of)
        return len(rows)

    def refresh_if_stale(self, today: date, fetch: Fetcher) -> bool:
        """Download once per day. Falls back to a stale cache if the download fails.

        Returns True if a download happened. Raises if there is no usable cache.
        """
        if self.cached_date() == today and self.count() > 0:
            return False
        try:
            self.load_csv(fetch(), today)
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
        if exchange == "IDX":
            sql = (
                "SELECT exchange_segment, security_id, lot_size, tick_size, name FROM instruments"
                " WHERE exchange_segment = 'IDX_I' AND trading_symbol = ? LIMIT 1"
            )
            params: tuple[str, ...] = (trading_symbol,)
        else:
            # Prefer the regular EQ series when a symbol trades in several series.
            sql = (
                "SELECT exchange_segment, security_id, lot_size, tick_size, name FROM instruments"
                " WHERE exchange = ? AND trading_symbol = ? AND instrument = 'EQUITY'"
                " ORDER BY CASE series WHEN 'EQ' THEN 0 WHEN 'A' THEN 1 ELSE 2 END LIMIT 1"
            )
            params = (exchange, trading_symbol)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return Instrument(canonical, row[0], str(row[1]), int(row[2]), float(row[3]), row[4])

    def reverse(self, exchange_segment: str, security_id: str) -> str | None:
        """(exchange_segment, security_id) -> canonical symbol."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT exchange, trading_symbol FROM instruments"
                " WHERE exchange_segment = ? AND security_id = ?",
                (exchange_segment, str(security_id)),
            ).fetchone()
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
