"""The child half of `rynmesh.services.document_extract`. Never imported.

Run as `python -m rynmesh.services.document_extract_child`, one document per
process. `apply_limits` runs before anything is opened, but the memory ceiling
it asks for is not available everywhere: Linux honours `RLIMIT_AS`, so there a
decompression bomb meets that ceiling rather than the host's memory, while
macOS honours neither `RLIMIT_AS` nor `RLIMIT_DATA` and the process runs with
no address-space bound at all. `memory_limit_active` reports which of the two
this process got, and the docs name the degraded case rather than hiding it.
Because that ceiling can be absent, this module enforces the input-size cap on
its own bounded read instead of trusting the parent's pre-spawn `stat`.

The path arrives on stdin, never in `argv`: another local user can read a
process's arguments out of `ps`, and the name of a document a user imported is
itself private. It is the whole of stdin with no delimiter and no trimming --
newlines and edge whitespace are legal in POSIX filenames, so any framing that
splits or strips would let the parent validate one file and the child open
another. Exactly one JSON object goes to stdout and nothing else; stderr is the
parent's problem and is discarded there, because document parsers print
fragments of document content in their warnings.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

from rynmesh.services.document_extract import (
    FAILED_MEMORY,
    FAILED_NOT_TEXT,
    FAILED_TOO_LARGE,
    FAILED_UNREADABLE,
    MAX_INPUT_BYTES,
    MAX_OUTPUT_CHARS,
    MEMORY_BYTES,
    STATUS_PARSED,
    STATUS_TRUNCATED,
    STATUS_UNSUPPORTED,
    classify,
)

_KINDS = ("text", "markdown", "pdf", "unknown")

# Built at import time, while the process is still healthy: the MemoryError
# handler must not have to allocate a payload under memory pressure.
_MEMORY_PAYLOADS = {
    kind: json.dumps({"status": FAILED_MEMORY, "kind": kind, "text": "", "chars": 0})
    for kind in _KINDS
}

_memory_limit_active = False


def memory_limit_active() -> bool:
    """Whether this process actually carries an address-space ceiling.

    False until `apply_limits` has run, and False afterwards on any host that
    refuses `RLIMIT_AS` -- macOS refuses it, and refuses `RLIMIT_DATA` too. The
    parent's protocol never carries this; it exists so the platform contract is
    assertable rather than assumed.
    """

    return _memory_limit_active


def _set_limit(resource_module, limit: int, value: int) -> bool:
    """Lower ``limit`` to ``value`` and report whether it actually took.

    A `setrlimit` that does not raise is not proof that the ceiling exists, so
    the limit is read back and compared. Returns False when the host refuses
    the limit in either way.
    """

    try:
        _soft, hard = resource_module.getrlimit(limit)
    except (OSError, ValueError):  # pragma: no cover - platform dependent
        return False
    ceiling = value if hard == resource_module.RLIM_INFINITY else min(value, hard)
    try:
        resource_module.setrlimit(limit, (ceiling, hard))
    except (OSError, ValueError):
        return False
    try:
        applied, _hard = resource_module.getrlimit(limit)
    except (OSError, ValueError):  # pragma: no cover - platform dependent
        return False
    return applied == ceiling


def apply_limits() -> None:
    """Cap address space and CPU for this process. POSIX only; a no-op on Windows.

    Self-applied rather than passed through `preexec_fn`, which runs between
    fork and exec and is unsafe in a threaded parent -- and the node is
    threaded. Each limit is verified by read-back; `memory_limit_active` then
    reports whether the address-space ceiling is real on this host.
    """

    global _memory_limit_active

    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        _memory_limit_active = False
        return
    # A refused limit is not fatal, but it is also not silently equivalent: the
    # wall-clock deadline and the output cap bound time and output, never
    # memory. The only memory bound left when this returns False is the
    # input-size cap enforced in `_read_bounded`.
    _memory_limit_active = _set_limit(resource, resource.RLIMIT_AS, MEMORY_BYTES)
    for limit, value in (
        (resource.RLIMIT_CPU, 30),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_NOFILE, 64),
    ):
        _set_limit(resource, limit, value)


def _write(payload: str) -> None:
    sys.stdout.write(payload)
    sys.stdout.flush()


def _emit(status: str, kind: str, text: str = "") -> None:
    _write(json.dumps({"status": status, "kind": kind, "text": text, "chars": len(text)}))


def _read_bounded(path: Path, limit: int) -> tuple[bytes, bool]:
    """At most ``limit`` bytes of ``path``, plus whether the file overran it.

    The parent's pre-spawn `stat` is a courtesy that saves a process; it is not
    a bound, because the file can grow between that check and this read. This
    read is the bound.
    """

    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        return b"", True
    return data, False


def pdf_available() -> bool:
    """True when a PDF extractor is installed and not disabled."""

    if os.environ.get("RYNMESH_DOC_EXTRACT_DISABLE_PDF") == "1":
        return False
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def _read_pdf(data: bytes, limit: int) -> str:
    """Bounded text from the PDF bytes in ``data``. Raises on a malformed file."""

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        # A page's own extractor is the untrusted part; a failure on one page
        # must not discard the pages already read.
        try:
            piece = page.extract_text() or ""
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 - third-party detail must not leak
            continue
        parts.append(piece)
        total += len(piece)
        if total > limit:
            break
    return "\n".join(parts)


def _extract(path: Path, kind: str) -> tuple[str, str]:
    """Bounded text for ``path``. Returns (text, failure_code); one is always empty."""

    data, oversized = _read_bounded(path, MAX_INPUT_BYTES)
    if oversized:
        return "", FAILED_TOO_LARGE
    if kind in {"text", "markdown"}:
        try:
            return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"), ""
        except UnicodeDecodeError:
            return "", FAILED_NOT_TEXT
    try:
        return _read_pdf(data, MAX_OUTPUT_CHARS), ""
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - a malformed document is not a crash
        return "", FAILED_UNREADABLE


def main() -> int:
    apply_limits()
    raw = sys.stdin.buffer.read()
    if not raw:
        _emit(FAILED_UNREADABLE, "unknown")
        return 0
    path = Path(os.fsdecode(raw))
    kind = classify(path)
    try:
        if not path.is_file():
            _emit(FAILED_UNREADABLE, kind)
            return 0
    except OSError:
        _emit(FAILED_UNREADABLE, kind)
        return 0

    if kind not in {"text", "markdown"} and not (kind == "pdf" and pdf_available()):
        _emit(STATUS_UNSUPPORTED, kind)
        return 0

    try:
        text, failure = _extract(path, kind)
    except MemoryError:
        # Deliberately allocation-free beyond the write itself: the payload was
        # built at import time. Reachable where `memory_limit_active()` is True.
        _write(_MEMORY_PAYLOADS[kind])
        return 0
    except OSError:
        _emit(FAILED_UNREADABLE, kind)
        return 0
    if failure:
        _emit(failure, kind)
        return 0

    if len(text) > MAX_OUTPUT_CHARS:
        _emit(STATUS_TRUNCATED, kind, text[:MAX_OUTPUT_CHARS])
        return 0
    _emit(STATUS_PARSED, kind, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
