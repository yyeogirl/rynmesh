"""Signed, sealed, short-TTL peer mailbox envelopes.

Two nodes that can each reach a registry but not each other still need a way
to exchange small control messages (pairing acceptance, revocation notices,
queued peer messages). This module defines the wire objects for that path:

* the envelope is *signed* by the sender identity (Ed25519, the peer id), so
  the registry and the recipient can both attribute it;
* the payload is *sealed* to the recipient's X25519 messaging key with a fresh
  ephemeral key per message, so the registry stores ciphertext only;
* every envelope carries a short TTL, so an undelivered mailbox drains itself.

No I/O lives here — see ``rynmesh.mailbox_store`` for the registry-side store.
Nothing in this module logs, and no exception message ever carries plaintext,
ciphertext, or a key: errors are short, stable codes safe to return over HTTP.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .crypto import (
    SignatureError,
    SignedPayload,
    canonical_json,
    public_key_from_private,
    sign_payload,
    verify_signed_payload,
)

MAILBOX_VERSION = "rynmesh.mailbox.v1"
POLL_KIND = "mailbox_poll"
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_TTL_S = 24 * 3600
DEFAULT_TTL_S = 3600
MAX_POLL_LIMIT = 50
POLL_SKEW_S = 300
MAX_KIND_LEN = 96
MAX_ACK_IDS = 200
#: HKDF label for the mailbox channel. Distinct from the direct peer-message
#: label, so a mailbox ciphertext captured off the registry cannot be replayed
#: into ``/api/peer/msg`` (and vice versa): the derived key simply differs.
MAILBOX_SEAL_INFO = b"rynmesh-mailbox-v1"
# A full poll (50 x 64 KiB) would be 3.2 MiB, over the 2 MiB the registry HTTP
# client will read back. The server stops filling a poll response at this
# budget instead, so a large mailbox drains over several polls rather than
# producing one response no client can accept.
MAX_POLL_RESPONSE_BYTES = 1536 * 1024

_HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")
# A kind is a routing label the registry stores in the clear and the node logs.
# Restricting it to a conservative identifier charset keeps control characters,
# newlines and terminal escapes out of both.
_KIND_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_X25519_KEY_BYTES = 32
_ED25519_KEY_BYTES = 32
_SEAL_NONCE_BYTES = 12


class MailboxError(ValueError):
    """Raised when a mailbox envelope or poll request is not acceptable.

    The message is always a short, stable code (``expired``, ``duplicate``,
    ``rate_limited``, ...). Registry HTTP handlers return it verbatim, so it
    must never embed peer input, ciphertext, or key material.
    """


def _peer_box():
    """Deferred import: ``rynmesh.services`` reaches back into the registry.

    Importing it at module scope would close the loop
    registry -> mailbox -> services -> store -> registry.
    """

    from .services import peer_box

    return peer_box


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_now(now: Callable[[], datetime] | None) -> datetime:
    moment = (now or _utcnow)()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def rfc3339(value: datetime) -> str:
    """RFC 3339 UTC with a ``Z`` suffix — the mailbox timestamp format."""

    text = value.astimezone(timezone.utc).isoformat()
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise MailboxError("invalid_timestamp") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _b64_len(value: str, expected: int) -> None:
    try:
        raw = base64.b64decode(str(value).encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise MailboxError("invalid_envelope_field") from exc
    if len(raw) != expected:
        raise MailboxError("invalid_envelope_field")


def _require_peer_id(peer_id: str) -> str:
    """Peer ids are base64 Ed25519 public keys — 32 bytes, nothing else.

    Checked on the registry side too: the recipient id picks the spool
    directory, so junk here would scatter unreachable boxes across the disk.
    """

    cleaned = str(peer_id or "")
    if not cleaned:
        raise MailboxError("peer_id_required")
    try:
        raw = base64.b64decode(cleaned.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise MailboxError("invalid_peer_id") from exc
    if len(raw) != _ED25519_KEY_BYTES:
        raise MailboxError("invalid_peer_id")
    return cleaned


def _require_kind(kind: str) -> str:
    cleaned = str(kind or "")
    if not cleaned or len(cleaned) > MAX_KIND_LEN:
        raise MailboxError("invalid_kind")
    if not _KIND_RE.match(cleaned):
        raise MailboxError("invalid_kind")
    return cleaned


def _require_message_id(message_id: str) -> str:
    cleaned = str(message_id or "")
    if not _HEX32.match(cleaned):
        raise MailboxError("invalid_message_id")
    return cleaned


@dataclass(frozen=True)
class MailboxEnvelope:
    """Routing metadata plus the sealed body — the registry sees only this."""

    version: str
    kind: str
    message_id: str
    from_peer_id: str
    to_peer_id: str
    created_at: str
    expires_at: str
    ephemeral_pub: str
    nonce: str
    ciphertext: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MailboxEnvelope":
        # The envelope is the *only* part of a mailbox message the registry can
        # read, so it carries exactly the declared routing fields and nothing
        # else. Without this an arbitrary plaintext side-channel could ride
        # along inside the signed payload and be stored in the clear.
        if not isinstance(data, dict) or set(data) - set(_ENVELOPE_FIELDS):
            raise MailboxError("unknown_field")
        try:
            return cls(**{name: str(data[name]) for name in _ENVELOPE_FIELDS})
        except (KeyError, TypeError) as exc:
            raise MailboxError("invalid_envelope") from exc


_ENVELOPE_FIELDS = tuple(field.name for field in fields(MailboxEnvelope))


def envelope_size_bytes(signed: SignedPayload) -> int:
    """Serialized size of what actually travels and is stored."""

    return len(canonical_json(signed.to_dict()))


def seal_mailbox_message(
    *,
    kind: str,
    body: dict[str, Any],
    from_private_key_bytes: bytes,
    to_peer_id: str,
    to_messaging_pub: str,
    ttl_s: int = DEFAULT_TTL_S,
    now: Callable[[], datetime] | None = None,
    message_id: str | None = None,
) -> SignedPayload:
    """Seal ``body`` for ``to_peer_id`` and sign the resulting envelope.

    A fresh X25519 key is generated per message, so compromising one message's
    ephemeral secret never unlocks any other message to the same recipient.
    """

    cleaned_kind = _require_kind(kind)
    if not isinstance(body, dict):
        raise MailboxError("invalid_body")
    try:
        ttl = int(ttl_s)
    except (TypeError, ValueError) as exc:
        raise MailboxError("invalid_ttl") from exc
    if ttl <= 0 or ttl > MAX_TTL_S:
        raise MailboxError("invalid_ttl")
    recipient = _require_peer_id(to_peer_id)
    messaging_pub = str(to_messaging_pub or "")
    _b64_len(messaging_pub, _X25519_KEY_BYTES)

    resolved_id = (
        _require_message_id(message_id) if message_id is not None else secrets.token_hex(16)
    )
    created = _resolve_now(now)

    try:
        plaintext = canonical_json(
            {"message_id": resolved_id, "kind": cleaned_kind, "body": body}
        )
    except (TypeError, ValueError) as exc:
        raise MailboxError("invalid_body") from exc

    box = _peer_box()
    # Imported here, not at module scope: `rynmesh.registry` imports this
    # module, and the desktop packaging script imports the package with a
    # bare interpreter that has no `cryptography` (same rule as crypto.py).
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    ephemeral = X25519PrivateKey.generate()
    try:
        nonce, ciphertext = box.seal(
            ephemeral, messaging_pub, plaintext, info=MAILBOX_SEAL_INFO
        )
    except Exception as exc:  # noqa: BLE001 - never surface crypto internals
        raise MailboxError("seal_failed") from exc

    envelope = MailboxEnvelope(
        version=MAILBOX_VERSION,
        kind=cleaned_kind,
        message_id=resolved_id,
        from_peer_id=public_key_from_private(from_private_key_bytes),
        to_peer_id=recipient,
        created_at=rfc3339(created),
        expires_at=rfc3339(created + timedelta(seconds=ttl)),
        ephemeral_pub=box.public_key_b64(ephemeral),
        nonce=nonce,
        ciphertext=ciphertext,
    )
    signed = sign_payload(envelope.to_dict(), private_key_bytes=from_private_key_bytes)
    if envelope_size_bytes(signed) > MAX_ENVELOPE_BYTES:
        raise MailboxError("envelope_too_large")
    return signed


def verify_mailbox_envelope(
    signed: SignedPayload,
    *,
    now: Callable[[], datetime] | None = None,
) -> MailboxEnvelope:
    """Validate signature, identity binding, freshness and size. Never decrypts.

    This is what the registry runs: it establishes that the envelope is
    well-formed and attributable without ever touching the sealed body.
    """

    try:
        verify_signed_payload(signed)
    except SignatureError as exc:
        raise MailboxError("invalid_signature") from exc

    envelope = MailboxEnvelope.from_dict(signed.payload)
    if envelope.version != MAILBOX_VERSION:
        raise MailboxError("version_unsupported")
    if envelope.from_peer_id != signed.public_key:
        raise MailboxError("sender_mismatch")
    _require_peer_id(envelope.from_peer_id)
    _require_peer_id(envelope.to_peer_id)
    _require_kind(envelope.kind)
    _require_message_id(envelope.message_id)
    _b64_len(envelope.ephemeral_pub, _X25519_KEY_BYTES)
    _b64_len(envelope.nonce, _SEAL_NONCE_BYTES)
    if not envelope.ciphertext:
        raise MailboxError("invalid_envelope_field")

    moment = _resolve_now(now)
    created = _parse_time(envelope.created_at)
    expires = _parse_time(envelope.expires_at)
    if created > moment + timedelta(seconds=POLL_SKEW_S):
        raise MailboxError("created_at_in_future")
    if expires <= moment:
        raise MailboxError("expired")
    lifetime = (expires - created).total_seconds()
    if lifetime <= 0 or lifetime > MAX_TTL_S:
        raise MailboxError("invalid_ttl")
    if envelope_size_bytes(signed) > MAX_ENVELOPE_BYTES:
        raise MailboxError("envelope_too_large")
    return envelope


def open_mailbox_message(
    signed: SignedPayload,
    *,
    my_peer_id: str,
    messaging_private_key: X25519PrivateKey,
    kind: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[MailboxEnvelope, dict[str, Any]]:
    """Verify, then unseal. Returns ``(envelope, body)`` for the recipient only."""

    envelope = verify_mailbox_envelope(signed, now=now)
    if envelope.to_peer_id != str(my_peer_id or ""):
        raise MailboxError("recipient_mismatch")
    if kind is not None and envelope.kind != kind:
        raise MailboxError("kind_mismatch")

    try:
        plaintext = _peer_box().open_sealed(
            messaging_private_key,
            envelope.ephemeral_pub,
            envelope.nonce,
            envelope.ciphertext,
            info=MAILBOX_SEAL_INFO,
        )
    except Exception as exc:  # noqa: BLE001 - decryption detail must not leak
        raise MailboxError("open_failed") from exc

    try:
        sealed = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MailboxError("invalid_plaintext") from exc
    if not isinstance(sealed, dict):
        raise MailboxError("invalid_plaintext")
    # The sealed copy of the routing fields is what binds the ciphertext to
    # this envelope: a registry that swapped headers around would be caught.
    if str(sealed.get("message_id")) != envelope.message_id:
        raise MailboxError("message_id_mismatch")
    if str(sealed.get("kind")) != envelope.kind:
        raise MailboxError("kind_mismatch")
    body = sealed.get("body")
    if not isinstance(body, dict):
        raise MailboxError("invalid_plaintext")
    return envelope, body


def build_poll_request(
    *,
    private_key_bytes: bytes,
    ack: Iterable[str] = (),
    limit: int = MAX_POLL_LIMIT,
    now: Callable[[], datetime] | None = None,
    nonce: str | None = None,
) -> SignedPayload:
    """Sign a mailbox poll: proves the caller holds the recipient's key."""

    try:
        wanted = int(limit)
    except (TypeError, ValueError) as exc:
        raise MailboxError("invalid_limit") from exc
    if wanted < 1 or wanted > MAX_POLL_LIMIT:
        raise MailboxError("invalid_limit")
    ack_ids = [str(item) for item in ack]
    if len(ack_ids) > MAX_ACK_IDS:
        raise MailboxError("too_many_acks")
    for item in ack_ids:
        _require_message_id(item)
    resolved_nonce = _require_message_id(nonce) if nonce is not None else secrets.token_hex(16)
    payload = {
        "kind": POLL_KIND,
        "peer_id": public_key_from_private(private_key_bytes),
        "issued_at": rfc3339(_resolve_now(now)),
        "nonce": resolved_nonce,
        "ack": ack_ids,
        "limit": wanted,
    }
    return sign_payload(payload, private_key_bytes=private_key_bytes)


def verify_poll_request(
    signed: SignedPayload,
    *,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Validate a poll request and return its payload dict."""

    try:
        verify_signed_payload(signed)
    except SignatureError as exc:
        raise MailboxError("invalid_signature") from exc

    payload = dict(signed.payload)
    if payload.get("kind") != POLL_KIND:
        raise MailboxError("poll_kind_unsupported")
    peer_id = str(payload.get("peer_id") or "")
    if not peer_id or peer_id != signed.public_key:
        raise MailboxError("poll_peer_mismatch")

    moment = _resolve_now(now)
    issued_at = _parse_time(payload.get("issued_at"))
    if abs((issued_at - moment).total_seconds()) > POLL_SKEW_S:
        raise MailboxError("poll_skew")

    raw_limit = payload.get("limit")
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise MailboxError("invalid_limit")
    if raw_limit < 1 or raw_limit > MAX_POLL_LIMIT:
        raise MailboxError("invalid_limit")

    ack = payload.get("ack", [])
    if not isinstance(ack, list):
        raise MailboxError("invalid_ack")
    if len(ack) > MAX_ACK_IDS:
        raise MailboxError("too_many_acks")
    for item in ack:
        _require_message_id(str(item))

    _require_message_id(str(payload.get("nonce") or ""))
    return payload
