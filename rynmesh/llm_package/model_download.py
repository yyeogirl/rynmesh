"""Resumable HTTPS model download: Range-based resume, size guard, checksum quarantine.

Split out of `lifecycle.py` to keep that module under the project's module
size ceiling. `lifecycle._download` re-exports `download` here under its
historical name so existing monkeypatch call sites (`lifecycle._download`)
keep working; a caller may also patch `model_download.download` directly,
or this module's own `_urlopen`.

Every request goes through the shared HTTPS-only opener (`https_only`), so a
redirect off HTTPS is refused mid-download rather than followed.

Nothing raised from here may contain a filesystem path or a URL: only fixed,
path-free strings and (for genuine I/O failures) the exception's type name.
"""

from __future__ import annotations

import hashlib
import http.client
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import LifecycleError
from .https_only import build_https_only_opener

ProgressCallback = Callable[[str, int, str], None]
CancelCheck = Callable[[], bool]

CHUNK_BYTES = 1024 * 1024
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)", re.I)


def _urlopen(request: urllib.request.Request, timeout: float = 300) -> Any:
    """Open `request` with the HTTPS-only opener (the whole redirect chain).

    The single seam tests replace to serve a model body without a network.
    """
    return build_https_only_opener().open(request, timeout=timeout)


def _header(headers: object, name: str) -> str | None:
    """Case-tolerant header lookup for both real and test-double header objects."""
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    return getter(name) or getter(name.lower()) or getter(name.title())


def _report(progress: ProgressCallback | None, cancel_check: CancelCheck | None,
           stage: str, percent: int, message: str) -> None:
    if cancel_check and cancel_check():
        raise LifecycleError("setup cancelled")
    if progress:
        progress(stage, max(0, min(100, percent)), message)


def file_sha256(path: Path) -> str:
    """Fresh SHA-256 of the complete file (never a running/partial digest)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _response_plan(response: Any, resume_from: int, size_bytes: int | None) -> tuple[int, int | None, int | None]:
    """Validate byte offsets before opening the partial file for writing."""
    try:
        status = int(getattr(response, "status", 200) or 200)
        raw_length = _header(response.headers, "content-length")
        if raw_length is not None and not re.fullmatch(r"[0-9]+", raw_length):
            raise ValueError
        length = int(raw_length) if raw_length is not None else None
        encoding = _header(response.headers, "content-encoding")
        if encoding and encoding.strip().lower() != "identity":
            raise ValueError
        if status == 200:
            return 0, length, size_bytes if size_bytes is not None else length
        if status != 206:
            raise ValueError
        match = _CONTENT_RANGE.fullmatch((_header(response.headers, "content-range") or "").strip())
        if match is None:
            raise ValueError
        start, end = int(match[1]), int(match[2])
        total = None if match[3] == "*" else int(match[3])
        if start not in {0, resume_from} or end < start or (total is not None and end >= total):
            raise ValueError
        if size_bytes is not None and total is not None and total != size_bytes:
            raise ValueError
        count = end - start + 1
        if length is not None and length != count:
            raise ValueError
        return start, count, size_bytes if size_bytes is not None else total
    except (TypeError, ValueError) as exc:
        raise LifecycleError("download response has invalid byte range or encoding; retry to resume") from exc


def download(
    url: str,
    destination: Path,
    expected_sha256: str,
    *,
    size_bytes: int | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> str:
    """Download `url` to `destination`, resuming a prior partial attempt.

    A `<destination>.part` left over from an earlier call (a dropped
    connection, a cancelled setup, or the app quitting) is resumed with a
    `Range` request rather than restarted from zero:

    - A valid `206` at the requested offset appends to the existing part.
    - `200` (the server ignored `Range`) truncates and restarts.
    - A valid `206` starting at zero restarts. Other mismatched or missing
      range information is rejected before altering the saved prefix.
    - `416` permits complete-file verification only when the part is not
      shorter than the pinned size; a short part remains resumable.

    The part is deleted only for a size-guard violation (untrustworthy
    data) or replaced with `.corrupt` on a checksum mismatch. Every other
    failure — cancellation, a network error — leaves it in place so the
    next call can resume.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise LifecycleError("install downloads require an HTTPS URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    resume_from = temporary.stat().st_size if temporary.exists() else 0
    _report(progress, cancel_check, "download_model", 15, "Preparing model download; verification pending")
    if size_bytes is not None and resume_from > size_bytes:
        temporary.unlink(missing_ok=True)
        raise LifecycleError("download exceeded the pinned size")
    headers = {"User-Agent": "Rynmesh/0.6", "Accept-Encoding": "identity"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
        _report(progress, cancel_check, "download_model", 15, "Resuming model download; verification pending")

    request = urllib.request.Request(url, headers=headers)
    already_complete = False
    try:
        connection = _urlopen(request, timeout=300)
    except urllib.error.HTTPError as exc:
        if resume_from and exc.code == 416:
            exc.close()
            if size_bytes is not None and resume_from < size_bytes:
                raise LifecycleError("download incomplete; retry to resume") from exc
            # A range refusal is not proof of integrity. Only the complete
            # checksum below can authorize publishing the existing part.
            already_complete = True
            connection = None
        else:
            raise LifecycleError("download failed: " + type(exc).__name__) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise LifecycleError("download failed: " + type(exc).__name__) from exc

    if not already_complete:
        overflow = False
        try:
            with connection as response:
                offset, response_size, full_size = _response_plan(response, resume_from, size_bytes)
                downloaded = offset
                mode = "ab" if offset else "wb"
                with temporary.open(mode) as handle:
                    while chunk := response.read(CHUNK_BYTES):
                        if cancel_check and cancel_check():
                            raise LifecycleError("setup cancelled")
                        downloaded += len(chunk)
                        if size_bytes is not None and downloaded > size_bytes:
                            # Not trustworthy: stop writing and discard below,
                            # once the handle is closed (Windows cannot unlink
                            # an open file).
                            overflow = True
                            break
                        handle.write(chunk)
                        percent = 15 + int(downloaded / full_size * 45) if full_size else 35
                        if progress:
                            progress("download_model", min(60, percent), "Downloading model data; verification pending")
                if not overflow:
                    if response_size is not None and downloaded - offset != response_size:
                        raise LifecycleError("download incomplete; retry to resume")
                    if full_size is not None and downloaded < full_size:
                        raise LifecycleError("download incomplete; retry to resume")
        except LifecycleError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise LifecycleError("download failed: " + type(exc).__name__) from exc
        if overflow:
            temporary.unlink(missing_ok=True)
            raise LifecycleError("download exceeded the pinned size")

    # Hash the complete file fresh (not a running digest), so a resumed part
    # verifies against the bytes actually on disk rather than just this session.
    _report(progress, cancel_check, "checksum", 60, "Verifying complete model download")
    actual = file_sha256(temporary)
    _report(None, cancel_check, "checksum", 60, "Verifying complete model download")
    if actual != expected_sha256.lower():
        corrupt = destination.with_suffix(destination.suffix + ".corrupt")
        corrupt.unlink(missing_ok=True)
        temporary.replace(corrupt)
        raise LifecycleError("model checksum mismatch; the download was quarantined and will restart")
    temporary.replace(destination)
    return actual
