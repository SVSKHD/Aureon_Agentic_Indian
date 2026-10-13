"""Single-instance lock: the Dhan feed must exist exactly once per deployment."""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO

LOCK_NAME = "bridge.lock"


class InstanceLockError(RuntimeError):
    pass


def acquire_instance_lock(data_dir: Path) -> IO[str]:
    """Hold an exclusive lock for the app's lifetime. Raises if another instance holds it.

    Works across containers sharing the data volume (same kernel, same inode).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = (data_dir / LOCK_NAME).open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise InstanceLockError(
            f"another dhan_bridge instance holds {data_dir / LOCK_NAME}; "
            "only one instance may run per data directory"
        ) from None
    return handle
