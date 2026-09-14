# Issue #39 — sandboxed document extraction helper (work plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: in progress (system track). Tracks
[#39](https://github.com/yeogirlyun/rynmesh/issues/39).

**Goal:** Parse untrusted user documents in a child process with size, time, and
memory limits, so a decompression bomb or a parser crash kills the child instead
of the node, and return bounded plain text plus a stable status code.

**Architecture:** A plain library module (`rynmesh/services/document_extract.py`)
with no HTTP surface, following `rynmesh/services/reader.py` — the direct
precedent, a bytes-to-text extractor that owns no routes. The parent validates
and supervises; a separate child module (`document_extract_child.py`) applies its
own resource limits as its first act and does the parsing. Consumers (My Content
Slice 4, Ask Ryn file import) are unbuilt, so no route package is added yet;
`scripts/new_route_package.py` can scaffold one when an endpoint is actually needed.

**Tech Stack:** stdlib (`subprocess`, `resource`, `mimetypes`, `json`) plus an
optional `pypdf` behind a new `documents` extra.

**Spec:** [issue #39](https://github.com/yeogirlyun/rynmesh/issues/39);
evidence requirements in [`TESTING_STRATEGY.md`](TESTING_STRATEGY.md) §2 and §3.

## Global Constraints

- Python floor `>=3.10` (`pyproject.toml`); CI backend job runs 3.12 on
  ubuntu-latest only. **pytest never runs on macOS or Windows in CI.**
- Ruff `select = ["E","F","W","I","N","B","C4"]`, `ignore = ["E501","N818","N815"]`,
  `line-length = 100`, `known-first-party = ["rynmesh"]`.
- **No filesystem path, document content, or exception message may appear in a
  returned value, a raised message, or anything the node logs.** Every failure
  code is a fixed literal; `"/" not in code and "\\" not in code` holds by
  construction.
- Module size ceiling 10K lines (`CONTRIBUTING.md:201`). Both modules here are
  a few hundred lines; keep them that way.
- Never pass the document path in `argv` — it is visible to other local users via
  `ps`. It travels on the child's stdin.
- The child's stderr is discarded, never captured: parsers print document
  fragments in warnings.

## As delivered: where the implementation diverges from this plan

This document is the pre-execution snapshot written before Task 1 started;
the final review wave (#39) changed several things the embedded code samples
below still show the old way. The samples are left as they were written —
the modules themselves (`rynmesh/services/document_extract.py`,
`rynmesh/services/document_extract_child.py`) are the source of truth, in the
same spirit `docs/ROUTE_PACKAGES.md` uses for `install_llm_routes`: described
as it is, not as the plan said it should be. Known divergences:

- **Stdin framing (:267, :442):** the child no longer reads one UTF-8 path
  line via `sys.stdin.readline().strip()`. It reads the whole of stdin with
  `sys.stdin.buffer.read()` and decodes it with `os.fsdecode`; the parent
  writes it with `os.fsencode`. Newlines and edge whitespace are legal in
  POSIX filenames, and `readline().strip()` let the parent validate one file
  while the child opened another.
- **The rlimit-probe assertion (:350-357):** `assert 0 < int(probe.stdout.strip())`
  is satisfied by `resource.RLIM_INFINITY` (a very large sentinel int), so it
  passed even when the limit was never actually applied — the false-green
  that hid the macOS bug where `RLIMIT_AS` is refused outright.
- **`apply_limits` (:400-422):** the loop that set each limit and silently
  `continue`d past a refused `setrlimit` is gone. `apply_limits` now reads
  each limit back with `getrlimit` after setting it and only trusts a limit
  that reads back as applied; whether the address-space ceiling actually took
  is exposed via `memory_limit_active()`.
- **Platform coverage (:1022):** the plan names Windows as the only platform
  missing the resource limits. macOS is also affected: Darwin honours
  neither `RLIMIT_AS` nor `RLIMIT_DATA`, so there is no address-space
  ceiling there either. `RLIMIT_CPU`, `RLIMIT_FSIZE`, and `RLIMIT_NOFILE` do
  still apply on Darwin.
- **Input-size enforcement (Task 2's `_read_text`):** the child no longer
  trusts the parent's pre-spawn `stat()` for the size cap. The parent's stat
  is a courtesy that avoids spawning a process for an already-oversized file,
  not a bound — a file can grow between that check and the child's open. The
  child now enforces `max_input_bytes` itself with a bounded
  `read(limit + 1)`, closing that TOCTOU gap.
- **`failed:memory` reachability (Task 2/3's memory-bomb tests):** the plan's
  own integration test tolerated either `failed:crashed` or `failed:memory`
  for a real memory bomb, because a `MemoryError` handler that itself has to
  allocate a JSON payload can fail under the pressure that triggered it. The
  delivered child pre-serializes each kind's `failed:memory` payload at
  import time, while the process is still healthy, so the handler only
  writes bytes it already has. `failed:memory` is now deterministically
  reachable (see `test_child_reports_memory_when_the_extractor_runs_out` and
  `test_extract_document_surfaces_failed_memory_from_the_child` in
  `tests/test_document_extract.py`); the real-bomb integration test still
  allows either outcome, since a live OS-level bomb's exact failure mode
  still depends on host timing.

## Result contract

```python
{
    "status": "parsed" | "truncated" | "unsupported" | "failed:<code>",
    "kind": "text" | "markdown" | "pdf" | "unknown",
    "text": str,     # "" for every status except parsed and truncated
    "chars": int,    # len(text)
}
```

Closed failure-code set — nothing else may ever be returned:

| code | meaning |
|---|---|
| `failed:too_large` | input exceeds `max_input_bytes` (checked before any spawn) |
| `failed:timeout` | wall-clock deadline hit; child was terminated |
| `failed:memory` | child hit `RLIMIT_AS` and reported `MemoryError` (only where the ceiling exists — see the acceptance doc's non-goals; macOS has none, so there a bomb reaches the wall-clock deadline or the input cap instead) |
| `failed:crashed` | child died on a signal or exited non-zero |
| `failed:unreadable` | the path does not exist, is not a regular file, or cannot be opened |
| `failed:not_text` | a text/markdown file that is not valid UTF-8 |
| `failed:internal` | child produced no parseable result (should not happen) |

`unsupported` covers both an unknown kind and a PDF when `pypdf` is absent; the
`kind` field is how a caller tells those apart.

## File Structure

- Create `rynmesh/services/document_extract.py` — constants, kind
  classification, the parent supervisor `extract_document(...)`. Public API.
- Create `rynmesh/services/document_extract_child.py` — `python -m` entry point.
  Applies rlimits, reads the path from stdin, parses, writes one JSON object to
  stdout. Imported by nobody at runtime.
- Create `tests/test_document_extract.py` — drives real child processes, per
  `TESTING_STRATEGY.md` §2 ("drive the real thing").
- Modify `pyproject.toml` — add the `documents` optional extra.
- Create `docs/acceptance/document-extraction/README.md` — numbered manual steps.

---

### Task 1: Codes, limits, and kind classification

**Files:**
- Create: `rynmesh/services/document_extract.py`
- Test: `tests/test_document_extract.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `classify(path: Path) -> str` returning one of `"text"`,
  `"markdown"`, `"pdf"`, `"unknown"`; the module constants `MAX_INPUT_BYTES`,
  `MAX_OUTPUT_CHARS`, `TIMEOUT_S`, `MEMORY_BYTES`; and every `FAILED_*` literal.
  Task 2 and Task 3 both import these.

- [ ] **Step 1: Write the failing test**

```python
"""The extraction helper: classification, limits, and the code contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from rynmesh.services import document_extract as de


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("notes.txt", "text"),
        ("notes.TXT", "text"),
        ("readme.md", "markdown"),
        ("readme.markdown", "markdown"),
        ("paper.pdf", "pdf"),
        ("archive.zip", "unknown"),
        ("noextension", "unknown"),
    ],
)
def test_classify_maps_suffix_to_kind(tmp_path: Path, name: str, expected: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"x")
    assert de.classify(path) == expected


def test_every_failure_code_is_path_free_and_prefixed() -> None:
    codes = de.FAILURE_CODES
    assert codes, "the closed failure set must not be empty"
    for code in codes:
        assert code.startswith("failed:")
        assert "/" not in code and "\\" not in code
        assert code == code.strip()


def test_limits_have_sane_defaults() -> None:
    assert de.MAX_INPUT_BYTES > 0
    assert de.MAX_OUTPUT_CHARS > 0
    assert de.TIMEOUT_S > 0
    assert de.MEMORY_BYTES > de.MAX_OUTPUT_CHARS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_document_extract.py -q`
Expected: FAIL — `ModuleNotFoundError: rynmesh.services.document_extract`

- [ ] **Step 3: Write minimal implementation**

```python
"""Bounded plain-text extraction from untrusted local documents.

Parsing a user's document is an attack surface: a decompression bomb, a
malformed cross-reference table, or a parser bug can exhaust memory or crash
the interpreter. Nothing here parses in the node process. `extract_document`
validates and supervises; the real work happens in
`rynmesh.services.document_extract_child`, a short-lived child that applies its
own address-space and CPU limits before it opens anything.

Privacy rule for everything in both modules: no filesystem path, no byte of
document content, and no third-party exception message may appear in a returned
value, in a raised message, or in anything the node logs. Every failure is one
of a closed set of fixed literals.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

__all__ = [
    "FAILURE_CODES",
    "MAX_INPUT_BYTES",
    "MAX_OUTPUT_CHARS",
    "MEMORY_BYTES",
    "TIMEOUT_S",
    "classify",
]

# A 32 MiB ceiling on what is worth opening at all. The parent stats the file
# and refuses before spawning, so an oversized input costs no process.
MAX_INPUT_BYTES = int(os.environ.get("RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES", 32 * 1024 * 1024))
# Matches RYNMESH_LOCAL_BODY_MAX_BYTES, the cap the local content-body route
# already applies, so a caller that shows both sees one consistent bound.
MAX_OUTPUT_CHARS = int(os.environ.get("RYNMESH_DOC_EXTRACT_MAX_OUTPUT_CHARS", 1024 * 1024))
TIMEOUT_S = float(os.environ.get("RYNMESH_DOC_EXTRACT_TIMEOUT_S", "20"))
MEMORY_BYTES = int(os.environ.get("RYNMESH_DOC_EXTRACT_MEMORY_BYTES", 512 * 1024 * 1024))

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_document_extract.py -q && python -m ruff check rynmesh/services/document_extract.py tests/test_document_extract.py`
Expected: PASS, `All checks passed`

- [ ] **Step 5: Commit**

```bash
git add rynmesh/services/document_extract.py tests/test_document_extract.py
git commit -m "feat(doc-extract): kind classification, limits, and the closed failure-code set (#39)"
```

---

### Task 2: The child process

**Files:**
- Create: `rynmesh/services/document_extract_child.py`
- Test: `tests/test_document_extract.py` (append)

**Interfaces:**
- Consumes: every constant from Task 1.
- Produces: a `python -m rynmesh.services.document_extract_child` entry point.
  Reads one UTF-8 path line from stdin; writes exactly one JSON object
  (`{"status", "kind", "text", "chars"}`) to stdout; exit code 0 on any
  determinate answer including `unsupported`, 1 only when it could not produce
  one. Task 3 spawns it.

- [ ] **Step 1: Write the failing test**

```python
import json
import os
import subprocess
import sys

CHILD = [sys.executable, "-m", "rynmesh.services.document_extract_child"]

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="resource limits are POSIX-only"
)


def _run_child(path: Path, timeout: float = 30.0) -> dict:
    done = subprocess.run(
        CHILD,
        input=f"{path}\n".encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    return json.loads(done.stdout.decode("utf-8"))


def test_child_extracts_plain_text(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello document\n", encoding="utf-8")
    result = _run_child(path)
    assert result["status"] == "parsed"
    assert result["kind"] == "text"
    assert result["text"] == "hello document\n"
    assert result["chars"] == len(result["text"])


def test_child_truncates_beyond_the_output_cap(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "big.txt"
    path.write_text("a" * 5000, encoding="utf-8")
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_MAX_OUTPUT_CHARS", "1000")
    result = _run_child(path)
    assert result["status"] == "truncated"
    assert result["chars"] == 1000


def test_child_reports_unsupported_for_unknown_kinds(tmp_path: Path) -> None:
    path = tmp_path / "archive.zip"
    path.write_bytes(b"PK\x03\x04rest")
    result = _run_child(path)
    assert result["status"] == "unsupported"
    assert result["kind"] == "unknown"
    assert result["text"] == ""


def test_child_reports_not_text_for_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "broken.txt"
    path.write_bytes(b"\xff\xfe\x00binary")
    result = _run_child(path)
    assert result["status"] == "failed:not_text"


def test_child_reports_unreadable_for_a_missing_file(tmp_path: Path) -> None:
    result = _run_child(tmp_path / "absent.txt")
    assert result["status"] == "failed:unreadable"


@posix_only
def test_child_applies_an_address_space_limit(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import resource,sys;"
            "sys.path.insert(0,'.');"
            "from rynmesh.services import document_extract_child as c;"
            "c.apply_limits();"
            "print(resource.getrlimit(resource.RLIMIT_AS)[0])",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    assert 0 < int(probe.stdout.strip())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_document_extract.py -q`
Expected: FAIL — `No module named rynmesh.services.document_extract_child`

- [ ] **Step 3: Write minimal implementation**

```python
"""The child half of `rynmesh.services.document_extract`. Never imported.

Run as `python -m rynmesh.services.document_extract_child`, one document per
process. `apply_limits` runs before anything is opened, so a decompression bomb
meets the address-space ceiling rather than the host's memory.

The path arrives on stdin, never in `argv`: another local user can read a
process's arguments out of `ps`, and the name of a document a user imported is
itself private. Exactly one JSON object goes to stdout and nothing else; stderr
is the parent's problem and is discarded there, because document parsers print
fragments of document content in their warnings.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rynmesh.services.document_extract import (
    FAILED_NOT_TEXT,
    FAILED_UNREADABLE,
    MAX_OUTPUT_CHARS,
    MEMORY_BYTES,
    STATUS_PARSED,
    STATUS_TRUNCATED,
    STATUS_UNSUPPORTED,
    classify,
)


def apply_limits() -> None:
    """Cap address space and CPU for this process. POSIX only; a no-op on Windows.

    Self-applied rather than passed through `preexec_fn`, which runs between
    fork and exec and is unsafe in a threaded parent — and the node is threaded.
    """

    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return
    for limit, value in (
        (resource.RLIMIT_AS, MEMORY_BYTES),
        (resource.RLIMIT_CPU, 30),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_NOFILE, 64),
    ):
        try:
            soft, hard = resource.getrlimit(limit)
            ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(limit, (ceiling, hard))
        except (OSError, ValueError):  # pragma: no cover - platform dependent
            # A limit the host refuses is not fatal: the parent's wall-clock
            # deadline and output cap still bound this process.
            continue


def _emit(status: str, kind: str, text: str = "") -> None:
    payload = {"status": status, "kind": kind, "text": text, "chars": len(text)}
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


def _read_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return "", FAILED_NOT_TEXT


def main() -> int:
    apply_limits()
    raw = sys.stdin.readline().strip()
    if not raw:
        _emit(FAILED_UNREADABLE, "unknown")
        return 0
    path = Path(raw)
    kind = classify(path)
    try:
        if not path.is_file():
            _emit(FAILED_UNREADABLE, kind)
            return 0
    except OSError:
        _emit(FAILED_UNREADABLE, kind)
        return 0

    if kind in {"text", "markdown"}:
        try:
            text, failure = _read_text(path)
        except OSError:
            _emit(FAILED_UNREADABLE, kind)
            return 0
        if failure:
            _emit(failure, kind)
            return 0
    else:
        # PDF arrives in Task 4; every other kind has no extractor at all.
        _emit(STATUS_UNSUPPORTED, kind)
        return 0

    if len(text) > MAX_OUTPUT_CHARS:
        _emit(STATUS_TRUNCATED, kind, text[:MAX_OUTPUT_CHARS])
        return 0
    _emit(STATUS_PARSED, kind, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_document_extract.py -q && python -m ruff check rynmesh/services/`
Expected: PASS, `All checks passed`

- [ ] **Step 5: Commit**

```bash
git add rynmesh/services/document_extract_child.py tests/test_document_extract.py
git commit -m "feat(doc-extract): self-limiting child process for text and markdown (#39)"
```

---

### Task 3: The parent supervisor

**Files:**
- Modify: `rynmesh/services/document_extract.py`
- Test: `tests/test_document_extract.py` (append)

**Interfaces:**
- Consumes: Task 1's constants, Task 2's child entry point.
- Produces:
  ```python
  def extract_document(
      path: str | Path,
      *,
      max_input_bytes: int = MAX_INPUT_BYTES,
      max_output_chars: int = MAX_OUTPUT_CHARS,
      timeout_s: float = TIMEOUT_S,
      memory_bytes: int = MEMORY_BYTES,
      spawn: Callable[..., subprocess.Popen] | None = None,
  ) -> dict[str, Any]:
  ```
  `spawn` is injectable so a test can drive a fake child, mirroring
  `rynmesh/services/updater.py`, which takes its process-spawning callables as
  parameters so tests never fork.

- [ ] **Step 1: Write the failing test**

```python
def test_extract_document_reads_a_real_file(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("# heading\n\nbody\n", encoding="utf-8")
    result = de.extract_document(path)
    assert result["status"] == "parsed"
    assert result["kind"] == "markdown"
    assert "heading" in result["text"]


def test_extract_document_refuses_an_oversized_file_without_spawning(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_text("a" * 4096, encoding="utf-8")
    spawned: list[object] = []

    def _never(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("must not spawn for an oversized input")

    result = de.extract_document(path, max_input_bytes=10, spawn=_never)
    assert result["status"] == de.FAILED_TOO_LARGE
    assert result["text"] == ""
    assert spawned == []


def test_extract_document_times_out_and_kills_the_child(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")

    def _sleeper(*args, **kwargs):
        kwargs.pop("args", None)
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    result = de.extract_document(path, timeout_s=0.5, spawn=_sleeper)
    assert result["status"] == de.FAILED_TIMEOUT


def test_extract_document_reports_a_crashed_child(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")

    def _crasher(*args, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "import os; os._exit(3)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    result = de.extract_document(path, spawn=_crasher)
    assert result["status"] == de.FAILED_CRASHED


def test_extract_document_reports_internal_for_unparseable_output(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")

    def _garbage(*args, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "print('not json')"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    result = de.extract_document(path, spawn=_garbage)
    assert result["status"] == de.FAILED_INTERNAL


@posix_only
def test_a_memory_bomb_is_contained(tmp_path: Path) -> None:
    """The child dies; the node keeps running and gets a code back."""
    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")

    def _bomb(*args, **kwargs):
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import resource;"
                "resource.setrlimit(resource.RLIMIT_AS,(64*1024*1024,)*2);"
                "b=bytearray();\n"
                "while True: b.extend(b'x'*(1024*1024))",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    result = de.extract_document(path, timeout_s=30, spawn=_bomb)
    assert result["status"] in {de.FAILED_CRASHED, de.FAILED_MEMORY}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_document_extract.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'extract_document'`

- [ ] **Step 3: Write minimal implementation**

Append to `rynmesh/services/document_extract.py` (and add `extract_document` to
`__all__`, plus `import json`, `import subprocess`, `import sys`, `import time`,
`from typing import Any, Callable`):

```python
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
        stat = target.stat()
    except OSError:
        return _failure(FAILED_UNREADABLE, kind)
    if not target.is_file():
        return _failure(FAILED_UNREADABLE, kind)
    if stat.st_size > max_input_bytes:
        return _failure(FAILED_TOO_LARGE, kind)

    child_env = dict(os.environ)
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
            raw, _ = process.communicate(
                input=f"{target}\n".encode("utf-8"), timeout=timeout_s
            )
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
    text = parsed.get("text") or ""
    if not isinstance(text, str) or len(text) > max_output_chars:
        return _failure(FAILED_INTERNAL, kind)
    return {"status": status, "kind": kind, "text": text, "chars": len(text)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_document_extract.py -q && python -m ruff check rynmesh/services/ tests/test_document_extract.py`
Expected: PASS, `All checks passed`

- [ ] **Step 5: Commit**

```bash
git add rynmesh/services/document_extract.py tests/test_document_extract.py
git commit -m "feat(doc-extract): supervise the child with a deadline, kill escalation, and bounded output (#39)"
```

---

### Task 4: PDF support behind an optional extra

**Files:**
- Modify: `pyproject.toml`
- Modify: `rynmesh/services/document_extract_child.py`
- Test: `tests/test_document_extract.py` (append)

**Interfaces:**
- Consumes: Task 2's `main()` dispatch.
- Produces: `pdf_available() -> bool` in `document_extract_child`, following
  `rynmesh/services/recap.py`'s `pdf_available()` optional-dependency probe.
  A PDF with `pypdf` absent returns `unsupported` with `kind == "pdf"`.

- [ ] **Step 1: Write the failing test**

```python
from rynmesh.services import document_extract_child as child

pdf_only = pytest.mark.skipif(
    not child.pdf_available(), reason="pypdf is not installed (the documents extra)"
)


def _minimal_pdf(text: str) -> bytes:
    from pypdf import PdfWriter

    import io

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pdf_only
def test_a_valid_pdf_parses(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(_minimal_pdf("hello"))
    result = de.extract_document(path)
    assert result["status"] in {"parsed", "truncated"}
    assert result["kind"] == "pdf"


def test_a_corrupt_pdf_fails_without_taking_the_node_down(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\nnot really a pdf at all\n%%EOF\n")
    result = de.extract_document(path)
    assert result["kind"] == "pdf"
    assert result["status"] in {"unsupported", de.FAILED_UNREADABLE, de.FAILED_CRASHED}
    # The node is still here to make the next call.
    assert de.extract_document(path)["kind"] == "pdf"


def test_pdf_is_unsupported_when_the_parser_is_absent(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.7\n%%EOF\n")
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_DISABLE_PDF", "1")
    result = de.extract_document(path)
    assert result["status"] == "unsupported"
    assert result["kind"] == "pdf"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_document_extract.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'pdf_available'`

- [ ] **Step 3: Write minimal implementation**

In `pyproject.toml`, beside the existing `recap` extra:

```toml
# Text extraction from user PDFs, used by the sandboxed document helper.
# Optional: without it a PDF import reports `unsupported` rather than failing,
# and plain text and Markdown are unaffected. pypdf is pure Python (BSD-3),
# so it adds no build step to the desktop sidecar.
documents = ["pypdf>=5.0.0"]
```

Add `documents` to the `dev` extra's install line in CI so the PDF tests run
rather than skip:

```yaml
      - name: Install
        run: python -m pip install -e ".[dev,documents]"
```

In `document_extract_child.py`:

```python
def pdf_available() -> bool:
    """True when a PDF extractor is installed and not disabled."""

    if os.environ.get("RYNMESH_DOC_EXTRACT_DISABLE_PDF") == "1":
        return False
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def _read_pdf(path: Path, limit: int) -> tuple[str, str]:
    """Bounded text from a PDF. Returns (text, failure_code)."""

    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        # A page's own extractor is the untrusted part; a failure on one page
        # must not discard the pages already read.
        try:
            piece = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - third-party detail must not leak
            continue
        parts.append(piece)
        total += len(piece)
        if total > limit:
            break
    return "\n".join(parts), ""
```

and in `main()`, replace the `else` branch:

```python
    elif kind == "pdf" and pdf_available():
        try:
            text, failure = _read_pdf(path, MAX_OUTPUT_CHARS)
        except OSError:
            _emit(FAILED_UNREADABLE, kind)
            return 0
        except Exception:  # noqa: BLE001 - a malformed document is not a crash
            _emit(FAILED_UNREADABLE, kind)
            return 0
        if failure:
            _emit(failure, kind)
            return 0
    else:
        _emit(STATUS_UNSUPPORTED, kind)
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pip install -e ".[dev,documents]" && python -m pytest tests/test_document_extract.py -q && python -m ruff check rynmesh/services/`
Expected: PASS, `All checks passed`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .github/workflows/ci.yml rynmesh/services/document_extract_child.py tests/test_document_extract.py
git commit -m "feat(doc-extract): optional pypdf extractor, degrading to unsupported when absent (#39)"
```

---

### Task 5: Privacy proof, cold start, and acceptance evidence

**Files:**
- Test: `tests/test_document_extract.py` (append)
- Create: `docs/acceptance/document-extraction/README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: no new API. This task exists because `TESTING_STRATEGY.md` §2 makes
  the marker-string test mandatory for anything carrying a body, and §3 requires
  a cold-start case, an acceptance script, and a stated non-goal.

- [ ] **Step 1: Write the failing test**

```python
import json as _json
import logging


def test_no_path_or_content_reaches_the_result_or_logs(tmp_path, caplog) -> None:
    """The one defect class that using the product cannot reveal."""
    marker_path = "SECRET_PATH_MARKER"
    marker_content = "SECRET_CONTENT_MARKER"
    directory = tmp_path / marker_path
    directory.mkdir()

    cases = [
        (directory / f"{marker_path}.zip", marker_content.encode("utf-8")),
        (directory / f"{marker_path}.txt", b"\xff\xfe" + marker_content.encode("utf-8")),
        (directory / f"{marker_path}.pdf", b"%PDF-1.7 " + marker_content.encode("utf-8")),
        (directory / f"{marker_path}.absent.txt", None),
    ]
    with caplog.at_level(logging.DEBUG):
        for path, data in cases:
            if data is not None:
                path.write_bytes(data)
            result = de.extract_document(path)
            blob = _json.dumps(result)
            assert marker_path not in blob
            assert marker_content not in blob
            assert str(path) not in blob
            if result["status"] in de.FAILURE_CODES:
                assert "/" not in result["status"] and "\\" not in result["status"]

    logged = "\n".join(record.message for record in caplog.records)
    assert marker_path not in logged
    assert marker_content not in logged


def test_oversized_result_never_exceeds_the_caller_s_cap(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_text("b" * 20_000, encoding="utf-8")
    result = de.extract_document(path, max_output_chars=500)
    assert result["status"] == "truncated"
    assert result["chars"] <= 500


def test_works_on_a_cold_home_with_no_prior_state(tmp_path, monkeypatch) -> None:
    """No node home, no store, no config: the helper owns no state at all."""
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "empty-home"))
    path = tmp_path / "first.md"
    path.write_text("# cold\n", encoding="utf-8")
    assert de.extract_document(path)["status"] == "parsed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_document_extract.py -q`
Expected: the marker test FAILS if any code path interpolates a path; PASS
otherwise. If it passes immediately, verify it is real by temporarily making
`_failure` return `{"status": f"failed:{target}"}` and confirming it goes red.

- [ ] **Step 3: Write the acceptance script**

Create `docs/acceptance/document-extraction/README.md`:

```markdown
# Acceptance: sandboxed document extraction (#39)

Run on a real machine with the node installed. Record the elapsed time and the
returned `status` for each step. **Record no filename, no absolute path, and no
line of document content** — a status code and a duration are the whole result.

Prepare four files in a scratch directory: a 2-page Markdown note, a plain text
file of about 5 MB, a normal PDF, and a PDF truncated halfway through with a
hex editor.

1. Extract the Markdown note. Expect `parsed`; record the duration.
2. Extract the 5 MB text file with the default caps. Expect `truncated`;
   confirm the returned character count equals the configured cap exactly.
3. Extract the normal PDF. Expect `parsed` when the `documents` extra is
   installed, `unsupported` when it is not. Record which.
4. Extract the truncated PDF. Expect `unsupported` or `failed:unreadable`, and
   confirm the node still answers `/health` afterwards.
5. Re-run step 4 twenty times in a loop. Confirm the node's memory does not
   grow and no child process is left behind (`pgrep -f document_extract_child`
   returns nothing).
6. Point the helper at a path that does not exist. Expect `failed:unreadable`.
7. Point it at a directory rather than a file. Expect `failed:unreadable`.
8. Grep the node log for the scratch directory's name. Expect no match.

## Non-goals for this version

- No DOCX, ODT, RTF, EPUB, or spreadsheet extraction — those return
  `unsupported` by design.
- No OCR: a scanned PDF with no text layer yields empty text, not an error.
- No encoding detection. A text file that is not valid UTF-8 is
  `failed:not_text`; the node does not guess at code pages.
- No caching. Every call re-extracts; `ReaderCache` is the model to copy if a
  consumer later needs one.
- On Windows the address-space and CPU limits are absent (`resource` is POSIX
  only). The wall-clock deadline and the output cap still apply, so a bomb is
  bounded in time and output but not in memory. Named here rather than hidden.
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q && python -m ruff check rynmesh/ tests/`
Expected: PASS with no new failures. Name any pre-existing failure explicitly in
the PR body rather than letting it hide behind the new tests
(`TESTING_STRATEGY.md` §6).

- [ ] **Step 5: Commit**

```bash
git add tests/test_document_extract.py docs/acceptance/document-extraction/README.md
git commit -m "test(doc-extract): marker-string privacy proof, cold start, and the acceptance script (#39)"
```

---

## Self-review against the issue

| Issue requirement | Task |
|---|---|
| Subprocess-isolated extraction helper | 2, 3 |
| Size limit | 1 (`MAX_INPUT_BYTES`, pre-spawn), 3 |
| Time limit | 3 (deadline + kill escalation) |
| Memory limit | 2 (`RLIMIT_AS`, self-applied) |
| Bounded plain text out | 2 (`MAX_OUTPUT_CHARS`), 3 (re-checked parent-side) |
| Status `parsed` / `truncated` / `unsupported` / `failed:<code>` | 1 (closed set), 2, 3 |
| Never logs file paths or content | 5 (marker test), enforced by construction in 1 |
| Owner filenames stay out of manifests and logs | 5; path travels on stdin, never argv |

Not in scope, deliberately: any HTTP route (no consumer exists yet — see
`docs/ROUTE_PACKAGES.md`), and any change to My Content or Ask Ryn.
