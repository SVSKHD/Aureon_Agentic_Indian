"""Settings and secrets.

Non-secret settings come from the environment / ``.env``. Secrets are read ONLY
from dedicated secrets files so that they never appear in ``docker inspect``,
compose files, CI logs or process environments, and so they can be rotated by
rewriting the file without a restart:

* ``/run/secrets/dhan.env``: ``DHAN_CLIENT_ID``, ``DHAN_ACCESS_TOKEN``
* ``/run/secrets/db.env``:   ``DATABASE_URL`` (a libpq ``postgresql://`` URI)
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, TypeVar
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__version__ = "0.1.0"
VERSION = __version__

REPO_ROOT = Path(__file__).resolve().parent.parent

BridgeMode = Literal["live", "replay"]
SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]


class Settings(BaseSettings):
    """Non-secret runtime settings. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bridge_mode: BridgeMode = "live"
    universe_file: Path = REPO_ROOT / "universe" / "nifty50.txt"
    paper_capital: int = Field(default=1_000_000, gt=0)
    admin_key_file: Path = Path("/run/secrets/admin.key")
    secrets_file: Path = Path("/run/secrets/dhan.env")
    db_secrets_file: Path = Path("/run/secrets/db.env")
    # Applied when DATABASE_URL has no sslmode. "require" for Supabase/any remote host.
    db_sslmode: SslMode = "require"
    data_dir: Path = REPO_ROOT / "data"
    holidays_dir: Path = REPO_ROOT / "holidays"
    scrip_master_url: str = "https://images.dhan.co/api-data/api-scrip-master.csv"
    token_check_interval_s: float = Field(default=3600.0, gt=0)
    secrets_poll_interval_s: float = Field(default=5.0, gt=0)
    # Daily write to bridge.heartbeat: keeps a free Supabase project from pausing.
    heartbeat_enabled: bool = True
    heartbeat_interval_s: float = Field(default=86_400.0, gt=0)
    # Host-side ops tools (backup.py / dbmove.py)
    bridge_container: str = "dhan_bridge"
    pg_tools_image: str = "postgres:{major}-alpine"
    pg_tools_network: str = "host"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def migrations_log_dir(self) -> Path:
        return self.data_dir / "migrations-log"


# --------------------------------------------------------------------------- secrets


def mask(value: str, keep: int = 4) -> str:
    """Mask all but the last ``keep`` characters (fewer for short values)."""
    if not value:
        return "<empty>"
    shown = min(keep, len(value) // 4)
    return "*" * 8 + (value[-shown:] if shown else "")


class SecretsError(RuntimeError):
    """The secrets file is missing or malformed."""


@dataclass(frozen=True)
class DhanSecrets:
    client_id: str
    access_token: str

    def __repr__(self) -> str:
        return (
            f"DhanSecrets(client_id={mask(self.client_id)!r}, "
            f"access_token={mask(self.access_token)!r})"
        )

    __str__ = __repr__

    @property
    def fingerprint(self) -> str:
        """Short, non-reversible id for logs: lets operators see *that* the token changed."""
        digest = hashlib.sha256(f"{self.client_id}:{self.access_token}".encode()).hexdigest()
        return digest[:10]


@dataclass(frozen=True)
class DatabaseSecrets:
    """``DATABASE_URL`` from db.env. Never logged unmasked."""

    url: str

    def __repr__(self) -> str:
        return f"DatabaseSecrets(url={mask_url(self.url)!r})"

    __str__ = __repr__

    @property
    def password(self) -> str:
        return urlsplit(self.url).password or ""

    @property
    def host(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.hostname or ''}:{parts.port or 5432}/{parts.path.lstrip('/')}"

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()[:10]


DB_URL_SCHEMES = ("postgresql", "postgres", "postgresql+psycopg")


def mask_url(url: str) -> str:
    """``postgresql://user:secret@host/db`` -> ``postgresql://user:****@host/db``."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    netloc = parts.netloc.replace(f":{parts.password}@", ":****@", 1)
    return urlunsplit(parts._replace(netloc=netloc))


def _parse_key_values(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Blank lines and ``#`` comments are ignored."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        if not sep:
            raise SecretsError("malformed line in secrets file (expected KEY=VALUE)")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def parse_secrets_text(text: str) -> DhanSecrets:
    values = _parse_key_values(text)
    missing = [k for k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN") if not values.get(k)]
    if missing:
        raise SecretsError(f"secrets file is missing {', '.join(missing)}")
    return DhanSecrets(values["DHAN_CLIENT_ID"], values["DHAN_ACCESS_TOKEN"])


def parse_db_secrets_text(text: str) -> DatabaseSecrets:
    url = _parse_key_values(text).get("DATABASE_URL", "")
    if not url:
        raise SecretsError("db secrets file is missing DATABASE_URL")
    parts = urlsplit(url)
    if parts.scheme not in DB_URL_SCHEMES or not parts.hostname:
        raise SecretsError("DATABASE_URL must be a postgresql://user:password@host:port/db URL")
    return DatabaseSecrets(url)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretsError(f"cannot read secrets file {path}: {exc.strerror}") from None


def read_secrets_file(path: Path) -> DhanSecrets:
    return parse_secrets_text(_read(path))


def read_db_secrets_file(path: Path) -> DatabaseSecrets:
    return parse_db_secrets_text(_read(path))


class RedactingFilter(logging.Filter):
    """Defence in depth: scrub any known secret value from every log record."""

    def __init__(self) -> None:
        super().__init__()
        self._by_source: dict[str, tuple[str, ...]] = {}
        self._secrets: tuple[str, ...] = ()

    def set_secrets(self, *values: str, source: str = "dhan") -> None:
        self._by_source[source] = tuple(v for v in values if v)
        # longest first, so a URL is masked before the password inside it
        self._secrets = tuple(
            sorted({v for vs in self._by_source.values() for v in vs}, key=len, reverse=True)
        )

    def _scrub(self, text: str) -> str:
        for value in self._secrets:
            text = text.replace(value, mask(value))
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = self._scrub(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        # Tracebacks: format them now so the scrubbed text is what handlers emit
        # (logging.Formatter reuses record.exc_text when it is already set).
        if record.exc_info and not record.exc_text:
            record.exc_text = _FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = self._scrub(record.stack_info)
        return True


_FORMATTER = logging.Formatter()


REDACTOR = RedactingFilter()


def install_redactor() -> None:
    """Attach the redactor to every root handler (idempotent)."""
    root = logging.getLogger()
    for handler in root.handlers:
        if REDACTOR not in handler.filters:
            handler.addFilter(REDACTOR)


T = TypeVar("T", DhanSecrets, DatabaseSecrets)


class WatchedSecrets(Generic[T]):
    """Holds parsed secrets from one file and reloads them when the file changes.

    ``poll()`` is cheap (one ``stat``) and is called periodically by the app; it
    re-reads the file only when its mtime/size/inode changed. ``reload()`` forces
    a re-read (used by ``POST /admin/reload-token``). Listeners are notified only
    when the parsed values actually differ.
    """

    label = "secrets"

    def __init__(self, path: Path, parse: Callable[[str], T], source: str) -> None:
        self.path = path
        self._parse: Callable[[str], T] = parse
        self._source = source
        self._lock = threading.Lock()
        self._current: T | None = None
        self._stat_key: tuple[int, int, int] | None = None
        self._listeners: list[Callable[[T], None]] = []

    @property
    def current(self) -> T | None:
        return self._current

    def require(self) -> T:
        if self._current is None:
            raise SecretsError(f"{self.label} not loaded from {self.path}")
        return self._current

    def add_listener(self, listener: Callable[[T], None]) -> None:
        self._listeners.append(listener)

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _redact_values(self, value: T) -> tuple[str, ...]:
        raise NotImplementedError

    def reload(self) -> bool:
        """Re-read the file. Returns True if the secrets changed. Raises SecretsError."""
        with self._lock:
            stat_key = self._stat()
            new = self._parse(_read(self.path))
            self._stat_key = stat_key
            changed = new != self._current
            self._current = new
        if changed:
            REDACTOR.set_secrets(*self._redact_values(new), source=self._source)
            logging.getLogger(__name__).info(
                "%s loaded: %r fingerprint=%s", self.label, new, new.fingerprint
            )
            for listener in self._listeners:
                listener(new)
        return changed

    def poll(self) -> bool:
        """Reload only if the file looks different on disk. Never raises."""
        stat_key = self._stat()
        if stat_key is None or stat_key == self._stat_key:
            return False
        try:
            return self.reload()
        except SecretsError as exc:
            # A half-written file during rotation: keep the old value, retry next poll.
            logging.getLogger(__name__).warning("%s reload skipped: %s", self.label, exc)
            return False


class SecretsStore(WatchedSecrets[DhanSecrets]):
    """Dhan credentials (dhan.env)."""

    label = "Dhan secrets"

    def __init__(self, path: Path) -> None:
        super().__init__(path, parse_secrets_text, source="dhan")

    def _redact_values(self, value: DhanSecrets) -> tuple[str, ...]:
        return (value.client_id, value.access_token)


class DbSecretsStore(WatchedSecrets[DatabaseSecrets]):
    """Database URL (db.env)."""

    label = "Database secrets"

    def __init__(self, path: Path) -> None:
        super().__init__(path, parse_db_secrets_text, source="db")

    def _redact_values(self, value: DatabaseSecrets) -> tuple[str, ...]:
        return (value.url, value.password, unquote(value.password))


def read_admin_key(path: Path) -> str | None:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None
