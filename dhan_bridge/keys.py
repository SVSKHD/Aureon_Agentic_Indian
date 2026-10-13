"""API keys: generation, hashed storage, verification and a small admin CLI.

Keys look like ``dbk_<43 url-safe chars>``. Only ``sha256(key)`` is stored, so a
leaked database does not leak usable keys. The plaintext is shown exactly once.

CLI (inside the container: ``docker exec dhan_bridge python -m dhan_bridge.keys ...``)::

    python -m dhan_bridge.keys create <name>
    python -m dhan_bridge.keys list
    python -m dhan_bridge.keys revoke <id>
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from dhan_bridge.db import Database, DatabaseUnavailable
from dhan_bridge.models import api_keys

KEY_PREFIX = "dbk_"


class KeyStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiKey:
    id: int
    name: str
    created_at: str
    revoked_at: str | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _record(row: Any) -> ApiKey:
    def iso(value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat(timespec="seconds") if value else None

    return ApiKey(int(row.id), row.name, iso(row.created_at) or "", iso(row.revoked_at))


def _guard(exc: SQLAlchemyError) -> KeyStoreError:
    if isinstance(exc, ProgrammingError) and "api_keys" in str(exc):
        return KeyStoreError("bridge.api_keys does not exist; start the app once to migrate")
    return KeyStoreError(f"database error: {type(exc).__name__}")


def create_key(db: Database, name: str) -> tuple[int, str]:
    """Create a key. Returns ``(id, plaintext)``; the plaintext is not recoverable later."""
    name = name.strip()
    if not name:
        raise KeyStoreError("key name must not be empty")
    key = generate_key()
    try:
        with db.begin() as conn:
            key_id = conn.execute(
                insert(api_keys).values(name=name, key_hash=hash_key(key)).returning(api_keys.c.id)
            ).scalar_one()
    except SQLAlchemyError as exc:
        raise _guard(exc) from exc
    return int(key_id), key


def list_keys(db: Database) -> list[ApiKey]:
    try:
        with db.connect() as conn:
            rows = conn.execute(select(api_keys).order_by(api_keys.c.id)).all()
    except SQLAlchemyError as exc:
        raise _guard(exc) from exc
    return [_record(r) for r in rows]


def revoke_key(db: Database, key_id: int) -> bool:
    """Revoke a key. Returns False if no active key has that id."""
    try:
        with db.begin() as conn:
            result = conn.execute(
                update(api_keys)
                .where(api_keys.c.id == key_id, api_keys.c.revoked_at.is_(None))
                .values(revoked_at=func.now())
            )
    except SQLAlchemyError as exc:
        raise _guard(exc) from exc
    return result.rowcount == 1


def verify_key(db: Database, key: str | None) -> ApiKey | None:
    """Return the active key record for ``key``, or None (unknown, revoked or malformed).

    Raises :class:`DatabaseUnavailable` when the database cannot be asked, so callers
    can answer 503 instead of a misleading 401.
    """
    if not key or not key.startswith(KEY_PREFIX) or len(key) > 200:
        return None
    try:
        with db.connect() as conn:
            row = conn.execute(
                select(api_keys).where(
                    api_keys.c.key_hash == hash_key(key), api_keys.c.revoked_at.is_(None)
                )
            ).first()
    except SQLAlchemyError as exc:
        raise DatabaseUnavailable(str(exc).splitlines()[0]) from exc
    return _record(row) if row else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dhan_bridge.keys")
    parser.add_argument("--db-url", help="override DATABASE_URL (default: DB_SECRETS_FILE)")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a key and print it once")
    create.add_argument("name")
    sub.add_parser("list", help="list keys (never prints keys or hashes)")
    revoke = sub.add_parser("revoke", help="revoke a key by id")
    revoke.add_argument("id", type=int)
    args = parser.parse_args(argv)

    from dhan_bridge.config import Settings, parse_db_secrets_text, read_db_secrets_file

    settings = Settings()
    try:
        secrets = (
            parse_db_secrets_text(f"DATABASE_URL={args.db_url}")
            if args.db_url
            else read_db_secrets_file(settings.db_secrets_file)
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    db = Database(secrets, settings.db_sslmode)

    try:
        if args.command == "create":
            key_id, key = create_key(db, args.name)
            print(f"created key id={key_id} name={args.name.strip()!r}")
            print("Store this key now; it cannot be shown again:")
            print(key)
        elif args.command == "list":
            for k in list_keys(db):
                status = "active" if k.active else f"revoked {k.revoked_at}"
                print(f"{k.id}\t{k.name}\tcreated {k.created_at}\t{status}")
        else:
            if not revoke_key(db, args.id):
                print(f"no active key with id {args.id}", file=sys.stderr)
                return 1
            print(f"revoked key id={args.id}")
    except (KeyStoreError, DatabaseUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        db.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
