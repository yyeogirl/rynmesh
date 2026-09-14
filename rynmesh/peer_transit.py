"""End-to-end encrypted single-peer transit over two direct ICE/UDP legs.

This module deliberately separates two meanings of relay:

* TURN/ICE ``relay`` candidates are prohibited.
* an ordinary Rynmesh peer may forward opaque application frames between two
  separately nominated host/server-reflexive ICE pairs.

The transit peer never receives the session key.  It validates only bounded
frame headers and forwards the authenticated ciphertext unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .crypto import SignedPayload, sign_payload, verify_signed_payload
from .llm_package.p2p import receive_bytes, selected_pair, send_bytes
from .types import now_iso

PROTOCOL_VERSION = "rynmesh.peer-transit.v1"
TRANSIT_CAPABILITY = PROTOCOL_VERSION

_FRAME_MAGIC = b"RYNTRN1\0"
_FRAME_HEADER = struct.Struct("!8s16sBQIB")
_REQUEST = 1
_RESPONSE = 2
_FINAL = 1
_MAX_PLAINTEXT_BYTES = 1024 * 1024
_TAG_BYTES = 16
_INFO = b"rynmesh-peer-transit-v1"


class PeerTransitError(RuntimeError):
    pass


def new_session_id() -> str:
    return uuid.uuid4().hex


def _parse_session_id(value: str) -> bytes:
    try:
        parsed = uuid.UUID(hex=str(value))
    except (ValueError, AttributeError) as exc:
        raise PeerTransitError("invalid transit session id") from exc
    return parsed.bytes


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PeerTransitError("invalid transit expiry") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class TransitSessionOpen:
    session_id: str
    source_peer_id: str
    target_peer_id: str
    source_ephemeral_pub: str
    expires_at: str
    hop_limit: int = 1
    protocol_version: str = PROTOCOL_VERSION
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransitSessionOpen":
        return cls(
            session_id=str(value["session_id"]),
            source_peer_id=str(value["source_peer_id"]),
            target_peer_id=str(value["target_peer_id"]),
            source_ephemeral_pub=str(value["source_ephemeral_pub"]),
            expires_at=str(value["expires_at"]),
            hop_limit=int(value.get("hop_limit", 0)),
            protocol_version=str(value.get("protocol_version", "")),
            created_at=str(value.get("created_at", "")),
        )


def sign_session_open(
    session: TransitSessionOpen, *, source_signing_key: bytes
) -> SignedPayload:
    return sign_payload(
        {"kind": "peer_transit_session_open", **session.to_dict()},
        private_key_bytes=source_signing_key,
    )


def verify_session_open(
    signed: SignedPayload | dict[str, Any],
    *,
    expected_target_peer_id: str = "",
    now: datetime | None = None,
) -> TransitSessionOpen:
    envelope = signed if isinstance(signed, SignedPayload) else SignedPayload.from_dict(signed)
    try:
        verify_signed_payload(envelope)
        if envelope.payload.get("kind") != "peer_transit_session_open":
            raise PeerTransitError("invalid transit session kind")
        session = TransitSessionOpen.from_dict(envelope.payload)
        _parse_session_id(session.session_id)
        X25519PublicKey.from_public_bytes(base64.b64decode(session.source_ephemeral_pub))
    except PeerTransitError:
        raise
    except Exception as exc:
        raise PeerTransitError("invalid signed transit session") from exc
    if session.protocol_version != PROTOCOL_VERSION:
        raise PeerTransitError("unsupported transit protocol version")
    if session.source_peer_id != envelope.public_key:
        raise PeerTransitError("transit source identity/signature mismatch")
    if expected_target_peer_id and session.target_peer_id != expected_target_peer_id:
        raise PeerTransitError("transit target identity mismatch")
    if session.source_peer_id == session.target_peer_id:
        raise PeerTransitError("transit source and target must differ")
    if session.hop_limit != 1:
        raise PeerTransitError("peer transit requires hop_limit=1")
    current = now or datetime.now(timezone.utc)
    if _parse_time(session.expires_at) <= current.astimezone(timezone.utc):
        raise PeerTransitError("transit session expired")
    return session


def messaging_public_key(private_key: X25519PrivateKey) -> str:
    return base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii")


def _derive_key(
    private_key: X25519PrivateKey,
    remote_public_b64: str,
    *,
    session_id: str,
    source_peer_id: str,
    target_peer_id: str,
) -> bytes:
    try:
        remote = X25519PublicKey.from_public_bytes(base64.b64decode(remote_public_b64))
    except Exception as exc:
        raise PeerTransitError("invalid transit X25519 public key") from exc
    context = "\0".join(
        [PROTOCOL_VERSION, session_id, source_peer_id, target_peer_id]
    ).encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(context).digest(),
        info=_INFO,
    ).derive(private_key.exchange(remote))


@dataclass(frozen=True)
class TransitFrameHeader:
    session_id: str
    direction: str
    sequence: int
    plaintext_bytes: int
    final: bool


class TransitCipher:
    """Per-session streaming AEAD state shared only by source and target."""

    def __init__(
        self,
        *,
        session_id: str,
        source_peer_id: str,
        target_peer_id: str,
        key: bytes,
    ) -> None:
        self.session_id = str(session_id)
        self.source_peer_id = str(source_peer_id)
        self.target_peer_id = str(target_peer_id)
        self._session_bytes = _parse_session_id(self.session_id)
        self._aead = ChaCha20Poly1305(key)
        self._next_send = {"request": 0, "response": 0}
        self._next_receive = {"request": 0, "response": 0}

    @classmethod
    def for_source(
        cls,
        *,
        session_id: str,
        source_peer_id: str,
        target_peer_id: str,
        target_messaging_pub: str,
        ephemeral_private: X25519PrivateKey | None = None,
    ) -> tuple["TransitCipher", X25519PrivateKey]:
        private_key = ephemeral_private or X25519PrivateKey.generate()
        key = _derive_key(
            private_key,
            target_messaging_pub,
            session_id=session_id,
            source_peer_id=source_peer_id,
            target_peer_id=target_peer_id,
        )
        return (
            cls(
                session_id=session_id,
                source_peer_id=source_peer_id,
                target_peer_id=target_peer_id,
                key=key,
            ),
            private_key,
        )

    @classmethod
    def for_target(
        cls,
        *,
        session: TransitSessionOpen,
        target_messaging_key: X25519PrivateKey,
    ) -> "TransitCipher":
        key = _derive_key(
            target_messaging_key,
            session.source_ephemeral_pub,
            session_id=session.session_id,
            source_peer_id=session.source_peer_id,
            target_peer_id=session.target_peer_id,
        )
        return cls(
            session_id=session.session_id,
            source_peer_id=session.source_peer_id,
            target_peer_id=session.target_peer_id,
            key=key,
        )

    def seal(self, direction: str, plaintext: bytes, *, final: bool = False) -> bytes:
        direction_code = _direction_code(direction)
        if not plaintext or len(plaintext) > _MAX_PLAINTEXT_BYTES:
            raise PeerTransitError("transit plaintext frame size is invalid")
        sequence = self._next_send[direction]
        header = _FRAME_HEADER.pack(
            _FRAME_MAGIC,
            self._session_bytes,
            direction_code,
            sequence,
            len(plaintext),
            _FINAL if final else 0,
        )
        ciphertext = self._aead.encrypt(
            _nonce(direction_code, sequence),
            bytes(plaintext),
            self._associated_data(header),
        )
        self._next_send[direction] += 1
        return header + ciphertext

    def open(self, direction: str, frame: bytes) -> tuple[bytes, TransitFrameHeader]:
        header = inspect_frame(frame)
        if header.session_id != self.session_id:
            raise PeerTransitError("transit frame session mismatch")
        if header.direction != direction:
            raise PeerTransitError("transit frame direction mismatch")
        expected = self._next_receive[direction]
        if header.sequence != expected:
            if header.sequence < expected:
                raise PeerTransitError("replayed transit frame")
            raise PeerTransitError("out-of-order transit frame")
        header_bytes = frame[:_FRAME_HEADER.size]
        try:
            plaintext = self._aead.decrypt(
                _nonce(_direction_code(direction), header.sequence),
                frame[_FRAME_HEADER.size:],
                self._associated_data(header_bytes),
            )
        except InvalidTag as exc:
            raise PeerTransitError("transit frame authentication failed") from exc
        if len(plaintext) != header.plaintext_bytes:
            raise PeerTransitError("transit plaintext length mismatch")
        self._next_receive[direction] += 1
        return plaintext, header

    def _associated_data(self, header: bytes) -> bytes:
        return b"\0".join(
            [
                PROTOCOL_VERSION.encode("ascii"),
                self.source_peer_id.encode("utf-8"),
                self.target_peer_id.encode("utf-8"),
                header,
            ]
        )


def _direction_code(direction: str) -> int:
    if direction == "request":
        return _REQUEST
    if direction == "response":
        return _RESPONSE
    raise PeerTransitError("invalid transit frame direction")


def _direction_name(value: int) -> str:
    if value == _REQUEST:
        return "request"
    if value == _RESPONSE:
        return "response"
    raise PeerTransitError("invalid transit frame direction")


def _nonce(direction: int, sequence: int) -> bytes:
    prefix = b"REQ1" if direction == _REQUEST else b"RES1"
    return prefix + int(sequence).to_bytes(8, "big")


def inspect_frame(frame: bytes) -> TransitFrameHeader:
    if len(frame) < _FRAME_HEADER.size + _TAG_BYTES:
        raise PeerTransitError("short transit frame")
    magic, raw_session, raw_direction, sequence, plaintext_bytes, flags = _FRAME_HEADER.unpack(
        frame[:_FRAME_HEADER.size]
    )
    if magic != _FRAME_MAGIC:
        raise PeerTransitError("invalid transit frame magic")
    if flags & ~_FINAL:
        raise PeerTransitError("invalid transit frame flags")
    if plaintext_bytes < 1 or plaintext_bytes > _MAX_PLAINTEXT_BYTES:
        raise PeerTransitError("transit frame declaration exceeds safe limits")
    if len(frame) != _FRAME_HEADER.size + plaintext_bytes + _TAG_BYTES:
        raise PeerTransitError("transit ciphertext length mismatch")
    return TransitFrameHeader(
        session_id=uuid.UUID(bytes=raw_session).hex,
        direction=_direction_name(raw_direction),
        sequence=int(sequence),
        plaintext_bytes=int(plaintext_bytes),
        final=bool(flags & _FINAL),
    )


def _with_final(chunks: Iterable[bytes]) -> Iterable[tuple[bytes, bool]]:
    iterator = iter(chunks)
    try:
        current = bytes(next(iterator))
    except StopIteration as exc:
        raise PeerTransitError("transit stream requires at least one chunk") from exc
    for following in iterator:
        yield current, False
        current = bytes(following)
    yield current, True


async def send_encrypted_stream(
    connection: Any,
    cipher: TransitCipher,
    *,
    direction: str,
    chunks: Iterable[bytes],
    timeout_s: float,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    plaintext_bytes = 0
    wire_bytes = 0
    frames = 0
    for chunk, final in _with_final(chunks):
        if not chunk:
            raise PeerTransitError("transit stream contains an empty chunk")
        digest.update(chunk)
        plaintext_bytes += len(chunk)
        frame = cipher.seal(direction, chunk, final=final)
        wire_bytes += await send_bytes(connection, frame, timeout_s=timeout_s)
        frames += 1
    return {
        "frames": frames,
        "plaintext_bytes": plaintext_bytes,
        "wire_bytes": wire_bytes,
        "sha256": "sha256:" + digest.hexdigest(),
    }


async def receive_encrypted_stream(
    connection: Any,
    cipher: TransitCipher,
    *,
    direction: str,
    sink: Callable[[bytes], Any],
    timeout_s: float,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    plaintext_bytes = 0
    wire_bytes = 0
    frames = 0
    while True:
        frame, received_bytes = await receive_bytes(connection, timeout_s=timeout_s)
        try:
            plaintext, header = cipher.open(direction, frame)
        except PeerTransitError as exc:
            # Hop-level retransmission may leave an already authenticated
            # message queued after its acknowledgement crossed the wire.
            # Discard it; future/out-of-order frames and authentication errors
            # remain fatal.
            if str(exc) == "replayed transit frame":
                continue
            raise
        sink(plaintext)
        digest.update(plaintext)
        plaintext_bytes += len(plaintext)
        wire_bytes += received_bytes
        frames += 1
        if header.final:
            break
    return {
        "frames": frames,
        "plaintext_bytes": plaintext_bytes,
        "wire_bytes": wire_bytes,
        "sha256": "sha256:" + digest.hexdigest(),
    }


async def relay_bidirectional_once(
    source_connection: Any,
    target_connection: Any,
    *,
    session_id: str,
    timeout_s: float,
    audit_frame: Callable[[bytes], Any] | None = None,
) -> dict[str, Any]:
    """Forward one request stream and one response stream without decrypting."""

    counters = {
        "session_id": session_id,
        "request_frames": 0,
        "response_frames": 0,
        "transit_rx_bytes": 0,
        "transit_tx_bytes": 0,
    }
    expected = {"request": 0, "response": 0}

    async def forward(source: Any, target: Any, direction: str) -> None:
        while True:
            frame, received_bytes = await receive_bytes(source, timeout_s=timeout_s)
            header = inspect_frame(frame)
            if header.session_id != session_id or header.direction != direction:
                raise PeerTransitError("transit peer received a mismatched frame")
            if header.sequence < expected[direction]:
                # Same bounded retransmission race as the endpoint receive
                # path: acknowledge-and-discard an already forwarded frame.
                continue
            if header.sequence > expected[direction]:
                raise PeerTransitError("transit peer received a non-monotonic frame")
            expected[direction] += 1
            if audit_frame is not None:
                audit_frame(frame)
            counters["transit_rx_bytes"] += received_bytes
            counters[f"{direction}_frames"] += 1
            counters["transit_tx_bytes"] += await send_bytes(
                target, frame, timeout_s=timeout_s
            )
            if header.final:
                break

    await forward(source_connection, target_connection, "request")
    await forward(target_connection, source_connection, "response")
    return counters


def validate_ice_hop(evidence: dict[str, Any]) -> None:
    if evidence.get("relay_used") is not False:
        raise PeerTransitError("peer transit hop used an ICE relay")
    for side in ("local", "remote"):
        candidate = dict(evidence.get(side) or {})
        if str(candidate.get("transport", "")).lower() != "udp":
            raise PeerTransitError("peer transit hop is not UDP")
        if str(candidate.get("type", "")) not in {"host", "srflx", "prflx"}:
            raise PeerTransitError("peer transit hop nominated a non-direct candidate")


def transit_evidence(
    *,
    source_peer_id: str,
    transit_peer_id: str,
    target_peer_id: str,
    source_connection: Any,
    target_connection: Any,
    counters: dict[str, Any],
    source_sha256: str,
    target_sha256: str,
    plaintext_found_on_transit: bool,
) -> dict[str, Any]:
    hop_1 = selected_pair(source_connection)
    hop_2 = selected_pair(target_connection)
    validate_ice_hop(hop_1)
    validate_ice_hop(hop_2)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "source_peer_id": source_peer_id,
        "transit_peer_id": transit_peer_id,
        "target_peer_id": target_peer_id,
        "path_mode": "peer_transit",
        "hop_1": hop_1,
        "hop_2": hop_2,
        "ice_relay_candidate_used": False,
        "registry_payload_bytes": 0,
        **dict(counters),
        "source_sha256": source_sha256,
        "target_sha256": target_sha256,
        "plaintext_found_on_transit": bool(plaintext_found_on_transit),
        "result": (
            "pass"
            if source_sha256 == target_sha256 and not plaintext_found_on_transit
            else "fail"
        ),
    }


class RouteState(str, Enum):
    DIRECT = "direct"
    DEGRADED = "degraded"
    PEER_TRANSIT = "peer_transit"
    RECOVERING = "recovering"


@dataclass(frozen=True)
class PathMetrics:
    reachable: bool
    rtt_p95_ms: float
    loss_ratio: float
    consecutive_failures: int = 0

    def score(self) -> float:
        if not self.reachable:
            return float("inf")
        return max(0.0, self.rtt_p95_ms) + 2000.0 * max(0.0, min(1.0, self.loss_ratio))


@dataclass(frozen=True)
class RoutePolicy:
    hard_failure_count: int = 3
    loss_threshold: float = 0.08
    latency_threshold_ms: float = 250.0
    transit_improvement_ratio: float = 0.25
    degraded_hold_s: float = 30.0
    transit_min_hold_s: float = 60.0
    recovery_hold_s: float = 120.0
    recovery_probe_count: int = 5

    @classmethod
    def from_env(cls) -> "RoutePolicy":
        """Load operator-tunable routing thresholds with validated bounds."""

        def integer(name: str, default: int, minimum: int) -> int:
            try:
                value = int(os.environ.get(name, default))
            except ValueError as exc:
                raise PeerTransitError(f"{name} must be an integer") from exc
            if value < minimum:
                raise PeerTransitError(f"{name} must be >= {minimum}")
            return value

        def number(name: str, default: float, minimum: float) -> float:
            try:
                value = float(os.environ.get(name, default))
            except ValueError as exc:
                raise PeerTransitError(f"{name} must be numeric") from exc
            if value < minimum:
                raise PeerTransitError(f"{name} must be >= {minimum}")
            return value

        loss = number("RYNMESH_TRANSIT_LOSS_THRESHOLD", cls.loss_threshold, 0.0)
        improvement = number(
            "RYNMESH_TRANSIT_IMPROVEMENT_RATIO",
            cls.transit_improvement_ratio,
            0.0,
        )
        if loss > 1 or improvement > 1:
            raise PeerTransitError("transit loss and improvement ratios must be <= 1")
        return cls(
            hard_failure_count=integer(
                "RYNMESH_TRANSIT_HARD_FAILURE_COUNT",
                cls.hard_failure_count,
                1,
            ),
            loss_threshold=loss,
            latency_threshold_ms=number(
                "RYNMESH_TRANSIT_LATENCY_THRESHOLD_MS",
                cls.latency_threshold_ms,
                1.0,
            ),
            transit_improvement_ratio=improvement,
            degraded_hold_s=number(
                "RYNMESH_TRANSIT_DEGRADED_HOLD_S",
                cls.degraded_hold_s,
                0.0,
            ),
            transit_min_hold_s=number(
                "RYNMESH_TRANSIT_MIN_HOLD_S",
                cls.transit_min_hold_s,
                0.0,
            ),
            recovery_hold_s=number(
                "RYNMESH_TRANSIT_RECOVERY_HOLD_S",
                cls.recovery_hold_s,
                0.0,
            ),
            recovery_probe_count=integer(
                "RYNMESH_TRANSIT_RECOVERY_PROBES",
                cls.recovery_probe_count,
                1,
            ),
        )


class RouteManager:
    """Hysteresis-controlled request-boundary path selection."""

    def __init__(self, policy: RoutePolicy | None = None) -> None:
        self.policy = policy or RoutePolicy.from_env()
        self.state = RouteState.DIRECT
        self.changed_at = 0.0
        self.degraded_since: float | None = None
        self.recovery_since: float | None = None
        self.recovery_probes = 0
        self.events: list[dict[str, Any]] = []

    @property
    def path_mode(self) -> str:
        return "peer_transit" if self.state in {
            RouteState.PEER_TRANSIT,
            RouteState.RECOVERING,
        } else "direct"

    def update(
        self,
        *,
        direct: PathMetrics,
        transit: PathMetrics | None,
        now_monotonic: float | None = None,
    ) -> str:
        now_value = time.monotonic() if now_monotonic is None else float(now_monotonic)
        degraded = (
            not direct.reachable
            or direct.consecutive_failures >= self.policy.hard_failure_count
            or direct.loss_ratio > self.policy.loss_threshold
            or direct.rtt_p95_ms > self.policy.latency_threshold_ms
        )
        transit_better = bool(
            transit
            and transit.reachable
            and (
                not direct.reachable
                or transit.score()
                <= direct.score() * (1.0 - self.policy.transit_improvement_ratio)
            )
        )

        if self.state == RouteState.DIRECT:
            if degraded:
                self.degraded_since = now_value
                self._change(RouteState.DEGRADED, now_value, "direct_degraded")
                if (
                    transit_better
                    and direct.consecutive_failures >= self.policy.hard_failure_count
                ):
                    self._change(RouteState.PEER_TRANSIT, now_value, "hard_failure")
        elif self.state == RouteState.DEGRADED:
            if not degraded:
                self.degraded_since = None
                self._change(RouteState.DIRECT, now_value, "direct_recovered_before_switch")
            elif (
                transit_better
                and self.degraded_since is not None
                and (
                    direct.consecutive_failures >= self.policy.hard_failure_count
                    or now_value - self.degraded_since >= self.policy.degraded_hold_s
                )
            ):
                self._change(RouteState.PEER_TRANSIT, now_value, "transit_better")
        elif self.state == RouteState.PEER_TRANSIT:
            if (
                not degraded
                and now_value - self.changed_at >= self.policy.transit_min_hold_s
            ):
                self.recovery_since = now_value
                self.recovery_probes = 1
                self._change(RouteState.RECOVERING, now_value, "direct_recovery_started")
        elif self.state == RouteState.RECOVERING:
            if degraded:
                self.recovery_since = None
                self.recovery_probes = 0
                self._change(RouteState.PEER_TRANSIT, now_value, "direct_recovery_failed")
            else:
                self.recovery_probes += 1
                if (
                    self.recovery_since is not None
                    and now_value - self.recovery_since >= self.policy.recovery_hold_s
                    and self.recovery_probes >= self.policy.recovery_probe_count
                ):
                    self.degraded_since = None
                    self.recovery_since = None
                    self.recovery_probes = 0
                    self._change(RouteState.DIRECT, now_value, "direct_recovered")
        return self.path_mode

    def _change(self, state: RouteState, at: float, reason: str) -> None:
        previous = self.state
        self.state = state
        self.changed_at = at
        self.events.append(
            {"from": previous.value, "to": state.value, "at": at, "reason": reason}
        )


def evidence_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8")
