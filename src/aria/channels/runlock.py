"""
aria/channels/runlock.py — One receiver per channel at a time.

A channel may run as a background service OR attached to the `aria` CLI, and
the user may have both set up (or switch modes). Two processes polling the
same Telegram token conflict, so whoever receives for a channel holds an
exclusive flock on ~/.aria/run/<channel>.lock:

  - the attached CLI tries once (non-blocking) and, if a service holds it,
    tells the user the channel is already online as a service;
  - a service waits (blocking) — it takes over when the CLI exits instead of
    crash-looping, which would trip the systemd start limit and rollback.

The lock dies with its process (kernel-released), so a crash never leaves a
stale lock. No-op where fcntl is unavailable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

try:
    import fcntl as _fcntl
except ImportError:          # pragma: no cover — Windows
    _fcntl = None  # type: ignore[assignment]


def _path(name: str) -> Path:
    d = Path.home() / ".aria" / "run"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / f"{name}.lock"


class RunLock:
    def __init__(self, name: str) -> None:
        self.name = name
        self._fh: IO[str] | None = None

    def acquire(self, blocking: bool = False) -> bool:
        if self._fh is not None:
            return True
        fh = open(_path(self.name), "a+", encoding="utf-8")
        if _fcntl is not None:
            flags = _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB)
            try:
                _fcntl.flock(fh.fileno(), flags)
            except OSError:
                fh.close()
                return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(self._fh.fileno(), _fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None


def hold_for_service(name: str, log=None) -> RunLock:
    """Service entry points: take the channel's run lock, waiting (with a log
    line) while an attached `aria` session holds it."""
    lock = RunLock(name)
    if not lock.acquire(blocking=False):
        if log is not None:
            log.info("Channel %s is attached to a running `aria` session — "
                     "waiting for it to exit before going online", name)
        lock.acquire(blocking=True)
    return lock
