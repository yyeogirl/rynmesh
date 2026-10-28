"""Local-only message history + attachment blobs. Each node stores ONLY its own
conversations under RYNMESH_HOME/messages/. No other node holds this data."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from ..atomic_io import atomic_write_bytes
from ..file_transactions import file_transaction

MAX_CONVERSATION_BYTES = 64 * 1024 * 1024

log = logging.getLogger("rynmesh.messaging_store")


def _safe(peer_id: str) -> str:
    return base64.urlsafe_b64encode(peer_id.encode("utf-8")).decode("ascii").rstrip("=")


def _peer_hash(peer_id: str) -> str:
    """A stable, non-reversible handle for a conversation, safe to log."""

    return hashlib.sha256(peer_id.encode("utf-8")).hexdigest()[:16]


class MessagingStore:
    def __init__(self, home: str | Path) -> None:
        self.root = Path(home) / "messages"
        #: Lines `history()` could not parse since this store was constructed.
        self.skipped_history_lines = 0

    def _conv_path(self, peer_id: str) -> Path:
        return self.root / f"{_safe(peer_id)}.jsonl"

    def append(self, peer_id: str, record: dict[str, Any]) -> None:
        path = self._conv_path(peer_id)
        with file_transaction(path.with_suffix(".lock")):
            if path.exists() and path.stat().st_size > MAX_CONVERSATION_BYTES:
                raise OSError("conversation_storage_full")
            raw = path.read_bytes() if path.exists() else b""
            if raw and not raw.endswith(b"\n"):
                raw += b"\n"  # Keep a damaged final line separate from the next valid message.
            raw += (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
            atomic_write_bytes(path, raw, max_bytes=MAX_CONVERSATION_BYTES)

    def append_unique(self, peer_id: str, record: dict[str, Any]) -> bool:
        with file_transaction(self._conv_path(peer_id).with_suffix(".lock")):
            if any(row.get("msg_id") == record.get("msg_id") for row in self.history(peer_id)):
                return False
            self.append(peer_id, record)
            return True

    def history(self, peer_id: str) -> list[dict[str, Any]]:
        """Every stored record for one conversation; unparseable lines are skipped.

        The file is append-only, so a truncated tail (a crash mid-write, a full
        disk) or one corrupt line would otherwise take the whole conversation
        down with it — including the dedupe check `PeerMessenger.receive` runs
        against this list. Only a hash of the peer id and a count are logged;
        the line itself is message content and never reaches the log.
        """

        path = self._conv_path(peer_id)
        if not path.exists():
            return []
        if path.stat().st_size > MAX_CONVERSATION_BYTES:
            raise OSError("conversation_storage_full")
        records: list[dict[str, Any]] = []
        skipped = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                skipped += 1
        if skipped:
            self.skipped_history_lines += skipped
            log.warning(
                "messaging history: skipped %d unparseable line(s) conversation=%s",
                skipped,
                _peer_hash(peer_id),
            )
        return records

    def save_attachment(self, msg_id: str, data: bytes) -> str:
        d = self.root / "attachments"
        d.mkdir(parents=True, exist_ok=True)
        path = d / _safe(msg_id)
        atomic_write_bytes(path, data)
        return str(path)

    def load_attachment(self, msg_id: str) -> bytes:
        return (self.root / "attachments" / _safe(msg_id)).read_bytes()
