"""Rynmesh registry-assisted peer discovery.

The registry is a coordination plane, not a trust authority. Peer records are
self-signed by the node identity, so a registry such as rynmesh.ai can help
nodes find each other without deciding which peers are trusted.
"""

from __future__ import annotations

import json
import os
import ssl
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .crypto import SignatureError, SignedPayload, sign_payload, verify_signed_payload
from .jobs import (
    JobError,
    order_is_open,
    record_within_age,
    verify_job_capacity,
    verify_work_order,
    verify_work_result,
)
from .mailbox import MailboxError, verify_mailbox_envelope
from .mailbox_store import FileMailboxStore
from .types import now_iso


class RegistryError(RuntimeError):
    """A registry call failed.

    ``status`` and ``detail`` are populated when the failure came back as an
    HTTP response, so a caller can branch on *why* (409 ``duplicate`` versus
    429 ``rate_limited``) instead of pattern-matching the message string.
    Both stay unset for local backends and transport-level failures.
    """

    def __init__(self, message: str = "", *, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


MAX_REGISTRY_RESPONSE_BYTES = 2 * 1024 * 1024
RYNMESH_USER_AGENT = "Rynmesh/0.1"
BLOCKED_REGISTRY_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata",
}
ATOMIC_REPLACE_ATTEMPTS = 5
ATOMIC_REPLACE_RETRY_S = 0.05


def default_registry_dir(network_dir: Path) -> Path:
    return Path(os.environ.get("RYNMESH_REGISTRY_DIR", network_dir / "registry")).expanduser()


def _peer_slug(peer_id: str) -> str:
    from .store import _hash_hex

    return _hash_hex(peer_id)[:16]


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Replace one registry record without exposing a partially written JSON file."""
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        for attempt in range(ATOMIC_REPLACE_ATTEMPTS):
            try:
                Path(temporary_name).replace(path)
                break
            except PermissionError:
                # Windows can deny os.replace briefly while another process
                # has the destination open for reading. Keep the temporary
                # file and retry for at most 200 ms before failing closed.
                if attempt + 1 >= ATOMIC_REPLACE_ATTEMPTS:
                    raise
                time.sleep(ATOMIC_REPLACE_RETRY_S)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


@dataclass(frozen=True)
class PeerRecord:
    peer_id: str
    node_name: str
    endpoints: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ("publish", "seed", "verify")
    safety_packs: tuple[str, ...] = ("@rynmesh/safety-core@0.1.0",)
    network_id: str = "rynmesh-main"
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "node_name": self.node_name,
            "endpoints": list(self.endpoints),
            "capabilities": list(self.capabilities),
            "safety_packs": list(self.safety_packs),
            "network_id": self.network_id,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PeerRecord":
        return cls(
            peer_id=str(data["peer_id"]),
            node_name=str(data.get("node_name", "")),
            endpoints=tuple(str(item) for item in data.get("endpoints", ())),
            capabilities=tuple(str(item) for item in data.get("capabilities", ())),
            safety_packs=tuple(str(item) for item in data.get("safety_packs", ())),
            network_id=str(data.get("network_id", "rynmesh-main")),
            updated_at=str(data.get("updated_at", now_iso())),
            metadata=dict(data.get("metadata", {})),
        )


class PeerRegistry(Protocol):
    def publish(self, signed_record: SignedPayload) -> dict[str, Any]: ...
    def list_peers(
        self,
        *,
        network_id: str = "rynmesh-main",
        max_age_hours: float | None = None,
    ) -> list[SignedPayload]: ...
    def publish_job_capacity(self, signed_record: SignedPayload) -> dict[str, Any]: ...
    def list_job_capacities(
        self,
        *,
        network_id: str = "rynmesh-main",
        capability: str = "",
        max_age_hours: float | None = None,
    ) -> list[SignedPayload]: ...
    def submit_work_order(self, signed_order: SignedPayload) -> dict[str, Any]: ...
    def list_work_orders(
        self,
        *,
        network_id: str = "rynmesh-main",
        provider_peer_id: str = "",
        requester_peer_id: str = "",
        capability: str = "",
        status: str = "open",
        max_age_hours: float | None = None,
    ) -> list[SignedPayload]: ...
    def publish_work_result(self, signed_result: SignedPayload) -> dict[str, Any]: ...
    def list_work_results(
        self,
        *,
        work_order_id: str = "",
        network_id: str = "rynmesh-main",
        requester_peer_id: str = "",
        provider_peer_id: str = "",
        status: str = "",
    ) -> list[SignedPayload]: ...
    def deposit_mailbox(self, signed: SignedPayload) -> dict[str, Any]: ...
    def poll_mailbox(self, signed_poll: SignedPayload) -> list[SignedPayload]: ...


def sign_peer_record(record: PeerRecord, *, private_key_bytes: bytes) -> SignedPayload:
    return sign_payload(record.to_dict(), private_key_bytes=private_key_bytes)


def verify_peer_record(signed_record: SignedPayload) -> PeerRecord:
    try:
        verify_signed_payload(signed_record)
        record = PeerRecord.from_dict(signed_record.payload)
    except (KeyError, TypeError, ValueError, SignatureError) as exc:
        raise RegistryError(f"invalid_peer_record: {exc}") from exc
    if record.peer_id != signed_record.public_key:
        raise RegistryError("peer_record_key_mismatch")
    return record


class FilePeerRegistry:
    """Local registry backend used by phase-2 tests and LAN-style demos."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.peers_dir = self.root / "peers"
        self.job_capacity_dir = self.root / "job-capacity"
        self.work_orders_dir = self.root / "work-orders"
        self.work_results_dir = self.root / "work-results"
        self.open_work_orders_dir = self.root / "open-work-orders"
        self.peers_dir.mkdir(parents=True, exist_ok=True)
        self.job_capacity_dir.mkdir(parents=True, exist_ok=True)
        self.work_orders_dir.mkdir(parents=True, exist_ok=True)
        self.work_results_dir.mkdir(parents=True, exist_ok=True)
        self.open_work_orders_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_open_work_order_index()
        self._mailbox: FileMailboxStore | None = None

    @property
    def _open_index_ready_path(self) -> Path:
        return self.open_work_orders_dir / ".index-v1-ready.json"

    def _open_order_marker_path(self, *, provider_peer_id: str, order_path: Path) -> Path:
        return self.open_work_orders_dir / _peer_slug(provider_peer_id) / order_path.name

    def _write_open_order_marker(self, *, provider_peer_id: str, order_path: Path) -> None:
        marker = self._open_order_marker_path(
            provider_peer_id=provider_peer_id,
            order_path=order_path,
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(marker, {"work_order_file": order_path.name})

    def _remove_open_order_marker(self, *, provider_peer_id: str, order_path: Path) -> None:
        marker = self._open_order_marker_path(
            provider_peer_id=provider_peer_id,
            order_path=order_path,
        )
        self._discard_open_order_marker(marker)

    @staticmethod
    def _discard_open_order_marker(marker: Path) -> None:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            # A stale marker is safe: the read path verifies the canonical
            # order and its latest signed result before returning anything.
            pass

    def _initialize_open_work_order_index(self) -> None:
        """Build the auxiliary open-order index once for legacy registries.

        The index is an availability/performance hint, never a trust source:
        every indexed order is read from its canonical file and signature
        checked before it is returned. Supported writers maintain the index
        after this one-time migration.
        """

        ready = self._open_index_ready_path
        if ready.is_file():
            return
        for path in sorted(self.work_orders_dir.glob("*.json")):
            try:
                signed = SignedPayload.from_dict(json.loads(path.read_text(encoding="utf-8")))
                order = verify_work_order(signed)
            except (OSError, json.JSONDecodeError, KeyError, ValueError, JobError):
                continue
            if not order_is_open(order):
                continue
            latest_status = self._latest_work_result_status(
                order.work_order_id,
                network_id=order.network_id,
                provider_peer_id=order.provider_peer_id,
                requester_peer_id=order.requester_peer_id,
            )
            if not latest_status:
                self._write_open_order_marker(
                    provider_peer_id=order.provider_peer_id,
                    order_path=path,
                )
        _write_json_atomic(ready, {"version": 1})

    @property
    def mailbox(self) -> FileMailboxStore:
        """Lazily built: registries that never carry mail never create the spool."""

        if self._mailbox is None:
            self._mailbox = FileMailboxStore(self.root)
        return self._mailbox

    def deposit_mailbox(self, signed: SignedPayload) -> dict[str, Any]:
        return self.mailbox.deposit(signed)

    def poll_mailbox(self, signed_poll: SignedPayload) -> list[SignedPayload]:
        return self.mailbox.poll(signed_poll)

    def sweep_mailbox(self) -> int:
        return self.mailbox.sweep()

    def publish(self, signed_record: SignedPayload) -> dict[str, Any]:
        record = verify_peer_record(signed_record)
        path = self.peers_dir / f"{_peer_slug(record.peer_id)}.json"
        path.write_text(json.dumps(signed_record.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return {
            "status": "registered",
            "peer_id": record.peer_id,
            "peer_slug": _peer_slug(record.peer_id),
            "path": str(path),
        }

    def list_peers(
        self,
        *,
        network_id: str = "rynmesh-main",
        max_age_hours: float | None = None,
    ) -> list[SignedPayload]:
        records: list[SignedPayload] = []
        for path in sorted(self.peers_dir.glob("*.json")):
            try:
                signed = SignedPayload.from_dict(json.loads(path.read_text(encoding="utf-8")))
                record = verify_peer_record(signed)
            except (OSError, json.JSONDecodeError, KeyError, ValueError, RegistryError):
                continue
            if record.network_id == network_id and peer_record_within_age(
                record,
                max_age_hours=max_age_hours,
            ):
                records.append(signed)
        return records

    def publish_job_capacity(self, signed_record: SignedPayload) -> dict[str, Any]:
        record = verify_job_capacity(signed_record)
        path = self.job_capacity_dir / f"{_peer_slug(record.peer_id)}.json"
        _write_json_atomic(path, signed_record.to_dict())
        return {
            "status": "registered",
            "peer_id": record.peer_id,
            "capabilities": list(record.capabilities),
            "path": str(path),
        }

    def list_job_capacities(
        self,
        *,
        network_id: str = "rynmesh-main",
        capability: str = "",
        max_age_hours: float | None = None,
    ) -> list[SignedPayload]:
        records: list[SignedPayload] = []
        wanted = str(capability or "").strip()
        for path in sorted(self.job_capacity_dir.glob("*.json")):
            try:
                signed = SignedPayload.from_dict(json.loads(path.read_text(encoding="utf-8")))
                record = verify_job_capacity(signed)
            except (OSError, json.JSONDecodeError, KeyError, ValueError, JobError):
                continue
            if record.network_id != network_id:
                continue
            if wanted and wanted not in record.capabilities:
                continue
            if not record_within_age(record.updated_at, max_age_hours=max_age_hours):
                continue
            records.append(signed)
        return records

    def submit_work_order(self, signed_order: SignedPayload) -> dict[str, Any]:
        order = verify_work_order(signed_order)
        path = self.work_orders_dir / f"{_peer_slug(order.work_order_id)}.json"
        encoded = json.dumps(signed_order.to_dict(), indent=2, sort_keys=True)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
        except FileExistsError as exc:
            try:
                existing = SignedPayload.from_dict(json.loads(path.read_text(encoding="utf-8")))
                verify_work_order(existing)
            except (OSError, json.JSONDecodeError, KeyError, ValueError, JobError) as read_exc:
                raise RegistryError("work_order_id_conflict") from read_exc
            if existing.to_dict() != signed_order.to_dict():
                raise RegistryError("work_order_id_conflict") from exc
        latest_status = self._latest_work_result_status(
            order.work_order_id,
            network_id=order.network_id,
            provider_peer_id=order.provider_peer_id,
            requester_peer_id=order.requester_peer_id,
        )
        if order_is_open(order) and not latest_status:
            self._write_open_order_marker(
                provider_peer_id=order.provider_peer_id,
                order_path=path,
            )
        else:
            self._remove_open_order_marker(
                provider_peer_id=order.provider_peer_id,
                order_path=path,
            )
        return {
            "status": "submitted",
            "work_order_id": order.work_order_id,
            "provider_peer_id": order.provider_peer_id,
            "path": str(path),
        }

    def list_work_orders(
        self,
        *,
        network_id: str = "rynmesh-main",
        provider_peer_id: str = "",
        requester_peer_id: str = "",
        capability: str = "",
        status: str = "open",
        max_age_hours: float | None = None,
    ) -> list[SignedPayload]:
        wanted_provider = str(provider_peer_id or "").strip()
        wanted_requester = str(requester_peer_id or "").strip()
        wanted_capability = str(capability or "").strip()
        wanted_status = str(status or "").strip()
        if wanted_status == "open":
            return self._list_open_work_orders(
                network_id=network_id,
                provider_peer_id=wanted_provider,
                requester_peer_id=wanted_requester,
                capability=wanted_capability,
                max_age_hours=max_age_hours,
            )

        records: list[SignedPayload] = []
        for path in sorted(self.work_orders_dir.glob("*.json")):
            try:
                signed = SignedPayload.from_dict(json.loads(path.read_text(encoding="utf-8")))
                order = verify_work_order(signed)
            except (OSError, json.JSONDecodeError, KeyError, ValueError, JobError):
                continue
            if order.network_id != network_id:
                continue
            if wanted_provider and order.provider_peer_id != wanted_provider:
                continue
            if wanted_requester and order.requester_peer_id != wanted_requester:
                continue
            if wanted_capability and order.capability != wanted_capability:
                continue
            if not record_within_age(order.created_at, max_age_hours=max_age_hours):
                continue
            latest_status = self._latest_work_result_status(
                order.work_order_id,
                network_id=network_id,
                provider_peer_id=order.provider_peer_id,
                requester_peer_id=order.requester_peer_id,
            )
            if wanted_status == "open":
                if not order_is_open(order):
                    continue
                if latest_status:
                    continue
            elif wanted_status and latest_status != wanted_status:
                continue
            records.append(signed)
        return records

    def _list_open_work_orders(
        self,
        *,
        network_id: str,
        provider_peer_id: str,
        requester_peer_id: str,
        capability: str,
        max_age_hours: float | None,
    ) -> list[SignedPayload]:
        marker_paths = (
            list(
                (
                    self.open_work_orders_dir / _peer_slug(provider_peer_id)
                ).glob("*.json")
            )
            if provider_peer_id
            else list(self.open_work_orders_dir.glob("*/*.json"))
        )
        records: list[SignedPayload] = []
        for marker_path in sorted(marker_paths, key=lambda item: item.name):
            order_path = self.work_orders_dir / marker_path.name
            try:
                signed = SignedPayload.from_dict(
                    json.loads(order_path.read_text(encoding="utf-8"))
                )
                order = verify_work_order(signed)
            except (OSError, json.JSONDecodeError, KeyError, ValueError, JobError):
                self._discard_open_order_marker(marker_path)
                continue
            expected_marker = self._open_order_marker_path(
                provider_peer_id=order.provider_peer_id,
                order_path=order_path,
            )
            if marker_path != expected_marker:
                self._discard_open_order_marker(marker_path)
                continue
            if not order_is_open(order):
                self._discard_open_order_marker(marker_path)
                continue
            latest_status = self._latest_work_result_status(
                order.work_order_id,
                network_id=order.network_id,
                provider_peer_id=order.provider_peer_id,
                requester_peer_id=order.requester_peer_id,
            )
            if latest_status:
                self._discard_open_order_marker(marker_path)
                continue
            if order.network_id != network_id:
                continue
            if provider_peer_id and order.provider_peer_id != provider_peer_id:
                continue
            if requester_peer_id and order.requester_peer_id != requester_peer_id:
                continue
            if capability and order.capability != capability:
                continue
            if not record_within_age(order.created_at, max_age_hours=max_age_hours):
                continue
            records.append(signed)
        return records

    def publish_work_result(self, signed_result: SignedPayload) -> dict[str, Any]:
        result = verify_work_result(signed_result)
        order_path = self.work_orders_dir / f"{_peer_slug(result.work_order_id)}.json"
        try:
            signed_order = SignedPayload.from_dict(
                json.loads(order_path.read_text(encoding="utf-8"))
            )
            order = verify_work_order(signed_order)
        except FileNotFoundError as exc:
            raise RegistryError("work_result_order_not_found") from exc
        except (OSError, json.JSONDecodeError, KeyError, ValueError, JobError) as exc:
            raise RegistryError("work_result_order_invalid") from exc
        if (
            result.work_order_id != order.work_order_id
            or result.network_id != order.network_id
            or result.provider_peer_id != order.provider_peer_id
            or result.requester_peer_id != order.requester_peer_id
        ):
            raise RegistryError("work_result_order_identity_mismatch")
        result_dir = self.work_results_dir / _peer_slug(result.work_order_id)
        result_dir.mkdir(parents=True, exist_ok=True)
        path = result_dir / f"{_peer_slug(signed_result.subject_hash)}.json"
        _write_json_atomic(path, signed_result.to_dict())
        self._remove_open_order_marker(
            provider_peer_id=order.provider_peer_id,
            order_path=order_path,
        )
        return {
            "status": "recorded",
            "work_order_id": result.work_order_id,
            "result_status": result.status,
            "path": str(path),
        }

    def list_work_results(
        self,
        *,
        work_order_id: str = "",
        network_id: str = "rynmesh-main",
        requester_peer_id: str = "",
        provider_peer_id: str = "",
        status: str = "",
    ) -> list[SignedPayload]:
        records: list[tuple[str, SignedPayload]] = []
        wanted_order = str(work_order_id or "").strip()
        wanted_requester = str(requester_peer_id or "").strip()
        wanted_provider = str(provider_peer_id or "").strip()
        wanted_status = str(status or "").strip()
        paths = (
            sorted((self.work_results_dir / _peer_slug(wanted_order)).glob("*.json"))
            if wanted_order
            else sorted(self.work_results_dir.glob("*/*.json"))
        )
        for path in paths:
            try:
                signed = SignedPayload.from_dict(json.loads(path.read_text(encoding="utf-8")))
                result = verify_work_result(signed)
            except (OSError, json.JSONDecodeError, KeyError, ValueError, JobError):
                continue
            if result.network_id != network_id:
                continue
            if wanted_order and result.work_order_id != wanted_order:
                continue
            if wanted_requester and result.requester_peer_id != wanted_requester:
                continue
            if wanted_provider and result.provider_peer_id != wanted_provider:
                continue
            if wanted_status and result.status != wanted_status:
                continue
            records.append((result.created_at, signed))
        records.sort(key=lambda item: item[0])
        return [signed for _, signed in records]

    def _latest_work_result_status(
        self,
        work_order_id: str,
        *,
        network_id: str,
        provider_peer_id: str,
        requester_peer_id: str,
    ) -> str:
        latest = ""
        for signed in self.list_work_results(
            work_order_id=work_order_id,
            network_id=network_id,
            provider_peer_id=provider_peer_id,
            requester_peer_id=requester_peer_id,
        ):
            try:
                result = verify_work_result(signed)
            except JobError:
                continue
            if (
                result.work_order_id != work_order_id
                or result.network_id != network_id
                or result.provider_peer_id != provider_peer_id
                or result.requester_peer_id != requester_peer_id
            ):
                continue
            latest = result.status
        return latest


class HttpPeerRegistry:
    """Small HTTP client for the future rynmesh.ai registry service."""

    def __init__(self, base_url: str, *, timeout_s: float = 10.0) -> None:
        self.base_url = _validate_registry_url(base_url)
        self.timeout_s = float(timeout_s)
        # Envelopes this client refused on the way in. A hostile or broken
        # registry must not be able to abort a poll by injecting one bad
        # record, but a silently shrinking mailbox should still be observable.
        self.dropped_mailbox_messages = 0

    def deposit_mailbox(self, signed: SignedPayload) -> dict[str, Any]:
        body = json.dumps(signed.to_dict()).encode("utf-8")
        req = Request(
            f"{self.base_url}/api/v1/mailbox/deposit",
            data=body,
            headers={
                "content-type": "application/json",
                "user-agent": RYNMESH_USER_AGENT,
            },
            method="POST",
        )
        payload = self._json(req)
        if not isinstance(payload, dict):
            raise RegistryError("registry_response_not_object")
        return payload

    def poll_mailbox(self, signed_poll: SignedPayload) -> list[SignedPayload]:
        body = json.dumps(signed_poll.to_dict()).encode("utf-8")
        req = Request(
            f"{self.base_url}/api/v1/mailbox/poll",
            data=body,
            headers={
                "content-type": "application/json",
                "user-agent": RYNMESH_USER_AGENT,
            },
            method="POST",
        )
        payload = self._json(req)
        messages = (
            payload.get("messages", []) if isinstance(payload, dict)
            else payload if isinstance(payload, list) else None
        )
        if not isinstance(messages, list):
            raise RegistryError("registry response missing messages list")
        # A registry that hands back somebody else's mail is either broken or
        # hostile; either way it is not this peer's to hold.
        expected_to_peer_id = str(signed_poll.payload.get("peer_id") or "")
        records: list[SignedPayload] = []
        for item in messages:
            if not isinstance(item, dict):
                self.dropped_mailbox_messages += 1
                continue
            try:
                signed = SignedPayload.from_dict(item)
                envelope = verify_mailbox_envelope(signed)
            except (KeyError, TypeError, ValueError, MailboxError):
                self.dropped_mailbox_messages += 1
                continue
            if envelope.to_peer_id != expected_to_peer_id:
                self.dropped_mailbox_messages += 1
                continue
            records.append(signed)
        return records

    def publish(self, signed_record: SignedPayload) -> dict[str, Any]:
        body = json.dumps(signed_record.to_dict()).encode("utf-8")
        req = Request(
            f"{self.base_url}/api/v1/peers/register",
            data=body,
            headers={
                "content-type": "application/json",
                "user-agent": RYNMESH_USER_AGENT,
            },
            method="POST",
        )
        return self._json(req)

    def publish_job_capacity(self, signed_record: SignedPayload) -> dict[str, Any]:
        body = json.dumps(signed_record.to_dict()).encode("utf-8")
        req = Request(
            f"{self.base_url}/api/v1/jobs/capacity/register",
            data=body,
            headers={
                "content-type": "application/json",
                "user-agent": RYNMESH_USER_AGENT,
            },
            method="POST",
        )
        return self._json(req)

    def list_job_capacities(
        self,
        *,
        network_id: str = "rynmesh-main",
        capability: str = "",
        max_age_hours: float | None = None,
    ) -> list[SignedPayload]:
        params: dict[str, str] = {"network_id": network_id}
        if capability:
            params["capability"] = capability
        if max_age_hours and max_age_hours > 0:
            params["max_age_hours"] = str(float(max_age_hours))
        payload = self._json(
            Request(
                f"{self.base_url}/api/v1/jobs/capacity?{urlencode(params)}",
                headers={"user-agent": RYNMESH_USER_AGENT},
                method="GET",
            )
        )
        records = payload.get("capacities", payload if isinstance(payload, list) else [])
        if not isinstance(records, list):
            raise RegistryError("registry response missing capacities list")
        return _signed_payload_list(records, verify_job_capacity)

    def submit_work_order(self, signed_order: SignedPayload) -> dict[str, Any]:
        body = json.dumps(signed_order.to_dict()).encode("utf-8")
        req = Request(
            f"{self.base_url}/api/v1/jobs/work-orders",
            data=body,
            headers={
                "content-type": "application/json",
                "user-agent": RYNMESH_USER_AGENT,
            },
            method="POST",
        )
        return self._json(req)

    def list_work_orders(
        self,
        *,
        network_id: str = "rynmesh-main",
        provider_peer_id: str = "",
        requester_peer_id: str = "",
        capability: str = "",
        status: str = "open",
        max_age_hours: float | None = None,
    ) -> list[SignedPayload]:
        params: dict[str, str] = {"network_id": network_id}
        if provider_peer_id:
            params["provider_peer_id"] = provider_peer_id
        if requester_peer_id:
            params["requester_peer_id"] = requester_peer_id
        if capability:
            params["capability"] = capability
        if status:
            params["status"] = status
        if max_age_hours and max_age_hours > 0:
            params["max_age_hours"] = str(float(max_age_hours))
        payload = self._json(
            Request(
                f"{self.base_url}/api/v1/jobs/work-orders?{urlencode(params)}",
                headers={"user-agent": RYNMESH_USER_AGENT},
                method="GET",
            )
        )
        records = payload.get("work_orders", payload if isinstance(payload, list) else [])
        if not isinstance(records, list):
            raise RegistryError("registry response missing work_orders list")
        return _signed_payload_list(records, verify_work_order)

    def publish_work_result(self, signed_result: SignedPayload) -> dict[str, Any]:
        body = json.dumps(signed_result.to_dict()).encode("utf-8")
        req = Request(
            f"{self.base_url}/api/v1/jobs/work-results",
            data=body,
            headers={
                "content-type": "application/json",
                "user-agent": RYNMESH_USER_AGENT,
            },
            method="POST",
        )
        return self._json(req)

    def list_work_results(
        self,
        *,
        work_order_id: str = "",
        network_id: str = "rynmesh-main",
        requester_peer_id: str = "",
        provider_peer_id: str = "",
        status: str = "",
    ) -> list[SignedPayload]:
        params: dict[str, str] = {"network_id": network_id}
        if work_order_id:
            params["work_order_id"] = work_order_id
        if requester_peer_id:
            params["requester_peer_id"] = requester_peer_id
        if provider_peer_id:
            params["provider_peer_id"] = provider_peer_id
        if status:
            params["status"] = status
        payload = self._json(
            Request(
                f"{self.base_url}/api/v1/jobs/work-results?{urlencode(params)}",
                headers={"user-agent": RYNMESH_USER_AGENT},
                method="GET",
            )
        )
        records = payload.get("work_results", payload if isinstance(payload, list) else [])
        if not isinstance(records, list):
            raise RegistryError("registry response missing work_results list")
        return _signed_payload_list(records, verify_work_result)

    def list_peers(
        self,
        *,
        network_id: str = "rynmesh-main",
        max_age_hours: float | None = None,
    ) -> list[SignedPayload]:
        params: dict[str, str] = {"network_id": network_id}
        if max_age_hours and max_age_hours > 0:
            params["max_age_hours"] = str(float(max_age_hours))
        query = urlencode(params)
        req = Request(
            f"{self.base_url}/api/v1/peers?{query}",
            headers={"user-agent": RYNMESH_USER_AGENT},
            method="GET",
        )
        payload = self._json(req)
        peers = payload.get("peers", payload if isinstance(payload, list) else [])
        if not isinstance(peers, list):
            raise RegistryError("registry response missing peers list")
        records: list[SignedPayload] = []
        for item in peers:
            if not isinstance(item, dict):
                continue
            signed = SignedPayload.from_dict(item)
            verify_peer_record(signed)
            records.append(signed)
        return records

    def _json(self, req: Request) -> dict[str, Any] | list[Any]:
        try:
            with _open_registry_request(req, timeout_s=self.timeout_s) as response:
                raw_bytes = response.read(MAX_REGISTRY_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_REGISTRY_RESPONSE_BYTES:
                    raise RegistryError("registry_response_too_large")
                raw = raw_bytes.decode("utf-8")
        except HTTPError as exc:
            raise RegistryError(
                f"registry_http_error: {exc}",
                status=int(getattr(exc, "code", 0) or 0) or None,
                detail=_http_error_detail(exc),
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise RegistryError(f"registry_http_error: {exc}") from exc
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise RegistryError(f"registry_invalid_json: {exc}") from exc
        if not isinstance(payload, (dict, list)):
            raise RegistryError("registry_response_not_object_or_list")
        return payload


def _http_error_detail(exc: HTTPError) -> str:
    """Best-effort ``detail`` from a JSON error body; "" when there isn't one."""

    try:
        body = exc.read(MAX_REGISTRY_RESPONSE_BYTES + 1)
    except (OSError, ValueError):
        return ""
    if len(body) > MAX_REGISTRY_RESPONSE_BYTES:
        return ""
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, str) else ""


def _signed_payload_list(
    records: list[Any],
    validator: Callable[[SignedPayload], Any],
) -> list[SignedPayload]:
    signed_records: list[SignedPayload] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        # One non-conforming record must not poison the whole listing: a
        # single hostile (or legacy) signed order would otherwise abort every
        # poll until it aged out. FilePeerRegistry already skips bad records;
        # the HTTP client path gets the same tolerance.
        try:
            signed = SignedPayload.from_dict(item)
            validator(signed)
        except (KeyError, TypeError, ValueError, JobError):
            continue
        signed_records.append(signed)
    return signed_records


def _open_registry_request(req: Request, *, timeout_s: float):
    from .transport import network_key_header

    for name, value in network_key_header().items():
        req.add_header(name, value)
    kwargs: dict[str, Any] = {"timeout": timeout_s}
    if str(req.full_url).startswith("https://"):
        kwargs["context"] = _https_context()
    return urlopen(req, **kwargs)


def _https_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def default_peer_registry(network_dir: Path) -> PeerRegistry:
    registry_url = os.environ.get("RYNMESH_REGISTRY_URL", "").strip()
    if registry_url:
        return HttpPeerRegistry(registry_url)
    return FilePeerRegistry(default_registry_dir(network_dir))


def _validate_registry_url(url: str) -> str:
    cleaned = str(url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise RegistryError("registry_url_scheme_unsupported")
    if not parsed.hostname:
        raise RegistryError("registry_url_host_required")
    if parsed.username or parsed.password or parsed.fragment:
        raise RegistryError("registry_url_not_allowed")
    if _host_blocked(parsed.hostname):
        raise RegistryError("registry_url_host_blocked")
    return cleaned


def peer_record_within_age(record: PeerRecord, *, max_age_hours: float | None) -> bool:
    if not max_age_hours or max_age_hours <= 0:
        return True
    updated_at = _parse_time(record.updated_at)
    if updated_at is None:
        return False
    return updated_at >= datetime.now(timezone.utc) - timedelta(hours=float(max_age_hours))


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _host_blocked(host: str) -> bool:
    normalized = host.strip("[]").lower()
    if normalized in BLOCKED_REGISTRY_HOSTS:
        return True
    try:
        import ipaddress

        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return ip.is_link_local
