"""Single-instance guard for a database + cache pair.

Two bot processes sharing one SQLite ledger and one cache directory corrupt each
other: each keeps its own in-memory book, so they overwrite one another's
positions and trip a false reconcile halt. It has already happened here, once.

The guard is an OS-level byte-range lock on a file derived from the database
path. That matters over the usual "write a PID file and check it" approach for two
reasons: the lock is released by the kernel when the process exits or crashes,
so there is no stale lock to clean up by hand, and a second process is refused at
the operating system rather than by a check that can be raced.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover - platform selection
    import msvcrt
else:  # pragma: no cover - platform selection
    import fcntl


class InstanceAlreadyRunning(RuntimeError):
    """Raised when another process already owns this database."""


def lock_path_for(db_path: str | os.PathLike) -> Path:
    return Path(str(db_path) + ".lock")


class InstanceLock:
    """Exclusive advisory lock held for the lifetime of the process."""

    def __init__(self, db_path: str | os.PathLike):
        self.db_path = Path(db_path).resolve()
        self.path = lock_path_for(self.db_path)
        self._handle = None
        self._held = False

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+b" never truncates, so the byte we are about to lock always exists.
        handle = open(self.path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise InstanceAlreadyRunning(
                f"another StratBot instance already holds {self.db_path}.\n"
                f"  lock file : {self.path}\n"
                f"  refused because: {exc}\n"
                f"  Stop the other instance, or point this one at a different --db."
            ) from exc

        self._handle = handle
        self._held = True
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode())
        handle.flush()
        return self

    def release(self) -> None:
        if not self._held or self._handle is None:
            return
        handle, self._handle, self._held = self._handle, None, False
        try:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            # The kernel drops the lock when the handle closes either way; a
            # failed explicit unlock must not mask the caller's shutdown.
            pass
        finally:
            handle.close()

    @property
    def held(self) -> bool:
        return self._held

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()