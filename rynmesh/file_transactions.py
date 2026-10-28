"""Reentrant, process-shared locks for local read/modify/atomic-replace stores."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

_guard = threading.Lock()
_locks: dict[str, tuple[threading.RLock, threading.local]] = {}


@contextmanager
def file_transaction(lock_path: Path):
    key = os.path.normcase(str(lock_path.resolve()))
    with _guard:
        lock, local = _locks.setdefault(key, (threading.RLock(), threading.local()))
    with lock:
        if getattr(local, "depth", 0):
            local.depth += 1
            try:
                yield
            finally:
                local.depth -= 1
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            local.depth = 1
            try:
                yield
            finally:
                local.depth = 0
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
