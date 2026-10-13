"""Database fingerprints: one verification routine shared by backups and host moves.

``fingerprint(conn)`` returns a JSON-serialisable dict:

* ``alembic_revision`` and ``server_major``;
* for EVERY table in schema ``bridge`` (from the catalog): the exact row count;
* for tables in the model metadata: integrity aggregates discovered from the
  column types, so new tables are covered automatically:
  ``sum`` of every NUMERIC column, ``min``/``max`` of every TIMESTAMPTZ column.

Values are rendered exactly (Decimal as text, timestamps as UTC ISO-8601), so two
fingerprints are equal only if the data is. Run it inside the same transaction /
snapshot you are dumping to get a fingerprint that describes the dump.

``compare(a, b)`` lists every difference; an empty list means identical.

CLI::

    python -m dhan_bridge.dbverify fingerprint [--db-url URL]
    python -m dhan_bridge.dbverify compare a.json b.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, DateTime, Numeric, Table, func, select, text
from sqlalchemy import table as table_clause

from dhan_bridge.models import SCHEMA, metadata

FORMAT = 1


def _render(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def _aggregates(table: Table) -> tuple[list[str], list[str]]:
    numeric = [c.name for c in table.columns if isinstance(c.type, Numeric)]
    times = [c.name for c in table.columns if isinstance(c.type, DateTime)]
    return numeric, times


def catalog_tables(conn: Connection) -> list[str]:
    rows = conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = :s ORDER BY tablename"),
        {"s": SCHEMA},
    )
    return [r[0] for r in rows]


def alembic_revision(conn: Connection) -> str | None:
    if conn.execute(text("SELECT to_regclass('bridge.alembic_version')")).scalar() is None:
        return None
    return conn.execute(text("SELECT version_num FROM bridge.alembic_version")).scalar()


def fingerprint(conn: Connection) -> dict[str, Any]:
    from dhan_bridge.db import server_major

    tables: dict[str, dict[str, Any]] = {}
    model_tables = {t.name: t for t in metadata.sorted_tables}
    for name in catalog_tables(conn):
        # catalog name, quoted by SQLAlchemy: covers tables the models don't know yet
        rows = conn.execute(
            select(func.count()).select_from(table_clause(name, schema=SCHEMA))
        ).scalar_one()
        entry: dict[str, Any] = {"rows": int(rows)}
        table = model_tables.get(name)
        if table is not None:
            numeric, times = _aggregates(table)
            if numeric or times:
                exprs: list[Any] = [func.sum(table.c[c]) for c in numeric]
                exprs += [f(table.c[c]) for c in times for f in (func.min, func.max)]
                values = list(conn.execute(select(*exprs)).one())
                entry["sum"] = {c: _render(values.pop(0)) for c in numeric}
                entry["min_max"] = {
                    c: [_render(values.pop(0)), _render(values.pop(0))] for c in times
                }
        tables[name] = entry
    return {
        "format": FORMAT,
        "alembic_revision": alembic_revision(conn),
        "server_major": server_major(conn),
        "tables": tables,
    }


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
    fp = sub.add_parser("fingerprint")
    fp.add_argument("--db-url")
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("a", type=Path)
    cmp_.add_argument("b", type=Path)
    args = parser.parse_args(argv)

    if args.command == "compare":
        diffs = compare(json.loads(args.a.read_text()), json.loads(args.b.read_text()))
        for line in diffs:
            print(line)
        return 1 if diffs else 0

    from dhan_bridge.config import DatabaseSecrets, Settings, read_db_secrets_file
    from dhan_bridge.db import make_engine

    settings = Settings()
    url = args.db_url or read_db_secrets_file(settings.db_secrets_file).url
    engine = make_engine(DatabaseSecrets(url).url, settings.db_sslmode)
    try:
        with engine.connect() as conn:
            sys.stdout.write(dumps(fingerprint(conn)))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
