"""The extraction helper: classification, limits, and the code contract."""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rynmesh.services import document_extract as de
from rynmesh.services import document_extract_child as child

CHILD = [sys.executable, "-m", "rynmesh.services.document_extract_child"]

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="resource limits are POSIX-only"
)


def _run_child(path: Path, timeout: float = 30.0, env: dict | None = None) -> dict:
    done = subprocess.run(
        CHILD,
        input=os.fsencode(path),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        env=env,
    )
    return json.loads(done.stdout.decode("utf-8"))


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


def test_env_override_well_formed_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """An integer override is honoured if well-formed."""
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES", "1234")
    try:
        importlib.reload(de)
        assert de.MAX_INPUT_BYTES == 1234
    finally:
        importlib.reload(de)


def test_env_override_well_formed_float(monkeypatch: pytest.MonkeyPatch) -> None:
    """A float override is honoured if well-formed."""
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_TIMEOUT_S", "42.5")
    try:
        importlib.reload(de)
        assert de.TIMEOUT_S == 42.5
    finally:
        importlib.reload(de)


def test_env_override_malformed_falls_back_to_default_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed integer override falls back to the default."""
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES", "not_a_number")
    try:
        importlib.reload(de)
        assert de.MAX_INPUT_BYTES == 32 * 1024 * 1024
    finally:
        importlib.reload(de)


def test_env_override_malformed_falls_back_to_default_float(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed float override falls back to the default."""
    monkeypatch.setenv("RYNMESH_DOC_EXTRACT_TIMEOUT_S", "not_a_float")
    try:
        importlib.reload(de)
        assert de.TIMEOUT_S == 20.0
    finally:
        importlib.reload(de)


def test_env_absent_yields_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent environment variable yields the default."""
    monkeypatch.delenv("RYNMESH_DOC_EXTRACT_MAX_OUTPUT_CHARS", raising=False)
    try:
        importlib.reload(de)
        assert de.MAX_OUTPUT_CHARS == 1024 * 1024
    finally:
        importlib.reload(de)


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


_LIMIT_PROBE = (
    "import json,resource,sys;"
    "sys.path.insert(0,'.');"
    "from rynmesh.services import document_extract_child as c;"
    "before=resource.getrlimit(resource.RLIMIT_AS);"
    "c.apply_limits();"
    "after=resource.getrlimit(resource.RLIMIT_AS);"
    "print(json.dumps({'hard_before': before[1], 'soft_after': after[0],"
    " 'active': c.memory_limit_active(), 'want': c.MEMORY_BYTES,"
    " 'infinity': resource.RLIM_INFINITY}))"
)


@posix_only
def test_child_applies_an_address_space_limit() -> None:
    """The ceiling is real where the host honours it and provably absent where it is not.

    An earlier version asserted only ``0 < soft``, which RLIM_INFINITY
    satisfies -- it passed identically whether the limit applied or not. This
    encodes the real, platform-dependent contract instead: on Darwin the
    absence is asserted positively, so a future OS or interpreter that starts
    honouring ``RLIMIT_AS`` fails this test and forces the docs to be updated.
    """

    probe = subprocess.run(
        [sys.executable, "-c", _LIMIT_PROBE],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    seen = json.loads(probe.stdout)

    if sys.platform == "darwin":
        # Darwin honours neither RLIMIT_AS nor RLIMIT_DATA; documented in
        # docs/acceptance/document-extraction/README.md under non-goals.
        assert seen["active"] is False
        assert seen["soft_after"] == seen["infinity"]
        return

    hard = seen["hard_before"]
    expected = seen["want"] if hard == seen["infinity"] else min(seen["want"], hard)
    assert seen["active"] is True
    assert seen["soft_after"] == expected


class _FakeResource:
    """A `resource` module stand-in, so all three host behaviours are testable.

    No real platform available to this suite silently ignores a `setrlimit`,
    but that is exactly the failure the read-back exists to catch, so it is
    modelled here rather than assumed away.
    """

    RLIM_INFINITY = -1

    def __init__(self, hard: int, behaviour: str) -> None:
        self.soft = self.RLIM_INFINITY
        self.hard = hard
        self.behaviour = behaviour

    def getrlimit(self, _limit: int) -> tuple[int, int]:
        return (self.soft, self.hard)

    def setrlimit(self, _limit: int, values: tuple[int, int]) -> None:
        if self.behaviour == "refuses":
            raise ValueError("current limit exceeds maximum limit")
        if self.behaviour == "ignores":
            return
        self.soft = values[0]


@pytest.mark.parametrize(
    ("behaviour", "expected"),
    [("honours", True), ("refuses", False), ("ignores", False)],
)
def test_set_limit_believes_the_read_back_not_the_absence_of_an_exception(
    behaviour: str, expected: bool
) -> None:
    fake = _FakeResource(_FakeResource.RLIM_INFINITY, behaviour)
    assert child._set_limit(fake, 0, 512 * 1024 * 1024) is expected


def test_set_limit_clamps_to_a_lower_hard_limit() -> None:
    """A host whose hard limit is below the request still gets a real ceiling."""

    fake = _FakeResource(64 * 1024 * 1024, "honours")
    assert child._set_limit(fake, 0, 512 * 1024 * 1024) is True
    assert fake.soft == 64 * 1024 * 1024


@posix_only
def test_child_reports_whether_the_memory_ceiling_is_real() -> None:
    """`memory_limit_active` must not claim a ceiling the host refused."""

    probe = subprocess.run(
        [sys.executable, "-c", _LIMIT_PROBE],
        capture_output=True,
        text=True,
        timeout=30,
    )
    seen = json.loads(probe.stdout)
    # The flag and the read-back limit agree, whichever platform this is.
    assert seen["active"] is (seen["soft_after"] != seen["infinity"])


def test_child_opens_exactly_the_path_the_parent_validated(tmp_path: Path) -> None:
    """A newline in the path must not let the child open a different file.

    Line-framed stdin let ``victim.txt\nignored`` reach the child as
    ``victim.txt``: the parent stat'd one file and the child opened another.
    """

    victim = tmp_path / "victim.txt"
    victim.write_text("secret", encoding="utf-8")
    injected = Path(f"{victim}\nignored")
    assert not injected.exists()
    result = _run_child(injected)
    assert result["status"] == "failed:unreadable"
    assert result["text"] == ""


@pytest.mark.parametrize("name", ["odd\nname.txt", " leading.txt", "trailing .txt"])
def test_child_reads_paths_with_newlines_and_edge_whitespace(
    tmp_path: Path, name: str
) -> None:
    """Those characters are legal in POSIX filenames; framing must preserve them."""

    if os.name == "nt":  # pragma: no cover - Windows forbids these names
        pytest.skip("Windows rejects these filenames")
    path = tmp_path / name
    path.write_text("kept", encoding="utf-8")
    result = _run_child(path)
    assert result["status"] == "parsed"
    assert result["text"] == "kept"


def test_child_enforces_the_input_cap_itself(tmp_path: Path) -> None:
    """The parent's pre-spawn stat is TOCTOU; the child must bound its own read."""

    path = tmp_path / "grown.txt"
    path.write_text("c" * 200_000, encoding="utf-8")
    env = dict(os.environ, RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES="1024")
    result = _run_child(path, env=env)
    assert result["status"] == de.FAILED_TOO_LARGE
    assert result["text"] == ""
    assert result["chars"] == 0


class _BoundedOnlyHandle:
    """A file handle that refuses an unbounded `read()`."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.requested: int | None = None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError("the child must never read a document unbounded")
        self.requested = size
        return self._data[:size]

    def __enter__(self) -> _BoundedOnlyHandle:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeDocument:
    """Duck-typed stand-in for `Path`, exposing only what `_read_bounded` uses."""

    def __init__(self, data: bytes) -> None:
        self.handle = _BoundedOnlyHandle(data)

    def open(self, _mode: str) -> _BoundedOnlyHandle:
        return self.handle


def test_read_bounded_never_slurps_the_whole_file() -> None:
    """Reading it all and then measuring is not a bound; a 20 GiB file is why."""

    document = _FakeDocument(b"x" * 5000)
    data, oversized = child._read_bounded(document, 1024)
    assert oversized is True
    assert data == b""
    # One byte past the cap is all it takes to know the file overran it.
    assert document.handle.requested == 1025


def test_read_bounded_returns_a_file_that_fits() -> None:
    document = _FakeDocument(b"y" * 100)
    data, oversized = child._read_bounded(document, 1024)
    assert oversized is False
    assert data == b"y" * 100


def test_child_preserves_a_path_ending_in_whitespace(tmp_path: Path) -> None:
    """Trailing whitespace is legal in a POSIX filename; `.strip()` destroyed it.

    Under line framing this path was trimmed to a name that does not exist, so
    the child answered `failed:unreadable` about a file the parent had already
    validated. Preserved, the file is found and merely has no known suffix.
    """

    if os.name == "nt":  # pragma: no cover - Windows rejects the name
        pytest.skip("Windows strips trailing whitespace from filenames")
    path = tmp_path / "notes.txt "
    path.write_text("kept", encoding="utf-8")
    assert path.is_file()
    result = _run_child(path)
    assert result["status"] == "unsupported"


def test_child_accepts_input_at_exactly_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "exact.txt"
    path.write_text("d" * 1024, encoding="utf-8")
    env = dict(os.environ, RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES="1024")
    result = _run_child(path, env=env)
    assert result["status"] == "parsed"
    assert result["chars"] == 1024


class _StdinBytes:
    """Just enough of `sys.stdin` for `main` to read a framed path."""

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def test_child_reports_memory_when_the_extractor_runs_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`failed:memory` is reachable: the child catches MemoryError and names it.

    Raised from the extraction step rather than by exhausting real memory, so
    the contract is asserted deterministically on every platform. On a host
    where `memory_limit_active()` is False nothing will raise it in practice --
    that is the documented macOS gap, not a different contract.
    """

    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")

    def _exhausted(*args: object, **kwargs: object) -> tuple[str, str]:
        raise MemoryError

    # `main` normally lowers this process's rlimits; in-process that would
    # cripple the test run itself (RLIMIT_FSIZE=0 breaks every later write).
    monkeypatch.setattr(child, "apply_limits", lambda: None)
    monkeypatch.setattr(child, "_extract", _exhausted)
    monkeypatch.setattr(sys, "stdin", _StdinBytes(os.fsencode(path)))
    assert child.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == de.FAILED_MEMORY
    assert payload["kind"] == "text"
    assert payload["text"] == ""
    assert de.FAILED_MEMORY in de.FAILURE_CODES


_MEMORY_CHILD_SCRIPT = """
import sys

sys.path.insert(0, '.')
from rynmesh.services import document_extract_child as c


def _exhausted(*args, **kwargs):
    raise MemoryError


c.apply_limits = lambda: None
c._extract = _exhausted
raise SystemExit(c.main())
"""


@pytest.mark.parametrize("bad_text", ["0", "false", "[]", "{}", "null", "3"])
def test_extract_document_rejects_a_non_string_text_field(
    tmp_path: Path, bad_text: str
) -> None:
    """The type guard must actually see the value the child sent.

    `parsed.get("text") or ""` coerced every falsy non-string to "" first, so
    `isinstance(text, str)` could never reject this class of malformed payload
    and a broken child was reported as a clean `parsed` with empty text.
    """

    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")
    payload = '{"status": "parsed", "kind": "text", "text": %s, "chars": 0}' % bad_text

    def _malformed(*args: object, **kwargs: object) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", f"import sys; sys.stdout.write({payload!r})"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    result = de.extract_document(path, spawn=_malformed)
    assert result["status"] == de.FAILED_INTERNAL


def test_extract_document_propagates_the_input_cap_to_the_child(tmp_path: Path) -> None:
    """The child cannot enforce a cap it was never told about."""

    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")
    seen: dict[str, str] = {}

    def _record(*args: object, **kwargs: object) -> subprocess.Popen:
        seen.update(kwargs["env"])  # type: ignore[arg-type]
        return subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    de.extract_document(path, max_input_bytes=4096, spawn=_record)
    assert seen["RYNMESH_DOC_EXTRACT_MAX_INPUT_BYTES"] == "4096"


def test_extract_document_surfaces_failed_memory_from_the_child(tmp_path: Path) -> None:
    """The whole chain, not just the child: `failed:memory` reaches the caller.

    Without this the code was in `FAILURE_CODES` and documented but no path
    could ever produce it -- the parent maps every non-zero exit to
    `failed:crashed`, so the distinction only exists if the child exits 0 with
    this status.
    """

    path = tmp_path / "notes.txt"
    path.write_text("ok", encoding="utf-8")

    def _out_of_memory(*args: object, **kwargs: object) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", _MEMORY_CHILD_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    result = de.extract_document(path, spawn=_out_of_memory)
    assert result["status"] == de.FAILED_MEMORY
    assert result["text"] == ""
    assert result["chars"] == 0


def test_extract_document_reads_a_real_file(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("# heading\n\nbody\n", encoding="utf-8")
    result = de.extract_document(path)
    assert result["status"] == "parsed"
    assert result["kind"] == "markdown"
    assert "heading" in result["text"]


def test_extract_document_rejects_a_path_with_an_embedded_nul() -> None:
    """A NUL byte makes `Path.stat`/`Path.is_file` raise `ValueError`, not
    `OSError`. `extract_document` must still answer from the closed
    `FAILURE_CODES` set instead of letting that `ValueError` escape and break
    the "never raises" contract documented at the top of this module.
    """
    hostile = "a\x00b.txt"
    spawned: list[object] = []

    def _never(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("must not spawn for an un-stat-able path")

    result = de.extract_document(hostile, spawn=_never)
    assert result["status"] in de.FAILURE_CODES
    assert result["status"] == de.FAILED_UNREADABLE
    assert result["text"] == ""
    assert spawned == []
    serialized = json.dumps(result)
    assert hostile not in serialized
    assert "b.txt" not in serialized


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


pdf_only = pytest.mark.skipif(
    not child.pdf_available(), reason="pypdf is not installed (the documents extra)"
)


def _minimal_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pdf_only
def test_a_valid_pdf_parses(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(_minimal_pdf())
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


def _pdf_with_extractable_text(paragraph_count: int = 200) -> bytes:
    """A one-page PDF whose content stream actually draws text.

    `_minimal_pdf` above builds a blank page, so its extracted text is always
    empty and the truncation branch in `document_extract_child.main` is never
    exercised. This builds a minimal content stream by hand — one `Tj` on a
    standard Helvetica font, no embedding required — so pypdf's extractor
    returns the literal string back out, long enough that a small
    `max_output_chars` cap truncates it.
    """
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    text = "Lorem ipsum dolor sit amet consectetur " * paragraph_count
    content = f"BT /F1 12 Tf 10 750 Td ({text}) Tj ET".encode("latin-1")

    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)

    stream_obj = DecodedStreamObject()
    stream_obj.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream_obj)

    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_dict = DictionaryObject()
    font_dict[NameObject("/F1")] = writer._add_object(font)
    resources = DictionaryObject()
    resources[NameObject("/Font")] = font_dict
    page[NameObject("/Resources")] = resources

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pdf_only
def test_a_pdf_with_real_text_truncates_to_exactly_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(_pdf_with_extractable_text())
    result = de.extract_document(path, max_output_chars=500)
    assert result["status"] == "truncated"
    assert result["chars"] == 500
    assert len(result["text"]) == 500


def test_no_path_or_content_reaches_the_result_or_logs(tmp_path: Path, caplog) -> None:
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
            blob = json.dumps(result)
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


def test_works_on_a_cold_home_with_no_prior_state(tmp_path: Path) -> None:
    """No node home, no store, no config: the helper owns no state at all."""
    path = tmp_path / "first.md"
    path.write_text("# cold\n", encoding="utf-8")
    assert de.extract_document(path)["status"] == "parsed"
