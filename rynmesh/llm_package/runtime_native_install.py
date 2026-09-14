"""Obtain the pinned llama.cpp release for the native runtime backend.

Split out of `runtime_native.py` so each module has one job: that one resolves
and runs the server, this one gets it onto disk — the pinned per-platform asset
table, the verified HTTPS download (through the shared HTTPS-only opener in
`https_only`), and the hardened archive extraction.

Nothing raised from here may contain a filesystem path: these errors reach the
owner through setup progress and node logs.
"""

from __future__ import annotations

import hashlib
import os
import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from ..atomic_io import AtomicIOError, atomic_write_json
from .errors import LifecycleError
from .https_only import build_https_only_opener

RUNTIME_RELEASE = "b10774"  # ggml-org/llama.cpp, 2026-09-03
RUNTIME_BASE_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_RELEASE}/"
# (platform.system(), normalized machine) -> (asset, sha256, size_bytes)
RUNTIME_ASSETS: dict[tuple[str, str], tuple[str, str, int]] = {
    ("Darwin", "arm64"): ("llama-b10774-bin-macos-arm64.tar.gz",
        "aeb59ccd60191bdf96ddb57f352286430d9e0b6dc29281460e1bd217556c3c78", 11088473),
    ("Darwin", "x86_64"): ("llama-b10774-bin-macos-x64.tar.gz",
        "9acda4c44584970622ecc06086b5b8cd3e06b6dc0d039f8c95a63f1186f80e5e", 11146110),
    ("Linux", "x86_64"): ("llama-b10774-bin-ubuntu-x64.tar.gz",
        "68caa9c0e6dcdf32283fc4d8a0008fe389cb191bb79f5fa34c255beec388046d", 16718077),
    ("Linux", "arm64"): ("llama-b10774-bin-ubuntu-arm64.tar.gz",
        "eeb67bd32e163d09687e7a7a8bc25119bb2cbc637d5ec2b45c493c7df1675452", 13363523),
    ("Windows", "x86_64"): ("llama-b10774-bin-win-cpu-x64.zip",
        "04f25dd148fda9d66efd91e96c637126220273fec62cdf5007367722c11bc744", 18380733),
    ("Windows", "arm64"): ("llama-b10774-bin-win-cpu-arm64.zip",
        "a26b288a4a9b1d9163a171e6b834b4f2956f72fcb51d8b75560565939b4a755f", 11949368),
}

MARKER_NAME = "runtime.json"
MAX_EXTRACTED_BYTES = 200 * 2**20
CHUNK_BYTES = 1024 * 1024
UNAVAILABLE_REASON = (
    "no bundled inference runtime is available for this platform; "
    "connect an existing local API or install Docker instead"
)
UNREADABLE_ARCHIVE = "runtime archive is unreadable or corrupt"
UNWRITABLE_STATE = "unable to write runtime state"


def normalize_machine(value: str) -> str:
    value = value.strip().lower()
    if value in {"aarch64", "arm64"}:
        return "arm64"
    if value in {"amd64", "x86_64"}:
        return "x86_64"
    return value


def machine() -> str:
    return normalize_machine(platform.machine())


def asset_for(system: str, machine_name: str) -> tuple[str, str, int] | None:
    """Pinned (asset, sha256, size) for a platform pair, or None.

    The single source of truth for the pin: the desktop bundling script asks
    this instead of carrying a second copy of the digests.
    """
    return RUNTIME_ASSETS.get((system.strip().title(), normalize_machine(machine_name)))


def asset() -> tuple[str, str, int] | None:
    return asset_for(platform.system(), platform.machine())


def server_filename() -> str:
    return "llama-server.exe" if platform.system() == "Windows" else "llama-server"


def managed_root(root: Path | str) -> Path:
    return Path(root).expanduser() / "runtime" / f"llama-{RUNTIME_RELEASE}"


def usable_server(path: Path) -> Path | None:
    """A candidate counts only when it is a regular file we may execute."""
    try:
        return path if path.is_file() and os.access(path, os.X_OK) else None
    except OSError:
        return None


def find_server(directory: Path) -> Path | None:
    """Usable server inside `directory`, allowing the release `build/bin` layout."""
    name = server_filename()
    for candidate in (directory / name, directory / "build" / "bin" / name):
        found = usable_server(candidate)
        if found is not None:
            return found
    return None


def report(progress: Any, cancel_check: Any, percent: int, message: str) -> None:
    if cancel_check and cancel_check():
        raise LifecycleError("setup cancelled")
    if progress:
        progress("pull_runtime", percent, message)


def _fetch(url: str, destination: Path, expected_sha256: str, size_bytes: int, *,
           progress: Any = None, cancel_check: Any = None) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise LifecycleError("runtime downloads require an HTTPS URL")
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    downloaded = 0
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "Rynmesh/0.6"})
        opener = build_https_only_opener()
        with opener.open(request, timeout=300) as response, temporary.open("wb") as handle:
            while chunk := response.read(CHUNK_BYTES):
                if cancel_check and cancel_check():
                    raise LifecycleError("setup cancelled")
                downloaded += len(chunk)
                if downloaded > size_bytes:
                    raise LifecycleError("runtime archive is larger than its pinned size")
                digest.update(chunk)
                handle.write(chunk)
                percent = min(80, 65 + int(downloaded / size_bytes * 15))
                report(progress, None, percent, "Downloading the local inference runtime")
    except LifecycleError:
        _discard(temporary)
        raise
    except OSError as exc:
        _discard(temporary)
        raise LifecycleError("downloading the local inference runtime failed") from exc
    if digest.hexdigest() != expected_sha256.lower():
        _discard(temporary)
        raise LifecycleError("runtime archive checksum mismatch")
    try:
        temporary.replace(destination)
    except OSError as exc:
        _discard(temporary)
        raise LifecycleError(UNWRITABLE_STATE) from exc


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _check_member(name: str) -> None:
    if not name or name.startswith(("/", "\\")) or "\\" in name or (len(name) > 1 and name[1] == ":"):
        raise LifecycleError("runtime archive contains an unsafe member path")
    if ".." in PurePosixPath(name).parts:
        raise LifecycleError("runtime archive contains an unsafe member path")


def _check_total(total: int) -> int:
    if total > MAX_EXTRACTED_BYTES:
        raise LifecycleError("runtime archive expands past the extraction size limit")
    return total


def _extract_tar(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        total = 0
        for member in members:
            _check_member(member.name)
            if not (member.isfile() or member.isdir()):
                raise LifecycleError("runtime archive contains an unsafe member type")
            total = _check_total(total + max(0, int(member.size)))
        bundle.extractall(target, members=members, filter="data")


def _extract_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        total = 0
        for entry in entries:
            _check_member(entry.filename)
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise LifecycleError("runtime archive contains an unsafe member type")
            total = _check_total(total + max(0, int(entry.file_size)))
        written = 0
        for entry in entries:
            # Written bytes are counted as they land, so a member that expands
            # past its declared size still cannot fill the disk.
            written = _write_zip_entry(bundle, entry, target, written)


def _write_zip_entry(bundle: zipfile.ZipFile, entry: zipfile.ZipInfo, target: Path,
                     written: int) -> int:
    destination = target / PurePosixPath(entry.filename)  # name validated above
    if entry.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        return written
    destination.parent.mkdir(parents=True, exist_ok=True)
    with bundle.open(entry) as source, destination.open("wb") as handle:
        while chunk := source.read(CHUNK_BYTES):
            written = _check_total(written + len(chunk))
            handle.write(chunk)
    return written


def _extract(archive: Path, target: Path) -> None:
    try:
        target.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(target, 0o700)
    except OSError as exc:
        raise LifecycleError(UNWRITABLE_STATE) from exc
    try:
        if archive.name.endswith(".zip"):
            _extract_zip(archive, target)
        else:
            _extract_tar(archive, target)
    except LifecycleError:
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
        raise LifecycleError(UNREADABLE_ARCHIVE) from exc


def _extracted_server(target: Path) -> Path | None:
    """Locate the server before it is marked executable."""
    name = server_filename()
    for candidate in (target / name, target / "build" / "bin" / name):
        if candidate.is_file():
            return candidate
    return None


def _write_marker(target: Path, server: Path, expected_sha256: str) -> None:
    payload = {"release": RUNTIME_RELEASE, "server": server.relative_to(target).as_posix(),
               "sha256": expected_sha256.lower()}
    try:
        atomic_write_json(target / MARKER_NAME, payload, indent=2, sort_keys=True)
    except AtomicIOError as exc:
        raise LifecycleError(UNWRITABLE_STATE) from exc


def download(base: Path, *, progress: Any = None, cancel_check: Any = None) -> Path:
    """Fetch, verify, and unpack the pinned release under `base`; return the server."""
    pinned = asset()
    if pinned is None:
        raise LifecycleError(UNAVAILABLE_REASON)
    name, expected_sha256, size_bytes = pinned
    report(progress, cancel_check, 65, "Downloading the local inference runtime")
    archive = base / "runtime" / name
    _fetch(RUNTIME_BASE_URL + name, archive, expected_sha256, size_bytes,
           progress=progress, cancel_check=cancel_check)
    target = managed_root(base)
    try:
        # A repair must not leave an older completion marker authorizing a
        # partly overwritten extraction if the process exits or I/O fails.
        try:
            (target / MARKER_NAME).unlink(missing_ok=True)
        except OSError as exc:
            raise LifecycleError(UNWRITABLE_STATE) from exc
        _extract(archive, target)
    finally:
        _discard(archive)
    server = _extracted_server(target)
    if server is None:
        raise LifecycleError("the runtime archive did not contain an inference server")
    try:
        if os.name != "nt":
            server.chmod(0o755)
    except OSError as exc:
        raise LifecycleError(UNWRITABLE_STATE) from exc
    report(progress, cancel_check, 79, "Finishing local inference runtime installation")
    _write_marker(target, server, expected_sha256)
    report(progress, None, 80, "Local inference runtime installed")
    return server
