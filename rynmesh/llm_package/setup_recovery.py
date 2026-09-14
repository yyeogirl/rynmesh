"""Private, bounded rollback record for a local model setup attempt.

Snapshots preserve exact configuration bytes, including unknown extensions.
Nothing returned or raised contains a path, manifest body or runtime key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from pathlib import Path

from rynmesh.atomic_io import atomic_write_bytes, atomic_write_json

from .errors import LifecycleError

MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_JOURNAL_BYTES = 6 * 1024 * 1024
RECOVERY_FAILED = "Previous configuration could not be restored. Check local storage and retry configuration."


def managed_resume_configuration(body: dict) -> dict | None:
    """Persist only bounded UI choices, never URLs, paths, keys or consent."""
    if body.get("mode") != "managed" or body.get("runtime", "auto") != "auto":
        return None
    profile = body.get("profile")
    package = body.get("package_id", "local-small")
    port = body.get("port", 18080)
    if not isinstance(profile, str) or profile not in {"light", "balanced", "quality"}:
        return None
    if not isinstance(package, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,254}", package):
        return None
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    return {"mode": "managed", "profile": profile, "package_id": package, "port": port}


class SetupRecoveryError(LifecycleError):
    pass


class SetupRecovery:
    def __init__(self, home: Path):
        self.path = home / "llm" / "setup-recovery.json"
        self.settings = home / "llm" / "provider-settings.json"
        self.pending = self.path.exists()

    def _read(self) -> dict | None:
        try:
            if not self.path.exists():
                self.pending = False
                return None
            with self.path.open("rb") as source:
                raw = source.read(MAX_JOURNAL_BYTES + 1)
            if len(raw) > MAX_JOURNAL_BYTES:
                raise ValueError
            record = json.loads(raw)
            if not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1:
                raise ValueError
            if record.get("state") not in {"pending", "committed"}:
                raise ValueError
            if record["state"] == "committed" and set(record) != {"version", "state"}:
                raise ValueError
            self.pending = record["state"] == "pending"
            return record
        except (OSError, ValueError) as exc:
            self.pending = True
            raise SetupRecoveryError(RECOVERY_FAILED) from exc

    @staticmethod
    def _snapshot(path: Path) -> dict | None:
        if not path.exists():
            return None
        with path.open("rb") as source:
            raw = source.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError
        return {"bytes": base64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest()}

    def capture(self, manifest_path: str) -> None:
        """Called before setup mutates either configuration file."""
        current = self._read()
        if current and current["state"] == "pending":
            raise SetupRecoveryError(RECOVERY_FAILED)
        try:
            target = Path(manifest_path).expanduser().resolve() if manifest_path else None
            record = {
                "version": 1, "state": "pending",
                "manifest_path": str(target) if target else "",
                "manifest": self._snapshot(target) if target else None,
                "settings": self._snapshot(self.settings),
            }
            atomic_write_json(self.path, record, max_bytes=MAX_JOURNAL_BYTES)
            self.pending = True
        except (OSError, ValueError) as exc:
            raise LifecycleError("Unable to preserve the previous configuration; setup has not started.") from exc

    @staticmethod
    def _decode(value: object) -> bytes | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"bytes", "sha256"}:
            raise ValueError
        if not isinstance(value["bytes"], str):
            raise ValueError
        raw = base64.b64decode(value["bytes"], validate=True)
        if len(raw) > MAX_CONFIG_BYTES or hashlib.sha256(raw).hexdigest() != value["sha256"]:
            raise ValueError
        return raw

    def restore(self) -> str:
        """Restore files, without claiming that the old model is running.

        Returns absent, not_needed, restored, or missing. A failed write keeps
        the original snapshot, so a new process can retry the same recovery.
        """
        record = self._read()
        if record is None or record["state"] == "committed":
            return "absent"
        try:
            if set(record) != {"version", "state", "manifest_path", "manifest", "settings"}:
                raise ValueError
            target = record["manifest_path"]
            if not isinstance(target, str) or (target and not Path(target).is_absolute()):
                raise ValueError
            manifest = self._decode(record["manifest"])
            settings = self._decode(record["settings"])
            if manifest is not None and not target:
                raise ValueError
            # Validate the entire record before touching either destination.
            if target and manifest is not None:
                atomic_write_bytes(Path(target), manifest, max_bytes=MAX_CONFIG_BYTES)
            elif target:
                Path(target).unlink(missing_ok=True)
            if settings is None:
                self.settings.unlink(missing_ok=True)
            else:
                atomic_write_bytes(self.settings, settings, max_bytes=MAX_CONFIG_BYTES)
            self.commit()
            return "restored" if manifest is not None else "missing" if target else "not_needed"
        except (OSError, ValueError, binascii.Error) as exc:
            raise SetupRecoveryError(RECOVERY_FAILED) from exc

    def commit(self) -> None:
        # Erase the snapshot durably before unlinking; a leftover committed
        # record must never roll a later successful configuration back.
        atomic_write_json(self.path, {"version": 1, "state": "committed"})
        self.pending = False
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
