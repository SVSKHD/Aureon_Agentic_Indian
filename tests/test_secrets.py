from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import pytest

from dhan_bridge.config import (
    REDACTOR,
    DhanSecrets,
    SecretsError,
    SecretsStore,
    Settings,
    mask,
    parse_secrets_text,
    read_admin_key,
)
from tests.conftest import write_secrets

CLIENT_ID = "1100223344"
TOKEN = "eyJhbGciOiJIUzUxMiJ9.super-secret-payload.sig"


def test_repr_and_str_mask_both_values() -> None:
    s = DhanSecrets(CLIENT_ID, TOKEN)
    for text in (repr(s), str(s), f"{s}", f"{s!r}"):
        assert CLIENT_ID not in text
        assert TOKEN not in text
        assert "super-secret" not in text
    assert repr(s).endswith("'********.sig')")


def test_mask_short_values_reveal_nothing() -> None:
    assert mask("abc") == "********"
    assert mask("") == "<empty>"


def test_parse_secrets_text() -> None:
    s = parse_secrets_text(
        f"# dev secrets\nexport DHAN_CLIENT_ID='{CLIENT_ID}'\n\nDHAN_ACCESS_TOKEN=\"{TOKEN}\"\n"
    )
    assert s == DhanSecrets(CLIENT_ID, TOKEN)


@pytest.mark.parametrize("text", ["DHAN_CLIENT_ID=1\n", "garbage line\n", ""])
def test_parse_secrets_rejects_bad_files(text: str) -> None:
    with pytest.raises(SecretsError) as err:
        parse_secrets_text(text)
    assert TOKEN not in str(err.value)


def test_secrets_never_come_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("DHAN_CLIENT_ID", CLIENT_ID)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert TOKEN not in repr(settings)
    assert CLIENT_ID not in repr(settings)
    store = SecretsStore(tmp_path / "missing.env")
    with pytest.raises(SecretsError, match="cannot read"):
        store.reload()


def test_logs_are_masked(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "dhan.env"
    write_secrets(path, CLIENT_ID, TOKEN)
    caplog.handler.addFilter(REDACTOR)
    with caplog.at_level(logging.INFO):
        store = SecretsStore(path)
        store.reload()
        # even a careless log line that interpolates raw values gets scrubbed
        logging.getLogger("careless").info("token=%s client=%s", TOKEN, CLIENT_ID)
    assert caplog.text
    assert TOKEN not in caplog.text
    assert CLIENT_ID not in caplog.text
    assert "fingerprint=" in caplog.text


def test_reload_picks_up_new_token_without_restart(tmp_path: Path) -> None:
    path = tmp_path / "dhan.env"
    write_secrets(path, CLIENT_ID, "token-day-1-aaaaaaaa")
    store = SecretsStore(path)
    seen: list[DhanSecrets] = []
    store.reload()
    store.add_listener(seen.append)
    assert store.poll() is False  # unchanged on disk

    write_secrets(path, CLIENT_ID, "token-day-2-bbbbbbbb")
    os.utime(path, ns=(1, 1))  # guarantee a different mtime even on coarse filesystems
    assert store.poll() is True
    assert store.require().access_token == "token-day-2-bbbbbbbb"
    assert [s.access_token for s in seen] == ["token-day-2-bbbbbbbb"]


def test_atomic_replace_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "dhan.env"
    write_secrets(path, CLIENT_ID, "token-day-1-aaaaaaaa")
    store = SecretsStore(path)
    store.reload()
    tmp = tmp_path / "dhan.env.tmp"
    write_secrets(tmp, CLIENT_ID, "token-day-2-bbbbbbbb")
    tmp.replace(path)
    assert store.poll() is True


def test_half_written_file_keeps_old_token(tmp_path: Path) -> None:
    path = tmp_path / "dhan.env"
    write_secrets(path, CLIENT_ID, "token-day-1-aaaaaaaa")
    store = SecretsStore(path)
    store.reload()
    path.write_text("DHAN_CLIENT_ID=1100223344\n", encoding="utf-8")
    os.utime(path, ns=(2, 2))
    assert store.poll() is False
    assert store.require().access_token == "token-day-1-aaaaaaaa"
    path.unlink()
    assert store.poll() is False


def test_require_without_load() -> None:
    with pytest.raises(SecretsError):
        SecretsStore(Path("/nonexistent")).require()


def test_read_admin_key(tmp_path: Path) -> None:
    key = tmp_path / "admin.key"
    assert read_admin_key(key) is None
    key.write_text("  \n", encoding="utf-8")
    assert read_admin_key(key) is None
    key.write_text("k3y\n", encoding="utf-8")
    assert read_admin_key(key) == "k3y"


def test_tracebacks_are_masked(tmp_path: Path) -> None:
    path = tmp_path / "dhan.env"
    write_secrets(path, CLIENT_ID, TOKEN)
    SecretsStore(path).reload()

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(REDACTOR)
    logger = logging.getLogger("tb-test")
    logger.addHandler(handler)
    try:
        try:
            raise ValueError(f"bad token {TOKEN} for {CLIENT_ID}")
        except ValueError:
            logger.exception("request failed")
        logger.error("with stack", stack_info=True, extra={"x": TOKEN})
    finally:
        logger.removeHandler(handler)
    text = stream.getvalue()
    assert "ValueError: bad token" in text  # the traceback is still there...
    assert TOKEN not in text  # ...but scrubbed
    assert CLIENT_ID not in text
