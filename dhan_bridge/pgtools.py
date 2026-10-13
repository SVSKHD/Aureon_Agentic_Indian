"""Run PostgreSQL client tools in containers, never on the host or in the bridge image.

The host-side ops tools (backup.py, dbmove.py) never open a database connection
themselves: our compose Postgres sits on an internal network with no published
port. Every database operation runs as ``psql`` / ``pg_dump`` / ``pg_restore``
inside a postgres container through a :class:`PgRunner`:

* :class:`ExecRunner`: our own database. ``docker compose exec -T postgres sh -c
  ...``; the tools connect over the container's local unix socket, so no password
  is needed or passed (the official image trusts local socket connections).
* :class:`DockerRunner`: any other host. ``docker run --rm -i --network <net>
  postgres:<major>-alpine sh -c ...``; the URL travels in the environment
  (``-e PGDBURL``), never on a command line. ``<major>`` is the server's major
  (``SHOW server_version_num`` is asked first), so the client is never older.

Every runner executes a small POSIX ``sh`` script with ``$PGDBURL`` set; binary
data (dumps) streams through stdin/stdout, so no volumes are needed.
"""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
import shlex
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import quote, unquote, urlsplit

from dhan_bridge.config import Settings, mask_url
from dhan_bridge.dbverify import PSQL_SCRIPT, parse
from dhan_bridge.models import SCHEMA

log = logging.getLogger(__name__)

DOCKER = os.environ.get("DOCKER_BIN", "docker")
PROBE_MAJOR = 16  # any client can answer SHOW server_version_num
SOCKET_DIR = "/var/run/postgresql"


class PgToolError(RuntimeError):
    pass


def _scrub(text: str, url: str) -> str:
    return text.replace(url, mask_url(url)) if url else text


class PgRunner(Protocol):
    major: int
    label: str

    def sh(
        self,
        script: str,
        *,
        env: dict[str, str] | None = None,
        stdin: IO[bytes] | bytes | None = None,
        stdout: IO[bytes] | None = None,
        timeout_s: float = 3600,
    ) -> bytes: ...


def _run(
    cmd: list[str],
    *,
    env: dict[str, str],
    stdin: IO[bytes] | bytes | None,
    stdout: IO[bytes] | None,
    timeout_s: float,
    secret: str,
    what: str,
) -> bytes:
    kwargs: dict[str, Any] = {}
    if isinstance(stdin, bytes):
        kwargs["input"] = stdin
    else:
        kwargs["stdin"] = stdin if stdin is not None else subprocess.DEVNULL
    proc = subprocess.run(  # noqa: S603 - fixed argv, no host shell
        cmd,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **env},
        timeout=timeout_s,
        check=False,
        **kwargs,
    )
    if proc.returncode != 0:
        err = _scrub(proc.stderr.decode(errors="replace").strip(), secret)
        raise PgToolError(f"{what} failed (exit {proc.returncode}): {err}")
    return proc.stdout if stdout is None else b""


@dataclass
class ExecRunner:
    """Tools inside OUR database container (``docker compose exec -T postgres``)."""

    exec_cmd: list[str]
    target: str
    user: str
    dbname: str
    major: int = 0
    label: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.label = f"{' '.join(self.exec_cmd)} {self.target}"
        if not self.major:
            self.major = server_major(self)

    @property
    def url(self) -> str:
        # local unix socket inside the container: no host, no password
        return f"postgresql://{quote(self.user)}@/{quote(self.dbname)}?host={SOCKET_DIR}"

    def sh(
        self,
        script: str,
        *,
        env: dict[str, str] | None = None,
        stdin: IO[bytes] | bytes | None = None,
        stdout: IO[bytes] | None = None,
        timeout_s: float = 3600,
    ) -> bytes:
        values = {"PGDBURL": self.url, **(env or {})}
        flags = [arg for k, v in values.items() for arg in ("-e", f"{k}={v}")]
        cmd = [*self.exec_cmd, *flags, self.target, "sh", "-c", script]
        return _run(
            cmd, env={}, stdin=stdin, stdout=stdout, timeout_s=timeout_s, secret="", what=self.label
        )


@dataclass
class DockerRunner:
    """Tools in a throwaway ``postgres:<major>-alpine`` container, for any other host."""

    url: str
    image_template: str = "postgres:{major}-alpine"
    network: str = "bridge"
    major: int = 0
    label: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if not self.major:
            probe = DockerRunner(self.url, self.image_template, self.network, PROBE_MAJOR)
            self.major = server_major(probe)
        self.label = f"docker run {self.image}"

    @property
    def image(self) -> str:
        return self.image_template.format(major=self.major)

    def sh(
        self,
        script: str,
        *,
        env: dict[str, str] | None = None,
        stdin: IO[bytes] | bytes | None = None,
        stdout: IO[bytes] | None = None,
        timeout_s: float = 3600,
    ) -> bytes:
        values = {"PGDBURL": self.url, **(env or {})}
        flags = [arg for k in values for arg in ("-e", k)]  # names only: values via env
        cmd = [DOCKER, "run", "--rm", "-i", "--network", self.network, *flags, self.image,
               "sh", "-c", script]  # fmt: skip
        return _run(
            cmd,
            env=values,
            stdin=stdin,
            stdout=stdout,
            timeout_s=timeout_s,
            secret=self.url,
            what=self.label,
        )


def is_own_database(url: str, settings: Settings) -> bool:
    parts = urlsplit(url)
    host = parts.hostname or ""
    return settings.db_service in (host, f"{host}:{parts.port or 5432}")


def runner_for(url: str, settings: Settings) -> PgRunner:
    if is_own_database(url, settings):
        parts = urlsplit(url)
        return ExecRunner(
            shlex.split(settings.db_exec_cmd),
            settings.db_exec_target,
            unquote(parts.username or "postgres"),
            unquote(parts.path.lstrip("/") or "postgres"),
        )
    return DockerRunner(url, settings.pg_tools_image, settings.pg_tools_network)


# --------------------------------------------------------------------------- operations

PSQL = 'psql -X -qAt -v ON_ERROR_STOP=1 --dbname="$PGDBURL"'


def psql(runner: PgRunner, sql: str, timeout_s: float = 120) -> str:
    """Run SQL (from stdin) and return the unaligned, tuples-only output."""
    out = runner.sh(f"{PSQL} -f -", stdin=sql.encode(), timeout_s=timeout_s)
    return out.decode().strip()


def server_major(runner: PgRunner) -> int:
    return int(psql(runner, "SHOW server_version_num;")) // 10_000


def fingerprint(runner: PgRunner) -> dict[str, Any]:
    return parse(psql(runner, PSQL_SCRIPT))


def alembic_revision(runner: PgRunner) -> str | None:
    if psql(runner, f"SELECT to_regclass('{SCHEMA}.alembic_version') IS NOT NULL;") != "t":
        return None
    return psql(runner, f"SELECT version_num FROM {SCHEMA}.alembic_version LIMIT 1;") or None


def schema_info(runner: PgRunner) -> dict[str, int | bool]:
    out = psql(
        runner,
        f"""SELECT
            EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '{SCHEMA}'),
            (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = '{SCHEMA}'),
            (SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = '{SCHEMA}' AND c.relkind IN ('r', 'm'));""",
    )
    exists, objects, size = out.split("|")
    return {"exists": exists == "t", "objects": int(objects), "bytes": int(size)}


# The whole consistent dump happens in ONE psql session inside the tools container:
# the REPEATABLE READ, READ ONLY transaction exports its snapshot, the fingerprint
# is computed in it, and pg_dump (run from psql with \!) imports that snapshot
# while psql keeps the transaction open. Both files leave as a tar on stdout.
DUMP_SCRIPT = r"""
set -eu
d="$(mktemp -d)"
trap 'rm -rf "$d"' EXIT
export OUTDIR="$d"
printf '%s' "$PGSCRIPT" \
  | psql -X -qAt -v ON_ERROR_STOP=1 -v OUTDIR="$d" --dbname="$PGDBURL" -f - >/dev/null
[ -f "$d/dump.ok" ] || { echo "pg_dump did not complete" >&2; exit 1; }
pg_restore --list "$d/bridge.dump" >/dev/null
tar -C "$d" -cf - fingerprint.json bridge.dump
"""

DUMP_PSQL = (
    "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
    "SELECT pg_export_snapshot() AS snap \\gset\n"
    "\\setenv PGSNAP :snap\n"
    "\\o :OUTDIR/fingerprint.json\n"
    f"{PSQL_SCRIPT}"
    "\\o\n"
    f'\\! pg_dump -Fc --schema={SCHEMA} --snapshot="$PGSNAP" --dbname="$PGDBURL" '
    '-f "$OUTDIR/bridge.dump" && touch "$OUTDIR/dump.ok"\n'
    "COMMIT;\n"
)


def consistent_dump(runner: PgRunner, dump_path: Path) -> dict[str, Any]:
    """Write ``dump_path`` (pg_dump -Fc of schema bridge) and return its fingerprint."""
    raw = runner.sh(DUMP_SCRIPT, env={"PGSCRIPT": DUMP_PSQL})
    fp: dict[str, Any] | None = None
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        for member in tar.getmembers():
            handle = tar.extractfile(member)
            if handle is None:
                continue
            name = Path(member.name).name
            if name == "fingerprint.json":
                fp = json.loads(handle.read())
            elif name == "bridge.dump":
                with dump_path.open("wb") as out:
                    while chunk := handle.read(1 << 20):
                        out.write(chunk)
    if fp is None or not dump_path.exists():
        raise PgToolError("dump archive is incomplete")
    return fp


def restore(runner: PgRunner, dump_path: Path, pre_sql: str = "") -> None:
    """``pg_restore --clean --if-exists --schema=bridge`` (single transaction).

    ``pre_sql`` runs first in its own psql call (e.g. creating an empty schema,
    which ``--schema`` restores need because they skip CREATE SCHEMA).
    """
    if pre_sql:
        psql(runner, pre_sql)
    with dump_path.open("rb") as handle:
        runner.sh(
            "pg_restore --clean --if-exists --schema=bridge --no-owner --no-privileges "
            '--single-transaction --exit-on-error --dbname="$PGDBURL"',
            stdin=handle,
        )


# --------------------------------------------------------------------------- throwaway server


class ThrowawayPostgres:
    """``with ThrowawayPostgres(16, image) as runner:`` -> an empty plain postgres server.

    Nothing is published: tools run inside it (docker exec, local socket).
    """

    def __init__(self, major: int, image_template: str = "postgres:{major}-alpine") -> None:
        self.image = image_template.format(major=major)
        self.name = f"dhan-verify-{uuid.uuid4().hex[:10]}"
        self.major = major

    def __enter__(self) -> ExecRunner:
        subprocess.run(  # noqa: S603
            [
                DOCKER,
                "run",
                "-d",
                "--rm",
                "--name",
                self.name,
                "-e",
                "POSTGRES_PASSWORD",
                self.image,
            ],
            env={**os.environ, "POSTGRES_PASSWORD": secrets.token_hex(16)},
            check=True,
            capture_output=True,
        )
        try:
            deadline = time.monotonic() + 60
            while True:
                ready = subprocess.run(  # noqa: S603
                    [DOCKER, "exec", self.name, "pg_isready", "-U", "postgres", "-h", SOCKET_DIR],
                    capture_output=True,
                    check=False,
                )
                # the entrypoint restarts the server once after initdb: require 2 hits
                if ready.returncode == 0 and self._psql_ok():
                    break
                if time.monotonic() > deadline:
                    raise PgToolError(f"throwaway {self.image} did not become ready")
                time.sleep(0.5)
            return ExecRunner([DOCKER, "exec", "-i"], self.name, "postgres", "postgres", self.major)
        except BaseException:
            self.__exit__()
            raise

    def _psql_ok(self) -> bool:
        proc = subprocess.run(  # noqa: S603
            [DOCKER, "exec", self.name, "psql", "-U", "postgres", "-tAc",
             "SELECT 1 WHERE NOT pg_is_in_recovery()"],
            capture_output=True, check=False, text=True,
        )  # fmt: skip
        # during initdb the server listens on the socket briefly; the init log ends with
        # "PostgreSQL init process complete" before the final start
        logs = subprocess.run(  # noqa: S603
            [DOCKER, "logs", self.name], capture_output=True, check=False, text=True
        )
        init_done = "init process complete" in (logs.stdout + logs.stderr)
        return proc.returncode == 0 and proc.stdout.strip() == "1" and init_done

    def __exit__(self, *exc: object) -> None:
        subprocess.run([DOCKER, "rm", "-f", self.name], capture_output=True, check=False)  # noqa: S603


# --------------------------------------------------------------------------- bridge container


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
