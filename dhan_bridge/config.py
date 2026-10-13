"""Settings and Dhan secrets.

Non-secret settings come from the environment / ``.env``. The Dhan client id and
access token are read ONLY from a dedicated secrets file (default
``/run/secrets/dhan.env``) so that they never appear in ``docker inspect``,
compose files, CI logs or process environments, and so the daily token can be
rotated by rewriting that file without a restart.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__version__ = "0.1.0"
VERSION = __version__

REPO_ROOT = Path(__file__).resolve().parent.parent

BridgeMode = Literal["live", "replay"]


class Settings(BaseSettings):
    """Non-secret runtime settings. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bridge_mode: BridgeMode = "live"
    universe_file: Path = REPO_ROOT / "universe" / "nifty50.txt"
    paper_capital: int = Field(default=1_000_000, gt=0)
    admin_key_file: Path = Path("/run/secrets/admin.key")
    secrets_file: Path = Path("/run/secrets/dhan.env")
    data_dir: Path = REPO_ROOT / "data"
    holidays_dir: Path = REPO_ROOT / "holidays"
    scrip_master_url: str = "https://images.dhan.co/api-data/api-scrip-master.csv"
    token_check_interval_s: float = Field(default=3600.0, gt=0)
    secrets_poll_interval_s: float = Field(default=5.0, gt=0)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bridge.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"


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


def parse_secrets_text(text: str) -> DhanSecrets:
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
    missing = [k for k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN") if not values.get(k)]
    if missing:
        raise SecretsError(f"secrets file is missing {', '.join(missing)}")
    return DhanSecrets(values["DHAN_CLIENT_ID"], values["DHAN_ACCESS_TOKEN"])


def read_secrets_file(path: Path) -> DhanSecrets:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretsError(f"cannot read secrets file {path}: {exc.strerror}") from None
    return parse_secrets_text(text)


class RedactingFilter(logging.Filter):
    """Defence in depth: scrub any known secret value from every log record."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: tuple[str, ...] = ()

    def set_secrets(self, *values: str) -> None:
        self._secrets = tuple(v for v in values if v)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for value in self._secrets:
            redacted = redacted.replace(value, mask(value))
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


REDACTOR = RedactingFilter()


def install_redactor() -> None:
    """Attach the redactor to every root handler (idempotent)."""
    root = logging.getLogger()
    for handler in root.handlers:
        if REDACTOR not in handler.filters:
            handler.addFilter(REDACTOR)


Listener = Callable[[DhanSecrets], None]


class SecretsStore:
    """Holds the current Dhan secrets and reloads them when the file changes.

    ``poll()`` is cheap (one ``stat``) and is called periodically by the app; it
    re-reads the file only when its mtime/size/inode changed. ``reload()`` forces
    a re-read (used by ``POST /admin/reload-token``). Listeners are notified only
    when the parsed values actually differ.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._current: DhanSecrets | None = None
        self._stat_key: tuple[int, int, int] | None = None
        self._listeners: list[Listener] = []

    @property
    def current(self) -> DhanSecrets | None:
        return self._current

    def require(self) -> DhanSecrets:
        if self._current is None:
            raise SecretsError(f"Dhan secrets not loaded from {self.path}")
        return self._current

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def reload(self) -> bool:
        """Re-read the file. Returns True if the secrets changed. Raises SecretsError."""
        with self._lock:
            stat_key = self._stat()
            new = read_secrets_file(self.path)
            self._stat_key = stat_key
            changed = new != self._current
            self._current = new
        if changed:
            REDACTOR.set_secrets(new.client_id, new.access_token)
            logging.getLogger(__name__).info(
                "Dhan secrets loaded: %r fingerprint=%s", new, new.fingerprint
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
            # A half-written file during rotation: keep the old token, retry next poll.
            logging.getLogger(__name__).warning("secrets reload skipped: %s", exc)
            return False


def read_admin_key(path: Path) -> str | None:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None
