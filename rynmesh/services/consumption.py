"""Local reading, playback, bookmark, and completion state for the owner."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from ..atomic_io import atomic_write_json, migration_backup, read_json
from ..crypto import canonical_json
from ..device_sync.reading import ReadingState
from ..device_sync.reading import scopes as reading_scopes
from ..device_sync.records import SyncError, fingerprint, view
from ..file_transactions import file_transaction

__all__ = ["MAX_HISTORY_BYTES", "ConsumptionError", "ConsumptionStore"]

_ACTIONS = {"opened", "bookmark", "unbookmark", "progress", "completed"}
# This store writes its whole history as one JSON record, so its own bound
# (max_items, below) and its own per-field caps (_ITEM_FIELDS' 4000-char
# string fields, tags/reasons at 64 entries of 160 chars) must stay under
# this store's own cap, not atomic_io.MAX_RECORD_BYTES (that 16 MiB default
# is a generic per-record ceiling atomic_io picked to bound memory/disk; it
# says nothing about how much reading history a node should keep, so this
# store gets its own explicit budget instead of borrowing that one).
#
# Worst case at max_items=1000, every field _clean_item actually truncates
# maxed out: 12 string fields (including `link`) at 4000 chars + tags/reasons
# (2 x 64 entries x 160 chars) + item_id at its real 256-char cap (appearing
# twice per record: top-level and nested in `item`), x1000 records, plus
# JSON structure/indent overhead measures to ~68.2 MiB (see
# tests/test_consumption.py::test_consumption_store_worst_case_stays_under_atomic_cap,
# which builds exactly this — item_id and link included at their real caps,
# not shortened — and asserts it against MAX_HISTORY_BYTES). That leaves
# ~40% headroom under the 96 MiB cap below (worst case is ~71% of the cap).
# If you raise `max_items` or any `_ITEM_FIELDS` truncation length, re-run
# that test and raise MAX_HISTORY_BYTES together with it — the two move as a
# pair, and the test's own field-maxing must be kept in sync with whatever
# `_clean_item` actually truncates.
MAX_HISTORY_BYTES = 96 * 1024 * 1024  # 96 MiB; measured true worst case at 1000 items is ~68.2 MiB
PREVIOUS_SYNC_VERSION = "ryn.consumption.v2"
SYNC_VERSION = "ryn.consumption.v3"
PRIVACY_VERSION = "ryn.consumption.v4"
MAX_SYNC_HISTORY_BYTES = 256 * 1024 * 1024
_ITEM_FIELDS = {
    "item_id",
    "source_id",
    "source_title",
    "source_kind",
    "title",
    "link",
    "summary",
    "ai_summary",
    "author",
    "thumbnail",
    "media_url",
    "content_kind",
    "content_type",
    "tags",
    "published_unix",
    "score",
    "reasons",
}


class ConsumptionError(ValueError):
    """Raised when consumption state cannot be safely recorded."""


class ConsumptionStore:
    """Atomic, bounded history stored only beneath the local node home."""

    def __init__(self, path: str | Path, *, max_items: int = 1000) -> None:
        self.path = Path(path)
        self.max_items = max(1, int(max_items))
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._sync_validation = set()
        self._privacy_validation_digest = None

    def list(self) -> list[dict[str, Any]]:
        records = list(self._load().values())
        return sorted(
            records,
            key=lambda record: float(record.get("last_activity_unix", 0.0) or 0.0),
            reverse=True,
        )

    def record(
        self,
        item: Mapping[str, Any],
        action: str,
        *,
        progress: float | None = None,
        now_unix: float | None = None,
        content_version: str | None = None,
        expected_sync_revision: str | None = None,
    ) -> dict[str, Any]:
        with file_transaction(self.lock_path):
            return self._record(item, action, progress=progress, now_unix=now_unix,
                                content_version=content_version, expected_sync_revision=expected_sync_revision)

    def _record(self, item: Mapping[str, Any], action: str, *, progress: float | None,
                now_unix: float | None, content_version: str | None,
                expected_sync_revision: str | None) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action not in _ACTIONS:
            raise ConsumptionError("consumption_action_invalid")
        clean_item = self._clean_item(item)
        item_id = clean_item["item_id"]
        stamp = time.time() if now_unix is None else float(now_unix)
        if not math.isfinite(stamp) or stamp < 0:
            raise ConsumptionError("consumption_timestamp_invalid")
        document, local, sync = self._document()
        # Only this item's derived fields participate in the edit. Other local
        # rows remain intact and remote-only rows remain in the causal source;
        # projecting every synced item twice made one scroll write scale with
        # the full synchronized library's display work.
        records = dict(local)
        if sync:
            target = {item_id: records[item_id]} if item_id in records else {}
            records.update(sync.project(target, identifiers=[item_id]))
        record = dict(
            records.get(
                item_id,
                {
                    "item_id": item_id,
                    "first_opened_unix": 0.0,
                    "last_opened_unix": 0.0,
                    "open_count": 0,
                    "bookmarked": False,
                    "progress": 0.0,
                    "completed": False,
                },
            )
        )
        record["item"] = clean_item
        if content_version is not None:
            if not isinstance(content_version, str) or len(content_version) > 256 or any(ord(c) < 32 for c in content_version):
                raise ConsumptionError("consumption_content_version_invalid")
            record["content_version"] = content_version
        record["last_activity_unix"] = stamp
        if action == "opened":
            if not record.get("first_opened_unix"):
                record["first_opened_unix"] = stamp
            record["last_opened_unix"] = stamp
            record["open_count"] = int(record.get("open_count", 0) or 0) + 1
        elif action == "bookmark":
            record["bookmarked"] = True
        elif action == "unbookmark":
            record["bookmarked"] = False
        elif action == "progress":
            record["progress"] = self._clean_progress(progress)
            if record["progress"] >= 0.95:
                record["completed"] = True
        elif action == "completed":
            record["progress"] = 1.0
            record["completed"] = True
        scope = "bookmarks" if action in {"bookmark", "unbookmark"} else "reading"
        if sync:
            # Opening an existing position is a local history event, not a new
            # position. In particular it must not create a third conflict head
            # or supersede a position received while the reader was loading.
            position = sync.value['entities'].get(sync.key('reading', item_id))
            existing_position = position is not None and any(head['value'] is not None for head in position['record']['heads'])
            if action != "opened" or not existing_position:
                sync.capture(record, scope, expected_revision=expected_sync_revision)
        elif expected_sync_revision is not None:
            raise SyncError("sync_not_enabled")
        records[item_id] = record
        ordered = sorted(
            records.values(),
            key=lambda value: float(value.get("last_activity_unix", 0.0) or 0.0),
            reverse=True,
        )[: self.max_items]
        # Derived conflict/revision fields never become an independent source.
        saved = {str(value["item_id"]): {key: item for key, item in value.items()
                 if key not in {"sync_revisions", "sync_conflicts", "sync_reading_available"}} for value in ordered}
        self._save_document(document, saved, sync)
        return sync.project({item_id: record}, identifiers=[item_id])[item_id] if sync else record

    def clear(self) -> None:
        with file_transaction(self.lock_path):
            document, _, sync = self._document()
            if sync:
                sync.clear()
            self._save_document(document, {}, sync)

    def _load(self) -> dict[str, dict[str, Any]]:
        with file_transaction(self.lock_path):
            _, local, sync = self._document()
            return sync.project(local) if sync else local

    def _document(self):
        payload = read_json(self.path, max_bytes=MAX_SYNC_HISTORY_BYTES) if self.path.exists() else {}
        if not isinstance(payload, dict):
            raise ConsumptionError("consumption_history_invalid")
        if isinstance(payload.get("version"), str):
            if payload["version"] not in {PREVIOUS_SYNC_VERSION, SYNC_VERSION, PRIVACY_VERSION}:
                raise SyncError("sync_version_unsupported")
            if not isinstance(payload.get("records"), dict) or "sync" not in payload:
                raise ConsumptionError("consumption_history_invalid")
            if payload['version'] == PRIVACY_VERSION:
                from .reading_privacy import validate_receipt

                receipt = payload.get('privacy_erasure')
                # A receipt is unchanged across ordinary reading writes. Cache
                # only one fully validated content digest, never a parsed value
                # or file timestamp; changed bytes must pass validation again.
                digest = fingerprint(receipt)
                if digest != self._privacy_validation_digest:
                    validate_receipt(receipt)
                    self._privacy_validation_digest = digest
                if payload['sync'] is None:
                    return payload, payload['records'], None
                if not isinstance(payload['sync'], dict) or payload['sync'].get('actor') != receipt['actor']:
                    raise SyncError('sync_device_identity_changed')
            value = ReadingState.decode_compact(payload["sync"]) if payload["version"] != PREVIOUS_SYNC_VERSION else payload["sync"]
            return payload, payload["records"], ReadingState(value, validation_cache=self._sync_validation)
        return payload, {
            str(key): dict(value)
            for key, value in payload.items()
            if isinstance(value, dict)
        }, None

    def _save_document(self, document, local, sync):
        if document.get('version') == PRIVACY_VERSION:
            atomic_write_json(self.path, {**document, 'records': local, 'sync': sync.encode_compact() if sync else None},
                              sort_keys=False, ensure_ascii=False, separators=(',', ':'), max_bytes=MAX_SYNC_HISTORY_BYTES)
            return
        if sync:
            if document.get('version') == PREVIOUS_SYNC_VERSION:
                if migration_backup(self.path, suffix='.v2.migrated', max_bytes=MAX_SYNC_HISTORY_BYTES) is None:
                    raise ConsumptionError('consumption_backup_failed')
            atomic_write_json(self.path, {**document, "version": SYNC_VERSION, "records": local, "sync": sync.encode_compact()},
                              sort_keys=False, ensure_ascii=False, separators=(',', ':'), max_bytes=MAX_SYNC_HISTORY_BYTES)
            document['version'] = SYNC_VERSION
        else:
            self._write(local)

    def enable_sync(self, actor, scopes):
        """Opt in after device consent; capture persists even while transfer pauses."""
        with file_transaction(self.lock_path):
            document, local, sync = self._document()
            self._enable_sync(document, local, sync, actor, scopes)

    def _enable_sync(self, document, local, sync, actor, scopes):
        if document.get('version') == PRIVACY_VERSION:
            from .reading_privacy import seed_barriers

            if actor != document['privacy_erasure']['actor']:
                raise SyncError('sync_device_identity_changed')
            if sync is not None and reading_scopes(scopes) <= set(sync.value['scopes']):
                return document, local, sync
            sync = seed_barriers(document['privacy_erasure'], sync, actor, scopes, local)
            self._save_document(document, local, sync)
            return document, local, sync
        if sync is not None and sync.value['actor'] == actor and reading_scopes(scopes) <= set(sync.value['scopes']):
            return document, local, sync
        if sync is None:
            sync = ReadingState.create(actor)
            sync.enable(actor, scopes, local)
            if self.path.exists() and migration_backup(self.path, max_bytes=MAX_HISTORY_BYTES) is None:
                raise ConsumptionError("consumption_backup_failed")
            document = {"legacy_metadata": {key: value for key, value in document.items() if not isinstance(value, dict)}}
        else:
            sync.enable(actor, scopes, local)
        self._save_document(document, local, sync)
        return document, local, sync

    def sync_export(self, scopes):
        with file_transaction(self.lock_path):
            _, _, sync = self._document()
            if sync is None:
                raise SyncError("sync_not_enabled")
            return sync.export(scopes)

    def sync_snapshot(self, scopes, *, expected_actor, enable=False):
        """Validate the source once for an identity-bound bridge snapshot."""
        with file_transaction(self.lock_path):
            document, local, sync = self._document()
            if enable:
                _, _, sync = self._enable_sync(document, local, sync, expected_actor, scopes)
            if sync is None:
                raise SyncError("sync_not_enabled")
            if sync.value['actor'] != expected_actor:
                raise SyncError("sync_device_identity_changed")
            return sync.export(scopes)

    def sync_identity(self):
        with file_transaction(self.lock_path):
            _, _, sync = self._document()
            if sync is None:
                raise SyncError("sync_not_enabled")
            return sync.value['actor']

    def sync_read(self, scope, identifier):
        with file_transaction(self.lock_path):
            _, _, sync = self._document()
            if sync is None or scope not in sync.value['scopes']:
                raise SyncError("sync_scope_denied")
            entity = sync.value['entities'].get(sync.key(scope, identifier))
            return view(scope, identifier, entity['record']) if entity else None

    def sync_receive(self, rows, *, scopes, expected_actor=None, enable=False):
        """Confirm only after source fields and their causal state commit together."""
        from ..device_sync.store import MAX_BATCH, MAX_BATCH_BYTES

        if not isinstance(rows, list) or len(rows) > MAX_BATCH or len(canonical_json(rows)) > MAX_BATCH_BYTES:
            raise SyncError("sync_batch_limit")
        with file_transaction(self.lock_path):
            document, local, sync = self._document()
            if enable:
                document, local, sync = self._enable_sync(document, local, sync, expected_actor, scopes)
            if sync is None:
                raise SyncError("sync_not_enabled")
            if expected_actor is not None and sync.value['actor'] != expected_actor:
                raise SyncError("sync_device_identity_changed")
            receipts = sync.merge(rows, scopes)
            # Validate the actual projected source before acknowledging a batch.
            # Unchanged entities were validated by _document and are not part
            # of this commit's projection. Rechecking the whole library for
            # every 100-row batch made initial transfer quadratic.
            identifiers = {row['id'] for row in rows}
            sync.project({key: value for key, value in local.items() if key in identifiers}, identifiers=identifiers)
            self._save_document(document, local, sync)
            return receipts

    def sync_issues(self):
        with file_transaction(self.lock_path):
            _, _, sync = self._document()
            return sync.issues() if sync else []

    def sync_resolve(self, scope, identifier, *, choice_id, expected_revision):
        with file_transaction(self.lock_path):
            document, local, sync = self._document()
            if sync is None:
                raise SyncError('sync_not_enabled')
            result = sync.resolve(scope, identifier, choice_id=choice_id, expected_revision=expected_revision)
            sync.project({identifier: local[identifier]} if identifier in local else {}, identifiers=[identifier])
            self._save_document(document, local, sync)
            return result

    def _write(self, payload: Mapping[str, Any]) -> None:
        atomic_write_json(
            self.path, dict(payload), indent=2, sort_keys=True, ensure_ascii=False,
            max_bytes=MAX_HISTORY_BYTES,
        )

    @staticmethod
    def _clean_progress(value: float | None) -> float:
        try:
            if not math.isfinite(float(value)):
                raise ValueError
            return round(min(1.0, max(0.0, float(value))), 4)
        except (TypeError, ValueError):
            raise ConsumptionError("consumption_progress_invalid") from None

    @staticmethod
    def _clean_item(item: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(item, Mapping):
            raise ConsumptionError("consumption_item_invalid")
        item_id = str(item.get("item_id", "") or "").strip()[:256]
        if not item_id:
            raise ConsumptionError("consumption_item_id_required")
        clean: dict[str, Any] = {"item_id": item_id}
        for key in _ITEM_FIELDS - {"item_id"}:
            value = item.get(key)
            if key in {"tags", "reasons"}:
                clean[key] = [str(entry)[:160] for entry in value[:64]] if isinstance(value, list) else []
            elif key in {"published_unix", "score"}:
                try:
                    clean[key] = float(value or 0.0)
                except (TypeError, ValueError):
                    clean[key] = 0.0
            else:
                clean[key] = str(value or "")[:4000]
        link = clean.get("link", "")
        if not link.startswith(("http://", "https://")) and link != f"rynmesh://content/{quote(item_id, safe='')}":
            raise ConsumptionError("consumption_item_link_invalid")
        return clean
