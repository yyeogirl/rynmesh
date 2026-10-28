"""Registry-side mailbox file store.

The registry holds sealed envelopes for peers that cannot reach each other
directly. It is a dumb, self-draining spool: it validates the envelope
*shell* (signature, identity binding, freshness, size), never the body, and
deletes messages once acked or expired.

Abuse controls are deliberately local and cheap: a pending cap per recipient, a
second pending cap per (sender, recipient) pair so one peer cannot fill someone
else's box, an in-memory token bucket per sender, and a nonce replay cache per
poll. Nothing here logs, and every raised message is a short stable code that
a registry HTTP handler can return verbatim without leaking peer data.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .atomic_io import atomic_write_json
from .crypto import SignedPayload
from .mailbox import (
    MAX_POLL_RESPONSE_BYTES,
    POLL_SKEW_S,
    MailboxError,
    envelope_size_bytes,
    rfc3339,
    verify_mailbox_envelope,
    verify_poll_request,
)
from .private_permissions import restrict_windows_acl

MAX_REPLAY_ENTRIES = 4096
#: Ack tombstones kept per recipient box. They are tiny (one `expires_at`) and
#: normally expire on their own, but a sender that keeps a box churning could
#: otherwise accumulate one per delivered message with nothing to bound them.
MAX_TOMBSTONES_PER_BOX = 2048
_DIR_MODE = 0o700
_TOMBSTONE_SUFFIX = ".acked"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _recipient_hash(peer_id: str) -> str:
    return hashlib.sha256(peer_id.encode("utf-8")).hexdigest()


class FileMailboxStore:
    """Per-recipient spool of sealed envelopes under ``<root>/mailbox``."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_pending_per_recipient: int = 256,
        max_pending_per_sender: int = 16,
        sender_rate_per_minute: int = 120,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.mailbox_dir = self.root / "mailbox"
        self.max_pending_per_recipient = max(1, int(max_pending_per_recipient))
        # One sender must not be able to fill a recipient's whole box: without
        # this, a single peer holding a valid network key can deny every other
        # peer delivery to that recipient by parking 256 messages there for a
        # full TTL. The network key remains the real admission control.
        self.max_pending_per_sender = max(1, int(max_pending_per_sender))
        self.sender_rate_per_minute = max(1, int(sender_rate_per_minute))
        self._now = now or _utcnow
        self._lock = threading.RLock()
        # from_peer_id -> (tokens, last refill epoch seconds)
        self._buckets: dict[str, tuple[float, float]] = {}
        # (peer_id, nonce) -> expiry epoch seconds
        self._seen_nonces: dict[tuple[str, str], float] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._private_dir(self.mailbox_dir)

    # ------------------------------------------------------------------ API

    def deposit(self, signed: SignedPayload) -> dict[str, Any]:
        """Accept one sealed envelope for its recipient."""

        envelope = verify_mailbox_envelope(signed, now=self._now)
        with self._lock:
            # Charged before the cheap rejections so that a sender looping on
            # duplicates or on a full recipient is throttled just like one
            # delivering real mail.
            self._take_token(envelope.from_peer_id)
            recipient_dir = self._recipient_dir(envelope.to_peer_id)
            path = recipient_dir / f"{envelope.message_id}.json"
            tombstone = path.with_suffix(_TOMBSTONE_SUFFIX)
            if path.exists() or tombstone.exists():
                raise MailboxError("duplicate")
            self._sweep_dir(recipient_dir)
            if path.exists() or tombstone.exists():
                raise MailboxError("duplicate")
            pending, from_sender = self._pending_counts(recipient_dir, envelope.from_peer_id)
            if pending >= self.max_pending_per_recipient:
                raise MailboxError("recipient_full")
            if from_sender >= self.max_pending_per_sender:
                raise MailboxError("sender_quota")
            self._private_dir(recipient_dir)
            atomic_write_json(
                path,
                {"stored_at": rfc3339(self._moment()), "signed": signed.to_dict()},
            )
            return {
                "message_id": envelope.message_id,
                "expires_at": envelope.expires_at,
                "pending": pending + 1,
            }

    def poll(self, signed_poll: SignedPayload) -> list[SignedPayload]:
        """Return (and ack/expire) mail for the peer that signed the request."""

        payload = verify_poll_request(signed_poll, now=self._now)
        peer_id = str(payload["peer_id"])
        limit = int(payload["limit"])
        acks = [str(item) for item in payload.get("ack", [])]
        with self._lock:
            self._remember_nonce(peer_id, str(payload["nonce"]))
            recipient_dir = self._recipient_dir(peer_id)
            for message_id in acks:
                # verify_poll_request already constrained these to 32 hex
                # characters, so they cannot escape the recipient directory.
                self._ack(recipient_dir / f"{message_id}.json")
            self._sweep_dir(recipient_dir)
            # Oldest first, capped by both the caller's limit and a byte budget
            # the registry HTTP client is guaranteed to be able to read back.
            # The order comes from the file's own mtime (deposit order), so the
            # signature check runs only on the envelopes actually being served
            # rather than on the whole box on every poll.
            candidates: list[tuple[float, str, Path]] = []
            for path in recipient_dir.glob("*.json"):
                try:
                    candidates.append((path.stat().st_mtime, path.name, path))
                except OSError:
                    continue
            candidates.sort(key=lambda item: (item[0], item[1]))

            selected: list[SignedPayload] = []
            budget = MAX_POLL_RESPONSE_BYTES
            for _mtime, _name, path in candidates:
                if len(selected) >= limit:
                    break
                try:
                    signed = self._load(path)
                except OSError:
                    continue
                if signed is None:
                    continue
                try:
                    envelope = verify_mailbox_envelope(signed, now=self._now)
                except MailboxError:
                    path.unlink(missing_ok=True)
                    continue
                if envelope.to_peer_id != peer_id:
                    continue
                size = envelope_size_bytes(signed)
                if selected and size > budget:
                    break
                budget -= size
                selected.append(signed)
            return selected

    def sweep(self) -> int:
        """Delete every expired envelope and tombstone; returns the count."""

        removed = 0
        with self._lock:
            for recipient_dir in sorted(self.mailbox_dir.glob("*/*")):
                if not recipient_dir.is_dir():
                    continue
                removed += self._sweep_dir(recipient_dir)
                self._prune_empty(recipient_dir)
        return removed

    # -------------------------------------------------------------- internals

    def _moment(self) -> datetime:
        moment = self._now()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _recipient_dir(self, peer_id: str) -> Path:
        digest = _recipient_hash(peer_id)
        return self.mailbox_dir / digest[:2] / digest

    def _private_dir(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        cursor = path
        while True:
            if os.name == "nt":
                restrict_windows_acl(cursor, directory=True)
            try:
                os.chmod(cursor, _DIR_MODE)
            except OSError:
                pass
            if cursor == self.mailbox_dir or cursor == cursor.parent:
                break
            cursor = cursor.parent
        return path

    def _pending_counts(self, recipient_dir: Path, from_peer_id: str) -> tuple[int, int]:
        """``(pending in this box, pending from this sender)``.

        The sender count needs the stored envelope's ``from_peer_id``, so this
        reads each pending file. That is the same pass the sweep just ahead of
        it already makes, and the pending cap bounds it either way.
        """

        if not recipient_dir.is_dir():
            return 0, 0
        total = 0
        from_sender = 0
        for path in recipient_dir.glob("*.json"):
            total += 1
            try:
                signed = self._load(path)
            except OSError:
                continue  # unreadable now; it still counts against the box
            if signed is None:
                continue
            if str(signed.payload.get("from_peer_id") or "") == from_peer_id:
                from_sender += 1
        return total, from_sender

    def _load(self, path: Path) -> SignedPayload | None:
        """Read one stored envelope. ``None`` means corrupt; OSError propagates.

        The distinction matters: corrupt content is unrecoverable and gets
        deleted, but a transient read failure must never destroy a peer's mail.
        """

        raw = path.read_text(encoding="utf-8")
        try:
            return SignedPayload.from_dict(dict(json.loads(raw)["signed"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _ack(self, path: Path) -> None:
        """Delete a delivered envelope, leaving a tombstone until its TTL.

        Without the tombstone the message id is free again the moment it is
        acked, and anyone who observed the (public, signed) envelope could
        re-deposit it for a second delivery. The tombstone holds only the
        expiry — no routing fields, no ciphertext.
        """

        try:
            signed = self._load(path)
        except OSError:
            # The expiry lives inside the file, so a read failure means there is
            # no tombstone to write. Deleting anyway would free the message id
            # early and reopen the replay window this tombstone exists to close.
            # Leave the envelope: a later ack, or the sweep at TTL, clears it.
            return
        if not path.exists():
            return
        # A corrupt record has no recoverable expiry and was never deliverable
        # (poll drops it too), so it goes without a tombstone.
        expires_at = str(signed.payload.get("expires_at") or "") if signed else ""
        path.unlink(missing_ok=True)
        if expires_at:
            atomic_write_json(path.with_suffix(_TOMBSTONE_SUFFIX), {"expires_at": expires_at})

    def _sweep_dir(self, recipient_dir: Path) -> int:
        """Drop expired envelopes and tombstones without decrypting anything."""

        if not recipient_dir.is_dir():
            return 0
        moment = self._moment()
        removed = 0
        for path in sorted(recipient_dir.glob("*.json")):
            try:
                signed = self._load(path)
            except OSError:
                continue  # transient read failure: leave the mail alone
            expires_at = str(signed.payload.get("expires_at") or "") if signed else ""
            removed += self._expire(path, expires_at, moment)
        for path in sorted(recipient_dir.glob(f"*{_TOMBSTONE_SUFFIX}")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                expires_at = str(dict(record)["expires_at"])
            except OSError:
                continue
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                expires_at = ""
            removed += self._expire(path, expires_at, moment)
        removed += self._bound_tombstones(recipient_dir)
        return removed

    def _bound_tombstones(self, recipient_dir: Path) -> int:
        """Keep at most ``MAX_TOMBSTONES_PER_BOX`` markers, oldest evicted first.

        Expiry is the normal way a tombstone goes; this is the backstop for a
        box churning faster than its TTL. Evicting the oldest is the right
        order: an old marker is the closest to expiring anyway, so the replay
        window this reopens is the smallest one available.
        """

        try:
            markers = list(recipient_dir.glob(f"*{_TOMBSTONE_SUFFIX}"))
        except OSError:
            return 0
        if len(markers) <= MAX_TOMBSTONES_PER_BOX:
            return 0
        aged: list[tuple[float, str, Path]] = []
        for path in markers:
            try:
                aged.append((path.stat().st_mtime, path.name, path))
            except OSError:
                continue
        aged.sort(key=lambda item: (item[0], item[1]))
        removed = 0
        for _mtime, _name, path in aged[: len(aged) - MAX_TOMBSTONES_PER_BOX]:
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def _expire(self, path: Path, expires_at: str, moment: datetime) -> int:
        """Unlink ``path`` if its expiry has passed or cannot be read at all."""

        if not expires_at:
            path.unlink(missing_ok=True)
            return 1
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            path.unlink(missing_ok=True)
            return 1
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= moment:
            path.unlink(missing_ok=True)
            return 1
        return 0

    def _prune_empty(self, recipient_dir: Path) -> None:
        """Remove drained box directories so an idle spool leaves no fan-out."""

        for directory in (recipient_dir, recipient_dir.parent):
            if directory == self.mailbox_dir or directory == directory.parent:
                return
            try:
                directory.rmdir()
            except OSError:
                return  # still holds mail, already gone, or a deposit raced us

    def _take_token(self, sender_peer_id: str) -> None:
        """Per-sender token bucket: ``rate`` burst, ``rate/60`` refilled a second."""

        rate = float(self.sender_rate_per_minute)
        seconds = self._moment().timestamp()
        tokens, last = self._buckets.get(sender_peer_id, (rate, seconds))
        elapsed = max(0.0, seconds - last)
        tokens = min(rate, tokens + elapsed * (rate / 60.0))
        if tokens < 1.0:
            self._buckets[sender_peer_id] = (tokens, seconds)
            raise MailboxError("rate_limited")
        self._buckets[sender_peer_id] = (tokens - 1.0, seconds)

    def _remember_nonce(self, peer_id: str, nonce: str) -> None:
        seconds = self._moment().timestamp()
        expired = [key for key, expiry in self._seen_nonces.items() if expiry <= seconds]
        for key in expired:
            self._seen_nonces.pop(key, None)
        key = (peer_id, nonce)
        if key in self._seen_nonces:
            raise MailboxError("replay")
        if len(self._seen_nonces) >= MAX_REPLAY_ENTRIES:
            # Bounded memory: shed the entries closest to expiry first. Under a
            # sustained flood this can evict a nonce that has not expired yet,
            # briefly reopening the replay window for that one poll. Memory
            # exhaustion is the worse failure, so the bound wins; a shared cache
            # with a real TTL store is the fix if this ever matters.
            for stale, _ in sorted(self._seen_nonces.items(), key=lambda item: item[1])[:64]:
                self._seen_nonces.pop(stale, None)
        self._seen_nonces[key] = seconds + 2 * POLL_SKEW_S


__all__ = ["FileMailboxStore", "MAX_REPLAY_ENTRIES", "MAX_TOMBSTONES_PER_BOX"]
