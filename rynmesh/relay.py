"""Hash-addressed relay storage for NAT-safe Rynmesh artifact exchange."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .atomic_io import atomic_write_json

RYNMESH_RELAY_USER_AGENT = "RynmeshRelay/0.1"
DEFAULT_MAX_RELAY_BLOB_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_RELAY_DIRECT_UPLOAD_MAX_BYTES = 768 * 1024
DEFAULT_RELAY_CHUNK_BYTES = 768 * 1024
RELAY_JSON_LIMIT = 2 * 1024 * 1024
CHUNKED_MANIFEST_KIND = "rynmesh.chunked_blob.v1"
CHUNKED_MANIFEST_MEDIA_TYPE = "application/vnd.rynmesh.chunked-manifest+json"


class RelayError(RuntimeError):
    pass


@dataclass(frozen=True)
class RelayBlobRecord:
    content_hash: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    filename: str = ""
    uploader_peer_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelayBlobRecord":
        return cls(
            content_hash=str(data["content_hash"]),
            size_bytes=int(data["size_bytes"]),
            media_type=str(data.get("media_type", "application/octet-stream")),
            filename=str(data.get("filename", "")),
            uploader_peer_id=str(data.get("uploader_peer_id", "")),
            metadata=dict(data.get("metadata", {})),
        )


class FileRelayStore:
    """Content-addressed relay blob store.

    The relay is intentionally dumb: it stores bytes and metadata under a hash.
    Receivers must verify hashes locally before trusting any artifact.
    """

    def __init__(self, root: str | Path, *, max_blob_bytes: int | None = None) -> None:
        self.root = Path(root).expanduser()
        self.blobs_dir = self.root / "blobs"
        self.tmp_dir = self.root / "tmp"
        self.max_blob_bytes = int(
            max_blob_bytes
            if max_blob_bytes is not None
            else os.environ.get("RYNMESH_RELAY_MAX_BLOB_BYTES", DEFAULT_MAX_RELAY_BLOB_BYTES)
        )
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def put_file(
        self,
        path: str | Path,
        *,
        media_type: str = "application/octet-stream",
        filename: str = "",
        uploader_peer_id: str = "",
        metadata: dict[str, Any] | None = None,
        expected_hash: str = "",
    ) -> RelayBlobRecord:
        source = Path(path).expanduser()
        if not source.exists() or not source.is_file():
            raise RelayError(f"relay_source_not_found: {source}")
        with source.open("rb") as handle:
            return self.put_chunks(
                iter(lambda: handle.read(1024 * 1024), b""),
                media_type=media_type,
                filename=filename or source.name,
                uploader_peer_id=uploader_peer_id,
                metadata=metadata,
                expected_hash=expected_hash,
            )

    def put_chunks(
        self,
        chunks: Iterable[bytes],
        *,
        media_type: str = "application/octet-stream",
        filename: str = "",
        uploader_peer_id: str = "",
        metadata: dict[str, Any] | None = None,
        expected_hash: str = "",
    ) -> RelayBlobRecord:
        tmp_path = self.tmp_dir / f"{uuid.uuid4().hex}.blob"
        digest = hashlib.sha256()
        total = 0
        try:
            with tmp_path.open("wb") as handle:
                for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_blob_bytes:
                        raise RelayError("relay_blob_too_large")
                    digest.update(chunk)
                    handle.write(chunk)
            return self._commit_tmp(
                tmp_path,
                content_hash="sha256:" + digest.hexdigest(),
                size_bytes=total,
                media_type=media_type,
                filename=filename,
                uploader_peer_id=uploader_peer_id,
                metadata=metadata,
                expected_hash=expected_hash,
            )
        except Exception:
            with _SuppressOSError():
                tmp_path.unlink()
            raise

    async def put_async_chunks(
        self,
        chunks: AsyncIterable[bytes],
        *,
        media_type: str = "application/octet-stream",
        filename: str = "",
        uploader_peer_id: str = "",
        metadata: dict[str, Any] | None = None,
        expected_hash: str = "",
    ) -> RelayBlobRecord:
        tmp_path = self.tmp_dir / f"{uuid.uuid4().hex}.blob"
        digest = hashlib.sha256()
        total = 0
        try:
            with tmp_path.open("wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_blob_bytes:
                        raise RelayError("relay_blob_too_large")
                    digest.update(chunk)
                    handle.write(chunk)
            return self._commit_tmp(
                tmp_path,
                content_hash="sha256:" + digest.hexdigest(),
                size_bytes=total,
                media_type=media_type,
                filename=filename,
                uploader_peer_id=uploader_peer_id,
                metadata=metadata,
                expected_hash=expected_hash,
            )
        except Exception:
            with _SuppressOSError():
                tmp_path.unlink()
            raise

    def info(self, content_hash: str) -> RelayBlobRecord:
        meta_path = self._meta_path(content_hash)
        if not meta_path.exists():
            raise RelayError("relay_blob_not_found")
        return RelayBlobRecord.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))

    def path_for(self, content_hash: str) -> Path:
        path = self._blob_path(content_hash)
        if not path.exists():
            raise RelayError("relay_blob_not_found")
        return path

    def put_chunk_refs(
        self,
        chunks: Iterable[dict[str, Any] | str],
        *,
        media_type: str = "application/octet-stream",
        filename: str = "",
        uploader_peer_id: str = "",
        metadata: dict[str, Any] | None = None,
        expected_hash: str = "",
    ) -> RelayBlobRecord:
        expected = normalize_content_hash(expected_hash)
        tmp_path = self.tmp_dir / f"{uuid.uuid4().hex}.blob"
        digest = hashlib.sha256()
        total = 0
        chunk_count = 0
        try:
            with tmp_path.open("wb") as output:
                for chunk in chunks:
                    chunk_count += 1
                    if isinstance(chunk, dict):
                        chunk_hash = str(chunk.get("content_hash") or "")
                        expected_size = chunk.get("size_bytes")
                    else:
                        chunk_hash = str(chunk or "")
                        expected_size = None
                    normalized_chunk_hash = normalize_content_hash(chunk_hash)
                    record = self.info(normalized_chunk_hash)
                    if expected_size is not None and int(expected_size) != record.size_bytes:
                        raise RelayError("relay_chunk_size_mismatch")
                    with self.path_for(normalized_chunk_hash).open("rb") as source:
                        for data in iter(lambda: source.read(1024 * 1024), b""):
                            if not data:
                                continue
                            total += len(data)
                            if total > self.max_blob_bytes:
                                raise RelayError("relay_blob_too_large")
                            digest.update(data)
                            output.write(data)
            if chunk_count <= 0:
                raise RelayError("relay_chunks_required")
            final_metadata = dict(metadata or {})
            final_metadata.setdefault("relay_upload_mode", "chunked")
            final_metadata.setdefault("relay_chunk_count", chunk_count)
            return self._commit_tmp(
                tmp_path,
                content_hash="sha256:" + digest.hexdigest(),
                size_bytes=total,
                media_type=media_type,
                filename=filename,
                uploader_peer_id=uploader_peer_id,
                metadata=final_metadata,
                expected_hash=expected,
            )
        except Exception:
            with _SuppressOSError():
                tmp_path.unlink()
            raise

    def _commit_tmp(
        self,
        tmp_path: Path,
        *,
        content_hash: str,
        size_bytes: int,
        media_type: str,
        filename: str,
        uploader_peer_id: str,
        metadata: dict[str, Any] | None,
        expected_hash: str,
    ) -> RelayBlobRecord:
        normalized_hash = normalize_content_hash(content_hash)
        expected = str(expected_hash or "").strip()
        if expected and normalize_content_hash(expected) != normalized_hash:
            raise RelayError("relay_hash_mismatch")
        dest = self._blob_path(normalized_hash)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            tmp_path.unlink()
        else:
            tmp_path.replace(dest)
        record = RelayBlobRecord(
            content_hash=normalized_hash,
            size_bytes=size_bytes,
            media_type=str(media_type or "application/octet-stream"),
            filename=str(filename or ""),
            uploader_peer_id=str(uploader_peer_id or ""),
            metadata=dict(metadata or {}),
        )
        # The blob content itself was already committed above via its own
        # unique-tmp-name + rename; the small metadata record gets the same
        # durability treatment so a crash between the two never leaves a
        # zero-length or truncated `.json` sidecar next to a good blob.
        atomic_write_json(self._meta_path(normalized_hash), record.to_dict(), indent=2, sort_keys=True)
        return record

    def _blob_path(self, content_hash: str) -> Path:
        digest = _digest(content_hash)
        return self.blobs_dir / digest[:2] / digest

    def _meta_path(self, content_hash: str) -> Path:
        return self._blob_path(content_hash).with_suffix(".json")


class HttpRelayClient:
    def __init__(self, base_url: str, *, timeout_s: float = 60.0) -> None:
        cleaned = str(base_url or "").strip().rstrip("/")
        if not cleaned:
            raise RelayError("relay_url_required")
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RelayError("relay_url_invalid")
        self.base_url = cleaned
        self.timeout_s = float(timeout_s)

    def _download_timeout_s(self) -> float:
        raw = os.environ.get("RYNMESH_RELAY_DOWNLOAD_TIMEOUT_S", "").strip()
        if raw:
            try:
                return max(1.0, float(raw))
            except ValueError:
                pass
        return max(self.timeout_s, 300.0)

    def upload_file(
        self,
        path: str | Path,
        *,
        media_type: str = "application/octet-stream",
        filename: str = "",
        uploader_peer_id: str = "",
        expected_hash: str = "",
    ) -> dict[str, Any]:
        source = Path(path).expanduser()
        if not source.exists() or not source.is_file():
            raise RelayError(f"relay_source_not_found: {source}")
        expected = expected_hash or file_sha256(source)
        if _relay_direct_upload_max_bytes() > 0 and source.stat().st_size > _relay_direct_upload_max_bytes():
            return self._upload_chunked(
                source,
                media_type=media_type,
                filename=filename or source.name,
                uploader_peer_id=uploader_peer_id,
                expected_hash=expected,
            )
        return self._upload_streaming(
            source,
            media_type=media_type,
            filename=filename or source.name,
            uploader_peer_id=uploader_peer_id,
            expected_hash=expected,
        )

    def download_blob(self, content_hash: str, destination: str | Path) -> dict[str, Any]:
        normalized_hash = normalize_content_hash(content_hash)
        dest = Path(destination).expanduser()
        try:
            meta = self.blob_info(normalized_hash)
        except RelayError:
            meta = {}
        if _is_chunked_manifest_meta(meta):
            return self._download_chunked_manifest(normalized_hash, dest)
        return self._download_raw_blob(normalized_hash, dest)

    def _download_raw_blob(self, normalized_hash: str, destination: str | Path) -> dict[str, Any]:
        normalized_hash = normalize_content_hash(normalized_hash)
        dest = Path(destination).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        from .transport import network_key_header

        req = Request(
            f"{self.base_url}/api/v1/relay/blobs/{quote(normalized_hash, safe=':')}",
            headers={"user-agent": RYNMESH_RELAY_USER_AGENT, **network_key_header()},
            method="GET",
        )
        digest = hashlib.sha256()
        total = 0
        try:
            with urlopen(
                req,
                **_urlopen_tls_kwargs(self.base_url, self._download_timeout_s()),
            ) as response, dest.open("wb") as handle:
                timeout_failures = 0
                while True:
                    try:
                        chunk = response.read(1024 * 1024)
                    except TimeoutError:
                        timeout_failures += 1
                        if timeout_failures <= 5:
                            continue
                        raise
                    if not chunk:
                        break
                    timeout_failures = 0
                    digest.update(chunk)
                    total += len(chunk)
                    handle.write(chunk)
        except (HTTPError, URLError, TimeoutError) as exc:
            with _SuppressOSError():
                dest.unlink()
            raise RelayError(f"relay_http_error: {exc}") from exc
        actual_hash = "sha256:" + digest.hexdigest()
        if actual_hash != normalized_hash:
            with _SuppressOSError():
                dest.unlink()
            raise RelayError("relay_download_hash_mismatch")
        return {"content_hash": actual_hash, "size_bytes": total, "path": str(dest)}

    def _download_chunked_manifest(self, manifest_hash: str, destination: str | Path) -> dict[str, Any]:
        normalized_manifest_hash = normalize_content_hash(manifest_hash)
        dest = Path(destination).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        manifest_path = dest.parent / f".{dest.name}.{token}.manifest.json"
        assembled_path = dest.parent / f".{dest.name}.{token}.download"
        try:
            self._download_raw_blob(normalized_manifest_hash, manifest_path)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("kind") != CHUNKED_MANIFEST_KIND:
                raise RelayError("relay_manifest_invalid")
            expected_hash = normalize_content_hash(str(payload.get("content_hash") or ""))
            chunks = payload.get("chunks", [])
            if not isinstance(chunks, list) or not chunks:
                raise RelayError("relay_manifest_chunks_invalid")
            digest = hashlib.sha256()
            total = 0
            with assembled_path.open("wb") as output:
                for index, raw_chunk in enumerate(chunks):
                    if not isinstance(raw_chunk, dict):
                        raise RelayError("relay_manifest_chunk_invalid")
                    chunk_hash = normalize_content_hash(str(raw_chunk.get("content_hash") or ""))
                    chunk_path = dest.parent / f".{dest.name}.{token}.part{index:05d}"
                    try:
                        downloaded = self._download_raw_blob(chunk_hash, chunk_path)
                        expected_size = raw_chunk.get("size_bytes")
                        if expected_size is not None and int(expected_size) != int(downloaded.get("size_bytes") or 0):
                            raise RelayError("relay_manifest_chunk_size_mismatch")
                        with chunk_path.open("rb") as source:
                            for data in iter(lambda: source.read(1024 * 1024), b""):
                                if not data:
                                    continue
                                digest.update(data)
                                total += len(data)
                                output.write(data)
                    finally:
                        with _SuppressOSError():
                            chunk_path.unlink()
            actual_hash = "sha256:" + digest.hexdigest()
            if actual_hash != expected_hash:
                raise RelayError("relay_manifest_hash_mismatch")
            expected_size = payload.get("size_bytes")
            if expected_size is not None and int(expected_size) != total:
                raise RelayError("relay_manifest_size_mismatch")
            assembled_path.replace(dest)
            return {
                "content_hash": expected_hash,
                "relay_manifest_hash": normalized_manifest_hash,
                "size_bytes": total,
                "path": str(dest),
            }
        except Exception:
            with _SuppressOSError():
                assembled_path.unlink()
            with _SuppressOSError():
                dest.unlink()
            raise
        finally:
            with _SuppressOSError():
                manifest_path.unlink()

    def blob_info(self, content_hash: str) -> dict[str, Any]:
        normalized_hash = normalize_content_hash(content_hash)
        from .transport import network_key_header

        req = Request(
            f"{self.base_url}/api/v1/relay/meta/{quote(normalized_hash, safe=':')}",
            headers={"user-agent": RYNMESH_RELAY_USER_AGENT, **network_key_header()},
            method="GET",
        )
        try:
            with urlopen(
                req,
                **_urlopen_tls_kwargs(self.base_url, self.timeout_s),
            ) as response:
                raw = response.read(RELAY_JSON_LIMIT + 1)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RelayError(f"relay_http_error: {exc}") from exc
        if len(raw) > RELAY_JSON_LIMIT:
            raise RelayError("relay_response_too_large")
        payload = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise RelayError("relay_response_not_object")
        return payload

    def _upload_streaming(
        self,
        source: Path,
        *,
        media_type: str,
        filename: str,
        uploader_peer_id: str,
        expected_hash: str,
    ) -> dict[str, Any]:
        parsed = urlparse(self.base_url)
        path = (parsed.path.rstrip("/") if parsed.path else "") + "/api/v1/relay/blobs"
        connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        kwargs: dict[str, Any] = {"timeout": self.timeout_s}
        if parsed.scheme == "https":
            kwargs["context"] = _https_context()
        conn = connection_cls(parsed.hostname, parsed.port, **kwargs)
        try:
            from .transport import network_key_header

            conn.putrequest("POST", path, skip_host=True)
            conn.putheader("Host", parsed.netloc)
            conn.putheader("User-Agent", RYNMESH_RELAY_USER_AGENT)
            conn.putheader("Content-Type", media_type or "application/octet-stream")
            conn.putheader("Content-Length", str(source.stat().st_size))
            conn.putheader("X-Rynmesh-Expected-Hash", normalize_content_hash(expected_hash))
            for name, value in network_key_header().items():
                conn.putheader(name, value)
            if filename:
                conn.putheader("X-Rynmesh-Filename", filename)
            if uploader_peer_id:
                conn.putheader("X-Rynmesh-Uploader-Peer-Id", uploader_peer_id)
            conn.endheaders()
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    conn.send(chunk)
            response = conn.getresponse()
            raw = response.read(RELAY_JSON_LIMIT + 1)
            if response.status >= 400:
                raise RelayError(f"relay_http_error: {response.status} {raw[:200]!r}")
            if len(raw) > RELAY_JSON_LIMIT:
                raise RelayError("relay_response_too_large")
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise RelayError("relay_response_not_object")
            return payload
        finally:
            conn.close()

    def _upload_chunked(
        self,
        source: Path,
        *,
        media_type: str,
        filename: str,
        uploader_peer_id: str,
        expected_hash: str,
    ) -> dict[str, Any]:
        chunk_size = _relay_chunk_bytes()
        direct_limit = _relay_direct_upload_max_bytes()
        if direct_limit > 0:
            chunk_size = min(chunk_size, direct_limit)
        if chunk_size <= 0:
            raise RelayError("relay_chunk_size_invalid")
        chunks: list[dict[str, Any]] = []
        with source.open("rb") as handle:
            index = 0
            while True:
                data = handle.read(chunk_size)
                if not data:
                    break
                fd, tmp_name = tempfile.mkstemp(prefix="rynmesh-relay-chunk-", suffix=".bin")
                tmp_path = Path(tmp_name)
                try:
                    with os.fdopen(fd, "wb") as tmp_handle:
                        tmp_handle.write(data)
                    chunk_upload = self._upload_streaming(
                        tmp_path,
                        media_type="application/octet-stream",
                        filename=f"{filename}.part{index:05d}",
                        uploader_peer_id=uploader_peer_id,
                        expected_hash=file_sha256(tmp_path),
                    )
                    blob = chunk_upload.get("blob", {})
                    if not isinstance(blob, dict):
                        raise RelayError("relay_response_missing_blob")
                    chunks.append(
                        {
                            "index": index,
                            "content_hash": str(blob.get("content_hash") or ""),
                            "size_bytes": int(blob.get("size_bytes") or 0),
                        }
                    )
                finally:
                    with _SuppressOSError():
                        tmp_path.unlink()
                index += 1
        try:
            return self._assemble_chunks(
                chunks,
                media_type=media_type,
                filename=filename,
                uploader_peer_id=uploader_peer_id,
                expected_hash=expected_hash,
                source_size=source.stat().st_size,
            )
        except RelayError as exc:
            if not _relay_assemble_unavailable(exc):
                raise
            return self._upload_chunk_manifest(
                chunks,
                media_type=media_type,
                filename=filename,
                uploader_peer_id=uploader_peer_id,
                expected_hash=expected_hash,
                source_size=source.stat().st_size,
            )

    def _upload_chunk_manifest(
        self,
        chunks: list[dict[str, Any]],
        *,
        media_type: str,
        filename: str,
        uploader_peer_id: str,
        expected_hash: str,
        source_size: int,
    ) -> dict[str, Any]:
        manifest = {
            "kind": CHUNKED_MANIFEST_KIND,
            "content_hash": normalize_content_hash(expected_hash),
            "size_bytes": int(source_size),
            "media_type": media_type or "application/octet-stream",
            "filename": filename,
            "chunks": chunks,
        }
        data = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix="rynmesh-relay-manifest-", suffix=".json")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as tmp_handle:
                tmp_handle.write(data)
            uploaded = self._upload_streaming(
                tmp_path,
                media_type=CHUNKED_MANIFEST_MEDIA_TYPE,
                filename=f"{filename}.rynmesh-manifest.json" if filename else "rynmesh-manifest.json",
                uploader_peer_id=uploader_peer_id,
                expected_hash=file_sha256(tmp_path),
            )
        finally:
            with _SuppressOSError():
                tmp_path.unlink()
        uploaded["chunked_artifact"] = {
            "content_hash": normalize_content_hash(expected_hash),
            "size_bytes": int(source_size),
            "media_type": media_type or "application/octet-stream",
            "filename": filename,
            "manifest_kind": CHUNKED_MANIFEST_KIND,
        }
        return uploaded

    def _assemble_chunks(
        self,
        chunks: list[dict[str, Any]],
        *,
        media_type: str,
        filename: str,
        uploader_peer_id: str,
        expected_hash: str,
        source_size: int,
    ) -> dict[str, Any]:
        payload = {
            "expected_hash": normalize_content_hash(expected_hash),
            "media_type": media_type or "application/octet-stream",
            "filename": filename,
            "uploader_peer_id": uploader_peer_id,
            "chunks": chunks,
            "metadata": {
                "relay_upload_mode": "chunked",
                "relay_chunk_count": len(chunks),
                "relay_source_size_bytes": int(source_size),
            },
        }
        return self._post_json("/api/v1/relay/blobs/assemble", payload)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        from .transport import network_key_header

        target = f"{self.base_url}{path}"
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = Request(
            target,
            data=data,
            headers={
                "content-type": "application/json",
                "user-agent": RYNMESH_RELAY_USER_AGENT,
                **network_key_header(),
            },
            method="POST",
        )
        try:
            with urlopen(
                req,
                **_urlopen_tls_kwargs(self.base_url, self.timeout_s),
            ) as response:
                raw = response.read(RELAY_JSON_LIMIT + 1)
        except HTTPError as exc:
            raw = exc.read(200)
            raise RelayError(f"relay_http_error: {exc.code} {raw[:200]!r}") from exc
        except (URLError, TimeoutError) as exc:
            raise RelayError(f"relay_http_error: {exc}") from exc
        if len(raw) > RELAY_JSON_LIMIT:
            raise RelayError("relay_response_too_large")
        body = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(body, dict):
            raise RelayError("relay_response_not_object")
        return body


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).expanduser().open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def normalize_content_hash(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("sha256:"):
        digest = raw.split(":", 1)[1]
    else:
        digest = raw
    if len(digest) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in digest):
        raise RelayError("invalid_content_hash")
    return "sha256:" + digest.lower()


def _digest(content_hash: str) -> str:
    return normalize_content_hash(content_hash).split(":", 1)[1]


def _https_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _urlopen_tls_kwargs(base_url: str, timeout_s: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"timeout": timeout_s}
    if urlparse(str(base_url or "")).scheme == "https":
        kwargs["context"] = _https_context()
    return kwargs


def _is_chunked_manifest_meta(meta: dict[str, Any]) -> bool:
    return str(meta.get("media_type") or "") == CHUNKED_MANIFEST_MEDIA_TYPE


def _relay_assemble_unavailable(exc: RelayError) -> bool:
    message = str(exc)
    return (
        "relay_http_error: 404" in message
        or "relay_http_error: 405" in message
        or "relay_http_error: 501" in message
    )


def _relay_direct_upload_max_bytes() -> int:
    return max(
        0,
        int(os.environ.get("RYNMESH_RELAY_DIRECT_UPLOAD_MAX_BYTES", DEFAULT_RELAY_DIRECT_UPLOAD_MAX_BYTES) or 0),
    )


def _relay_chunk_bytes() -> int:
    return max(
        1,
        int(os.environ.get("RYNMESH_RELAY_CHUNK_BYTES", DEFAULT_RELAY_CHUNK_BYTES) or DEFAULT_RELAY_CHUNK_BYTES),
    )


class _SuppressOSError:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, OSError)
