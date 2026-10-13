"""NO-TRADE INVARIANT: dhan_bridge must never be able to place, modify or cancel a real order.

This test greps every non-test source file in the repository for Dhan order
functions, order classes and order REST endpoints. It is a hard CI gate
(run explicitly in the Jenkins "Safety: no-trade" stage). Paper trading must
use its own names (e.g. ``submit_paper_order``) and its own routes
(e.g. ``/api/paper/...``), which do not match these patterns.

Do not add exclusions to make this pass. See README "Safety".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that are not shipped source.
EXCLUDED_DIRS = {".git", ".venv", "venv", "tests", "data", "node_modules", "reports", "docs"}
SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".mjs", ".ts", ".vue", ".sh", ".sql",
    ".yml", ".yaml", ".toml", ".cfg", ".ini", ".conf", ".json", ".txt", ".in",
}  # fmt: skip
SOURCE_NAMES = {"Dockerfile", "Jenkinsfile", "Makefile"}

BANNED: dict[str, re.Pattern[str]] = {
    # order methods of the dhanhq order classes and of the legacy all-in-one client
    "order function": re.compile(r"\b(place|modify|cancel)_(slice_)?order\b"),
    "forever order function": re.compile(r"\b(place|modify|cancel)_forever\b"),
    "super order function": re.compile(r"\b(place|modify|cancel)_super_order\b"),
    "conditional order function": re.compile(r"\b(place|modify|cancel)_conditional_order\b"),
    "position conversion": re.compile(r"\bconvert_position\b"),
    "kill switch / P&L exit": re.compile(r"\b(kill_switch|set_pnl_exit|stop_pnl_exit)\b"),
    "eDIS / TPIN": re.compile(r"\b(generate_tpin|open_browser_for_tpin)\b"),
    # order-capable SDK classes
    "order class import": re.compile(
        r"\b(Order|ForeverOrder|SuperOrder|ConditionalOrder|TraderControl|OrderUpdate)\b"
        r"(?=[^\n]*\bimport\b|\s*\()"
        r"|import[^\n]*\b(Order|ForeverOrder|SuperOrder|ConditionalOrder|TraderControl)\b"
    ),
    "legacy dhanhq class": re.compile(r"from\s+dhanhq\s+import[^\n]*\bdhanhq\b|\bdhanhq\.dhanhq\b"),
    # Dhan v2 order / account-mutating REST endpoints, as string literals or full URLs
    "order endpoint": re.compile(
        r"""["'](/v2)?/(orders|forever/orders|super/orders|alerts/orders|positions/convert|killswitch|pnlExit|edis)\b"""
        r"|api\.dhan\.co/v2/(orders|forever|super|alerts|positions/convert|killswitch|pnlExit|edis)"
    ),
}


def source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts) or not path.is_file():
            continue
        if path.suffix in SOURCE_SUFFIXES or path.name in SOURCE_NAMES:
            files.append(path)
    return files


def scan(root: Path) -> list[str]:
    violations: list[str] = []
    for path in source_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pattern in BANNED.items():
                if pattern.search(line):
                    rel = path.relative_to(root)
                    violations.append(f"{rel}:{lineno}: {label}: {line.strip()}")
    return violations


def test_no_order_capability_in_source() -> None:
    files = source_files(REPO_ROOT)
    assert any(p.name == "dhan_client.py" for p in files), "scanner is not seeing the package"
    violations = scan(REPO_ROOT)
    assert not violations, "NO-TRADE INVARIANT VIOLATED:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "snippet",
    [
        "dhan.place_order(security_id='1', quantity=1)",
        "Order(ctx).modify_order(1)",
        "x.cancel_order('1')",
        "ctx.place_slice_order()",
        "ForeverOrder(ctx).place_forever()",
        "SuperOrder(ctx).place_super_order()",
        "c.cancel_super_order(1, 'ENTRY')",
        "ConditionalOrder(ctx).place_conditional_order({}, [])",
        "from dhanhq import Order",
        "from dhanhq import DhanContext, ForeverOrder",
        "from dhanhq import dhanhq",
        "http.post('/orders', payload)",
        'http.post("/super/orders", payload)',
        "requests.post('https://api.dhan.co/v2/orders')",
        "TraderControl(ctx).kill_switch('ACTIVATE')",
    ],
)
def test_scanner_catches_planted_order_code(tmp_path: Path, snippet: str) -> None:
    (tmp_path / "evil.py").write_text(snippet + "\n", encoding="utf-8")
    assert scan(tmp_path), f"scanner missed: {snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        "from dhanhq import DhanContext, Funds",
        "def submit_paper_order(order): ...",
        "router.post('/api/paper/orders')",
        "orders = []",
    ],
)
def test_scanner_allows_read_only_and_paper_code(tmp_path: Path, snippet: str) -> None:
    (tmp_path / "ok.py").write_text(snippet + "\n", encoding="utf-8")
    assert scan(tmp_path) == []
