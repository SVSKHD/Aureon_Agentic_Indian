"""IST market-calendar helpers and the deploy guard used by Jenkins.

CLI::

    python -m dhan_bridge.calendar deploy-guard [--at ISO_DATETIME]   # exit 0 allowed, 3 blocked
    python -m dhan_bridge.calendar status [--at ISO_DATETIME]         # JSON summary

Holiday data comes from ``holidays/*.json``. A year with no holiday file is
treated as having no holidays, so every weekday counts as a trading day; for
the deploy guard that errs on the side of blocking.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30), name="IST")  # India has no DST

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
DEPLOY_BLOCK_START = time(9, 0)
DEPLOY_BLOCK_END = time(15, 45)

EXIT_ALLOWED = 0
EXIT_BLOCKED = 3
EXIT_ERROR = 2

DEFAULT_HOLIDAYS_DIR = Path(__file__).resolve().parent.parent / "holidays"


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(moment: datetime) -> datetime:
    """Convert to IST. Naive datetimes are assumed to already be IST wall-clock time."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def load_holidays(directory: Path) -> dict[date, str]:
    """Load every ``*.json`` holiday file in ``directory``."""
    holidays: dict[date, str] = {}
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload["holidays"]:
            holidays[date.fromisoformat(item["date"])] = str(item["name"])
    return holidays


@lru_cache(maxsize=8)
def _cached_holidays(directory: str) -> dict[date, str]:
    return load_holidays(Path(directory))


def default_holidays() -> dict[date, str]:
    return _cached_holidays(os.environ.get("HOLIDAYS_DIR", str(DEFAULT_HOLIDAYS_DIR)))


def _holidays(holidays: dict[date, str] | None) -> dict[date, str]:
    return default_holidays() if holidays is None else holidays


def covered_years(holidays: dict[date, str]) -> set[int]:
    return {d.year for d in holidays}


def is_holiday(day: date, holidays: dict[date, str] | None = None) -> bool:
    return day in _holidays(holidays)


def is_trading_day(day: date, holidays: dict[date, str] | None = None) -> bool:
    return day.weekday() < 5 and not is_holiday(day, holidays)


def is_market_open(moment: datetime, holidays: dict[date, str] | None = None) -> bool:
    """True from 09:15:00 through 15:30:00 IST inclusive on a trading day."""
    ist = to_ist(moment)
    if not is_trading_day(ist.date(), holidays):
        return False
    return MARKET_OPEN <= ist.time() <= MARKET_CLOSE


def is_deploy_blocked(moment: datetime, holidays: dict[date, str] | None = None) -> bool:
    """True from 09:00:00 until 15:45:59 IST on a trading day (whole minutes 09:00-15:45)."""
    ist = to_ist(moment)
    if not is_trading_day(ist.date(), holidays):
        return False
    minute = ist.time().replace(second=0, microsecond=0)
    return DEPLOY_BLOCK_START <= minute <= DEPLOY_BLOCK_END


def status(moment: datetime, holidays: dict[date, str] | None = None) -> dict[str, object]:
    hols = _holidays(holidays)
    ist = to_ist(moment)
    return {
        "now_ist": ist.isoformat(timespec="seconds"),
        "trading_day": is_trading_day(ist.date(), hols),
        "holiday": hols.get(ist.date()),
        "market_open": is_market_open(ist, hols),
        "deploy_blocked": is_deploy_blocked(ist, hols),
        "holiday_data_for_year": ist.year in covered_years(hols),
    }


def _parse_at(value: str | None) -> datetime:
    return now_ist() if value is None else to_ist(datetime.fromisoformat(value))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dhan_bridge.calendar")
    parser.add_argument("command", choices=["deploy-guard", "status"])
    parser.add_argument("--at", help="ISO datetime to evaluate instead of now (naive = IST)")
    parser.add_argument("--holidays-dir", type=Path, help="directory of holiday JSON files")
    args = parser.parse_args(argv)

    try:
        moment = _parse_at(args.at)
        hols = load_holidays(args.holidays_dir) if args.holidays_dir else default_holidays()
    except (ValueError, OSError, KeyError) as exc:
        print(f"calendar error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    info = status(moment, hols)
    if args.command == "status":
        print(json.dumps(info))
        return EXIT_ALLOWED

    if not info["holiday_data_for_year"]:
        print(f"WARNING: no NSE holiday data for {moment.year}", file=sys.stderr)
    if info["deploy_blocked"]:
        print(
            f"BLOCKED: {info['now_ist']} is inside the 09:00-15:45 IST deploy freeze "
            "on a trading day"
        )
        return EXIT_BLOCKED
    print(f"ALLOWED: {info['now_ist']} is outside the deploy freeze")
    return EXIT_ALLOWED


if __name__ == "__main__":
    sys.exit(main())
