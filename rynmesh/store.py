"""File-backed Rynmesh node store.

This is the local node store for a Rynmesh peer. HTTP registry discovery and
direct peer HTTP are the primary multi-machine path; the shared-directory
announcement bus remains as a local development compatibility layer.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .credits import GENERAL_CATEGORY, GLOBAL_CATEGORY, FileCreditLedger
from .crypto import SignedPayload, b64, public_key_from_private, sha256_bytes, unb64
from .identity import (
    IdentityPolicy,
    IdentityTier,
    assess_identity,
)
from .jobs import (
    JobCapacityRecord,
    JobError,
    WorkOrder,
    WorkResult,
    default_expires_at,
    new_work_order_id,
    sign_job_capacity,
    sign_work_order,
    sign_work_result,
    validate_capability_params,
    verify_job_capacity,
    verify_work_order,
    verify_work_result,
)
from .manifest import ManifestValidation, build_media_asset, make_manifest, validate_manifest
from .provenance import (
    LINK_SAFETY_SCAN,
    LINK_WORK_ENVELOPE,
    append_link,
    make_genesis_link,
)
from .registry import (
    PeerRecord,
    RegistryError,
    default_peer_registry,
    peer_record_within_age,
    sign_peer_record,
    verify_peer_record,
)
from .relay import HttpRelayClient
from .safety import KeywordSafetyScanner, SafetyPolicy, sign_safety_receipt
from .types import MediaAsset, RunReceipt

DEFAULT_PREVIEW_BYTES = 256 * 1024


class StoreError(RuntimeError):
    pass


def default_rynmesh_home() -> Path:
    return Path(os.environ.get("RYNMESH_HOME", Path.home() / ".rynmesh")).expanduser()


def default_network_dir() -> Path:
    return Path(
        os.environ.get("RYNMESH_NETWORK_DIR", default_rynmesh_home() / "network")
    ).expanduser()


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if len(cleaned) > 96:
        return cleaned[:48] + "_" + sha256_bytes(value.encode("utf-8")).split(":", 1)[1][:16]
    return cleaned or "item"


def _hash_hex(value: str) -> str:
    return sha256_bytes(value.encode("utf-8")).split(":", 1)[1]


def _read_terms(env_name: str) -> tuple[str, ...]:
    raw = os.environ.get(env_name, "")
    return tuple(term.strip() for term in raw.split(",") if term.strip())


def _read_preview_bytes(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(DEFAULT_PREVIEW_BYTES)


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _machine_name() -> str:
    configured = os.environ.get("RYNMESH_MACHINE_NAME", "").strip()
    if configured:
        return configured
    for candidate in (os.environ.get("COMPUTERNAME", ""), socket.gethostname()):
        if candidate:
            return candidate.split(".", 1)[0]
    return "ryn-node"


def _local_ip_addresses() -> tuple[str, ...]:
    addresses: set[str] = set()
    configured = os.environ.get("RYNMESH_MACHINE_IP", "").strip()
    if configured:
        addresses.add(configured)
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            if address and not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect(("1.1.1.1", 80))
            address = sock.getsockname()[0]
            if address and not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    return tuple(sorted(addresses))


def _primary_lan_ip() -> str:
    addresses = _local_ip_addresses()
    return addresses[0] if addresses else ""


def _is_unspecified_host(host: str) -> bool:
    return host.strip().lower() in {"0.0.0.0", "::", "[::]"}


def _manifest_safety_outcome(manifest: Any) -> str:
    for signed_receipt in getattr(manifest, "safety_receipts", ()):
        if not isinstance(signed_receipt, dict):
            continue
        payload = signed_receipt.get("payload", {})
        if isinstance(payload, dict) and payload.get("outcome"):
            return str(payload["outcome"])
    return "unscanned"


def _guess_content_type(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _guess_content_kind(content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if "/" not in normalized:
        return "data"
    family, subtype = normalized.split("/", 1)
    if subtype in {"csv", "json", "jsonl", "x-ndjson"} or "json" in subtype:
        return "dataset"
    if normalized == "application/pdf":
        return "document"
    if "presentation" in subtype or "powerpoint" in subtype:
        return "presentation"
    if "spreadsheet" in subtype or "excel" in subtype:
        return "spreadsheet"
    if family in {"video", "image", "audio", "text"}:
        return family
    return "data"


def _content_kind_from_tags(tags: tuple[str, ...], content_type: str) -> str:
    for tag in tags:
        if tag.startswith("content-kind:"):
            return tag.split(":", 1)[1] or _guess_content_kind(content_type)
    return _guess_content_kind(content_type)


def _content_kind_from_asset(asset: MediaAsset, tags: tuple[str, ...]) -> str:
    return asset.content_kind or _content_kind_from_tags(tags, asset.media_type)


def _generate_private_key() -> bytes:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Rynmesh identity generation requires `cryptography`") from exc
    return Ed25519PrivateKey.generate().private_bytes_raw()


def load_or_create_private_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return unb64(path.read_text(encoding="utf-8").strip())
    private_key = _generate_private_key()
    path.write_text(b64(private_key) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return private_key


@dataclass
class ClipRecord:
    clip_id: str
    manifest: SignedPayload
    source_peer_id: str
    title: str
    created_at: str
    preview_path: str = ""
    media_path: str = ""
    content_id: str = ""
    local: bool = False
    validation_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "content_id": self.content_id or self.clip_id,
            "source_peer_id": self.source_peer_id,
            "title": self.title,
            "created_at": self.created_at,
            "preview_path": self.preview_path,
            "media_path": self.media_path,
            "local": self.local,
            "validation_errors": list(self.validation_errors),
            "manifest": self.manifest.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClipRecord":
        return cls(
            clip_id=str(data["clip_id"]),
            manifest=SignedPayload.from_dict(dict(data["manifest"])),
            source_peer_id=str(data.get("source_peer_id", "")),
            title=str(data.get("title", "")),
            created_at=str(data.get("created_at", "")),
            preview_path=str(data.get("preview_path", "")),
            media_path=str(data.get("media_path", "")),
            content_id=str(data.get("content_id", data["clip_id"])),
            local=bool(data.get("local", False)),
            validation_errors=list(data.get("validation_errors", [])),
        )


class RynmeshStore:
    """Persistent local node state plus a shared-directory mesh bus."""

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        network_dir: str | Path | None = None,
        node_name: str | None = None,
        policy: SafetyPolicy | None = None,
        identity_policy: IdentityPolicy | None = None,
        trusted_root_peer_ids: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self.home = Path(home).expanduser() if home is not None else default_rynmesh_home()
        self.network_dir = (
            Path(network_dir).expanduser() if network_dir is not None else default_network_dir()
        )
        self.node_name = node_name or os.environ.get("RYNMESH_NODE_NAME", self.home.name)
        self.private_key_bytes = load_or_create_private_key(self.home / "identity.ed25519")
        self.policy = policy or SafetyPolicy()
        self.scanner = KeywordSafetyScanner(
            blocked_terms=_read_terms("RYNMESH_BLOCKED_TERMS"),
            warning_terms=_read_terms("RYNMESH_WARNING_TERMS"),
        )
        self.home.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.peer_cache_dir.mkdir(parents=True, exist_ok=True)
        self.announcements_dir.mkdir(parents=True, exist_ok=True)
        self.network_peer_dir.mkdir(parents=True, exist_ok=True)
        self.identity_dir.mkdir(parents=True, exist_ok=True)
        self.identity_policy = identity_policy or IdentityPolicy()
        env_roots = os.environ.get("RYNMESH_TRUSTED_ROOTS", "")
        env_root_set = {root.strip() for root in env_roots.split(",") if root.strip()}
        self.trusted_root_peer_ids: tuple[str, ...] = tuple(
            sorted(env_root_set | set(trusted_root_peer_ids or ()))
        )
        self.registry = default_peer_registry(self.network_dir)
        # What this process has already advertised per network, so concurrent
        # services on one node merge into the single per-peer capacity record
        # instead of overwriting each other. See register_job_capacity.
        self._advertised_capacity: dict[str, dict[str, Any]] = {}
        self.credit_ledger = FileCreditLedger(
            self.network_dir / "credits",
            tier_resolver=self._resolve_identity_tier,
        )

    @property
    def peer_id(self) -> str:
        return public_key_from_private(self.private_key_bytes)

    @property
    def peer_slug(self) -> str:
        return _hash_hex(self.peer_id)[:16]

    @property
    def clips_dir(self) -> Path:
        return self.home / "clips"

    @property
    def announcements_dir(self) -> Path:
        return self.network_dir / "announcements"

    @property
    def network_peer_dir(self) -> Path:
        return self.network_dir / "peers" / self.peer_slug

    @property
    def identity_dir(self) -> Path:
        return self.home / "identity-claims"

    def _load_signed_claims(self, kind: str) -> list[SignedPayload]:
        bucket = self.identity_dir / kind
        if not bucket.exists():
            return []
        loaded: list[SignedPayload] = []
        for path in sorted(bucket.glob("*.json")):
            try:
                loaded.append(SignedPayload.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
        return loaded

    def _trusted_root_ids(self) -> set[str]:
        # The local node is its own implicit trust root. Operators can extend
        # this via the ``trusted_root_peer_ids`` constructor arg or
        # ``RYNMESH_TRUSTED_ROOTS`` env var. Vouches issued by these roots
        # promote remote peers per ``identity_policy``.
        return {self.peer_id} | set(self.trusted_root_peer_ids)

    def _resolve_identity_tier(self, peer_id: str) -> IdentityTier:
        if peer_id == self.peer_id:
            return IdentityTier.ATTESTED
        assessment = assess_identity(
            peer_id=peer_id,
            vouches=self._load_signed_claims("peer_vouch"),
            stake_commitments=self._load_signed_claims("stake_commitment"),
            proofs=self._load_signed_claims("proof_of_resource"),
            trusted_vouch_issuers=self._trusted_root_ids(),
            policy=self.identity_policy,
        )
        return assessment.tier

    @property
    def peer_cache_dir(self) -> Path:
        return self.home / "peer-cache"

    def node_info(self) -> dict[str, Any]:
        peer_endpoint = self._default_peer_endpoint()
        return {
            "node_name": self.node_name,
            "peer_id": self.peer_id,
            "peer_slug": self.peer_slug,
            "home": str(self.home),
            "network_dir": str(self.network_dir),
            "peer_endpoint": peer_endpoint,
            "machine_name": self.node_name or _machine_name(),
            "hostname": socket.gethostname(),
            "primary_ip": _primary_lan_ip(),
            "ip_addresses": list(_local_ip_addresses()),
            "registry": self._registry_info(),
            "relay": self._relay_info(),
            "credits": self.credit_summary(peer_id=self.peer_id),
            "blocked_terms": list(self.scanner.blocked_terms),
            "warning_terms": list(self.scanner.warning_terms),
        }

    def messaging_public_key(self) -> str:
        """This node's X25519 messaging public key, base64.

        Lives beside the identity key under ``self.home``. ``create_app`` loads
        the same path for ``/api/peer/pubkey`` and for the mailbox client, so
        the advertised key, the served key and the decrypting key are one key
        even when ``$RYNMESH_HOME`` points somewhere else.
        """
        from .services import peer_box

        try:
            key = peer_box.load_or_create_messaging_key(self.home / "messaging.x25519")
        except (OSError, ValueError):
            return ""
        return peer_box.public_key_b64(key)

    def register_node(
        self,
        *,
        endpoints: list[str] | tuple[str, ...] | None = None,
        capabilities: list[str] | tuple[str, ...] | None = None,
        network_id: str = "rynmesh-main",
    ) -> dict[str, Any]:
        active_endpoints = tuple(endpoints or self._default_registration_endpoints())
        record = PeerRecord(
            peer_id=self.peer_id,
            node_name=self.node_name,
            endpoints=active_endpoints,
            capabilities=tuple(capabilities or ("publish", "seed", "verify")),
            network_id=network_id,
            metadata={
                "peer_slug": self.peer_slug,
                "network_dir": str(self.network_dir),
                "machine_name": self.node_name or _machine_name(),
                "hostname": socket.gethostname(),
                "primary_ip": _primary_lan_ip(),
                "ip_addresses": list(_local_ip_addresses()),
                "peer_endpoint": active_endpoints[0] if active_endpoints else "",
                # How a peer that cannot reach this node's endpoint (both
                # behind NATs) still seals mailbox messages for it. The record
                # is signed, so the key is as trustworthy as the peer id.
                "messaging_pub": self.messaging_public_key(),
            },
        )
        signed = sign_peer_record(record, private_key_bytes=self.private_key_bytes)
        result = self.registry.publish(signed)
        credit = self.record_credit_event(
            kind="node_joined",
            role="node",
            category=GENERAL_CATEGORY,
            subject_id=network_id,
            evidence_hash=signed.subject_hash,
            reason="node registered with Rynmesh discovery",
            dedupe_key=f"node_joined:{network_id}:{self.peer_id}",
        )
        result["record"] = record.to_dict()
        result["record_hash"] = signed.subject_hash
        result["credit"] = credit
        return result

    def register_job_capacity(
        self,
        *,
        capabilities: list[str] | tuple[str, ...],
        network_id: str = "rynmesh-main",
        capacity_units: int = 1,
        max_concurrent: int = 1,
        price_credits: dict[str, float] | None = None,
        polling_interval_sec: int = 30,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cleaned_capabilities = tuple(str(item).strip() for item in capabilities if str(item).strip())
        if not cleaned_capabilities:
            raise ValueError("capabilities are required")
        # The registry stores ONE capacity record per peer, replaced wholesale.
        # Multiple services on one node (LLM provider, video, egress) each
        # register their own capability list, and the LLM provider republishes
        # every 30s — without merging, every publication wipes the others'
        # capabilities out of discovery. Merge against what this process has
        # already advertised: capabilities union, metadata/price update.
        merged = self._advertised_capacity.setdefault(network_id, {
            "capabilities": set(), "price_credits": {}, "metadata": {},
        })
        merged["capabilities"].update(cleaned_capabilities)
        merged["price_credits"].update(dict(price_credits or {}))
        merged["metadata"].update(dict(metadata or {}))
        record = JobCapacityRecord(
            peer_id=self.peer_id,
            node_name=self.node_name,
            capabilities=tuple(sorted(merged["capabilities"])),
            network_id=network_id,
            capacity_units=max(1, int(capacity_units or 1)),
            max_concurrent=max(1, int(max_concurrent or 1)),
            price_credits=dict(merged["price_credits"]),
            polling_interval_sec=max(1, int(polling_interval_sec or 30)),
            metadata=dict(merged["metadata"]),
        )
        signed = sign_job_capacity(record, private_key_bytes=self.private_key_bytes)
        result = self.registry.publish_job_capacity(signed)
        credit = self.record_credit_event(
            kind="job_capacity_advertised",
            role="provider",
            category="jobs",
            subject_id=network_id,
            evidence_hash=signed.subject_hash,
            reason="node advertised polling job capacity",
            dedupe_key=f"job_capacity:{network_id}:{self.peer_id}",
            metadata={"capabilities": list(cleaned_capabilities)},
        )
        result["record"] = record.to_dict()
        result["record_hash"] = signed.subject_hash
        result["credit"] = credit
        return result

    def list_job_capacities(
        self,
        *,
        network_id: str = "rynmesh-main",
        capability: str = "",
        max_age_hours: float | None = None,
    ) -> dict[str, Any]:
        signed_records = self.registry.list_job_capacities(
            network_id=network_id,
            capability=capability,
            max_age_hours=max_age_hours,
        )
        capacities = []
        for signed in signed_records:
            record = verify_job_capacity(signed)
            payload = record.to_dict()
            payload["record_hash"] = signed.subject_hash
            payload["signature"] = signed.signature
            capacities.append(payload)
        capacities.sort(key=lambda item: (item.get("node_name", ""), item.get("peer_id", "")))
        return {"network_id": network_id, "capacities": capacities}

    def submit_work_order(
        self,
        *,
        provider_peer_id: str,
        capability: str,
        operation: str,
        params: dict[str, Any] | None = None,
        network_id: str = "rynmesh-main",
        input_content_ids: list[str] | tuple[str, ...] | None = None,
        max_credit_cost: float = 0.0,
        idempotency_key: str = "",
        result_policy: dict[str, Any] | None = None,
        expires_at: str = "",
        expires_in_hours: float = 6.0,
    ) -> dict[str, Any]:
        provider = str(provider_peer_id or "").strip()
        if not provider:
            raise ValueError("provider_peer_id is required")
        cleaned_capability = str(capability or "").strip()
        if not cleaned_capability:
            raise ValueError("capability is required")
        cleaned_operation = str(operation or "").strip()
        if not cleaned_operation:
            raise ValueError("operation is required")
        cleaned_params = dict(params or {})
        try:
            validate_capability_params(cleaned_capability, cleaned_operation, cleaned_params)
        except JobError as exc:
            raise ValueError(str(exc)) from exc
        order = WorkOrder(
            work_order_id=new_work_order_id(),
            requester_peer_id=self.peer_id,
            provider_peer_id=provider,
            capability=cleaned_capability,
            operation=cleaned_operation,
            params=cleaned_params,
            network_id=network_id,
            input_content_ids=tuple(str(item) for item in (input_content_ids or ()) if str(item)),
            max_credit_cost=float(max_credit_cost or 0.0),
            idempotency_key=str(idempotency_key or ""),
            result_policy=dict(result_policy or {}),
            expires_at=str(expires_at or "") or default_expires_at(expires_in_hours),
        )
        signed = sign_work_order(order, private_key_bytes=self.private_key_bytes)
        result = self.registry.submit_work_order(signed)
        result["order"] = order.to_dict()
        result["order_hash"] = signed.subject_hash
        return result

    def poll_work_orders(
        self,
        *,
        network_id: str = "rynmesh-main",
        capability: str = "",
        status: str = "open",
        max_age_hours: float | None = None,
    ) -> dict[str, Any]:
        signed_orders = self.registry.list_work_orders(
            network_id=network_id,
            provider_peer_id=self.peer_id,
            capability=capability,
            status=status,
            max_age_hours=max_age_hours,
        )
        orders = []
        for signed in signed_orders:
            order = verify_work_order(signed)
            payload = order.to_dict()
            payload["order_hash"] = signed.subject_hash
            payload["signature"] = signed.signature
            orders.append(payload)
        orders.sort(key=lambda item: (item.get("created_at", ""), item.get("work_order_id", "")))
        return {"network_id": network_id, "work_orders": orders}

    def publish_work_result(
        self,
        *,
        work_order_id: str,
        requester_peer_id: str,
        status: str,
        message: str = "",
        result_content_ids: list[str] | tuple[str, ...] | None = None,
        result_refs: dict[str, Any] | None = None,
        credit_amount: float = 0.0,
        network_id: str = "rynmesh-main",
    ) -> dict[str, Any]:
        result = WorkResult(
            work_order_id=str(work_order_id or "").strip(),
            provider_peer_id=self.peer_id,
            requester_peer_id=str(requester_peer_id or "").strip(),
            status=str(status or "").strip(),
            message=str(message or ""),
            result_content_ids=tuple(str(item) for item in (result_content_ids or ()) if str(item)),
            result_refs=dict(result_refs or {}),
            credit_amount=float(credit_amount or 0.0),
            network_id=network_id,
        )
        if not result.work_order_id:
            raise ValueError("work_order_id is required")
        if not result.requester_peer_id:
            raise ValueError("requester_peer_id is required")
        signed = sign_work_result(result, private_key_bytes=self.private_key_bytes)
        registry_result = self.registry.publish_work_result(signed)
        if result.status == "completed" and result.credit_amount > 0:
            registry_result["credit"] = self.record_credit_event(
                kind="work_order_completed",
                amount=result.credit_amount,
                role="provider",
                category="jobs",
                subject_id=result.work_order_id,
                evidence_hash=signed.subject_hash,
                reason="provider completed a signed work order",
                dedupe_key=f"work_order_completed:{result.work_order_id}:{self.peer_id}",
                metadata={
                    "requester_peer_id": result.requester_peer_id,
                    "result_content_ids": list(result.result_content_ids),
                },
            )
        registry_result["result"] = result.to_dict()
        registry_result["result_hash"] = signed.subject_hash
        return registry_result

    def list_work_results(
        self,
        *,
        work_order_id: str = "",
        network_id: str = "rynmesh-main",
        requester_peer_id: str = "",
        provider_peer_id: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        signed_results = self.registry.list_work_results(
            work_order_id=work_order_id,
            network_id=network_id,
            requester_peer_id=requester_peer_id,
            provider_peer_id=provider_peer_id,
            status=status,
        )
        results = []
        for signed in signed_results:
            result = verify_work_result(signed)
            payload = result.to_dict()
            payload["result_hash"] = signed.subject_hash
            payload["signature"] = signed.signature
            results.append(payload)
        results.sort(key=lambda item: (item.get("created_at", ""), item.get("work_order_id", "")))
        return {"network_id": network_id, "work_results": results}

    def upload_relay_artifact(
        self,
        path: str | Path,
        *,
        relay_url: str = "",
        media_type: str = "",
        filename: str = "",
        expected_hash: str = "",
    ) -> dict[str, Any]:
        source = Path(path).expanduser()
        client = self._relay_client(relay_url)
        return client.upload_file(
            source,
            media_type=media_type or _guess_content_type(source),
            filename=filename or source.name,
            uploader_peer_id=self.peer_id,
            expected_hash=expected_hash,
        )

    def download_relay_artifact(
        self,
        content_hash: str,
        destination: str | Path,
        *,
        relay_url: str = "",
    ) -> dict[str, Any]:
        client = self._relay_client(relay_url)
        return client.download_blob(content_hash, destination)

    def relay_artifact_info(
        self,
        content_hash: str,
        *,
        relay_url: str = "",
    ) -> dict[str, Any]:
        client = self._relay_client(relay_url)
        return client.blob_info(content_hash)

    def discover_peers(
        self,
        *,
        network_id: str = "rynmesh-main",
        include_self: bool = False,
        max_age_hours: float | None = None,
        use_cache_on_error: bool = True,
    ) -> dict[str, Any]:
        source = "registry"
        try:
            signed_records = self.registry.list_peers(
                network_id=network_id,
                max_age_hours=max_age_hours,
            )
            self._cache_peer_records(network_id, signed_records)
        except RegistryError:
            if not use_cache_on_error:
                raise
            signed_records = self._cached_peer_records(
                network_id=network_id,
                max_age_hours=max_age_hours,
            )
            source = "cache"

        peers: list[dict[str, Any]] = []
        for signed in signed_records:
            record = verify_peer_record(signed)
            if not include_self and record.peer_id == self.peer_id:
                continue
            item = record.to_dict()
            item["peer_slug"] = _hash_hex(record.peer_id)[:16]
            item["record_hash"] = signed.subject_hash
            item["discovery_source"] = source
            peers.append(item)
        peers.sort(key=lambda item: (item.get("node_name", ""), item.get("peer_id", "")))
        return {"peers": peers, "network_id": network_id, "source": source}

    def publish_clip(
        self,
        media_path: str | Path,
        *,
        title: str = "",
        description: str = "",
        transcript: str = "",
        preview_path: str | Path | None = None,
        run_id: str = "",
        work_id: str = "",
        envelope_hash: str = "",
        category: str = GENERAL_CATEGORY,
        content_type: str = "",
        content_kind: str = "video",
    ) -> dict[str, Any]:
        return self.publish_content(
            media_path,
            title=title,
            description=description,
            transcript=transcript,
            preview_path=preview_path,
            run_id=run_id,
            work_id=work_id,
            envelope_hash=envelope_hash,
            category=category,
            content_type=content_type,
            content_kind=content_kind,
            _credit_kind="clip_published",
        )

    def publish_content(
        self,
        content_path: str | Path,
        *,
        title: str = "",
        description: str = "",
        transcript: str = "",
        summary_text: str = "",
        preview_path: str | Path | None = None,
        run_id: str = "",
        work_id: str = "",
        envelope_hash: str = "",
        category: str = GENERAL_CATEGORY,
        content_type: str = "",
        content_kind: str = "",
        tags: list[str] | tuple[str, ...] | None = None,
        model_id: str = "rynmesh-unspecified-model",
        prompt_hash: str = "",
        _credit_kind: str = "content_published",
    ) -> dict[str, Any]:
        media = Path(content_path).expanduser().resolve()
        if not media.exists():
            raise FileNotFoundError(media)

        active_content_type = content_type or _guess_content_type(media)
        active_content_kind = content_kind or _guess_content_kind(active_content_type)
        scan_text = transcript or summary_text
        preview_bytes = (
            Path(preview_path).expanduser().resolve().read_bytes()
            if preview_path
            else _read_preview_bytes(media)
        )
        asset = self._build_asset(
            media,
            preview_bytes=preview_bytes,
            transcript=scan_text,
            content_type=active_content_type,
            content_kind=active_content_kind,
        )
        safety = self.scanner.scan(clip_id=asset.clip_id, transcript=scan_text)
        signed_safety = sign_safety_receipt(safety, private_key_bytes=self.private_key_bytes)
        run_receipt = RunReceipt(
            run_id=run_id or f"rynmesh_content_{uuid.uuid4().hex[:16]}",
            work_id=work_id or sha256_bytes(f"rynmesh.content:{asset.clip_id}".encode("utf-8")),
            envelope_hash=envelope_hash or sha256_bytes(
                json.dumps(
                    {
                        "clip_id": asset.clip_id,
                        "content_id": asset.clip_id,
                        "media_hash": asset.media_hash,
                        "content_hash": asset.media_hash,
                        "transcript_hash": asset.transcript_hash,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ),
            workflow="@rynmesh.publish_content",
        )
        provenance_chain = [
            make_genesis_link(
                content_id=asset.clip_id,
                publisher_peer_id=self.peer_id,
                private_key_bytes=self.private_key_bytes,
                run_id=run_receipt.run_id,
                model_id=model_id,
                prompt_hash=prompt_hash,
            )
        ]
        append_link(
            provenance_chain,
            link_type=LINK_WORK_ENVELOPE,
            issuer_peer_id=self.peer_id,
            private_key_bytes=self.private_key_bytes,
            payload={
                "work_id": run_receipt.work_id,
                "envelope_hash": run_receipt.envelope_hash,
                "replay_hash": run_receipt.replay_hash,
            },
        )
        append_link(
            provenance_chain,
            link_type=LINK_SAFETY_SCAN,
            issuer_peer_id=self.peer_id,
            private_key_bytes=self.private_key_bytes,
            payload={
                "scanner_id": safety.scanner_id,
                "scanner_version": safety.scanner_version,
                "outcome": safety.outcome.value,
                "policy_version": safety.policy_version,
            },
        )
        signed_manifest = make_manifest(
            asset=asset,
            run_receipt=run_receipt,
            safety_receipts=[signed_safety],
            publisher=self.peer_id,
            private_key_bytes=self.private_key_bytes,
            title=title,
            description=description,
            tags=tuple(
                dict.fromkeys(
                    tag
                    for tag in (
                        "ai-generated",
                        "rynmesh",
                        f"category:{category}",
                        f"content-kind:{active_content_kind}",
                        *(tags or ()),
                    )
                    if tag
                )
            ),
            provenance_chain=provenance_chain,
        )
        validation = validate_manifest(signed_manifest, policy=self.policy)
        if not validation.propagates:
            return {
                "status": "blocked",
                "clip_id": asset.clip_id,
                "content_id": asset.clip_id,
                "content_type": asset.media_type,
                "content_kind": active_content_kind,
                "safety_outcome": safety.outcome.value,
                "errors": validation.errors,
                "credit": self.record_credit_event(
                    kind="safety_blocked",
                    role="creator",
                    category=category or GENERAL_CATEGORY,
                    subject_id=asset.clip_id,
                    evidence_hash=signed_manifest.subject_hash,
                    reason="local safety policy blocked publication",
                    dedupe_key=f"safety_blocked:{asset.clip_id}",
                ),
            }

        clip_dir = self._clip_dir(asset.clip_id)
        clip_dir.mkdir(parents=True, exist_ok=True)
        local_preview = clip_dir / "preview.bin"
        local_media = clip_dir / media.name
        local_manifest = clip_dir / "manifest.json"
        local_preview.write_bytes(preview_bytes)
        shutil.copyfile(media, local_media)
        local_manifest.write_text(
            json.dumps(signed_manifest.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        network_clip_dir = self.network_peer_dir / _safe_id(asset.clip_id)
        network_clip_dir.mkdir(parents=True, exist_ok=True)
        network_preview = network_clip_dir / "preview.bin"
        network_media = network_clip_dir / media.name
        network_preview.write_bytes(preview_bytes)
        shutil.copyfile(media, network_media)
        announcement = ClipRecord(
            clip_id=asset.clip_id,
            manifest=signed_manifest,
            source_peer_id=self.peer_id,
            title=title,
            created_at=run_receipt.created_at,
            preview_path=str(network_preview),
            media_path=str(network_media),
            content_id=asset.clip_id,
            local=True,
        )
        self._announcement_path(asset.clip_id, self.peer_slug).write_text(
            json.dumps(announcement.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        credit = self.record_credit_event(
            kind=_credit_kind,
            role="creator",
            category=category or GENERAL_CATEGORY,
            subject_id=asset.clip_id,
            evidence_hash=signed_manifest.subject_hash,
            reason="safe AI content published into Rynmesh",
            dedupe_key=f"{_credit_kind}:{asset.clip_id}",
        )
        return {
            "status": "published",
            "clip_id": asset.clip_id,
            "content_id": asset.clip_id,
            "title": title,
            "content_type": asset.media_type,
            "content_kind": active_content_kind,
            "safety_outcome": safety.outcome.value,
            "manifest_hash": signed_manifest.subject_hash,
            "announcement_path": str(self._announcement_path(asset.clip_id, self.peer_slug)),
            "credit": credit,
        }

    def list_clips(self, *, include_invalid: bool = False) -> dict[str, Any]:
        clips: list[dict[str, Any]] = []
        for record in self._iter_announcements():
            validation = validate_manifest(record.manifest, policy=self.policy)
            if not include_invalid and not validation.propagates:
                continue
            manifest = validation.manifest
            content_type = manifest.asset.media_type if manifest else ""
            content_kind = (
                _content_kind_from_asset(manifest.asset, manifest.tags) if manifest else "data"
            )
            account = self.credit_ledger.account(record.source_peer_id)
            clips.append(
                {
                    "clip_id": record.clip_id,
                    "content_id": record.content_id or record.clip_id,
                    "title": record.title,
                    "content_type": content_type,
                    "content_kind": content_kind,
                    "source_peer_id": record.source_peer_id,
                    "source_peer_slug": _hash_hex(record.source_peer_id)[:16],
                    "created_at": record.created_at,
                    "preview_available": bool(record.preview_path),
                    "full_available": bool(record.media_path),
                    "local_preview": (self._clip_dir(record.clip_id) / "preview.bin").exists(),
                    "local_full": self._has_local_full(record.clip_id),
                    "status": "propagates" if validation.propagates else "blocked",
                    "errors": validation.errors,
                    "media_hash": manifest.asset.media_hash if manifest else "",
                    "content_hash": manifest.asset.media_hash if manifest else "",
                    "manifest_hash": record.manifest.subject_hash,
                    "source_credit_score": account.to_dict()["score"],
                    "distribution_weight": account.distribution_weight,
                }
            )
        return {"clips": sorted(clips, key=lambda item: item["created_at"], reverse=True)}

    def list_content(self, *, include_invalid: bool = False) -> dict[str, Any]:
        """Content-native alias for the shared announcement list.

        The alpha manifest still uses `clip_id` internally for compatibility.
        Public agent-facing surfaces should prefer `content_id` and `content`.
        """

        payload = self.list_clips(include_invalid=include_invalid)
        content = payload["clips"]
        return {"content": content, "clips": content}

    def fetch_preview(self, clip_id: str) -> dict[str, Any]:
        record = self._find_record(clip_id)
        validation = validate_manifest(record.manifest, policy=self.policy)
        if not validation.propagates:
            raise StoreError("; ".join(validation.errors) or "manifest_not_allowed")
        source = Path(record.preview_path)
        if not source.exists():
            raise FileNotFoundError(source)
        dest_dir = self._clip_dir(clip_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "preview.bin"
        shutil.copyfile(source, dest)
        if validation.manifest and validation.manifest.asset.preview_hash:
            preview_hash = _file_sha256(dest)
            if preview_hash != validation.manifest.asset.preview_hash:
                try:
                    dest.unlink()
                except OSError:
                    pass
                raise StoreError("preview_hash_mismatch")
        credit = self.record_credit_event(
            kind="preview_served",
            subject_peer_id=record.source_peer_id,
            role="provider",
            category=GENERAL_CATEGORY,
            subject_id=clip_id,
            evidence_hash=record.manifest.subject_hash,
            reason="peer preview fetched and verified",
            dedupe_key=f"preview_served:{self.peer_id}:{clip_id}",
        )
        return {
            "clip_id": clip_id,
            "content_id": clip_id,
            "preview_path": str(dest),
            "manifest_hash": record.manifest.subject_hash,
            "credit": credit,
        }

    def fetch_content_preview(self, content_id: str) -> dict[str, Any]:
        return self.fetch_preview(content_id)

    def fetch_full(self, clip_id: str) -> dict[str, Any]:
        record = self._find_record(clip_id)
        validation = validate_manifest(record.manifest, policy=self.policy)
        if not validation.propagates or validation.manifest is None:
            raise StoreError("; ".join(validation.errors) or "manifest_not_allowed")
        source = Path(record.media_path)
        if not source.exists():
            raise FileNotFoundError(source)
        media_bytes_hash = _file_sha256(source)
        if media_bytes_hash != validation.manifest.asset.media_hash:
            raise StoreError("media_hash_mismatch")
        dest_dir = self._clip_dir(clip_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / source.name
        shutil.copyfile(source, dest)
        credit = self.record_credit_event(
            kind="full_served",
            subject_peer_id=record.source_peer_id,
            role="provider",
            category=GENERAL_CATEGORY,
            subject_id=clip_id,
            evidence_hash=record.manifest.subject_hash,
            reason="full media fetched and hash-verified",
            dedupe_key=f"full_served:{self.peer_id}:{clip_id}",
        )
        return {
            "clip_id": clip_id,
            "content_id": clip_id,
            "media_path": str(dest),
            "content_path": str(dest),
            "media_hash": media_bytes_hash,
            "content_hash": media_bytes_hash,
            "credit": credit,
        }

    def fetch_content_full(self, content_id: str) -> dict[str, Any]:
        return self.fetch_full(content_id)

    def list_local_clips(self) -> dict[str, Any]:
        clips: list[dict[str, Any]] = []
        for clip_dir in sorted(self.clips_dir.glob("*")):
            if not clip_dir.is_dir():
                continue
            try:
                manifest = self.get_local_manifest_from_dir(clip_dir)
                validation = validate_manifest(manifest, policy=self.policy)
            except (OSError, KeyError, ValueError, StoreError):
                continue
            if not validation.propagates or validation.manifest is None:
                continue
            clip_id = validation.manifest.asset.clip_id
            content_type = validation.manifest.asset.media_type
            content_kind = _content_kind_from_asset(
                validation.manifest.asset,
                validation.manifest.tags,
            )
            publisher_peer_id = validation.manifest.publisher
            if publisher_peer_id != manifest.public_key:
                continue
            provider_account = self.credit_ledger.account(self.peer_id)
            publisher_account = self.credit_ledger.account(publisher_peer_id)
            clips.append(
                {
                    "clip_id": clip_id,
                    "content_id": clip_id,
                    "title": validation.manifest.title,
                    "description": validation.manifest.description,
                    "tags": list(validation.manifest.tags),
                    "content_type": content_type,
                    "content_kind": content_kind,
                    "size_bytes": validation.manifest.asset.size_bytes,
                    "source_peer_id": publisher_peer_id,
                    "source_peer_slug": _hash_hex(publisher_peer_id)[:16],
                    "publisher_peer_id": publisher_peer_id,
                    "provider_peer_id": self.peer_id,
                    "provider_peer_slug": self.peer_slug,
                    "created_at": validation.manifest.created_at,
                    "preview_available": (clip_dir / "preview.bin").exists(),
                    "full_available": self._has_local_full(clip_id),
                    "status": "propagates",
                    "media_hash": validation.manifest.asset.media_hash,
                    "content_hash": validation.manifest.asset.media_hash,
                    "manifest_hash": manifest.subject_hash,
                    "safety_outcome": _manifest_safety_outcome(validation.manifest),
                    "provenance_head_hash": validation.manifest.provenance_head_hash,
                    "provider_credit_score": provider_account.to_dict()["score"],
                    "publisher_credit_score": publisher_account.to_dict()["score"],
                    "distribution_weight": max(
                        provider_account.distribution_weight,
                        publisher_account.distribution_weight,
                    ),
                }
            )
        return {"clips": sorted(clips, key=lambda item: item["created_at"], reverse=True)}

    def list_local_content(self) -> dict[str, Any]:
        payload = self.list_local_clips()
        content = payload["clips"]
        return {"content": content, "clips": content}

    def get_local_manifest(self, clip_id: str) -> SignedPayload:
        return self.get_local_manifest_from_dir(self._clip_dir(clip_id))

    def get_local_manifest_from_dir(self, clip_dir: Path) -> SignedPayload:
        manifest_path = clip_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(manifest_path)
        return SignedPayload.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))

    def get_local_preview_bytes(self, clip_id: str) -> bytes:
        manifest = self.get_local_manifest(clip_id)
        validation = self._validate_peer_manifest(manifest)
        path = self._clip_dir(clip_id) / "preview.bin"
        if not path.exists():
            raise FileNotFoundError(path)
        data = path.read_bytes()
        if (
            validation.manifest.asset.preview_hash
            and sha256_bytes(data) != validation.manifest.asset.preview_hash
        ):
            raise StoreError("preview_hash_mismatch")
        return data

    def get_local_media_path(self, clip_id: str) -> Path:
        manifest = self.get_local_manifest(clip_id)
        validation = self._validate_peer_manifest(manifest)
        for path in self._clip_dir(clip_id).glob("*"):
            if path.is_file() and path.name not in {"manifest.json", "preview.bin"}:
                if _file_sha256(path) != validation.manifest.asset.media_hash:
                    raise StoreError("media_hash_mismatch")
                return path
        raise FileNotFoundError(f"full_media_not_found: {clip_id}")

    def get_local_content_path(self, content_id: str) -> Path:
        return self.get_local_media_path(content_id)

    def list_peer_clips(self, endpoint: str, *, timeout_s: float = 20.0) -> dict[str, Any]:
        from .peer_http import HttpPeerClient

        client = HttpPeerClient(endpoint, timeout_s=timeout_s)
        peer_info = client.node_info()
        payload = client.list_clips()
        payload["peer"] = peer_info
        for item in payload.get("clips", []):
            if isinstance(item, dict):
                item["peer_endpoint"] = endpoint
                item["provider_peer_id"] = str(peer_info.get("peer_id", "") or "")
                item["provider_peer_slug"] = str(peer_info.get("peer_slug", "") or "")
        return payload

    def list_peer_content(self, endpoint: str, *, timeout_s: float = 20.0) -> dict[str, Any]:
        from .peer_http import HttpPeerClient

        client = HttpPeerClient(endpoint, timeout_s=timeout_s)
        peer_info = client.node_info()
        payload = client.list_content()
        payload["peer"] = peer_info
        content = payload.get("content", payload.get("clips", []))
        payload["content"] = content
        for item in content:
            if isinstance(item, dict):
                item["peer_endpoint"] = endpoint
                item["provider_peer_id"] = str(peer_info.get("peer_id", "") or "")
                item["provider_peer_slug"] = str(peer_info.get("peer_slug", "") or "")
        return payload

    def fetch_peer_preview(
        self,
        endpoint: str,
        clip_id: str,
        *,
        expected_peer_id: str = "",
        timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        return self._fetch_peer_preview(
            endpoint,
            clip_id,
            expected_peer_id=expected_peer_id,
            use_content_api=False,
            timeout_s=timeout_s,
        )

    def _fetch_peer_preview(
        self,
        endpoint: str,
        item_id: str,
        *,
        expected_peer_id: str = "",
        use_content_api: bool,
        timeout_s: float,
    ) -> dict[str, Any]:
        from .peer_http import HttpPeerClient

        client = HttpPeerClient(endpoint, timeout_s=timeout_s)
        peer_info = self._validate_peer_endpoint(client, expected_peer_id=expected_peer_id)
        manifest = (
            client.get_content_manifest(item_id) if use_content_api else client.get_manifest(item_id)
        )
        validation = self._validate_peer_manifest(manifest)
        preview_bytes = (
            client.get_content_preview(item_id) if use_content_api else client.get_preview(item_id)
        )
        if validation.manifest and validation.manifest.asset.preview_hash:
            if sha256_bytes(preview_bytes) != validation.manifest.asset.preview_hash:
                raise StoreError("preview_hash_mismatch")
        clip_dir = self._clip_dir(item_id)
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        preview_path = clip_dir / "preview.bin"
        preview_path.write_bytes(preview_bytes)
        credit = self.record_credit_event(
            kind="preview_served",
            subject_peer_id=str(peer_info.get("peer_id", "") or ""),
            role="provider",
            category=GENERAL_CATEGORY,
            subject_id=item_id,
            evidence_hash=manifest.subject_hash,
            reason="direct peer preview fetched and verified",
            dedupe_key=f"peer_preview_served:{self.peer_id}:{item_id}",
        )
        self._post_serve_receipt_from_record(endpoint, credit)
        return {
            "clip_id": item_id,
            "content_id": item_id,
            "peer_endpoint": endpoint,
            "provider_peer_id": peer_info.get("peer_id", ""),
            "publisher_peer_id": validation.manifest.publisher if validation.manifest else "",
            "preview_path": str(preview_path),
            "manifest_hash": manifest.subject_hash,
            "credit": credit,
        }

    def fetch_peer_content_preview(
        self,
        endpoint: str,
        content_id: str,
        *,
        expected_peer_id: str = "",
        timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        return self._fetch_peer_preview(
            endpoint,
            content_id,
            expected_peer_id=expected_peer_id,
            use_content_api=True,
            timeout_s=timeout_s,
        )

    def fetch_peer_full(
        self,
        endpoint: str,
        clip_id: str,
        *,
        expected_peer_id: str = "",
        timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        return self._fetch_peer_full(
            endpoint,
            clip_id,
            expected_peer_id=expected_peer_id,
            use_content_api=False,
            timeout_s=timeout_s,
        )

    def _fetch_peer_full(
        self,
        endpoint: str,
        item_id: str,
        *,
        expected_peer_id: str = "",
        use_content_api: bool,
        timeout_s: float,
    ) -> dict[str, Any]:
        from .peer_http import HttpPeerClient

        client = HttpPeerClient(endpoint, timeout_s=timeout_s)
        peer_info = self._validate_peer_endpoint(client, expected_peer_id=expected_peer_id)
        manifest = (
            client.get_content_manifest(item_id) if use_content_api else client.get_manifest(item_id)
        )
        validation = self._validate_peer_manifest(manifest)
        clip_dir = self._clip_dir(item_id)
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        media_path = (
            client.download_content(item_id, clip_dir / f"{_safe_id(item_id)}.media")
            if use_content_api
            else client.download_media(item_id, clip_dir / f"{_safe_id(item_id)}.media")
        )
        if validation.manifest is None:
            raise StoreError("manifest_not_allowed")
        media_hash = _file_sha256(media_path)
        if media_hash != validation.manifest.asset.media_hash:
            try:
                media_path.unlink()
            except OSError:
                pass
            raise StoreError("media_hash_mismatch")
        credit = self.record_credit_event(
            kind="full_served",
            subject_peer_id=str(peer_info.get("peer_id", "") or ""),
            role="provider",
            category=GENERAL_CATEGORY,
            subject_id=item_id,
            evidence_hash=manifest.subject_hash,
            reason="direct peer full media fetched and hash-verified",
            dedupe_key=f"peer_full_served:{self.peer_id}:{item_id}",
        )
        self._post_serve_receipt_from_record(endpoint, credit)
        return {
            "clip_id": item_id,
            "content_id": item_id,
            "peer_endpoint": endpoint,
            "provider_peer_id": peer_info.get("peer_id", ""),
            "publisher_peer_id": validation.manifest.publisher if validation.manifest else "",
            "media_path": str(media_path),
            "content_path": str(media_path),
            "media_hash": media_hash,
            "content_hash": media_hash,
            "manifest_hash": manifest.subject_hash,
            "credit": credit,
        }

    def fetch_peer_content_full(
        self,
        endpoint: str,
        content_id: str,
        *,
        expected_peer_id: str = "",
        timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        return self._fetch_peer_full(
            endpoint,
            content_id,
            expected_peer_id=expected_peer_id,
            use_content_api=True,
            timeout_s=timeout_s,
        )

    def record_credit_event(
        self,
        *,
        kind: str,
        subject_peer_id: str | None = None,
        amount: float | None = None,
        role: str = "node",
        category: str = GENERAL_CATEGORY,
        subject_id: str = "",
        evidence_hash: str = "",
        reason: str = "",
        dedupe_key: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.credit_ledger.record(
            subject_peer_id=subject_peer_id or self.peer_id,
            issuer_peer_id=self.peer_id,
            private_key_bytes=self.private_key_bytes,
            kind=kind,
            amount=amount,
            role=role,
            category=category or GENERAL_CATEGORY,
            subject_id=subject_id,
            evidence_hash=evidence_hash,
            reason=reason,
            dedupe_key=dedupe_key,
            metadata=metadata,
        )

    def _post_serve_receipt_from_record(
        self,
        endpoint: str,
        record_result: dict[str, Any],
        timeout_s: float = 5.0,
    ) -> None:
        """Best-effort: ship this just-signed consumer attestation to the
        provider so its local scoreboard reflects what consumers validated
        (closes the F1 propagation gap). Gated by
        RYNMESH_PROPAGATE_SERVE_RECEIPTS so existing tests are unaffected by
        default; the rynnet testbed and the desktop sidecar set it on.
        """
        if os.environ.get("RYNMESH_PROPAGATE_SERVE_RECEIPTS", "").strip().lower() not in {
            "1", "true", "yes",
        }:
            return
        signed_path = record_result.get("path", "")
        if not signed_path:
            return  # dedupe hit (already_recorded) — nothing new to ship
        import urllib.error
        import urllib.request
        try:
            signed_dict = json.loads(Path(signed_path).read_text(encoding="utf-8"))
            body = json.dumps(signed_dict).encode("utf-8")
            req = urllib.request.Request(
                f"{endpoint.rstrip('/')}/api/v1/credits/append",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=timeout_s).read()
        except Exception:
            # consumer's local ledger is unaffected; propagation is best-effort.
            pass

    def credit_summary(
        self,
        *,
        peer_id: str | None = None,
        category: str = GLOBAL_CATEGORY,
    ) -> dict[str, Any]:
        account = self.credit_ledger.account(peer_id or self.peer_id, category=category)
        payload = account.to_dict()
        payload["category"] = category or GLOBAL_CATEGORY
        payload["exploration_fraction"] = self.credit_ledger.policy.exploration_fraction
        return payload

    def credit_scoreboard(self, *, category: str = GLOBAL_CATEGORY) -> dict[str, Any]:
        accounts = [
            account.to_dict()
            for account in self.credit_ledger.scoreboard(category=category).values()
        ]
        accounts.sort(
            key=lambda item: (
                float(item["distribution_weight"]),
                float(item["score"]),
                int(item["event_count"]),
            ),
            reverse=True,
        )
        return {
            "category": category or GLOBAL_CATEGORY,
            "exploration_fraction": self.credit_ledger.policy.exploration_fraction,
            "accounts": accounts,
        }

    def rank_clips(
        self,
        *,
        include_invalid: bool = False,
        category: str = GLOBAL_CATEGORY,
    ) -> dict[str, Any]:
        payload = self.list_clips(include_invalid=include_invalid)
        ranked: list[dict[str, Any]] = []
        for item in payload["clips"]:
            peer_id = str(item.get("source_peer_id", "") or "")
            account = self.credit_ledger.account(peer_id, category=category)
            ranked_item = dict(item)
            ranked_item["credit_category"] = category or GLOBAL_CATEGORY
            ranked_item["source_credit_score"] = account.to_dict()["score"]
            ranked_item["distribution_weight"] = account.distribution_weight
            ranked.append(ranked_item)
        ranked.sort(
            key=lambda item: (
                float(item.get("distribution_weight", 1.0)),
                str(item.get("created_at", "")),
            ),
            reverse=True,
        )
        return {
            "category": category or GLOBAL_CATEGORY,
            "exploration_fraction": self.credit_ledger.policy.exploration_fraction,
            "clips": ranked,
        }

    def rank_content(
        self,
        *,
        include_invalid: bool = False,
        category: str = GLOBAL_CATEGORY,
    ) -> dict[str, Any]:
        payload = self.rank_clips(include_invalid=include_invalid, category=category)
        return {
            **payload,
            "content": payload["clips"],
        }

    def _build_asset(
        self,
        media: Path,
        *,
        preview_bytes: bytes,
        transcript: str,
        content_type: str = "application/octet-stream",
        content_kind: str = "",
    ) -> MediaAsset:
        base = build_media_asset(
            media,
            media_type=content_type,
            content_kind=content_kind,
            transcript_text=transcript,
        )
        return MediaAsset(
            clip_id=base.clip_id,
            media_hash=base.media_hash,
            size_bytes=base.size_bytes,
            media_type=base.media_type,
            content_kind=base.content_kind,
            duration_sec=base.duration_sec,
            width=base.width,
            height=base.height,
            preview_hash=sha256_bytes(preview_bytes),
            transcript_hash=base.transcript_hash,
            frame_hashes=base.frame_hashes,
        )

    def _clip_dir(self, clip_id: str) -> Path:
        return self.clips_dir / _safe_id(clip_id)

    def _validate_peer_manifest(
        self,
        manifest: SignedPayload,
    ) -> ManifestValidation:
        validation = validate_manifest(manifest, policy=self.policy)
        if not validation.propagates or validation.manifest is None:
            raise StoreError("; ".join(validation.errors) or "manifest_not_allowed")
        if validation.manifest.publisher != manifest.public_key:
            raise StoreError("manifest_publisher_key_mismatch")
        if validation.manifest.asset.clip_id != validation.manifest.asset.media_hash:
            raise StoreError("clip_id_media_hash_mismatch")
        return validation

    def _validate_peer_endpoint(
        self,
        client: Any,
        *,
        expected_peer_id: str = "",
    ) -> dict[str, Any]:
        peer_info = client.node_info()
        if expected_peer_id and peer_info.get("peer_id") != expected_peer_id:
            raise StoreError("peer_endpoint_id_mismatch")
        return peer_info

    def _default_peer_endpoint(self) -> str:
        configured = os.environ.get("RYNMESH_PEER_ENDPOINT", "").strip()
        if configured:
            return configured
        port = os.environ.get("RYNMESH_PEER_PORT", "").strip()
        if not port:
            return ""
        host = os.environ.get("RYNMESH_PEER_PUBLIC_HOST", "").strip() or os.environ.get(
            "RYNMESH_PEER_HOST",
            "127.0.0.1",
        )
        if _is_unspecified_host(host):
            host = _primary_lan_ip() or "127.0.0.1"
        return f"http://{host}:{port}"

    def _default_registration_endpoints(self) -> tuple[str, ...]:
        peer_endpoint = self._default_peer_endpoint()
        if peer_endpoint:
            return (peer_endpoint,)
        return (f"file://{self.network_peer_dir}",)

    def _registry_info(self) -> dict[str, Any]:
        registry_url = os.environ.get("RYNMESH_REGISTRY_URL", "").strip()
        if registry_url:
            return {"kind": "http", "url": registry_url}
        registry_dir = os.environ.get("RYNMESH_REGISTRY_DIR", "").strip()
        return {
            "kind": "file",
            "path": registry_dir or str(self.network_dir / "registry"),
        }

    def _relay_client(self, relay_url: str = "") -> HttpRelayClient:
        url = str(relay_url or "").strip() or self._default_relay_url()
        if not url:
            raise StoreError("relay_url_required")
        return HttpRelayClient(url)

    def _default_relay_url(self) -> str:
        return os.environ.get("RYNMESH_RELAY_URL", "").strip() or os.environ.get(
            "RYNMESH_REGISTRY_URL",
            "",
        ).strip()

    def _relay_info(self) -> dict[str, Any]:
        url = self._default_relay_url()
        if url:
            return {"kind": "http", "url": url}
        return {"kind": "none"}

    def _cache_peer_records(self, network_id: str, signed_records: list[SignedPayload]) -> None:
        cache_dir = self.peer_cache_dir / _safe_id(network_id)
        cache_dir.mkdir(parents=True, exist_ok=True)
        for stale_path in cache_dir.glob("*.json"):
            try:
                stale_path.unlink()
            except OSError:
                continue
        for signed in signed_records:
            try:
                record = verify_peer_record(signed)
            except RegistryError:
                continue
            path = cache_dir / f"{_hash_hex(record.peer_id)[:16]}.json"
            path.write_text(
                json.dumps(signed.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8",
            )

    def _cached_peer_records(
        self,
        *,
        network_id: str,
        max_age_hours: float | None,
    ) -> list[SignedPayload]:
        records: list[SignedPayload] = []
        for path in sorted((self.peer_cache_dir / _safe_id(network_id)).glob("*.json")):
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

    def _has_local_full(self, clip_id: str) -> bool:
        clip_dir = self._clip_dir(clip_id)
        return any(
            path.is_file() and path.name not in {"manifest.json", "preview.bin"}
            for path in clip_dir.glob("*")
        )

    def _announcement_path(self, clip_id: str, peer_slug: str) -> Path:
        return self.announcements_dir / f"{_safe_id(clip_id)}--{peer_slug}.json"

    def _iter_announcements(self) -> list[ClipRecord]:
        records: list[ClipRecord] = []
        for path in sorted(self.announcements_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                records.append(ClipRecord.from_dict(payload))
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
        return records

    def _find_record(self, clip_id: str) -> ClipRecord:
        for record in self._iter_announcements():
            if record.clip_id == clip_id:
                return record
        raise StoreError(f"clip_not_found: {clip_id}")
