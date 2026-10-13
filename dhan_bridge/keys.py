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
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    if not db_path.exists():
        raise KeyStoreError(f"database {db_path} does not exist; start the app once to migrate it")
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'api_keys'"
        ).fetchone()
        if not has_table:
            raise KeyStoreError(
                f"{db_path} has no api_keys table; start the app once to migrate it"
            )
        with conn:
            yield conn
    finally:
        conn.close()


def create_key(db_path: Path, name: str) -> tuple[int, str]:
    """Create a key. Returns ``(id, plaintext)``; the plaintext is not recoverable later."""
    name = name.strip()
    if not name:
        raise KeyStoreError("key name must not be empty")
    key = generate_key()
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO api_keys (name, key_hash, created_at) VALUES (?, ?, ?)",
            (name, hash_key(key), _now()),
        )
        key_id = cur.lastrowid
    assert key_id is not None
    return key_id, key


def list_keys(db_path: Path) -> list[ApiKey]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, revoked_at FROM api_keys ORDER BY id"
        ).fetchall()
    return [ApiKey(int(r[0]), r[1], r[2], r[3]) for r in rows]


def revoke_key(db_path: Path, key_id: int) -> bool:
    """Revoke a key. Returns False if no active key has that id."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_now(), key_id),
        )
    return cur.rowcount == 1


def verify_key(db_path: Path, key: str | None) -> ApiKey | None:
    """Return the active key record for ``key``, or None (unknown, revoked or malformed)."""
    if not key or not key.startswith(KEY_PREFIX) or len(key) > 200:
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, name, created_at, revoked_at FROM api_keys"
                " WHERE key_hash = ? AND revoked_at IS NULL",
                (hash_key(key),),
            ).fetchone()
    except (KeyStoreError, sqlite3.Error):
        return None
    return ApiKey(int(row[0]), row[1], row[2], row[3]) if row else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dhan_bridge.keys")
    parser.add_argument("--db", type=Path, help="SQLite path (default: DATA_DIR/bridge.db)")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a key and print it once")
    create.add_argument("name")
    sub.add_parser("list", help="list keys (never prints keys or hashes)")
    revoke = sub.add_parser("revoke", help="revoke a key by id")
    revoke.add_argument("id", type=int)
    args = parser.parse_args(argv)

    if args.db is None:
        from dhan_bridge.config import Settings

        db_path = Settings().db_path
    else:
        db_path = args.db

    try:
        if args.command == "create":
            key_id, key = create_key(db_path, args.name)
            print(f"created key id={key_id} name={args.name.strip()!r}")
            print("Store this key now; it cannot be shown again:")
            print(key)
        elif args.command == "list":
            for k in list_keys(db_path):
                status = "active" if k.active else f"revoked {k.revoked_at}"
                print(f"{k.id}\t{k.name}\tcreated {k.created_at}\t{status}")
        else:
            if not revoke_key(db_path, args.id):
                print(f"no active key with id {args.id}", file=sys.stderr)
                return 1
            print(f"revoked key id={args.id}")
    except KeyStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
