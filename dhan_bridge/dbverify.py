"""Database fingerprints: one verification routine shared by the app, backups and host moves.

A fingerprint is a JSON object:

* ``alembic_revision`` and ``server_major``;
* ``tables``: for EVERY table in schema ``bridge`` (from the catalog) the exact row
  count, plus integrity aggregates for tables in the model metadata: ``sum`` of
  every NUMERIC column and ``min_max`` of every TIMESTAMPTZ column (discovered
  from :mod:`dhan_bridge.models`, so new tables are covered automatically).

It is computed by ONE generated SQL statement (:data:`GENERATOR_SQL` produces it,
then it is executed). The same text runs through SQLAlchemy (:func:`fingerprint`)
and through ``psql`` inside a postgres container (``\\gexec``, see
:mod:`dhan_bridge.pgtools`), so the app and the backup/move tools can never
disagree about what a fingerprint means. Values are exact: NUMERIC sums as text,
timestamps formatted in UTC by the server.

``compare(a, b)`` lists every difference; an empty list means identical.

CLI::

    python -m dhan_bridge.dbverify compare a.json b.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, DateTime, Numeric, Table

from dhan_bridge.models import SCHEMA, metadata

FORMAT = 1
UTC_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _utc(expr: str) -> str:
    return f"to_char({expr} AT TIME ZONE 'UTC', '{UTC_FORMAT}')"


def aggregate_sql(table: Table) -> str:
    """``, 'sum', json_build_object(...), 'min_max', json_build_object(...)`` for one table."""
    numeric = [c.name for c in table.columns if isinstance(c.type, Numeric)]
    times = [c.name for c in table.columns if isinstance(c.type, DateTime)]
    parts = []
    if numeric:
        sums = ", ".join(f"{_quote_literal(c)}, sum({_quote_ident(c)})::text" for c in numeric)
        parts.append(f"'sum', json_build_object({sums})")
    if times:
        ranges = ", ".join(
            f"{_quote_literal(c)}, json_build_array("
            f"{_utc(f'min({_quote_ident(c)})')}, {_utc(f'max({_quote_ident(c)})')})"
            for c in times
        )
        parts.append(f"'min_max', json_build_object({ranges})")
    return "".join(f", {p}" for p in parts)


def generator_sql() -> str:
    """A query whose single result is the fingerprint query for this database."""
    values = [("", "")] + [
        (t.name, aggregate_sql(t)) for t in metadata.sorted_tables if aggregate_sql(t)
    ]
    rows = ", ".join(f"({_quote_literal(n)}, {_quote_literal(e)})" for n, e in values)
    return f"""
SELECT 'SELECT json_build_object(''format'', {FORMAT}'
    || ', ''alembic_revision'', '
    || CASE WHEN to_regclass('{SCHEMA}.alembic_version') IS NULL THEN 'NULL'
            ELSE '(SELECT version_num FROM {SCHEMA}.alembic_version LIMIT 1)' END
    || ', ''server_major'', current_setting(''server_version_num'')::int / 10000'
    || ', ''tables'', json_build_object('
    || coalesce(string_agg(
           format('%L, (SELECT json_build_object(''rows'', count(*)%s) FROM {SCHEMA}.%I)',
                  t.tablename, coalesce(a.expr, ''), t.tablename),
           ', ' ORDER BY t.tablename), '')
    || '))'
FROM pg_tables t
LEFT JOIN (VALUES {rows}) AS a(name, expr) ON a.name = t.tablename
WHERE t.schemaname = '{SCHEMA}'
"""


GENERATOR_SQL = generator_sql()
# For psql: run the generator, then execute its result (\gexec), print the JSON row.
PSQL_SCRIPT = GENERATOR_SQL.strip() + " \\gexec\n"


def parse(raw: object) -> dict[str, Any]:
    value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if not isinstance(value, dict):
        raise ValueError("fingerprint query did not return a JSON object")
    return value


def fingerprint(conn: Connection) -> dict[str, Any]:
    """Fingerprint through an open SQLAlchemy connection (inside its current snapshot)."""
    # Raw driver cursor, no parameters: the SQL contains format() '%' directives that
    # a parameterised execute would treat as placeholders. Same transaction/snapshot.
    raw: Any = conn.connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(GENERATOR_SQL)
        query = cur.fetchone()[0]
        cur.execute(query)
        return parse(cur.fetchone()[0])


def alembic_revision(conn: Connection) -> str | None:
    if conn.exec_driver_sql(f"SELECT to_regclass('{SCHEMA}.alembic_version')").scalar() is None:
        return None
    rev = conn.exec_driver_sql(f"SELECT version_num FROM {SCHEMA}.alembic_version LIMIT 1").scalar()
    return str(rev) if rev is not None else None


def compare(a: Any, b: Any, ignore: Iterable[str] = (), _path: str = "") -> list[str]:
    """Every difference between two fingerprints, as ``path: a != b`` lines."""
    skip = set(ignore)
    if isinstance(a, dict) and isinstance(b, dict):
        diffs: list[str] = []
        for key in sorted(set(a) | set(b)):
            path = f"{_path}.{key}" if _path else str(key)
            if path in skip:
                continue
            if key not in a:
                diffs.append(f"{path}: missing in first")
            elif key not in b:
                diffs.append(f"{path}: missing in second")
            else:
                diffs.extend(compare(a[key], b[key], skip, path))
        return diffs
    return [] if a == b else [f"{_path or '<root>'}: {a!r} != {b!r}"]


def dumps(fp: dict[str, Any]) -> str:
    return json.dumps(fp, indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dhan_bridge.dbverify")
    sub = parser.add_subparsers(dest="command", required=True)
    cmp_ = sub.add_parser("compare", help="compare two fingerprint JSON files")
    cmp_.add_argument("a", type=Path)
    cmp_.add_argument("b", type=Path)
    sub.add_parser("sql", help="print the fingerprint generator SQL")
    args = parser.parse_args(argv)
    if args.command == "sql":
        sys.stdout.write(PSQL_SCRIPT)
        return 0
    a = json.loads(args.a.read_text())
    b = json.loads(args.b.read_text())
    a = a.get("fingerprint", a)
    b = b.get("fingerprint", b)
    diffs = compare(a, b)
    for line in diffs:
        print(line)
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
