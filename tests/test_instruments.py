from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dhan_bridge.db import Database
from dhan_bridge.instruments import (
    InstrumentStore,
    UnknownSymbolsError,
    load_universe,
    normalise_symbol,
    parse_scrip_master,
)
from tests.conftest import REPO_ROOT

TODAY = date(2026, 10, 13)


@pytest.fixture
def store(db: Database, scrip_csv: str) -> InstrumentStore:
    s = InstrumentStore(db)
    s.load_csv(scrip_csv, TODAY)
    return s


def test_resolves_symbol_to_segment_and_security_id(store: InstrumentStore) -> None:
    inst = store.resolve("NSE:RELIANCE")
    assert inst is not None
    assert (inst.exchange_segment, inst.security_id) == ("NSE_EQ", "2885")
    assert store.resolve("BSE:RELIANCE").security_id == "500325"  # type: ignore[union-attr]


def test_reverse_lookup(store: InstrumentStore) -> None:
    assert store.reverse("NSE_EQ", "2885") == "NSE:RELIANCE"
    assert store.reverse("IDX_I", "13") == "IDX:NIFTY"
    assert store.reverse("NSE_EQ", "424242") is None


def test_symbols_with_special_characters_and_default_exchange(store: InstrumentStore) -> None:
    assert store.resolve("m&m").security_id == "2031"  # type: ignore[union-attr]
    assert store.resolve("NSE:BAJAJ-AUTO").security_id == "16669"  # type: ignore[union-attr]


def test_prefers_eq_series(store: InstrumentStore) -> None:
    assert store.resolve("NSE:SAMPLEDUP").security_id == "99998"  # type: ignore[union-attr]


def test_index_resolution(store: InstrumentStore) -> None:
    inst = store.resolve("IDX:NIFTY")
    assert inst is not None
    assert inst.exchange_segment == "IDX_I"


def test_derivatives_rows_are_not_cached(store: InstrumentStore) -> None:
    assert store.resolve("NSE:RELIANCE-OCT2026-FUT") is None


def test_unknown_symbol_fails_clearly(store: InstrumentStore) -> None:
    with pytest.raises(UnknownSymbolsError) as err:
        store.resolve_all(["NSE:RELIANCE", "NSE:NOTASTOCK", "NSE:ALSOFAKE"], "universe/x.txt")
    message = str(err.value)
    assert "NSE:NOTASTOCK" in message
    assert "NSE:ALSOFAKE" in message
    assert "universe/x.txt" in message
    assert err.value.symbols == ["NSE:NOTASTOCK", "NSE:ALSOFAKE"]


def test_refresh_downloads_once_per_day(db: Database, scrip_csv: str) -> None:
    s = InstrumentStore(db)
    calls: list[int] = []

    def fetch() -> str:
        calls.append(1)
        return scrip_csv

    assert s.refresh_if_stale(TODAY, fetch) is True
    assert s.refresh_if_stale(TODAY, fetch) is False
    assert s.refresh_if_stale(date(2026, 10, 14), fetch) is True
    assert len(calls) == 2


def test_refresh_falls_back_to_stale_cache(store: InstrumentStore) -> None:
    def broken() -> str:
        raise OSError("dns failure")

    assert store.refresh_if_stale(date(2026, 10, 14), broken) is False
    assert store.resolve("NSE:TCS") is not None


def test_refresh_without_cache_fails(db: Database) -> None:
    def broken() -> str:
        raise OSError("dns failure")

    with pytest.raises(RuntimeError, match="no cache"):
        InstrumentStore(db).refresh_if_stale(TODAY, broken)


def test_rejects_csv_with_wrong_columns() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        list(parse_scrip_master("a,b\n1,2\n"))


def test_load_universe(tmp_path: Path) -> None:
    f = tmp_path / "u.txt"
    f.write_text("# header\nRELIANCE\nNSE:RELIANCE  # dup\n\nbse:tcs\n", encoding="utf-8")
    assert load_universe(f) == ["NSE:RELIANCE", "BSE:TCS"]
    f.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no symbols"):
        load_universe(f)
    with pytest.raises(ValueError, match="invalid symbol"):
        normalise_symbol("NSE:")


def test_shipped_nifty50_universe_is_well_formed() -> None:
    symbols = load_universe(REPO_ROOT / "universe" / "nifty50.txt")
    assert len(symbols) == 50
    assert all(s.startswith("NSE:") for s in symbols)


def test_load_is_copy_in_one_transaction_and_failure_keeps_old_cache(
    store: InstrumentStore, scrip_csv: str
) -> None:
    before = store.count()
    # a NUL byte is valid CSV but rejected by Postgres mid-COPY -> whole replacement rolls back
    bad_csv = scrip_csv + "NSE,E,77,EQUITY,,BAD\x00SYM,1,x,,,,0.05,NA,ES,EQ,X\n"
    with pytest.raises(Exception):  # noqa: B017, PT011 - any DB error
        store.load_csv(bad_csv, date(2026, 10, 14))
    assert store.count() == before
    assert store.cached_date() == TODAY
    # and refresh_if_stale falls back to that stale cache
    assert store.refresh_if_stale(date(2026, 10, 14), lambda: bad_csv) is False
    assert store.resolve("NSE:RELIANCE") is not None


def test_duplicate_keys_in_csv_are_deduplicated(db: Database, scrip_csv: str) -> None:
    s = InstrumentStore(db)
    dup = scrip_csv + "NSE,E,2885,EQUITY,,RELIANCE,1.0,Dup,,,,5.0,NA,ES,EQ,DUP\n"
    assert s.load_csv(dup, TODAY) == s.load_csv(scrip_csv, TODAY)


def test_tick_size_is_exact_numeric(store: InstrumentStore) -> None:
    inst = store.resolve("NSE:RELIANCE")
    assert inst is not None
    assert inst.tick_size == 10.0
