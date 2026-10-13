"""Portability rules: the data must be able to move to ANY PostgreSQL host.

* only standard PostgreSQL types (checked on the model metadata);
* money is NUMERIC(18,4), timestamps are TIMESTAMPTZ;
* no extensions except an explicit allowlist (empty);
* no references to Supabase-owned schemas or to "public" in revisions/models;
* the app never talks to Supabase APIs (no SDKs in the dependency locks).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    SmallInteger,
    Text,
)

from dhan_bridge.db import MIGRATIONS_DIR
from dhan_bridge.models import SCHEMA, metadata
from tests.conftest import REPO_ROOT

ALLOWED_TYPES = (BigInteger, Boolean, Date, DateTime, Integer, Numeric, SmallInteger, Text)
EXTENSION_ALLOWLIST: frozenset[str] = frozenset()
SUPABASE_SCHEMAS = ("auth", "storage", "realtime", "extensions", "graphql", "graphql_public")

FORBIDDEN_SQL = {
    "extension": re.compile(r"\bCREATE\s+EXTENSION\b|\bcreate_extension\b", re.IGNORECASE),
    "supabase schema": re.compile(
        r"\b(" + "|".join(SUPABASE_SCHEMAS) + r")\s*\.\s*\w+|schema\s*=\s*[\"'](?:"
        + "|".join(SUPABASE_SCHEMAS)
        + r")[\"']",
        re.IGNORECASE,
    ),
    "public schema": re.compile(
        r"\bpublic\s*\.\s*\w+|schema\s*=\s*[\"']public[\"']", re.IGNORECASE
    ),
    "vendor type": re.compile(
        r"dialects\.postgresql|postgresql\.(JSONB|UUID|ARRAY|INET|MONEY)", re.IGNORECASE
    ),
    "money type": re.compile(r"\bMONEY\b"),
    "naive timestamp": re.compile(
        r"DateTime\(\s*\)|timezone\s*=\s*False|\bTIMESTAMP\s+WITHOUT\b", re.IGNORECASE
    ),
}  # fmt: skip


def scan(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            for label, pattern in FORBIDDEN_SQL.items():
                if pattern.search(code):
                    hits.append(f"{path.name}:{lineno}: {label}: {line.strip()}")
    return hits


def revision_files() -> list[Path]:
    return sorted((MIGRATIONS_DIR / "versions").glob("*.py"))


def test_revisions_and_models_are_portable() -> None:
    files = [*revision_files(), REPO_ROOT / "dhan_bridge" / "models.py"]
    assert len(files) >= 5
    assert scan(files) == []


def test_metadata_uses_standard_types_in_bridge() -> None:
    problems: list[str] = []
    for table in metadata.sorted_tables:
        if table.schema != SCHEMA:
            problems.append(f"{table.name}: schema {table.schema!r}")
        for col in table.columns:
            t = col.type
            if not isinstance(t, ALLOWED_TYPES):
                problems.append(f"{table.name}.{col.name}: {t!r} is not an allowed type")
            if isinstance(t, DateTime) and not t.timezone:
                problems.append(f"{table.name}.{col.name}: timestamps must be TIMESTAMPTZ")
            is_money = isinstance(t, Numeric) and not isinstance(t, (Integer, SmallInteger))
            if is_money and (t.precision, t.scale) != (18, 4):  # type: ignore[union-attr]
                problems.append(f"{table.name}.{col.name}: money must be NUMERIC(18,4)")
    assert problems == []


def test_no_extensions_in_live_database(db: object) -> None:
    from sqlalchemy import text

    from dhan_bridge.db import Database

    assert isinstance(db, Database)
    with db.connect() as conn:
        used = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars())
    # plpgsql is built in and present on every server
    assert used - {"plpgsql"} <= EXTENSION_ALLOWLIST


def test_no_supabase_sdk_or_services_in_dependencies() -> None:
    banned = re.compile(
        r"^(supabase|supabase-py|postgrest|gotrue|storage3|realtime|supafunc)\b", re.IGNORECASE
    )
    for lock in REPO_ROOT.glob("requirements*.txt"):
        names = [ln.split("==")[0] for ln in lock.read_text().splitlines() if "==" in ln]
        assert [n for n in names if banned.match(n.strip())] == [], lock.name


@pytest.mark.parametrize(
    ("snippet", "label"),
    [
        ('op.execute("CREATE EXTENSION pgcrypto")', "extension"),
        ('op.execute("select auth.uid()")', "supabase schema"),
        ("op.create_table('x', schema='storage')", "supabase schema"),
        ("op.create_table('x', schema='public')", "public schema"),
        ("sa.Column('x', postgresql.JSONB())", "vendor type"),
        ("sa.Column('x', sa.DateTime())", "naive timestamp"),
        ('op.execute("ALTER TABLE x ADD c MONEY")', "money type"),
    ],
)
def test_scanner_catches_violations(tmp_path: Path, snippet: str, label: str) -> None:
    path = tmp_path / "0099_bad.py"
    path.write_text(snippet + "\n", encoding="utf-8")
    hits = scan([path])
    assert any(label in h for h in hits), hits
