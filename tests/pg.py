"""Throwaway PostgreSQL servers for tests (docker CLI; no SQLite fallback).

* ``TEST_PG_ADMIN_URL`` set -> use that server (e.g. a Jenkins postgres sidecar).
* otherwise start ``$PG_TEST_IMAGE`` (default ``postgres:16-alpine``) on a random
  loopback port and remove it at the end of the session.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass, field

import psycopg

DEFAULT_IMAGE = "postgres:{major}-alpine"


def image_for(major: int) -> str:
    return os.environ.get("PG_TEST_IMAGE", DEFAULT_IMAGE).format(major=major)


@dataclass
class PgServer:
    admin_url: str  # libpq URI with superuser rights, database "postgres"
    container: str | None = None
    major: int = 16
    _databases: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, major: int = 16) -> PgServer:
        password = secrets.token_hex(12)
        name = f"dhan-test-pg{major}-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={password}",
             "-p", "127.0.0.1::5432", image_for(major), "-c", "fsync=off",
             "-c", "full_page_writes=off", "-c", "synchronous_commit=off"],
            check=True, capture_output=True, text=True,
        )  # fmt: skip
        try:
            port = (
                subprocess.run(
                    ["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True
                )
                .stdout.strip()
                .splitlines()[0]
                .rsplit(":", 1)[1]
            )
            server = cls(f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres", name, major)
            server.wait_ready()
            return server
        except BaseException:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            raise

    def wait_ready(self, timeout_s: float = 60) -> None:
        deadline = time.monotonic() + timeout_s
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.admin_url, connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                    return
            except psycopg.OperationalError as exc:
                last = exc
                time.sleep(0.3)
        raise RuntimeError(f"postgres did not become ready: {last}")

    def create_database(self) -> str:
        """Fresh database; returns its libpq URL (sslmode=disable: local test server)."""
        name = f"t_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        self._databases.append(name)
        return self.url_for(name)

    def url_for(self, database: str) -> str:
        base = self.admin_url.rsplit("/", 1)[0]
        return f"{base}/{database}?sslmode=disable"

    def stop(self) -> None:
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, check=False)
            return
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            for name in self._databases:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def from_env_or_start(major: int = 16) -> PgServer:
    admin = os.environ.get("TEST_PG_ADMIN_URL")
    if admin:
        with psycopg.connect(admin) as conn:
            version = int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10_000  # type: ignore[index]
        return PgServer(admin, None, version)
    return PgServer.start(major)
