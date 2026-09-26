"""Single-writer process lock for a run directory.

A second writer fails fast rather than interleaving appends into the journal.
An OS advisory flock owns the lock; the PID is diagnostic only. Kernel release
on process exit avoids stale-lock takeover races, including a torn PID file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..errors import RunDirLockedError

LOCK_NAME = "run.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunDirLock:
    """Exclusive advisory lock over a run directory."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / LOCK_NAME
        self._acquired = False

    def acquire(self, *, steal_stale: bool = True) -> None:
        import fcntl
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            raise RunDirLockedError(f"run directory {self.run_dir} has a live writer") from exc
        os.ftruncate(self._fd, 0)
        os.write(self._fd, json.dumps({"pid": os.getpid(), "acquired_at": time.time()}).encode())
        os.fsync(self._fd)
        self._acquired = True

    def _read_holder(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def release(self) -> None:
        if not self._acquired:
            return
        import fcntl
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._acquired = False

    def __enter__(self) -> "RunDirLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
