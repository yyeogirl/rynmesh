"""Persistent auto-update state at $RYNMESH_HOME/update-state.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json

_DEFAULTS: dict[str, Any] = {
    "pending_version": None,
    "pending_confirmed": True,
    "attempts": 0,
    "previous_wheel": None,
    "previous_version": None,
    "last_good": None,
    "quarantined": [],
    "available_version": None,
    "last_check": None,
    "last_error": None,
}


class UpdateState:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = dict(_DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text())
            if isinstance(loaded, dict):
                self.data = {**_DEFAULTS, **loaded}
        except (OSError, ValueError):
            self.data = dict(_DEFAULTS)

    def save(self) -> None:
        atomic_write_json(self.path, self.data, indent=2, sort_keys=False)

    def set(self, **kw: Any) -> None:
        self.data.update(kw)
        self.save()

    def quarantine(self, version: str) -> None:
        q = list(self.data.get("quarantined") or [])
        if version not in q:
            q.append(version)
        self.data["quarantined"] = q
        self.save()

    def is_quarantined(self, version: str) -> bool:
        return version in (self.data.get("quarantined") or [])
