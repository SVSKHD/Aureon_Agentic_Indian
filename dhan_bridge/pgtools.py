"""Host-side helpers for the ops tools (backup.py, dbmove.py).

* :class:`PgTools` runs ``pg_dump`` / ``pg_restore`` / ``psql`` in a throwaway
  ``postgres:<major>-alpine`` container (``docker run --rm``). The client major is
  chosen from the server (``SHOW server_version_num``) so it is always >= the
  server's. Neither the host nor the bridge image needs Postgres client tools.
  The connection URL is passed through the environment (``-e PGDBURL``), never as
  a command-line argument, so it does not show up in ``ps`` or ``docker inspect``
  of a running container's args. Dumps stream through stdin/stdout: no volumes.
* :class:`ThrowawayPostgres` starts a plain postgres server on a random loopback
  port for restore verification (nothing Supabase-specific involved).
* :class:`DockerHost` stops/starts the bridge container and runs the health gate.

Defaults assume a Linux host: tool containers use ``--network host`` so that both
remote hosts (Supabase) and loopback test servers are reachable.
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import IO

import psycopg

from dhan_bridge.config import mask_url

log = logging.getLogger(__name__)

DOCKER = os.environ.get("DOCKER_BIN", "docker")


class PgToolError(RuntimeError):
    pass


def _scrub(text: str, *urls: str) -> str:
    for url in urls:
        if url:
            text = text.replace(url, mask_url(url))
    return text


@dataclass(frozen=True)
class PgTools:
    major: int
    image_template: str = "postgres:{major}-alpine"
    network: str = "host"

    @property
    def image(self) -> str:
        return self.image_template.format(major=self.major)

    def run(
        self,
        tool: str,
        args: list[str],
        url: str,
        *,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
        timeout_s: float = 3600,
    ) -> str:
        """Run ``tool args --dbname=$PGDBURL`` in the pg tools container. Returns stderr."""
        cmd = [
            DOCKER, "run", "--rm", "-i", "--network", self.network, "-e", "PGDBURL",
            self.image, "sh", "-c", 'exec "$0" "$@" --dbname="$PGDBURL"', tool, *args,
        ]  # fmt: skip
        env = {**os.environ, "PGDBURL": url}
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell on the host
            cmd,
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout_s,
            check=False,
        )
        err = _scrub(proc.stderr.decode(errors="replace"), url)
        if proc.returncode != 0:
            raise PgToolError(f"{tool} ({self.image}) failed with exit {proc.returncode}: {err}")
        return err


class ThrowawayPostgres:
    """``with ThrowawayPostgres(16) as url:`` -> a fresh, empty, local postgres server."""

    def __init__(self, major: int, image_template: str = "postgres:{major}-alpine") -> None:
        self.image = image_template.format(major=major)
        self.name = f"dhan-verify-{uuid.uuid4().hex[:10]}"
        self.url = ""

    def __enter__(self) -> str:
        password = secrets.token_hex(16)
        subprocess.run(  # noqa: S603
            [DOCKER, "run", "-d", "--rm", "--name", self.name, "-e", "POSTGRES_PASSWORD",
             "-p", "127.0.0.1::5432", self.image],
            env={**os.environ, "POSTGRES_PASSWORD": password},
            check=True, capture_output=True,
        )  # fmt: skip
        try:
            out = subprocess.run(  # noqa: S603
                [DOCKER, "port", self.name, "5432/tcp"], check=True, capture_output=True, text=True
            ).stdout
            port = out.strip().splitlines()[0].rsplit(":", 1)[1]
            self.url = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres?sslmode=disable"
            wait_ready(self.url)
        except BaseException:
            self.__exit__()
            raise
        return self.url

    def __exit__(self, *exc: object) -> None:
        subprocess.run([DOCKER, "rm", "-f", self.name], capture_output=True, check=False)  # noqa: S603


def wait_ready(url: str, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
                return
        except psycopg.OperationalError as exc:
            last = exc
            time.sleep(0.3)
    raise PgToolError(f"throwaway postgres did not become ready: {type(last).__name__}")


class DockerHost:
    """The bridge container on this host."""

    def __init__(self, container: str = "dhan_bridge") -> None:
        self.container = container

    def _docker(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [DOCKER, *args], capture_output=True, text=True, check=check
        )

    def is_running(self) -> bool:
        proc = self._docker("inspect", "-f", "{{.State.Running}}", self.container, check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def exists(self) -> bool:
        return self._docker("inspect", self.container, check=False).returncode == 0

    def stop(self) -> None:
        log.info("stopping %s", self.container)
        self._docker("stop", "-t", "20", self.container)

    def start(self) -> None:
        log.info("starting %s", self.container)
        self._docker("start", self.container)

    def restart(self) -> None:
        log.info("restarting %s", self.container)
        self._docker("restart", "-t", "20", self.container)

    def health_gate(self, timeout_s: float = 120, strict: bool = True) -> bool:
        """Poll ``python -m dhan_bridge.health probe [--strict]`` inside the container."""
        args = ["exec", self.container, "python", "-m", "dhan_bridge.health", "probe"]
        if strict:
            args.append("--strict")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            proc = self._docker(*args, check=False)
            if proc.returncode == 0:
                log.info("health gate passed: %s", proc.stdout.strip())
                return True
            time.sleep(5)
        log.error("health gate failed after %.0fs", timeout_s)
        return False
