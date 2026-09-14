"""Rebuildable encrypted index; current source records gate every result.

The source callback must read local data only and exclude inaccessible records.
It is also called at query time: an old index is never authority to disclose a
deleted body or revoked share. No query or plaintext index is written to disk.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Callable, Iterable

from cryptography.exceptions import InvalidTag

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction
from ..services import peer_box

VERSION = "ryn.local-search.v1"
CHANNEL = b"rynmesh-local-search-v1"
MAX_PLAINTEXT = 64 * 1024 * 1024
MAX_FILE = 90 * 1024 * 1024
MAX_DOCUMENTS = 100_000
MAX_POSTINGS = 1_000_000
KINDS = frozenset({"saved", "history", "share", "chat"})
SOURCE_SCOPES = frozenset({'saved_documents', 'offline_downloads'})


class SearchSnapshot(list):
    """Local rows plus bounded scope codes, never exceptions or private paths."""
    def __init__(self, rows=(), *, unavailable_sources=()):
        super().__init__(rows)
        self.unavailable_sources = sorted(set(unavailable_sources))


class SearchError(ValueError):
    pass


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _terms(query: str) -> list[str]:
    if not isinstance(query, str) or len(query.encode()) > 512:
        raise SearchError("search_query_invalid")
    terms = list(dict.fromkeys(query.casefold().split()))
    if len(terms) > 16:
        raise SearchError("search_query_invalid")
    return terms


def _grams(text: str, size: int) -> set[str]:
    return {text[i:i + size] for i in range(len(text) - size + 1)}


def _snippet(text: str, terms: list[str]) -> dict:
    # casefold can expand characters (Straße -> strasse); keep original offsets
    # so the UI can highlight literal text without injecting HTML.
    normalized = text.casefold()
    hits = [(normalized.find(term), len(term)) for term in terms if term in normalized]
    if len(normalized) == len(text):
        matches = [(start, start + size) for start, size in hits]
    else:
        # Keep only match boundaries, not an integer offset for every byte of
        # a multi-megabyte document.
        pending = sorted({point for start, size in hits for point in (start, start + size - 1)}, reverse=True)
        positions, offset = {}, 0
        for index, char in enumerate(text):
            end = offset + len(char.casefold())
            while pending and pending[-1] < end:
                positions[pending.pop()] = index
            offset = end
            if not pending:
                break
        matches = [(positions[start], positions[start + size - 1] + 1) for start, size in hits]
    anchor = min((start for start, _ in matches), default=0)
    left = max(0, anchor - 50)
    right = min(len(text), left + 220)
    ranges = sorted({(max(left, start) - left, min(right, end) - left)
                     for start, end in matches if start < right and end > left})
    return {"text": text[left:right], "matches": [list(item) for item in ranges],
            "offset_unit": "unicode_codepoints", "prefix_omitted": left > 0, "suffix_omitted": right < len(text)}


def _documents(rows: Iterable[dict]) -> dict[str, dict]:
    result = {}
    total = 0
    for value in rows:
        if not isinstance(value, dict):
            raise SearchError("search_source_invalid")
        row = deepcopy(value)
        for key, limit in (("id", 512), ("title", 2048), ("text", 8 * 1024 * 1024), ("source", 2048)):
            if not isinstance(row.get(key), str) or len(row[key].encode()) > limit:
                raise SearchError("search_source_invalid")
        if not row["id"] or row["id"] in result:
            raise SearchError("search_source_invalid")
        if (not isinstance(row.get("kinds"), list) or not row["kinds"]
                or any(kind not in KINDS for kind in row["kinds"])):
            raise SearchError("search_source_invalid")
        if (not isinstance(row.get("friend_ids", []), list)
                or any(not isinstance(peer, str) or len(peer) > 256 for peer in row.get("friend_ids", []))):
            raise SearchError("search_source_invalid")
        stamp = row.get("timestamp")
        if type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp < 0:
            raise SearchError("search_source_invalid")
        # Targets are internal app routes, never URLs supplied by remote content.
        targets = row.get("targets")
        if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
            raise SearchError("search_source_invalid")
        for target in targets:
            if (not isinstance(target, dict) or not isinstance(target.get("label"), str)
                    or len(target["label"]) > 128 or not isinstance(target.get("href"), str)
                    or not target["href"].startswith("/") or target["href"].startswith("//")
                    or "\\" in target["href"] or len(target["href"]) > 4096
                    or any(ord(char) < 32 for char in target["href"])):
                raise SearchError("search_source_invalid")
        total += len(_json(row))
        if total > MAX_PLAINTEXT - 4096 or len(result) >= MAX_DOCUMENTS:
            raise SearchError("search_index_limit")
        result[row["id"]] = row
    return result


class LocalSearchIndex:
    def __init__(self, root: str | Path, *, messaging_key, source: Callable[[], Iterable[dict]]):
        self.root = Path(root)
        self.path = self.root / "index.json"
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)
        self.source = source
        self.lock = threading.RLock()
        self.writer = threading.Lock()
        self.rows: dict[str, dict] = {}
        self.fingerprints: dict[str, str] = {}
        self.postings: dict[str, set[str]] | None = {}
        self.generation = ""
        self.state = "needs_rebuild"
        self.error = ""
        self.updated_at = None
        self.unavailable_sources = []
        try:
            _, data = self._read()
            if data:
                self._activate(_documents(data["documents"]), data["generation"], data["updated_at"])
        except SearchError as exc:
            self.error = str(exc)
            if self.error == "search_index_version_unsupported":
                self.state = "unsupported"

    def _read(self) -> tuple[dict, dict]:
        if not self.path.exists():
            return {}, {}
        try:
            envelope = read_json(self.path, max_bytes=MAX_FILE)
            if not isinstance(envelope, dict):
                raise ValueError
            if envelope.get("version") != VERSION:
                raise SearchError("search_index_version_unsupported")
            raw = peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
            if len(raw) > MAX_PLAINTEXT:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            if data.get("version") != VERSION:
                raise SearchError("search_index_version_unsupported")
            if (not isinstance(data.get("generation"), str) or len(data["generation"]) != 32
                    or type(data.get("updated_at")) not in {int, float}
                    or not math.isfinite(data["updated_at"]) or not isinstance(data.get("documents"), list)):
                raise ValueError
            return envelope, data
        except SearchError:
            raise
        except (OSError, ValueError, TypeError, KeyError, InvalidTag):
            raise SearchError("search_index_unreadable") from None

    def _activate(self, rows: dict, generation: str, stamp: float) -> None:
        postings: dict[str, set[str]] | None = {}
        posting_count = 0
        for identifier, row in rows.items():
            searchable = "\n".join(row[key] for key in ("title", "source", "text")).casefold()
            seen = set()
            for gram in (searchable[i:i + size] for size in (3, 2) for i in range(len(searchable) - size + 1)):
                if gram in seen:
                    continue
                seen.add(gram)
                postings.setdefault(gram, set()).add(identifier)
                posting_count += 1
                if posting_count > MAX_POSTINGS or len(postings) > 250_000:
                    # Pathological unique text must not grow an unbounded
                    # inverted index. Full candidate scanning remains correct.
                    postings = None
                    break
            if postings is None:
                break
        fingerprints = {key: _hash(row) for key, row in rows.items()}
        with self.lock:
            self.rows, self.fingerprints, self.postings = rows, fingerprints, postings
            self.generation, self.updated_at = generation, stamp
            self.state, self.error = "ready", ""

    def status(self) -> dict:
        with self.lock:
            return {"version": VERSION, "state": self.state, "error_code": self.error,
                    "indexed_count": len(self.rows), "updated_at": self.updated_at,
                    "unavailable_sources": list(self.unavailable_sources)}

    def _source(self):
        values = self.source()
        issues = getattr(values, 'unavailable_sources', [])
        if not isinstance(issues, list) or any(not isinstance(value, str) or value not in SOURCE_SCOPES for value in issues):
            raise SearchError('search_source_invalid')
        return _documents(values), sorted(set(issues))

    def resolve(self, identifier: str) -> dict:
        try:
            current, _ = self._source()
        except Exception:
            raise SearchError("search_source_unavailable") from None
        if identifier not in current:
            raise SearchError("search_result_unavailable")
        return current[identifier]

    def rebuild(self, *, force: bool = False) -> bool:
        if not self.writer.acquire(blocking=False):
            raise SearchError("search_index_busy")
        try:
            with self.lock:
                if force or not self.generation:
                    self.state = "building"
            with file_transaction(self.root / ".index.lock"):
                try:
                    envelope, data = self._read()
                except SearchError as exc:
                    if str(exc) == "search_index_version_unsupported":
                        raise
                    envelope, data = {}, {}  # Cache only; source files are untouched.
                rows, issues = self._source()
                fingerprints = {key: _hash(row) for key, row in rows.items()}
                with self.lock:
                    unchanged = fingerprints == self.fingerprints and bool(self.generation)
                    self.unavailable_sources = issues
                if not force and unchanged and data and data["generation"] == self.generation:
                    with self.lock:
                        self.state, self.error = "ready", ""
                    return False
                with self.lock:
                    self.state = "building"
                generation, stamp = uuid.uuid4().hex, time.time()
                data = {**data, "version": VERSION, "generation": generation, "updated_at": stamp, "documents": list(rows.values())}
                plaintext = _json(data)
                if len(plaintext) > MAX_PLAINTEXT:
                    raise SearchError("search_index_limit")
                nonce, ciphertext = peer_box.seal(self.key, self.pub, plaintext, info=CHANNEL)
                atomic_write_json(self.path, {**envelope, "version": VERSION, "nonce": nonce, "ciphertext": ciphertext}, max_bytes=MAX_FILE)
                self._activate(rows, generation, stamp)
                return True
        except Exception as exc:
            code = str(exc) if isinstance(exc, SearchError) else "search_source_unavailable"
            with self.lock:
                self.state = "unsupported" if code == "search_index_version_unsupported" else "needs_rebuild"
                self.error = code
            raise SearchError(code) from None
        finally:
            self.writer.release()

    def query(self, query: str, *, kind: str = "", source: str = "", friend_id: str = "",
              after: float = 0, before: float | None = None, sort: str = "relevance",
              limit: int = 20, cursor: str = "") -> dict:
        terms = _terms(query)
        if (kind and kind not in KINDS) or sort not in {"relevance", "recent"} or type(limit) is not int or not 1 <= limit <= 100:
            raise SearchError("search_filter_invalid")
        if after is None:
            raise SearchError("search_filter_invalid")
        for stamp in (after, before):
            if stamp is not None and (type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp < 0):
                raise SearchError("search_filter_invalid")
        if before is not None and before < after:
            raise SearchError("search_filter_invalid")
        if any(not isinstance(value, str) or len(value) > 2048 for value in (source, friend_id, cursor)):
            raise SearchError("search_filter_invalid")
        if not terms:
            return {"results": [], "total": 0, "next_cursor": "", "partial": False, "indexing_pending": False,
                    "unavailable_sources": [], "index": self.status()}
        # Fail closed even when rebuilding has failed. Never fall back to old
        # snippets if the authoritative source cannot be read now.
        try:
            current, issues = self._source()
        except Exception:
            raise SearchError("search_source_unavailable") from None
        current_hashes = {key: _hash(row) for key, row in current.items()}
        with self.lock:
            indexing_pending = current_hashes != self.fingerprints or self.state != "ready"
            partial = indexing_pending or bool(issues)
            candidates = None
            for term in terms if self.postings is not None else []:
                grams = _grams(term, min(3, len(term))) if len(term) > 1 else set()
                for gram in grams:
                    posting = self.postings.get(gram, set())
                    candidates = set(posting) if candidates is None else candidates.intersection(posting)
            identifiers = list(self.rows) if candidates is None else list(candidates)
            matches = []
            for identifier in identifiers:
                row = current.get(identifier)
                if not row or current_hashes[identifier] != self.fingerprints.get(identifier):
                    continue
                if (kind and kind not in row["kinds"] or source and source != row["source"]
                        or friend_id and friend_id not in row.get("friend_ids", [])
                        or row["timestamp"] < after or before is not None and row["timestamp"] > before):
                    continue
                text = "\n".join(row[key] for key in ("title", "source", "text")).casefold()
                if all(term in text for term in terms):
                    score = sum(4 if term in row["title"].casefold() else 1 for term in terms)
                    matches.append((score, row))
            generation = self.generation
        matches.sort(key=lambda item: (-(item[0] if sort == "relevance" else 0), -item[1]["timestamp"], item[1]["id"]))
        signature = _hash([generation, terms, kind, source, friend_id, after, before, sort, limit,
                           [current_hashes[row["id"]] for _, row in matches]])
        offset = 0
        if cursor:
            try:
                value = json.loads(base64.urlsafe_b64decode(cursor))
                if value["signature"] != signature:
                    raise SearchError("search_results_changed")
                offset = value["offset"]
                if type(offset) is not int or not 0 <= offset <= len(matches):
                    raise ValueError
            except SearchError:
                raise
            except (ValueError, TypeError, KeyError):
                raise SearchError("search_cursor_invalid") from None
        results = []
        for _, row in matches[offset:offset + limit]:
            result = {key: row[key] for key in ("id", "title", "source", "timestamp", "kinds", "targets")}
            result["body_state"] = row.get("body_state", "available")
            result["text_truncated"] = bool(row.get("text_truncated"))
            result["snippet"] = _snippet(row["text"], terms)
            result["title_match"] = _snippet(row["title"], terms)
            results.append(result)
        next_offset = offset + limit
        next_cursor = base64.urlsafe_b64encode(_json({"signature": signature, "offset": next_offset})).decode() if next_offset < len(matches) else ""
        return {"results": results, "total": len(matches), "next_cursor": next_cursor, "partial": partial,
                "indexing_pending": indexing_pending, "unavailable_sources": issues, "index": self.status()}
