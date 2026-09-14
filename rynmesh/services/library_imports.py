"""Private managed documents, adapted from the product-suite import contract.

Metadata is the commit marker. Bytes and extracted text use the shared atomic
writer; extraction runs in the existing supervised document child. A failed
import is retryable without deleting another in-flight import's files.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
import time
from functools import wraps
from pathlib import Path
from typing import Any, Mapping

from ..atomic_io import atomic_write_bytes, atomic_write_json, migration_backup, read_json
from ..file_transactions import file_transaction
from .document_extract import extract_document

MAX_IMPORT_BYTES = 32 * 1024 * 1024
MAX_IMPORT_TOTAL_BYTES = 256 * 1024 * 1024
VERSION = "ryn.library-import.v2"
_ID = re.compile(r"^imp_[a-f0-9]{32}(?:[a-f0-9]{32})?$")
_SUFFIXES = {".txt", ".md", ".markdown", ".pdf"}


class LibraryImportError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _locked(method):
    @wraps(method)
    def read(self, *args, **kwargs):
        with file_transaction(self.root / ".imports.lock"):
            return method(self, *args, **kwargs)
    return read


class LibraryImportStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _directory(self, import_id: str) -> Path:
        if not _ID.fullmatch(import_id):
            raise LibraryImportError("library_import_not_found")
        directory = (self.root / import_id).resolve()
        if directory.parent != self.root or directory.name != import_id:
            raise LibraryImportError("library_import_not_found")
        return directory

    def generation(self) -> int:
        from .library_cleanup import control
        with file_transaction(self.root / ".imports.lock"):
            return control(self)["generation"]

    def remove(self, import_id: str | None = None) -> dict[str, int]:
        """Internal explicit cleanup. HTTP clients must supply a reviewed token."""
        from .library_cleanup import LibraryCleanup
        cleanup = LibraryCleanup(self)
        with file_transaction(self.root / ".imports.lock"):
            review = cleanup.preview(import_id)
            result = cleanup.begin(review_token=review['review_token'], scope=import_id)
            return {key: result[key] for key in ('removed', 'generation')}

    @_locked
    def get(self, import_id: str) -> dict[str, Any]:
        from .library_cleanup import pending_target
        if pending_target(self, import_id):
            raise LibraryImportError("library_cleanup_pending")
        directory = self._directory(import_id)
        if not (directory / "metadata.json").is_file():
            raise LibraryImportError("library_import_not_found")
        record = read_json(directory / "metadata.json", max_bytes=65536)
        if not isinstance(record, dict) or record.get("version") not in {VERSION, "ryn.library-import.v1"}:
            raise LibraryImportError("library_import_version_unsupported")
        if record.get("import_id") != import_id:
            raise LibraryImportError("library_import_corrupt")
        return record

    @_locked
    def list(self) -> list[dict[str, Any]]:
        from .library_cleanup import control
        job = control(self).get('cleanup')
        hidden = set(job['targets']) if job and job['done'] == ['source'] else set()
        records = []
        for path in self.root.glob("imp_*/metadata.json"):
            if path.parent.name in hidden:
                continue
            try:
                row = self.get(path.parent.name)
                stamp = row.get('created_at_unix', 0)
                if type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp < 0:
                    raise LibraryImportError('library_import_corrupt')
                records.append(row)
            except (OSError, ValueError):
                records.append({"import_id": path.parent.name, "state": "unavailable", "filename": "Unavailable saved document", "created_at_unix": 0})
        return sorted(records, key=lambda row: float(row.get("created_at_unix", 0)), reverse=True)

    def _blob(self, record: Mapping[str, Any]) -> Path:
        name = str(record.get("blob_name", "original.bin"))
        if name not in {"original.bin", *("original" + suffix for suffix in _SUFFIXES)}:
            raise LibraryImportError("library_import_corrupt")
        directory = self._directory(str(record["import_id"]))
        path = (directory / name).resolve()
        if path.parent != directory:
            raise LibraryImportError("library_import_corrupt")
        return path

    @_locked
    def read_bytes(self, import_id: str) -> bytes:
        record = self.get(import_id)
        with self._blob(record).open("rb") as handle:
            data = handle.read(MAX_IMPORT_BYTES + 1)
        if len(data) > MAX_IMPORT_BYTES or len(data) != record.get("size_bytes") or hashlib.sha256(data).hexdigest() != record.get("sha256"):
            raise LibraryImportError("library_import_hash_mismatch")
        return data

    @_locked
    def body(self, import_id: str) -> dict[str, Any]:
        record = self.get(import_id)
        self.read_bytes(import_id)
        payload = read_json(self._directory(import_id) / "extracted.json", max_bytes=8 * 1024 * 1024)
        if isinstance(payload, dict) and payload.get("version") != 1:
            raise LibraryImportError("library_import_version_unsupported")
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise LibraryImportError("library_import_corrupt")
        if record.get("extracted_sha256") and hashlib.sha256(payload["text"].encode()).hexdigest() != record["extracted_sha256"]:
            raise LibraryImportError("library_import_hash_mismatch")
        return {"text": payload["text"], "truncated": record.get("extraction_status") == "truncated" or (record.get("source") or {}).get("content_truncated") is True,
                "filename": record["filename"], "mime": record["mime"]}

    def extracted_text(self, import_id: str) -> str:
        return self.body(import_id)["text"]

    def import_bytes(self, body: Mapping[str, Any]) -> dict[str, Any]:
        encoded = body.get("data_base64", "")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMPORT_BYTES + 2) // 3) * 4:
            raise LibraryImportError("library_import_size_limit")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise LibraryImportError("library_import_invalid_base64") from None
        return self.save(data, filename=str(body.get("filename", "document.txt")), mime=str(body.get("mime", "text/plain")))

    def save(self, data: bytes, *, filename: str, mime: str, source: Mapping[str, Any] | None = None,
             repair: bool = False, expected_generation: int | None = None) -> dict[str, Any]:
        if not data or len(data) > MAX_IMPORT_BYTES:
            raise LibraryImportError("library_import_size_limit")
        filename = Path(filename.replace("\\", "/")).name[:180]
        suffix = Path(filename).suffix.lower()
        mime = mime.lower().split(";", 1)[0].strip()
        if suffix not in _SUFFIXES:
            raise LibraryImportError("library_import_type_unsupported")
        if suffix == ".pdf":
            if mime != "application/pdf" or not data.startswith(b"%PDF-"):
                raise LibraryImportError("library_import_mime_mismatch")
        elif mime not in {"text/plain", "text/markdown", "application/octet-stream"} or data.startswith(b"%PDF-"):
            raise LibraryImportError("library_import_mime_mismatch")
        digest = hashlib.sha256(data).hexdigest()
        # Keep attribution attached to its source; equal bytes received from a
        # different friend are not silently assigned the first friend's origin.
        from ..crypto import canonical_json
        origin = {key: str(value)[:2048] for key, value in (source or {}).items() if key in {"peer_id", "card_id", "title", "source_url", "publisher_peer_id"}}
        if (source or {}).get('content_truncated') is True:
            origin['content_truncated'] = True
        identity = hashlib.sha256(canonical_json({"sha256": digest, "mime": mime, "suffix": suffix, "source": origin})).hexdigest()
        import_id = "imp_" + identity
        directory = self._directory(import_id)
        with file_transaction(self.root / ".imports.lock"):
            generation = self.generation()
            if expected_generation is not None and expected_generation != generation:
                raise LibraryImportError("library_import_cancelled_by_cleanup")
            from .library_cleanup import pending_target
            if pending_target(self, import_id):
                raise LibraryImportError("library_cleanup_pending")
            prior: dict[str, Any] = {}
            prior_extracted: dict[str, Any] = {}
            if (directory / "metadata.json").exists():
                try:
                    prior = self.get(import_id)
                except (OSError, LibraryImportError) as exc:
                    if not repair or str(exc) == "library_import_version_unsupported":
                        raise
                try:
                    self.body(import_id)
                    return prior
                except (OSError, LibraryImportError) as exc:
                    if not repair or str(exc) == "library_import_version_unsupported":
                        raise
                extracted_path = directory / "extracted.json"
                if extracted_path.exists():
                    stored = read_json(extracted_path, default={}, max_bytes=8 * 1024 * 1024)
                    if isinstance(stored, dict):
                        if stored and stored.get("version") != 1:
                            raise LibraryImportError("library_import_version_unsupported")
                        prior_extracted = stored
                    migration_backup(extracted_path, suffix=".repaired")
                migration_backup(directory / "metadata.json", suffix=".repaired")
            existing = list(self.root.glob("imp_*/original.*"))
            if len(existing) >= 2000:
                raise LibraryImportError("library_import_quota")
            total = sum(path.stat().st_size for path in existing if path.parent != directory)
            if total + len(data) > MAX_IMPORT_TOTAL_BYTES:
                raise LibraryImportError("library_import_quota")
            blob = directory / ("original" + suffix)
            atomic_write_bytes(blob, data, max_bytes=MAX_IMPORT_BYTES)
            extracted = extract_document(blob, max_input_bytes=MAX_IMPORT_BYTES)
            if extracted["status"] not in {"parsed", "truncated"} or not extracted.get("text", "").strip():
                raise LibraryImportError("library_import_extract_unavailable")
            text = str(extracted["text"])
            atomic_write_json(directory / "extracted.json", {**prior_extracted, "version": 1, "text": text}, max_bytes=8 * 1024 * 1024)
            record = {**prior, "version": VERSION, "import_id": import_id, "filename": filename, "blob_name": blob.name,
                      "mime": mime, "kind": extracted["kind"], "state": "ready", "size_bytes": len(data),
                      "sha256": digest, "extracted_sha256": hashlib.sha256(text.encode()).hexdigest(),
                      "extraction_status": extracted["status"], "extracted_bytes": len(text.encode()),
                      "created_at_unix": prior.get("created_at_unix", time.time()), "source": origin}
            atomic_write_json(directory / "metadata.json", record, max_bytes=65536)
            return record
