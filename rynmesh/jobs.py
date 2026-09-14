"""Signed Rynmesh work-order primitives.

Work orders let one Ryn node ask another node to perform a bounded job without
requiring inbound connectivity to the provider. The registry stores only small
signed control messages; large outputs should be published back as normal
Rynmesh content or relay blobs and referenced by hash.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .crypto import SignatureError, SignedPayload, sign_payload, verify_signed_payload
from .types import now_iso

JOB_STATUS_VALUES = {"open", "accepted", "running", "completed", "failed", "cancelled"}
LLM_CONTROL_PARAMS = {
    "rynmesh.llm.private.infer.v1.p2p_offer": {"session_id", "ice_signal", "timeout_seconds"},
    "rynmesh.llm.private.infer.v1.relay": {"encrypted_task_ref"},
    "rynmesh.llm.private.infer.v1.settlement": {"signed_settlement"},
    "rynmesh.llm.private.infer.v1.cancel": {"signed_cancel"},
}


class JobError(RuntimeError):
    pass


# Registry-visible work-order params are control-plane metadata only. Bounding
# their size matters as much as bounding their keys: without it, a task body
# can simply ride inside an allowed key, which is the exact invariant this
# module exists to enforce.
_MAX_PARAMS_JSON_BYTES = 16 * 1024
_MAX_PARAM_STRING_CHARS = 2048


def validate_llm_control_params(operation: str, params: dict[str, Any]) -> None:
    """Allow only the body-free signaling shapes used by the private protocol."""
    allowed = LLM_CONTROL_PARAMS.get(str(operation))
    if allowed is None:
        raise JobError("unsupported LLM control operation; use the private task protocol")
    unexpected = set(params) - allowed
    if unexpected:
        raise JobError("LLM control params are not allowed; task bodies require the private task protocol")
    try:
        serialized = json.dumps(params, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise JobError("LLM control params must be JSON-serializable") from exc
    if len(serialized.encode("utf-8")) > _MAX_PARAMS_JSON_BYTES:
        raise JobError("LLM control params exceed the control-plane size limit")
    for key, value in params.items():
        # Signed envelopes and ICE metadata are structured; every bare string
        # is a short identifier and gets a hard length cap.
        if isinstance(value, str) and len(value) > _MAX_PARAM_STRING_CHARS:
            raise JobError(f"LLM control param '{key}' exceeds the control-plane length limit")


# capability prefix (with trailing dot or exact) -> params validator. Service
# packages register here; generic code (store, verify_work_order) dispatches
# through it instead of hard-coding service names.
CAPABILITY_PARAM_POLICIES: dict[str, Any] = {
    "rynmesh.llm.": validate_llm_control_params,
    "rynmesh.llm": validate_llm_control_params,
}


def validate_capability_params(capability: str, operation: str, params: dict[str, Any]) -> None:
    cleaned = str(capability or "")
    for prefix, validator in CAPABILITY_PARAM_POLICIES.items():
        if cleaned == prefix.rstrip(".") or cleaned.startswith(prefix if prefix.endswith(".") else prefix + "."):
            validator(operation, params)
            return


@dataclass(frozen=True)
class JobCapacityRecord:
    peer_id: str
    node_name: str
    capabilities: tuple[str, ...]
    network_id: str = "rynmesh-main"
    capacity_units: int = 1
    max_concurrent: int = 1
    price_credits: dict[str, float] = field(default_factory=dict)
    polling_interval_sec: int = 30
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "job_capacity",
            "peer_id": self.peer_id,
            "node_name": self.node_name,
            "capabilities": list(self.capabilities),
            "network_id": self.network_id,
            "capacity_units": int(self.capacity_units),
            "max_concurrent": int(self.max_concurrent),
            "price_credits": dict(self.price_credits),
            "polling_interval_sec": int(self.polling_interval_sec),
            "metadata": dict(self.metadata),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobCapacityRecord":
        return cls(
            peer_id=str(data["peer_id"]),
            node_name=str(data.get("node_name", "")),
            capabilities=tuple(str(item) for item in data.get("capabilities", ())),
            network_id=str(data.get("network_id", "rynmesh-main")),
            capacity_units=int(data.get("capacity_units", 1) or 1),
            max_concurrent=int(data.get("max_concurrent", 1) or 1),
            price_credits={
                str(key): float(value)
                for key, value in dict(data.get("price_credits", {})).items()
            },
            polling_interval_sec=int(data.get("polling_interval_sec", 30) or 30),
            metadata=dict(data.get("metadata", {})),
            updated_at=str(data.get("updated_at", now_iso())),
        )


@dataclass(frozen=True)
class WorkOrder:
    work_order_id: str
    requester_peer_id: str
    provider_peer_id: str
    capability: str
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    network_id: str = "rynmesh-main"
    input_content_ids: tuple[str, ...] = ()
    max_credit_cost: float = 0.0
    idempotency_key: str = ""
    result_policy: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "work_order",
            "work_order_id": self.work_order_id,
            "requester_peer_id": self.requester_peer_id,
            "provider_peer_id": self.provider_peer_id,
            "capability": self.capability,
            "operation": self.operation,
            "params": dict(self.params),
            "network_id": self.network_id,
            "input_content_ids": list(self.input_content_ids),
            "max_credit_cost": float(self.max_credit_cost),
            "idempotency_key": self.idempotency_key,
            "result_policy": dict(self.result_policy),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkOrder":
        return cls(
            work_order_id=str(data["work_order_id"]),
            requester_peer_id=str(data["requester_peer_id"]),
            provider_peer_id=str(data["provider_peer_id"]),
            capability=str(data.get("capability", "")),
            operation=str(data.get("operation", "")),
            params=dict(data.get("params", {})),
            network_id=str(data.get("network_id", "rynmesh-main")),
            input_content_ids=tuple(str(item) for item in data.get("input_content_ids", ())),
            max_credit_cost=float(data.get("max_credit_cost", 0.0) or 0.0),
            idempotency_key=str(data.get("idempotency_key", "")),
            result_policy=dict(data.get("result_policy", {})),
            created_at=str(data.get("created_at", now_iso())),
            expires_at=str(data.get("expires_at", "")),
        )


@dataclass(frozen=True)
class WorkResult:
    work_order_id: str
    provider_peer_id: str
    requester_peer_id: str
    status: str
    message: str = ""
    result_content_ids: tuple[str, ...] = ()
    result_refs: dict[str, Any] = field(default_factory=dict)
    credit_amount: float = 0.0
    network_id: str = "rynmesh-main"
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "work_result",
            "work_order_id": self.work_order_id,
            "provider_peer_id": self.provider_peer_id,
            "requester_peer_id": self.requester_peer_id,
            "status": self.status,
            "message": self.message,
            "result_content_ids": list(self.result_content_ids),
            "result_refs": dict(self.result_refs),
            "credit_amount": float(self.credit_amount),
            "network_id": self.network_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkResult":
        return cls(
            work_order_id=str(data["work_order_id"]),
            provider_peer_id=str(data["provider_peer_id"]),
            requester_peer_id=str(data["requester_peer_id"]),
            status=str(data.get("status", "")),
            message=str(data.get("message", "")),
            result_content_ids=tuple(str(item) for item in data.get("result_content_ids", ())),
            result_refs=dict(data.get("result_refs", {})),
            credit_amount=float(data.get("credit_amount", 0.0) or 0.0),
            network_id=str(data.get("network_id", "rynmesh-main")),
            created_at=str(data.get("created_at", now_iso())),
        )


def new_work_order_id() -> str:
    return "wo_" + uuid.uuid4().hex


def default_expires_at(hours: float = 6.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=float(hours))).isoformat()


def sign_job_capacity(record: JobCapacityRecord, *, private_key_bytes: bytes) -> SignedPayload:
    return sign_payload(record.to_dict(), private_key_bytes=private_key_bytes)


def verify_job_capacity(signed: SignedPayload) -> JobCapacityRecord:
    try:
        verify_signed_payload(signed)
        record = JobCapacityRecord.from_dict(signed.payload)
    except (KeyError, TypeError, ValueError, SignatureError) as exc:
        raise JobError(f"invalid_job_capacity: {exc}") from exc
    if signed.payload.get("kind") != "job_capacity":
        raise JobError("job_capacity_kind_invalid")
    if record.peer_id != signed.public_key:
        raise JobError("job_capacity_key_mismatch")
    if not record.capabilities:
        raise JobError("job_capacity_capabilities_required")
    if record.capacity_units < 1 or record.max_concurrent < 1 or record.polling_interval_sec < 1:
        raise JobError("job_capacity_limits_invalid")
    return record


def sign_work_order(order: WorkOrder, *, private_key_bytes: bytes) -> SignedPayload:
    return sign_payload(order.to_dict(), private_key_bytes=private_key_bytes)


def verify_work_order(signed: SignedPayload) -> WorkOrder:
    try:
        verify_signed_payload(signed)
        order = WorkOrder.from_dict(signed.payload)
    except (KeyError, TypeError, ValueError, SignatureError) as exc:
        raise JobError(f"invalid_work_order: {exc}") from exc
    if signed.payload.get("kind") != "work_order":
        raise JobError("work_order_kind_invalid")
    if order.requester_peer_id != signed.public_key:
        raise JobError("work_order_key_mismatch")
    if not order.provider_peer_id:
        raise JobError("work_order_provider_required")
    if not order.capability:
        raise JobError("work_order_capability_required")
    if not order.operation:
        raise JobError("work_order_operation_required")
    validate_capability_params(order.capability, order.operation, order.params)
    return order


def sign_work_result(result: WorkResult, *, private_key_bytes: bytes) -> SignedPayload:
    return sign_payload(result.to_dict(), private_key_bytes=private_key_bytes)


def verify_work_result(signed: SignedPayload) -> WorkResult:
    try:
        verify_signed_payload(signed)
        result = WorkResult.from_dict(signed.payload)
    except (KeyError, TypeError, ValueError, SignatureError) as exc:
        raise JobError(f"invalid_work_result: {exc}") from exc
    if signed.payload.get("kind") != "work_result":
        raise JobError("work_result_kind_invalid")
    if result.provider_peer_id != signed.public_key:
        raise JobError("work_result_key_mismatch")
    if result.status not in JOB_STATUS_VALUES:
        raise JobError("work_result_status_invalid")
    return result


def record_within_age(updated_at: str, *, max_age_hours: float | None) -> bool:
    if not max_age_hours or max_age_hours <= 0:
        return True
    parsed = _parse_time(updated_at)
    if parsed is None:
        return False
    return parsed >= datetime.now(timezone.utc) - timedelta(hours=float(max_age_hours))


def order_is_open(order: WorkOrder) -> bool:
    if not order.expires_at:
        return True
    parsed = _parse_time(order.expires_at)
    return parsed is None or parsed >= datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
