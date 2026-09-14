"""Persistent milestones for the owner's first useful Ryn session.

The record intentionally contains only booleans and timestamps. Content titles,
URLs, feedback text, and other personal material belong to their existing local
stores and are never duplicated here.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from rynmesh.atomic_io import atomic_write_json, migration_backup, read_json

__all__ = ["FIRST_SUCCESS_VERSION", "FirstSuccessStore"]

FIRST_SUCCESS_VERSION = "ryn.first-success.v1"
MAX_RECORD_BYTES = 64 * 1024
_MILESTONES = (
    "node_ready",
    "content_ready",
    "first_item_opened",
    "first_signal_recorded",
    "completed",
)


def _stamp(value: Any) -> float:
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _empty() -> dict[str, Any]:
    return {
        "version": FIRST_SUCCESS_VERSION,
        "dismissed": False,
        "dismissed_at_unix": 0.0,
        "replay_started_at_unix": 0.0,
        "milestones": dict.fromkeys(_MILESTONES, 0.0),
    }


class FirstSuccessStore:
    """Atomic, monotonic local record of first-success milestones."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    def record(self, milestone: str, *, now_unix: float | None = None) -> dict[str, Any]:
        name = str(milestone or "").strip()
        if name not in _MILESTONES:
            raise ValueError("first_success_milestone_invalid")
        stamp = _stamp(time.time() if now_unix is None else now_unix)
        with self._lock:
            data = self._load()
            if not float(data["milestones"].get(name, 0.0) or 0.0):
                data["milestones"][name] = stamp
                self._write(data)
            return data

    def sync(
        self,
        *,
        node_ready: bool,
        content_ready: bool,
        first_item_opened: bool,
        first_signal_recorded: bool,
        now_unix: float | None = None,
    ) -> dict[str, Any]:
        """Persist newly observed milestones without ever moving progress backwards."""
        stamp = _stamp(time.time() if now_unix is None else now_unix)
        observed = {
            "node_ready": bool(node_ready),
            "content_ready": bool(content_ready),
            "first_item_opened": bool(first_item_opened),
            "first_signal_recorded": bool(first_signal_recorded),
        }
        with self._lock:
            data = self._load()
            changed = False
            for name, ready in observed.items():
                if ready and not float(data["milestones"].get(name, 0.0) or 0.0):
                    data["milestones"][name] = stamp
                    changed = True
            complete = all(
                float(data["milestones"].get(name, 0.0) or 0.0)
                for name in ("first_item_opened", "first_signal_recorded")
            )
            if complete and not float(data["milestones"].get("completed", 0.0) or 0.0):
                data["milestones"]["completed"] = stamp
                changed = True
            if changed:
                self._write(data)
            return data

    def dismiss(self, *, now_unix: float | None = None) -> dict[str, Any]:
        stamp = _stamp(time.time() if now_unix is None else now_unix)
        with self._lock:
            data = self._load()
            data["dismissed"] = True
            if not float(data.get("dismissed_at_unix", 0.0) or 0.0):
                data["dismissed_at_unix"] = stamp
            self._write(data)
            return data

    def reset(self, *, now_unix: float | None = None) -> dict[str, Any]:
        stamp = _stamp(time.time() if now_unix is None else now_unix)
        with self._lock:
            data = self._load()
            data.update(_empty())
            data["replay_started_at_unix"] = stamp
            self._write(data)
            return data

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty()
        if self.path.stat().st_size > MAX_RECORD_BYTES:
            raise ValueError("first_success_record_too_large")
        # A malformed snapshot is recoverable, but preserve it before rebuilding.
        raw = read_json(self.path, default=None, max_bytes=MAX_RECORD_BYTES)
        if not isinstance(raw, dict):
            migration_backup(self.path, suffix=".corrupt")
            data = _empty()
            self._write(data)
            return data
        if raw.get("version") != FIRST_SUCCESS_VERSION:
            # A downgrade must never destroy a newer record, including on reset.
            raise ValueError("first_success_version_unsupported")
        data = {**_empty(), **raw}
        data["dismissed"] = bool(raw.get("dismissed", False))
        data["dismissed_at_unix"] = _stamp(raw.get("dismissed_at_unix"))
        data["replay_started_at_unix"] = _stamp(raw.get("replay_started_at_unix"))
        raw_milestones = raw.get("milestones", {})
        data["milestones"] = dict(raw_milestones) if isinstance(raw_milestones, dict) else {}
        for name in _MILESTONES:
            data["milestones"][name] = _stamp(data["milestones"].get(name))
        return data

    def _write(self, data: Mapping[str, Any]) -> None:
        atomic_write_json(
            self.path, dict(data), indent=2, max_bytes=MAX_RECORD_BYTES,
        )
