"""Durable JSON/bytes writer shared by every on-disk store in the node.

One write path: a uniquely named temp file in the destination's own directory,
written with a 0600 descriptor, flushed, fsynced, then renamed into place with
`os.replace` (atomic on POSIX and Windows within one filesystem). The parent
directory is fsynced where supported, so the rename itself survives a crash,
not just the file's bytes. On Windows, private files receive a protected DACL
before bytes are written; chmod alone cannot enforce owner-only access there.

Nothing here ever puts a filesystem path or record content into an exception
message: these errors can reach a log line or an HTTP response verbatim.

A hard kill (power loss, `SIGKILL`) between the temp file's creation and the
`os.replace` rename leaves an orphaned `.{name}.{uuid}.tmp` file behind in the
destination's directory; because each write picks a fresh random name, a
later retry cannot land on and overwrite it the way a fixed temp name would,
so such orphans accumulate until something else cleans the directory.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .private_permissions import restrict_windows_acl

_logger = logging.getLogger(__name__)

MAX_RECORD_BYTES = 16 * 1024 * 1024
FILE_MODE = 0o600
DIR_MODE = 0o700

_REQUIRED = object()


class AtomicIOError(OSError):
    """A record could not be written or read safely."""


def atomic_write_bytes(
    path: str | Path,
    data: bytes,
    *,
    mode: int = FILE_MODE,
    dir_mode: int = DIR_MODE,
    max_bytes: int = MAX_RECORD_BYTES,
    fsync_dir: bool = True,
) -> None:
    """Write ``data`` to ``path`` durably, or leave ``path`` untouched.

    A unique temp file in ``path``'s own directory is written, fsynced, and
    renamed over the destination. Any failure along the way removes the temp
    file and never leaves a partially written destination.
    """

    path = Path(path)
    if len(data) > max_bytes:
        raise AtomicIOError("record exceeds max_bytes")
    tmp: Path | None = None
    try:
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, dir_mode)
        except OSError:
            pass  # a shared parent directory may not be ours to chmod
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            if sys.platform == "win32" and mode == FILE_MODE:
                restrict_windows_acl(tmp)  # Restrict access before writing private bytes.
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass  # the open() mode above is masked by umask on some platforms
        _replace(tmp, path)
        tmp = None
        if fsync_dir:
            _fsync_dir(parent)
    except OSError as exc:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise AtomicIOError("record write failed") from exc
    except BaseException:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise


def _replace(source: Path, destination: Path) -> None:
    # Windows sharing violations can be transient (concurrent rename/read or
    # antivirus). Bound retries; permanent denial still leaves the old value.
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if sys.platform != "win32" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 7:
                raise
            time.sleep(min(0.005 * 2 ** attempt, 0.08))


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = True,
    ensure_ascii: bool = True,
    separators: tuple[str, str] | None = None,
    trailing_newline: bool = False,
    mode: int = FILE_MODE,
    dir_mode: int = DIR_MODE,
    max_bytes: int = MAX_RECORD_BYTES,
    fsync_dir: bool = True,
) -> None:
    """Serialize ``value`` as JSON and write it durably via `atomic_write_bytes`."""

    try:
        text = json.dumps(value, indent=indent, sort_keys=sort_keys, ensure_ascii=ensure_ascii, separators=separators)
    except (TypeError, ValueError) as exc:
        raise AtomicIOError("record is not JSON-serializable") from exc
    if trailing_newline:
        text += "\n"
    atomic_write_bytes(
        path,
        text.encode("utf-8"),
        mode=mode,
        dir_mode=dir_mode,
        max_bytes=max_bytes,
        fsync_dir=fsync_dir,
    )


def read_json(path: str | Path, *, default: Any = _REQUIRED, max_bytes: int = MAX_RECORD_BYTES) -> Any:
    """Read and parse the JSON record at ``path``.

    The size is checked with `os.stat` before any content is read, so an
    oversize file is never loaded into memory. A missing file, an oversize
    file, any other `OSError`, or invalid JSON returns ``default`` when one
    was given, and raises `AtomicIOError` otherwise.
    """

    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        if default is not _REQUIRED:
            return default
        raise AtomicIOError("record is unreadable") from exc
    if size > max_bytes:
        if default is not _REQUIRED:
            return default
        raise AtomicIOError("record exceeds max_bytes")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        if default is not _REQUIRED:
            return default
        raise AtomicIOError("record is unreadable") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        if default is not _REQUIRED:
            return default
        raise AtomicIOError("record is not valid JSON") from exc


def migration_backup(path: str | Path, *, suffix: str = ".migrated", max_bytes: int = MAX_RECORD_BYTES) -> Path | None:
    """Durably copy ``path`` aside to ``path`` + ``suffix``; return the backup path.

    Returns ``None`` when ``path`` does not exist (or is unreadable). An
    existing backup is only overwritten after the source has been read
    successfully. Stores with a larger explicit record budget must pass that
    budget here too; the generic default is otherwise retained.
    """

    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    backup = path.with_name(path.name + suffix)
    atomic_write_bytes(backup, data, max_bytes=max_bytes)
    return backup


def _fsync_dir(parent: Path) -> None:
    """Fsync ``parent`` so a rename inside it is durable; a no-op if we can't."""

    try:
        fd = os.open(str(parent), os.O_RDONLY)
    except OSError as exc:
        _logger.debug("directory fsync unavailable: %s", type(exc).__name__)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        _logger.debug("directory fsync failed: %s", type(exc).__name__)
    finally:
        os.close(fd)


__all__ = [
    "AtomicIOError",
    "DIR_MODE",
    "FILE_MODE",
    "MAX_RECORD_BYTES",
    "atomic_write_bytes",
    "atomic_write_json",
    "migration_backup",
    "read_json",
]
