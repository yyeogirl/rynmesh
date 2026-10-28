"""Bounded plain-text extraction from untrusted local documents.

Parsing a user's document is an attack surface: a decompression bomb, a
malformed cross-reference table, or a parser bug can exhaust memory or crash
the interpreter. Nothing here parses in the node process. `extract_document`
validates and supervises; the real work happens in
`rynmesh.services.document_extract_child`, a short-lived child that applies its
own resource limits and its own input-size cap before it opens anything. Where
the host refuses an address-space ceiling -- macOS refuses both `RLIMIT_AS` and
`RLIMIT_DATA` -- the wall-clock deadline and that input cap are what remain;
see the acceptance doc's non-goals.

Privacy rule for everything in both modules: no filesystem path, no byte of
document content, and no third-party exception message may appear in a returned
value, in a raised message, or in anything the node logs. Every failure is one
of a closed set of fixed literals.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "FAILED_CRASHED",
    "FAILED_INTERNAL",
    "FAILED_MEMORY",
    "FAILED_NOT_TEXT",
    "FAILED_TIMEOUT",
    "FAILED_TOO_LARGE",
    "FAILED_UNREADABLE",
    "FAILURE_CODES",
    "MAX_INPUT_BYTES",
    "MAX_OUTPUT_CHARS",
    "MEMORY_BYTES",
    "STATUS_PARSED",
    "STATUS_TRUNCATED",
    "STATUS_UNSUPPORTED",
    "TIMEOUT_S",
    "classify",
    "extract_document",
]


def _env_int(name: str, default: int) -> int:
    """An override from the environment, or ``default`` if it is absent or unparseable.

    A malformed value must not stop the node from starting: the cap simply
    stays at its default.
    """
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """An override from the environment, or ``default`` if it is absent or unparseable.

    A malformed value must not stop the node from starting: the cap simply
    stays at its default.
    """
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# A 32 MiB ceiling on what is worth opening at all. The parent stats the file
# and refuses before spawning, so an oversized input costs no process; that
# check is a saving, not the bound -- the child re-enforces this cap on its own
# bounded read, because a file can grow between the stat and the open.
MAX_INPUT_BYTES = _env_int("RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES", 32 * 1024 * 1024)
# Matches RYNMESH_LOCAL_BODY_MAX_BYTES, the cap the local content-body route
# already applies, so a caller that shows both sees one consistent bound.
MAX_OUTPUT_CHARS = _env_int("RYNMESH_DOC_EXTRACT_MAX_OUTPUT_CHARS", 1024 * 1024)
TIMEOUT_S = _env_float("RYNMESH_DOC_EXTRACT_TIMEOUT_S", 20.0)
MEMORY_BYTES = _env_int("RYNMESH_DOC_EXTRACT_MEMORY_BYTES", 512 * 1024 * 1024)

FAILED_TOO_LARGE = "failed:too_large"
FAILED_TIMEOUT = "failed:timeout"
FAILED_MEMORY = "failed:memory"
FAILED_CRASHED = "failed:crashed"
FAILED_UNREADABLE = "failed:unreadable"
FAILED_NOT_TEXT = "failed:not_text"
FAILED_INTERNAL = "failed:internal"

FAILURE_CODES = frozenset(
    {
        FAILED_TOO_LARGE,
        FAILED_TIMEOUT,
        FAILED_MEMORY,
        FAILED_CRASHED,
        FAILED_UNREADABLE,
        FAILED_NOT_TEXT,
        FAILED_INTERNAL,
    }
)

STATUS_PARSED = "parsed"
STATUS_TRUNCATED = "truncated"
STATUS_UNSUPPORTED = "unsupported"

_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_TEXT_SUFFIXES = {".txt", ".text", ".log", ".json", ".yaml", ".yml", ".xml", ".csv"}
_PDF_SUFFIXES = {".pdf"}


def classify(path: str | Path) -> str:
    """The extractor family for ``path``, from its suffix and guessed type.

    Suffix first, media type second: `mimetypes` is configured from the host's
    own tables and varies between machines, so it decides only what the suffix
    list does not already cover.
    """

    suffix = Path(path).suffix.lower()
    if suffix in _MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    if suffix in _PDF_SUFFIXES:
        return "pdf"
    guessed, _encoding = mimetypes.guess_type(str(path))
    if guessed == "application/pdf":
        return "pdf"
    if guessed and guessed.startswith("text/"):
        return "text"
    return "unknown"


_CHILD_ARGV = [sys.executable, "-m", "rynmesh.services.document_extract_child"]
# A child that answers correctly writes well under a kilobyte per 1000 chars of
# text; this ceiling only exists so a compromised child cannot stream forever.
_MAX_CHILD_STDOUT_BYTES = 8 * 1024 * 1024
_KILL_GRACE_S = 2.0


def _failure(code: str, kind: str) -> dict[str, Any]:
    return {"status": code, "kind": kind, "text": "", "chars": 0}


def _stop(process: subprocess.Popen) -> None:
    """Terminate, then kill after a grace period. Never raises."""

    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    deadline = time.monotonic() + _KILL_GRACE_S
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_KILL_GRACE_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            pass


def extract_document(
    path: str | Path,
    *,
    max_input_bytes: int = MAX_INPUT_BYTES,
    max_output_chars: int = MAX_OUTPUT_CHARS,
    timeout_s: float = TIMEOUT_S,
    memory_bytes: int = MEMORY_BYTES,
    spawn: Callable[..., subprocess.Popen] | None = None,
) -> dict[str, Any]:
    """Extract bounded plain text from ``path`` in a child process.

    Returns the result contract described in this module's work plan. Never
    raises for a bad document: every failure is one of ``FAILURE_CODES``.
    """

    target = Path(path)
    kind = classify(target)
    try:
        # A hostile path -- e.g. one with an embedded NUL byte -- makes
        # `stat`/`is_file` raise `ValueError` rather than `OSError`; both are
        # a bad-path signal here, so both fall into the same failure, never
        # out of this function.
        stat = target.stat()
        is_file = target.is_file()
    except (OSError, ValueError):
        return _failure(FAILED_UNREADABLE, kind)
    if not is_file:
        return _failure(FAILED_UNREADABLE, kind)
    if stat.st_size > max_input_bytes:
        return _failure(FAILED_TOO_LARGE, kind)

    child_env = dict(os.environ)
    child_env["RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES"] = str(max_input_bytes)
    child_env["RYNMESH_DOC_EXTRACT_MAX_OUTPUT_CHARS"] = str(max_output_chars)
    child_env["RYNMESH_DOC_EXTRACT_MEMORY_BYTES"] = str(memory_bytes)
    launcher = spawn or subprocess.Popen
    try:
        process = launcher(
            _CHILD_ARGV,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env,
        )
    except OSError:
        return _failure(FAILED_INTERNAL, kind)

    try:
        try:
            # The whole of stdin is the path: no delimiter to inject and no
            # trimming, so the child cannot open a file other than the one
            # validated above. Newlines and edge whitespace are legal in POSIX
            # filenames.
            raw, _ = process.communicate(input=os.fsencode(target), timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return _failure(FAILED_TIMEOUT, kind)
        except (OSError, ValueError):
            return _failure(FAILED_INTERNAL, kind)
    finally:
        _stop(process)

    if process.returncode != 0:
        return _failure(FAILED_CRASHED, kind)
    if not raw or len(raw) > _MAX_CHILD_STDOUT_BYTES:
        return _failure(FAILED_INTERNAL, kind)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _failure(FAILED_INTERNAL, kind)
    if not isinstance(parsed, dict) or "status" not in parsed:
        return _failure(FAILED_INTERNAL, kind)
    status = parsed.get("status")
    known = {STATUS_PARSED, STATUS_TRUNCATED, STATUS_UNSUPPORTED} | set(FAILURE_CODES)
    if status not in known:
        return _failure(FAILED_INTERNAL, kind)
    # `.get(name) or ""` would coerce a falsy non-string (0, False, [], {})
    # into "" before the type guard below could ever reject it.
    text = parsed.get("text", "")
    if not isinstance(text, str) or len(text) > max_output_chars:
        return _failure(FAILED_INTERNAL, kind)
    return {"status": status, "kind": kind, "text": text, "chars": len(text)}
