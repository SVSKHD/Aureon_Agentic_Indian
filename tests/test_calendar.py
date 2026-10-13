from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from dhan_bridge import calendar as cal
from dhan_bridge.calendar import IST
from tests.conftest import REPO_ROOT

HOLIDAYS = cal.load_holidays(REPO_ROOT / "holidays")
TUE = date(2026, 10, 13)  # regular trading Tuesday
SAT = date(2026, 10, 17)
SUN = date(2026, 10, 18)
DUSSEHRA = date(2026, 10, 20)  # Tuesday holiday


def at(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=IST)


def test_holiday_file_loaded() -> None:
    assert HOLIDAYS[date(2026, 1, 26)] == "Republic Day"
    assert 2026 in cal.covered_years(HOLIDAYS)


@pytest.mark.parametrize(
    ("day", "expected"),
    [(TUE, True), (SAT, False), (SUN, False), (DUSSEHRA, False), (date(2026, 12, 25), False)],
)
def test_is_trading_day(day: date, expected: bool) -> None:
    assert cal.is_trading_day(day, HOLIDAYS) is expected


@pytest.mark.parametrize(
    ("hh", "mm", "expected"),
    [(9, 14, False), (9, 15, True), (12, 0, True), (15, 30, True), (15, 31, False)],
)
def test_market_open_boundaries(hh: int, mm: int, expected: bool) -> None:
    assert cal.is_market_open(at(TUE, hh, mm), HOLIDAYS) is expected


def test_market_closed_on_weekend_and_holiday() -> None:
    assert cal.is_market_open(at(SAT, 11, 0), HOLIDAYS) is False
    assert cal.is_market_open(at(DUSSEHRA, 11, 0), HOLIDAYS) is False


@pytest.mark.parametrize(
    ("hh", "mm", "ss", "expected"),
    [
        (8, 59, 0, False),
        (8, 59, 59, False),
        (9, 0, 0, True),
        (12, 0, 0, True),
        (15, 45, 0, True),
        (15, 45, 59, True),
        (15, 46, 0, False),
    ],
)
def test_deploy_guard_boundaries(hh: int, mm: int, ss: int, expected: bool) -> None:
    assert cal.is_deploy_blocked(at(TUE, hh, mm, ss), HOLIDAYS) is expected


@pytest.mark.parametrize("day", [SAT, SUN, DUSSEHRA])
def test_deploy_allowed_all_day_on_non_trading_days(day: date) -> None:
    assert cal.is_deploy_blocked(at(day, 11, 0), HOLIDAYS) is False


def test_utc_input_is_converted_to_ist() -> None:
    # 04:00 UTC == 09:30 IST
    moment = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)  # noqa: UP017
    assert cal.is_market_open(moment, HOLIDAYS) is True
    assert cal.is_deploy_blocked(moment, HOLIDAYS) is True


def test_naive_datetime_is_treated_as_ist() -> None:
    assert cal.is_market_open(datetime(2026, 10, 13, 9, 15), HOLIDAYS) is True


def test_year_without_holiday_data_still_blocks_weekdays() -> None:
    assert cal.is_deploy_blocked(at(date(2031, 1, 7), 10, 0), HOLIDAYS) is True


@pytest.mark.parametrize(
    ("when", "code"),
    [
        ("2026-10-13T08:59", 0),
        ("2026-10-13T09:00", 3),
        ("2026-10-13T15:45", 3),
        ("2026-10-13T15:46", 0),
        ("2026-10-20T11:00", 0),  # holiday
        ("2026-10-17T11:00", 0),  # Saturday
    ],
)
def test_cli_exit_codes_in_process(when: str, code: int) -> None:
    assert cal.main(["deploy-guard", "--at", when]) == code


def test_cli_bad_input_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert cal.main(["deploy-guard", "--at", "not-a-date"]) == cal.EXIT_ERROR


def test_cli_status_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cal.main(["status", "--at", "2026-10-13T10:00"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["market_open"] is True
    assert info["deploy_blocked"] is True


def test_cli_custom_holidays_dir(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text(
        json.dumps({"holidays": [{"date": "2026-10-13", "name": "Test"}]}), encoding="utf-8"
    )
    assert (
        cal.main(["deploy-guard", "--at", "2026-10-13T10:00", "--holidays-dir", str(tmp_path)]) == 0
    )


@pytest.mark.parametrize(("when", "code"), [("2026-10-13T10:00", 3), ("2026-10-13T16:00", 0)])
def test_cli_as_module_subprocess(when: str, code: int) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dhan_bridge.calendar", "deploy-guard", "--at", when],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == code, result.stderr
