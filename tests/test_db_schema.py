"""Schema invariants: everything in "bridge", models == revisions, expand-only revisions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from dhan_bridge.db import MIGRATIONS_DIR, Database, head_revision
from dhan_bridge.models import SCHEMA, metadata
from tests.conftest import REPO_ROOT

VERSIONS = MIGRATIONS_DIR / "versions"


def test_every_table_is_in_bridge(db: Database) -> None:
    with db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT schemaname, tablename FROM pg_tables"
                " WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
            )
        ).all()
    assert rows, "no tables at all?"
    assert {r.schemaname for r in rows} == {SCHEMA}, "tables outside schema bridge"
    assert {t.name for t in metadata.sorted_tables} <= {r.tablename for r in rows}
    assert all(t.schema == SCHEMA for t in metadata.sorted_tables)


def test_other_roles_cannot_use_schema_bridge(db: Database) -> None:
    """0001 revokes PUBLIC access: only the owning role (the bridge) can reach the data."""
    with db.begin() as conn:
        conn.execute(text("DROP ROLE IF EXISTS other_role"))
        conn.execute(text("CREATE ROLE other_role"))
    with db.connect() as conn:
        conn.execute(text("SET ROLE other_role"))
        with pytest.raises(ProgrammingError, match="permission denied for schema bridge"):
            conn.execute(text("SELECT count(*) FROM bridge.meta"))
        conn.rollback()
    with db.begin() as conn:
        conn.execute(text("DROP ROLE other_role"))


def test_models_match_revisions(db: Database) -> None:
    """Autogenerate against the migrated database finds nothing to do."""
    with db.connect() as conn:

        def include_name(name: str | None, type_: str, _parent: object) -> bool:
            if type_ == "schema":
                return name == SCHEMA
            return not (type_ == "table" and name == "alembic_version")

        ctx = MigrationContext.configure(
            conn,
            opts={
                "version_table_schema": SCHEMA,
                "include_schemas": True,
                "include_name": include_name,
                "compare_server_default": True,
            },
        )
        diff = compare_metadata(ctx, metadata)
    assert diff == []


def test_search_path_and_head(db: Database) -> None:
    with db.connect() as conn:
        assert conn.execute(text("SHOW search_path")).scalar() == SCHEMA
    assert db.head == head_revision()
    assert db.status() == (True, True)


def test_db_ok_requires_head(db: Database) -> None:
    with db.begin() as conn:
        conn.execute(text("UPDATE bridge.alembic_version SET version_num = '0001_initial'"))
    assert db.status() == (True, False)
    assert db.ok() is False


# --------------------------------------------------------------------------- expand-only

DESTRUCTIVE = re.compile(
    r"\bop\.(drop_\w+|rename_table)\b|\balter_column\([^)]*new_column_name"
    r"|\b(DROP|RENAME)\b",
    re.IGNORECASE,
)
ALLOW = re.compile(
    r"^[ \t]*#?[ \t]*--[ \t]*allow-destructive:[ \t]*\S", re.IGNORECASE | re.MULTILINE
)


def upgrade_body(source: str) -> str:
    """Only the upgrade() function counts; downgrade() is irrelevant (we never run it)."""
    match = re.search(r"^def upgrade\(\).*?(?=^def |\Z)", source, re.MULTILINE | re.DOTALL)
    body = match.group(0) if match else ""
    # ignore comments and docstrings
    body = re.sub(r'""".*?"""', "", body, flags=re.DOTALL)
    return "\n".join(line.split("#", 1)[0] for line in body.splitlines())


def destructive_violations(directory: Path) -> list[str]:
    """Revisions must be expand-only so the previous release keeps working after a rollback."""
    bad: list[str] = []
    for path in sorted(directory.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if DESTRUCTIVE.search(upgrade_body(source)) and not ALLOW.search(source):
            bad.append(path.name)
    return bad


def test_shipped_revisions_are_expand_only() -> None:
    assert destructive_violations(VERSIONS) == [], (
        "drop_*/rename in a revision breaks rollback to the previous release. If unavoidable, "
        "add a line '# -- allow-destructive: <reason>' (see README 'Migrations')."
    )


UP = "def upgrade() -> None:\n    {}\n\n\ndef downgrade() -> None:\n    op.drop_table('x')\n"


@pytest.mark.parametrize(
    ("body", "header", "violates"),
    [
        ("op.drop_table('a')", "", True),
        ("op.drop_column('a', 'b', schema='bridge')", "", True),
        ("op.drop_index('i')", "", True),
        ("op.rename_table('a', 'b')", "", True),
        ("op.alter_column('a', 'b', new_column_name='c')", "", True),
        ('op.execute("ALTER TABLE bridge.a RENAME COLUMN x TO y")', "", True),
        ('op.execute("DROP TABLE bridge.a")', "", True),
        ("op.drop_table('a')", "# -- allow-destructive:\n", True),  # reason is mandatory
        ("op.drop_table('old')", "# -- allow-destructive: unused since 0.3.0\n", False),
        ("op.add_column('a', sa.Column('dropped_at', sa.Text()))", "", False),
        ("pass  # we never DROP anything", "", False),
    ],
)
def test_destructive_checker(tmp_path: Path, body: str, header: str, violates: bool) -> None:
    (tmp_path / "0099_x.py").write_text(header + UP.format(body), encoding="utf-8")
    assert bool(destructive_violations(tmp_path)) is violates


def test_revisions_form_a_single_linear_chain() -> None:
    from alembic.script import ScriptDirectory

    from dhan_bridge.db import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    revisions = list(script.walk_revisions())
    assert [r.revision for r in reversed(revisions)] == [
        p.stem for p in sorted(VERSIONS.glob("0*.py"))
    ]


def test_no_sqlite_anywhere_in_the_package() -> None:
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "dhan_bridge").rglob("*.py")
        if re.search(r"^\s*(import|from)\s+sqlite3\b", p.read_text(), re.MULTILINE)
    ]
    assert offenders == []
