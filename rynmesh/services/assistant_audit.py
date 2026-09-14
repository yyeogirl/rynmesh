"""Bounded local audit trail for personal-assistant actions."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from ..atomic_io import atomic_write_json

__all__ = ["AssistantAuditStore"]


class AssistantAuditStore:
    """Append-only-at-the-API audit history stored beneath ``RYNMESH_HOME``."""

    def __init__(self, path: str | Path, *, max_events: int = 2000) -> None:
        self.path = Path(path)
        self.max_events = max(1, int(max_events))
        self._lock = threading.Lock()

    def append(
        self,
        kind: str,
        text: str,
        *,
        details: Mapping[str, Any] | None = None,
        item_id: str = "",
        now_unix: float | None = None,
    ) -> dict[str, Any]:
        stamp = time.time() if now_unix is None else float(now_unix)
        event = {
            "id": "audit_" + uuid.uuid4().hex[:12],
            "timestamp_unix": stamp,
            "kind": str(kind or "verify")[:32],
            "text": str(text or "")[:500],
            "details": self._clean(dict(details or {})),
        }
        if item_id:
            event["itemId"] = str(item_id)[:256]
        with self._lock:
            events = self._load()
            events.insert(0, event)
            self._write(events[: self.max_events])
        return event

    def list(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            events = self._load()
        return events[: max(0, int(limit))] if limit is not None else events

    def _load(self) -> list[dict[str, Any]]:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(loaded, list):
            return []
        return [dict(value) for value in loaded if isinstance(value, dict)]

    def clear(self) -> None:
        with self._lock:
            self._write([])

    def _write(self, events: list[dict[str, Any]]) -> None:
        atomic_write_json(self.path, events, indent=2, sort_keys=True)

    @classmethod
    def _clean(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key)[:128]: cls._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._clean(item) for item in value[:100]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value if not isinstance(value, str) else value[:1000]
        return str(value)[:1000]
