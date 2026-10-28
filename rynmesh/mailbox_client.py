"""Node-side mailbox client: poll, verify, open, dispatch, ack.

The registry holds sealed envelopes for peers that cannot reach each other
directly (see ``rynmesh.mailbox`` / ``rynmesh.mailbox_store``). This module is
the other half: the node's own loop over its box.

Delivery is at-least-once by construction — an ack rides along with the *next*
poll, so a crash between "handler ran" and "ack sent" redelivers the message.
Two things keep that safe:

* a persistent **seen cache** (message id -> expiry) rejects a redelivery of a
  message a handler already completed, so handlers see each message once in
  the normal case;
* a per-message **attempt counter**, persisted beside the seen cache, retries a
  failing handler across polls *and across restarts* and gives up after
  ``max_attempts``, so one poisonous message cannot wedge the box forever.

An envelope that verifies but will not *decrypt* — the messaging key rotated
while the message was in flight — is retried on the same budget rather than
acked on sight, since acking would destroy mail a restored key could still
read. It cannot be retried indefinitely: a poll has no cursor, so a head of
never-acked messages would hide everything newer behind it. Every look counts
under ``undecryptable``, so a rotation stays visible in ``status()``.

Nothing here logs a body, a ciphertext, or a key: log records carry the
verified ``kind``, the 32-hex message id, and an exception *class* name only.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .atomic_io import AtomicIOError, atomic_write_json
from .mailbox import (
    DEFAULT_TTL_S,
    MAX_ACK_IDS,
    MAX_POLL_LIMIT,
    MailboxEnvelope,
    MailboxError,
    build_poll_request,
    open_mailbox_message,
    rfc3339,
    seal_mailbox_message,
)

log = logging.getLogger("rynmesh.mailbox_client")

Handler = Callable[[MailboxEnvelope, dict[str, Any]], None]

_HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")
#: v1 held only `entries` (the seen cache). v2 adds `attempts`, so a poison
#: message cannot reset its retry budget by outliving the process. v1 files load
#: unchanged — the attempts map simply starts empty.
_SEEN_VERSION = 2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_id(value: Any) -> str:
    """A message id is only safe to log once it looks like one.

    Ids arrive inside a not-yet-verified payload, so an arbitrary string could
    otherwise be written straight into the node's log by a remote sender.
    """

    text = str(value or "")
    return text if _HEX32.match(text) else "invalid"


class MailboxClient:
    """One node's mailbox: send with ``deposit``, receive with ``poll_once``."""

    def __init__(
        self,
        *,
        store: Any,
        messaging_key: X25519PrivateKey,
        home: str | Path,
        resolve_messaging_pub: Callable[[str], str],
        now: Callable[[], datetime] | None = None,
        max_attempts: int = 3,
        seen_capacity: int = 5000,
    ) -> None:
        self._store = store
        self._messaging_key = messaging_key
        self._home = Path(home).expanduser()
        self._resolve_messaging_pub = resolve_messaging_pub
        self._now = now or _utcnow
        self._max_attempts = max(1, int(max_attempts))
        self._seen_capacity = max(1, int(seen_capacity))
        self._lock = threading.RLock()
        self._handlers: dict[str, Handler] = {}
        self._pending_acks: list[str] = []
        # message_id -> (attempts so far, envelope expiry in epoch seconds).
        # Persisted beside the seen cache and bounded by the same capacity, so
        # a message that crashes its handler cannot restart its retry budget by
        # outliving the process.
        self._attempts: OrderedDict[str, tuple[int, float]] = OrderedDict()
        # message_id -> expiry, epoch seconds. Insertion-ordered so the bound
        # evicts the oldest entry rather than an arbitrary one.
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._handled_total = 0
        self._dropped_total = 0
        self._undecryptable_total = 0
        self._last_poll_dropped = 0
        self._pending_last = 0
        self._last_poll_at = ""
        self._last_error = ""
        self._seen_path = self._home / "mailbox" / "seen.json"
        self._load_seen()

    # ---------------------------------------------------------------- public

    def register_handler(self, kind: str, handler: Handler, *, replace: bool = False) -> None:
        """Bind one message kind to its handler. Kinds are claimed exactly once."""

        cleaned = str(kind or "").strip()
        if not cleaned:
            raise ValueError("mailbox handler kind must not be empty")
        if not callable(handler):
            raise TypeError("mailbox handler must be callable")
        with self._lock:
            if cleaned in self._handlers and not replace:
                raise ValueError(f"mailbox handler already registered: {cleaned}")
            self._handlers[cleaned] = handler

    def deposit(
        self,
        to_peer_id: str,
        kind: str,
        body: dict[str, Any],
        *,
        ttl_s: int = DEFAULT_TTL_S,
        to_messaging_pub: str | None = None,
    ) -> dict[str, Any]:
        """Seal ``body`` for a peer and hand the envelope to the registry."""

        registry = getattr(self._store, "registry", None)
        if registry is None:
            raise MailboxError("no_registry")
        pub = str(to_messaging_pub or "") or str(self._resolve_messaging_pub(to_peer_id) or "")
        signed = seal_mailbox_message(
            kind=kind,
            body=body,
            from_private_key_bytes=self._store.private_key_bytes,
            to_peer_id=to_peer_id,
            to_messaging_pub=pub,
            ttl_s=ttl_s,
            now=self._now,
        )
        receipt = registry.deposit_mailbox(signed)
        result = dict(receipt) if isinstance(receipt, dict) else {}
        result.setdefault("message_id", str(signed.payload.get("message_id", "")))
        return result

    @property
    def last_poll_dropped(self) -> int:
        """Messages dropped by the most recent poll.

        The supervised worker adds this to the handled count when it decides
        whether the box was busy: a batch of nothing but replays, bad
        envelopes or unknown kinds is still work, and must drain at the busy
        delay rather than backing off as if the box were empty.
        """

        with self._lock:
            return self._last_poll_dropped

    def poll_once(self) -> int:
        """Fetch, dispatch and ack one batch. Returns the number handled."""

        registry = getattr(self._store, "registry", None)
        if registry is None:
            with self._lock:
                self._last_error = "no_registry"
                self._last_poll_dropped = 0
            return 0

        with self._lock:
            # `build_poll_request` refuses more than MAX_ACK_IDS, and a batch is
            # capped at MAX_POLL_LIMIT, so this slice never starves an ack: the
            # remainder rides the next poll.
            acks = list(self._pending_acks[:MAX_ACK_IDS])
        try:
            signed_poll = build_poll_request(
                private_key_bytes=self._store.private_key_bytes,
                ack=acks,
                limit=MAX_POLL_LIMIT,
                now=self._now,
            )
            messages = registry.poll_mailbox(signed_poll)
        except Exception as exc:
            with self._lock:
                self._last_error = type(exc).__name__
            raise

        # Handlers run under the lock: one worker owns `poll_once`, and holding
        # it for the batch keeps the seen cache, the ack queue and the counters
        # consistent. The only reader it can delay is `status()`.
        with self._lock:
            # The acked ids were deleted server-side; anything queued while the
            # request was in flight stays pending.
            acked = set(acks)
            self._pending_acks = [item for item in self._pending_acks if item not in acked]
            handled, self._last_poll_dropped = self._dispatch(messages)
            self._pending_last = len(messages)
            self._last_poll_at = rfc3339(self._resolve_now())
            self._last_error = ""
        return handled

    def status(self) -> dict[str, Any]:
        """Counters and handler names only — never a body or an error message."""

        with self._lock:
            return {
                "handled_total": self._handled_total,
                "dropped_total": self._dropped_total,
                # Envelopes that verified but would not decrypt — a messaging
                # key rotated out from under mail already in flight, most
                # likely. They are never acked; they expire in the box.
                "undecryptable": self._undecryptable_total,
                "pending_last": self._pending_last,
                "last_poll_at": self._last_poll_at,
                "last_error": self._last_error,
                "handlers": sorted(self._handlers),
            }

    # --------------------------------------------------------------- private

    def _dispatch(self, messages: Any) -> tuple[int, int]:
        """Open, dispatch and ack one polled batch. Returns (handled, dropped)."""

        handled = 0
        dropped_before = self._dropped_total
        changed = False
        undecryptable = 0
        for signed_message in messages or ():
            payload = getattr(signed_message, "payload", None)
            raw_id = payload.get("message_id") if isinstance(payload, dict) else ""
            message_id = str(raw_id or "")

            if message_id and message_id in self._seen:
                # Already completed by a handler; the ack simply never landed.
                self._ack(message_id)
                self._dropped_total += 1
                log.warning("mailbox replay dropped id=%s", _safe_id(message_id))
                continue

            try:
                envelope, body = open_mailbox_message(
                    signed_message,
                    my_peer_id=self._store.peer_id,
                    messaging_private_key=self._messaging_key,
                    now=self._now,
                )
            except MailboxError as exc:
                if str(exc) == "open_failed":
                    # The envelope verified and really is addressed to us; only
                    # the seal would not open — the messaging key rotated while
                    # this was in flight, most likely. Acking on the first look
                    # would destroy mail a restored key could still read, so it
                    # is retried like a failing handler.
                    #
                    # It cannot be retried forever, though: `poll` serves the
                    # oldest MAX_POLL_LIMIT envelopes and has no cursor, so 50
                    # never-acked messages at the head of a box would hide every
                    # newer message behind them for a full TTL. The same
                    # `max_attempts` budget that bounds a poison message bounds
                    # this, and every look still counts as `undecryptable`, so a
                    # key rotation stays visible in `status()` either way.
                    undecryptable += 1
                    self._undecryptable_total += 1
                    expires_at = (
                        str(payload.get("expires_at") or "") if isinstance(payload, dict) else ""
                    )
                    attempts = self._record_attempt(message_id, expires_at)
                    changed = True
                    if attempts >= self._max_attempts:
                        self._attempts.pop(message_id, None)
                        self._ack(message_id)
                        self._dropped_total += 1
                    continue
                # Anything else is a verdict on the envelope shell — bad
                # signature, wrong recipient, expired, oversized. It will never
                # become deliverable, so it is acked and dropped.
                self._ack(message_id)
                self._dropped_total += 1
                log.warning(
                    "mailbox envelope rejected id=%s error=%s",
                    _safe_id(message_id),
                    type(exc).__name__,
                )
                continue
            except Exception as exc:
                # Broad on purpose: a non-envelope a hostile registry pushed at
                # us must not wedge the box. `kind` is unverified here, so it is
                # not logged.
                self._ack(message_id)
                self._dropped_total += 1
                log.warning(
                    "mailbox envelope rejected id=%s error=%s",
                    _safe_id(message_id),
                    type(exc).__name__,
                )
                continue

            message_id = envelope.message_id
            handler = self._handlers.get(envelope.kind)
            if handler is None:
                self._ack(message_id)
                self._dropped_total += 1
                log.warning(
                    "mailbox message dropped kind=%s id=%s reason=unknown_kind",
                    envelope.kind,
                    _safe_id(message_id),
                )
                continue

            try:
                handler(envelope, body)
            except Exception as exc:
                attempts = self._record_attempt(message_id, envelope.expires_at)
                changed = True
                log.error(
                    "mailbox handler failed kind=%s id=%s attempt=%d error=%s",
                    envelope.kind,
                    _safe_id(message_id),
                    attempts,
                    type(exc).__name__,
                )
                if attempts >= self._max_attempts:
                    # Poison message: stop redelivering it and let it expire.
                    self._attempts.pop(message_id, None)
                    self._ack(message_id)
                    self._dropped_total += 1
                continue

            changed = self._attempts.pop(message_id, None) is not None or changed
            changed = self._remember(message_id, envelope.expires_at) or changed
            self._ack(message_id)
            self._handled_total += 1
            handled += 1

        if undecryptable:
            # One line per poll, not per message: a rotated key makes every
            # queued message fail at once, and the class name is all there is
            # to say about it that is safe to write down.
            log.warning(
                "mailbox messages could not be opened count=%d error=%s",
                undecryptable,
                MailboxError.__name__,
            )
        if changed:
            self._save_seen()
        return handled, self._dropped_total - dropped_before

    def _ack(self, message_id: str) -> None:
        """Queue an ack for the next poll (ids are deduped and order-stable)."""

        if not message_id or not _HEX32.match(message_id):
            return
        if message_id not in self._pending_acks:
            self._pending_acks.append(message_id)

    def _resolve_now(self) -> datetime:
        moment = self._now()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _epoch(self) -> float:
        return self._resolve_now().timestamp()

    def _remember(self, message_id: str, expires_at: str) -> bool:
        """Record a completed message until its envelope would have expired."""

        try:
            parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        self._seen[message_id] = parsed.timestamp()
        self._seen.move_to_end(message_id)
        self._prune_seen()
        while len(self._seen) > self._seen_capacity:
            self._seen.popitem(last=False)
        return True

    def _record_attempt(self, message_id: str, expires_at: str) -> int:
        """Bump (and bound) the persistent retry counter for one message.

        A counter is only useful while the message it tracks can still be
        redelivered, so it is stored under that message's expiry and pruned
        with it. An expiry that cannot be read is therefore untrackable: rather
        than store a counter that the next prune would silently reset — turning
        the retry budget into an infinite loop — this reports the budget as
        already spent, and the caller gives up now.
        """

        self._prune_attempts()
        expiry = self._expiry_epoch(expires_at)
        if expiry <= self._epoch():
            self._attempts.pop(message_id, None)
            return self._max_attempts
        attempts = self._attempts.get(message_id, (0, 0.0))[0] + 1
        self._attempts[message_id] = (attempts, expiry)
        self._attempts.move_to_end(message_id)
        while len(self._attempts) > self._seen_capacity:
            self._attempts.popitem(last=False)
        return attempts

    @staticmethod
    def _expiry_epoch(expires_at: str) -> float:
        try:
            parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def _prune_seen(self) -> None:
        moment = self._epoch()
        for key in [key for key, expiry in self._seen.items() if expiry <= moment]:
            self._seen.pop(key, None)

    def _prune_attempts(self) -> None:
        # An expired message is never redelivered, so its counter is dead
        # weight. A counter with an unreadable expiry (0.0) prunes immediately.
        moment = self._epoch()
        for key in [key for key, (_, expiry) in self._attempts.items() if expiry <= moment]:
            self._attempts.pop(key, None)

    def _load_seen(self) -> None:
        try:
            raw = json.loads(self._seen_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            restored: list[tuple[str, float]] = []
            for key, value in entries.items():
                if not _HEX32.match(str(key)):
                    continue
                try:
                    restored.append((str(key), float(value)))
                except (TypeError, ValueError):
                    continue
            # The file is written with sorted keys for a stable diff, so
            # insertion order does not survive a round trip. Expiry is the age
            # proxy that does, and it is what the capacity bound evicts by.
            for key, expiry in sorted(restored, key=lambda item: item[1]):
                self._seen[key] = expiry
            self._prune_seen()
            while len(self._seen) > self._seen_capacity:
                self._seen.popitem(last=False)

        attempts = raw.get("attempts")  # absent in a v1 file
        if isinstance(attempts, dict):
            counters: list[tuple[str, int, float]] = []
            for key, value in attempts.items():
                if not _HEX32.match(str(key)) or not isinstance(value, dict):
                    continue
                try:
                    count = int(value["count"])
                    expiry = float(value["expires"])
                except (KeyError, TypeError, ValueError):
                    continue
                if count > 0:
                    counters.append((str(key), count, expiry))
            for key, count, expiry in sorted(counters, key=lambda item: item[2]):
                self._attempts[key] = (count, expiry)
            self._prune_attempts()
            while len(self._attempts) > self._seen_capacity:
                self._attempts.popitem(last=False)

    def _save_seen(self) -> None:
        """Atomic 0600 write; a failure here must never lose a delivery."""

        self._prune_seen()
        self._prune_attempts()
        value = {
            "version": _SEEN_VERSION,
            "entries": dict(self._seen),
            # Counts and expiries only: no kind, no body, no ciphertext.
            "attempts": {
                key: {"count": count, "expires": expiry}
                for key, (count, expiry) in self._attempts.items()
            },
        }
        try:
            atomic_write_json(self._seen_path, value, sort_keys=True)
        except AtomicIOError:
            # The cache is an optimization over an at-least-once channel: a
            # write failure costs a possible duplicate delivery, never a lost
            # message, so it must not fail the poll.
            log.warning("mailbox seen cache write failed")


__all__ = ["Handler", "MailboxClient"]
