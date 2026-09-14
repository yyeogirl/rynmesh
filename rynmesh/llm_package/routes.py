"""FastAPI routes for service publication and the private encrypted LLM data path."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from fastapi import HTTPException, Request

from rynmesh.ai_access.store import AIAccessError, AIAccessStore
from rynmesh.atomic_io import atomic_write_json
from rynmesh.background_workers import (
    BackgroundWorkerRegistry,
    BackgroundWorkerSpec,
    BackoffPolicy,
)
from rynmesh.crypto import SignedPayload, sign_payload, verify_signed_payload
from rynmesh.store import RynmeshStore

from . import runtime_native
from .adapters import RUNTIME_ERROR_CODES, AdapterError, LLMAdapter, adapter_from_manifest
from .chat_prompt import CHAT_FORMAT, decode_chat_prompt
from .consumer_commands import ConsumerCommands
from .lifecycle import (
    LifecycleError,
    connect_local_api,
    import_gguf,
    install_managed,
)
from .lifecycle import (
    restart as restart_runtime,
)
from .lifecycle import (
    self_test as run_self_test,
)
from .lifecycle import (
    start as start_runtime,
)
from .lifecycle import (
    status as runtime_status,
)
from .lifecycle import (
    stop as stop_runtime,
)
from .lifecycle import (
    uninstall as uninstall_runtime,
)
from .lifecycle import (
    update as update_runtime,
)
from .manifest import LLMPackageManifest, ManifestError, load_manifest
from .p2p import IceSignal, P2PCapacityError, P2PError, consumer_exchange, provider_exchange
from .setup_recovery import (
    RECOVERY_FAILED,
    SetupRecovery,
    SetupRecoveryError,
    managed_resume_configuration,
)
from .task_balance import TaskBalanceError, TaskBalanceLedger
from .task_protocol import (
    TERMINAL_STATES,
    TaskOrderStore,
    TaskProtocolError,
    open_task,
    seal_task,
)

CAPABILITY = "rynmesh.llm.private.v1"
OPERATION = "rynmesh.llm.private.infer.v1"


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _delivery_error_code(exc: Exception, *, transport: str) -> str:
    if isinstance(exc, P2PCapacityError):
        return "p2p_capacity_exhausted"
    message = str(exc).strip().lower()
    if transport == "p2p" or isinstance(exc, P2PError) or "p2p" in message:
        if "distinct public egress" in message:
            return "p2p_distinct_public_egress_required"
        if "server-reflexive" in message or "stun mapping" in message:
            return "p2p_public_mapping_unavailable"
        if "timed out" in message or isinstance(exc, asyncio.TimeoutError):
            return "p2p_connection_timed_out"
        return "p2p_transport_failed"
    if transport == "direct":
        return "direct_transport_failed"
    if transport == "relay":
        return "encrypted_relay_failed"
    return "delivery_or_processing_failed"


def _submission_error_code(detail: str) -> str:
    message = detail.strip().lower()
    if message in RUNTIME_ERROR_CODES or message == "service_unhealthy":
        return message
    if "insufficient development task balance" in message:
        return "insufficient_task_balance"
    if "capacity_exhausted" in message or "provider is busy" in message:
        return "capacity_exhausted"
    if "absent, stale, offline, or unhealthy" in message:
        return "provider_unavailable"
    if "max_tokens" in message or "required" in message or "context window" in message:
        return "invalid_order"
    return "submission_failed"


def _expires(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _price(manifest: LLMPackageManifest, input_tokens: int, output_tokens: int) -> float:
    value = (
        input_tokens * manifest.pricing.input_per_1k / 1000
        + output_tokens * manifest.pricing.output_per_1k / 1000
    )
    return round(max(manifest.pricing.minimum, value), 8)


def _estimate_input_tokens(prompt: str) -> int:
    return max(1, (len(prompt) + 3) // 4)


def _estimate_price(manifest: LLMPackageManifest, prompt: str, max_tokens: int) -> float:
    estimated_input = _estimate_input_tokens(prompt)
    return _price(manifest, estimated_input, max_tokens)


def _request_fingerprint(body: dict[str, Any], secret: bytes) -> str:
    """Return a local, keyed request digest that cannot be dictionary-probed."""
    encoded = json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "hmac-sha256:" + hmac.new(secret, encoded, hashlib.sha256).hexdigest()


def _validate_messaging_pub(value: str) -> None:
    try:
        raw = base64.b64decode(value, validate=True)
        remote = X25519PublicKey.from_public_bytes(raw)
        X25519PrivateKey.generate().exchange(remote)
    except (ValueError, TypeError) as exc:
        raise TaskProtocolError("reply messaging key is invalid") from exc


def _open_provider_response(
    encrypted_response: dict[str, Any], *, recipient_peer_id: str,
    messaging_key: Any, task_id: str, provider_peer_id: str, service_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    outer, result = open_task(
        encrypted_response, recipient_peer_id=recipient_peer_id,
        recipient_messaging_key=messaging_key, expected_kind="llm_response",
    )
    if outer.get("from_peer_id") != provider_peer_id:
        raise TaskProtocolError("LLM response signer is not the selected provider")
    if outer.get("task_id") != task_id:
        raise TaskProtocolError("LLM response task mismatch")
    if result.get("service_id") != service_id:
        raise TaskProtocolError("LLM response service mismatch")
    state = str(result.get("state") or "")
    if state not in TERMINAL_STATES:
        raise TaskProtocolError("LLM response state is not terminal")
    for name in ("input_tokens", "output_tokens", "duration_ms"):
        if name in result and int(result[name]) < 0:
            raise TaskProtocolError(f"LLM response {name} is invalid")
    return outer, result


class ProviderService:
    def __init__(self, *, manifest: LLMPackageManifest, adapter: LLMAdapter,
                 store: RynmeshStore, task_store: TaskOrderStore,
                 balance: TaskBalanceLedger, messaging_key: Any,
                 access_check: Callable | None = None) -> None:
        self.manifest = manifest
        self.adapter = adapter
        self.store = store
        self.task_store = task_store
        self.balance = balance
        self.messaging_key = messaging_key
        from rynmesh.friends.store import FriendStore

        relationships = FriendStore(store.home)
        permissions = AIAccessStore(store.home, relationship=relationships.relationship)
        self.access_check = access_check or permissions.authorize
        self._slots = threading.BoundedSemaphore(manifest.max_concurrent)
        self._lock = threading.Lock()
        self._running = 0
        self._active_permissions: dict[str, tuple[str, dict | None]] = {}
        self._pending_cancellations: dict[str, tuple[str, str]] = {}
        self._admission_lock = threading.Lock()
        self._request_times: dict[str, deque[float]] = {}
        self._request_limit_per_minute = _positive_env("RYNMESH_LLM_REQUESTS_PER_MINUTE", 60)
        self._max_provider_records = _positive_env("RYNMESH_LLM_MAX_PROVIDER_RECORDS", 10_000)
        self._max_provider_records_per_peer = _positive_env(
            "RYNMESH_LLM_MAX_PROVIDER_RECORDS_PER_PEER", 1_000,
        )
        self._provider_retention_seconds = _positive_env(
            "RYNMESH_LLM_PROVIDER_RETENTION_SECONDS", 86_400,
        )
        self._last_provider_prune = 0.0
        self._provider_record_count = 0
        self._provider_records_by_peer: Counter[str] = Counter()
        self.accepting_orders = True
        self._refresh_provider_record_counts(prune=True)
        if manifest.debug_log_bodies or os.environ.get("RYNMESH_LLM_DEBUG_BODIES", "") == "1":
            print("WARNING: RYNMESH LLM task-body debug logging is enabled; prompts/outputs may be exposed.")

    def _refresh_provider_record_counts(self, *, prune: bool = False) -> None:
        if prune:
            boundary = datetime.now(timezone.utc) - timedelta(
                seconds=self._provider_retention_seconds,
            )
            self.task_store.prune_terminal(older_than=boundary)
        records = self.task_store.list()
        self._provider_record_count = len(records)
        self._provider_records_by_peer = Counter(
            str(dict(record.get("bindings") or {}).get("consumer_peer_id") or "")
            for record in records
        )
        self._provider_records_by_peer.pop("", None)
        self._last_provider_prune = time.monotonic()

    def _claim_admitted_task(
        self,
        *,
        task_id: str,
        bindings: dict[str, str],
    ) -> tuple[dict[str, Any], bool]:
        consumer_peer_id = bindings["consumer_peer_id"]
        with self._admission_lock:
            existing = self.task_store.get(task_id)
            if existing is not None:
                return self.task_store.claim(task_id=task_id, bindings=bindings)

            now = time.monotonic()
            if now - self._last_provider_prune >= 60:
                self._refresh_provider_record_counts(prune=True)
                self._request_times = {
                    peer_id: values
                    for peer_id, values in self._request_times.items()
                    if values and now - values[-1] < 60
                }

            times = self._request_times.setdefault(consumer_peer_id, deque())
            while times and now - times[0] >= 60:
                times.popleft()
            if len(times) >= self._request_limit_per_minute:
                raise TaskProtocolError("provider_rate_limited")
            if self._provider_record_count >= self._max_provider_records:
                raise TaskProtocolError("provider_task_record_limit_reached")
            if (
                self._provider_records_by_peer[consumer_peer_id]
                >= self._max_provider_records_per_peer
            ):
                raise TaskProtocolError("provider_peer_task_record_limit_reached")

            record, claimed = self.task_store.claim(task_id=task_id, bindings=bindings)
            if claimed:
                times.append(now)
                self._provider_record_count += 1
                self._provider_records_by_peer[consumer_peer_id] += 1
            return record, claimed

    def public_status(self, *, benchmark: bool = False) -> dict[str, Any]:
        health = self.adapter.health()
        result: dict[str, Any] = {
            "configured": True,
            "ready": bool(health.get("ok")),
            "service": self.manifest.public_dict(),
            "online": bool(health.get("ok")) and self.accepting_orders,
            "accepting_orders": self.accepting_orders,
            "access_policy": "explicit_friends",
            "health": {k: v for k, v in health.items() if k not in {"base_url", "path"}},
            "capacity": {"max_concurrent": self.manifest.max_concurrent,
                         "running": self._running, "available": max(0, self.manifest.max_concurrent - self._running),
                         "queue_limit": self.manifest.queue_limit, "queue_policy": "reject_when_full"},
        }
        from rynmesh.services import peer_box

        if getattr(self.adapter, "supports_chat_messages", False):
            result["service"]["capabilities"] = list(dict.fromkeys([*result["service"].get("capabilities", []), CHAT_FORMAT]))
        result["node_messaging_pub"] = peer_box.public_key_b64(self.messaging_key)
        if benchmark and health.get("ok"):
            measured = self.adapter.infer(
                prompt="Reply with one word: ready", max_tokens=8,
                task_id="benchmark-" + uuid.uuid4().hex, timeout_s=self.manifest.timeout_seconds,
            )
            result["benchmark"] = {
                "duration_ms": measured["duration_ms"], "input_tokens": measured["input_tokens"],
                "output_tokens": measured["output_tokens"],
            }
        return result

    def publish(self, *, network_id: str, benchmark: bool = True,
                require_online: bool = True) -> dict[str, Any]:
        status = self.public_status(benchmark=benchmark)
        if require_online and not status["online"]:
            raise TaskProtocolError("unhealthy service cannot be published online")
        self.store.register_node(capabilities=["publish", "seed", CAPABILITY], network_id=network_id)
        return self.store.register_job_capacity(
            capabilities=[CAPABILITY], network_id=network_id,
            capacity_units=self.manifest.max_concurrent,
            max_concurrent=self.manifest.max_concurrent,
            price_credits={}, polling_interval_sec=15,
            metadata={"llm_service": status, "billing": "development_task_balance_not_credits"},
        )

    def handle(self, signed_request: dict[str, Any]) -> dict[str, Any]:
        outer, body = open_task(
            signed_request, recipient_peer_id=self.store.peer_id,
            recipient_messaging_key=self.messaging_key, expected_kind="llm_request",
        )
        task_id = str(outer["task_id"])
        if str(body.get("service_id")) != self.manifest.package_id:
            raise TaskProtocolError("requested service is not available")
        prompt = str(body.get("prompt") or "")
        if not prompt:
            raise TaskProtocolError("prompt is required")
        try:
            messages = decode_chat_prompt(prompt, body.get("prompt_format", "text"))
        except ValueError as exc:
            raise TaskProtocolError(str(exc)) from None
        if messages is not None and not getattr(self.adapter, "supports_chat_messages", False):
            raise TaskProtocolError("requested prompt format is not supported by this adapter")
        max_tokens = min(int(body.get("max_tokens") or 64), self.manifest.max_output_tokens)
        if max_tokens < 1:
            raise TaskProtocolError("max_tokens is invalid")
        reply_pub = str(body.get("reply_messaging_pub") or "")
        _validate_messaging_pub(reply_pub)
        max_amount = float(body.get("max_amount") or 0)
        own_request = outer["from_peer_id"] == self.store.peer_id
        if not own_request:
            try:
                self.access_check(outer["from_peer_id"], self.manifest.package_id, body.get("ai_permission"))
            except (AIAccessError, OSError, ValueError):
                return self._sealed_failure(
                    task_id, reply_pub, outer["from_peer_id"], "rejected", "ai_permission_denied",
                )
        if max_amount < 0 or (max_amount == 0 and not own_request):
            # Reject before inference: without a positive hold every completed
            # generation would fail the price check after burning compute.
            raise TaskProtocolError("max_amount must be a positive hold")
        if max_amount > self.manifest.pricing.maximum_per_task:
            raise TaskProtocolError("task maximum exceeds provider price limit")
        idempotency_key = str(body.get("idempotency_key") or task_id)
        fingerprint = _request_fingerprint(body, self.store.private_key_bytes)
        bindings = {
            "consumer_peer_id": str(outer["from_peer_id"]),
            "service_id": self.manifest.package_id,
            "idempotency_key": idempotency_key,
            "request_fingerprint": fingerprint,
        }
        existing = self.task_store.get(task_id)
        if existing is not None:
            existing, claimed = self.task_store.claim(task_id=task_id, bindings=bindings)
        else:
            if not self.accepting_orders and not own_request:
                return self._sealed_failure(
                    task_id, reply_pub, outer["from_peer_id"], "rejected", "service_paused",
                )
            if not self.adapter.health().get("ok"):
                return self._sealed_failure(
                    task_id, reply_pub, outer["from_peer_id"], "rejected", "service_unhealthy",
                )
            existing, claimed = self._claim_admitted_task(task_id=task_id, bindings=bindings)
        if not claimed:
            encrypted = existing.get("encrypted_response")
            if existing.get("state") in TERMINAL_STATES:
                if isinstance(encrypted, dict):
                    return dict(encrypted)
                # Terminal but the stored ciphertext was purged (settlement
                # cleanup). Answer immediately instead of spinning until the
                # timeout and then claiming the task was "still in progress".
                return self._sealed_failure(
                    task_id, reply_pub, str(outer["from_peer_id"]), "failed", _missing_provider_response_code(existing),
                )
            deadline = time.monotonic() + self.manifest.timeout_seconds + 30
            while time.monotonic() < deadline:
                # 0.25s keeps duplicate waits from monopolizing the threadpool
                # with 20Hz disk reads while staying responsive.
                time.sleep(0.25)
                existing = self.task_store.get(task_id) or {}
                encrypted = existing.get("encrypted_response")
                if existing.get("state") in TERMINAL_STATES:
                    if isinstance(encrypted, dict):
                        return dict(encrypted)
                    return self._sealed_failure(
                        task_id, reply_pub, str(outer["from_peer_id"]), "failed", _missing_provider_response_code(existing),
                    )
            raise TaskProtocolError("duplicate task is still in progress")
        with self._lock:
            pending_cancel = self._pending_cancellations.pop(task_id, None)
        if pending_cancel == (str(outer["from_peer_id"]), self.manifest.package_id):
            self.cancel(task_id)
            return self._failure(
                task_id, reply_pub, str(outer["from_peer_id"]), "cancelled", "consumer_cancelled",
            )
        metadata = {
            "consumer_peer_id": outer["from_peer_id"],
            "service_id": self.manifest.package_id,
            "request_hash": SignedPayload.from_dict(signed_request).subject_hash,
        }
        if not self._slots.acquire(blocking=False):
            return self._failure(task_id, reply_pub, outer["from_peer_id"], "rejected", "capacity_exhausted")
        with self._lock:
            self._running += 1
            if not own_request:
                self._active_permissions[task_id] = (outer["from_peer_id"], body.get("ai_permission"))
        try:
            self.task_store.transition(task_id=task_id, state="accepted", metadata=metadata)
            self.task_store.transition(task_id=task_id, state="running", metadata=metadata)
            self.recheck_permissions()
            if (self.task_store.get(task_id) or {}).get("state") == "cancelled":
                return self._failure(task_id, reply_pub, outer["from_peer_id"], "cancelled", "ai_permission_revoked")
            started = time.monotonic()
            result = self.adapter.infer(
                prompt=prompt, max_tokens=max_tokens, task_id=task_id,
                timeout_s=self.manifest.timeout_seconds,
                **({"messages": messages} if messages is not None else {}),
            )
            if (self.task_store.get(task_id) or {}).get("state") == "cancelled":
                return self._failure(
                    task_id, reply_pub, str(outer["from_peer_id"]),
                    "cancelled", "consumer_cancelled",
                )
            duration_ms = int(result.get("duration_ms") or (time.monotonic() - started) * 1000)
            # The consumer's hold is an estimate (its client cannot know the
            # provider tokenizer), so actual usage can price above it — CJK
            # prompts routinely tokenize 3-4x denser than the chars/4 guess.
            # The hold is a price ceiling the consumer consented to: bill at
            # most that ceiling instead of failing after inference already ran.
            amount = 0.0 if own_request else min(
                _price(self.manifest, int(result["input_tokens"]), int(result["output_tokens"])),
                max_amount,
            )
            response_body = {
                "task_id": task_id, "state": "succeeded", "service_id": self.manifest.package_id,
                "model_alias": self.manifest.public_model_alias, "output": str(result["text"]),
                "input_tokens": int(result["input_tokens"]), "output_tokens": int(result["output_tokens"]),
                "duration_ms": duration_ms, "amount": amount, "currency": "DEV_TASK_BALANCE",
            }
            encrypted = seal_task(
                body=response_body, task_id=task_id, kind="llm_response",
                sender_peer_id=self.store.peer_id, recipient_peer_id=str(outer["from_peer_id"]),
                sender_signing_key=self.store.private_key_bytes, recipient_messaging_pub=reply_pub,
                expires_at=_expires(max(300, self.manifest.timeout_seconds * 2)),
            ).to_dict()
            self.task_store.transition(
                task_id=task_id, state="succeeded",
                metadata={**metadata, "input_tokens": response_body["input_tokens"],
                          "output_tokens": response_body["output_tokens"], "duration_ms": duration_ms,
                          "amount": amount}, encrypted_response=encrypted,
            )
            return encrypted
        except Exception as exc:
            message = str(exc).lower()
            code = exc.code if isinstance(exc, AdapterError) else "inference_failed"
            state = "cancelled" if "task_cancelled" in message else (
                "timed_out" if code == "inference_timeout" or "timed out" in message else "failed"
            )
            return self._failure(task_id, reply_pub, outer["from_peer_id"], state,
                                 "consumer_cancelled" if state == "cancelled" else (
                                     "inference_timeout" if state == "timed_out" else code
                                 ))
        finally:
            prompt = ""  # best-effort reference cleanup; see privacy documentation
            with self._lock:
                self._running -= 1
                self._active_permissions.pop(task_id, None)
            self._slots.release()

    def recheck_permissions(self) -> bool:
        with self._lock:
            active = list(self._active_permissions.items())
        cancelled = False
        for task_id, (peer_id, permission) in active:
            try:
                self.access_check(peer_id, self.manifest.package_id, permission)
            except (AIAccessError, OSError, ValueError):
                self.cancel(task_id)
                cancelled = True
        return cancelled

    def _failure(self, task_id: str, reply_pub: str, consumer_peer_id: str,
                 state: str, code: str) -> dict[str, Any]:
        response = self._sealed_failure(task_id, reply_pub, consumer_peer_id, state, code)
        self.task_store.transition(task_id=task_id, state=state,
                                   metadata={"consumer_peer_id": consumer_peer_id,
                                             "service_id": self.manifest.package_id,
                                             "error_code": code}, encrypted_response=response)
        return response

    def _sealed_failure(self, task_id: str, reply_pub: str, consumer_peer_id: str,
                        state: str, code: str) -> dict[str, Any]:
        return seal_task(
            body={"task_id": task_id, "state": state, "error_code": code,
                  "service_id": self.manifest.package_id},
            task_id=task_id, kind="llm_response", sender_peer_id=self.store.peer_id,
            recipient_peer_id=consumer_peer_id, sender_signing_key=self.store.private_key_bytes,
            recipient_messaging_pub=reply_pub, expires_at=_expires(300),
        ).to_dict()

    def settle_earning(self, signed: dict[str, Any]) -> dict[str, Any]:
        envelope = SignedPayload.from_dict(signed)
        verify_signed_payload(envelope)
        payload = envelope.payload
        if payload.get("kind") != "llm_settlement" or payload.get("to_peer_id") != self.store.peer_id:
            raise TaskProtocolError("invalid settlement recipient/kind")
        if payload.get("from_peer_id") != envelope.public_key:
            raise TaskProtocolError("settlement identity mismatch")
        task_id = str(payload.get("task_id") or "")
        record = self.task_store.get(task_id)
        if not record or record.get("state") != "succeeded":
            raise TaskProtocolError("settlement task is not succeeded")
        bindings = dict(record.get("bindings") or {})
        if bindings.get("consumer_peer_id") != envelope.public_key:
            raise TaskProtocolError("settlement sender is not the task consumer")
        if payload.get("service_id") != bindings.get("service_id"):
            raise TaskProtocolError("settlement service mismatch")
        if payload.get("settlement_id") != "settle:" + task_id:
            raise TaskProtocolError("settlement id mismatch")
        final = record["history"][-1]
        amount = float(payload.get("amount") or 0)
        if amount != float(final.get("amount") or 0):
            raise TaskProtocolError("settlement amount mismatch")
        earning = self.balance.earn(
            task_id=task_id, amount=amount, input_tokens=int(final.get("input_tokens") or 0),
            output_tokens=int(final.get("output_tokens") or 0), duration_ms=int(final.get("duration_ms") or 0),
            service_id=self.manifest.package_id, consumer_peer_id=envelope.public_key,
        )
        self.task_store.purge_encrypted_response(task_id)
        return earning

    def cancel(self, task_id: str) -> bool:
        self.adapter.cancel(task_id)
        record = self.task_store.get(task_id)
        if record and record.get("state") not in TERMINAL_STATES:
            self.task_store.transition(task_id=task_id, state="cancelled", metadata={"reason": "consumer_cancelled"})
        return True

    def cancel_signed(self, signed: dict[str, Any]) -> bool:
        envelope = SignedPayload.from_dict(signed)
        verify_signed_payload(envelope)
        payload = envelope.payload
        if payload.get("kind") != "llm_cancel" or payload.get("to_peer_id") != self.store.peer_id:
            raise TaskProtocolError("invalid cancellation recipient/kind")
        if payload.get("from_peer_id") != envelope.public_key:
            raise TaskProtocolError("cancellation identity mismatch")
        task_id = str(payload.get("task_id") or "")
        if payload.get("cancel_id") != "cancel:" + task_id:
            raise TaskProtocolError("cancellation id mismatch")
        if payload.get("service_id") != self.manifest.package_id:
            raise TaskProtocolError("cancellation service mismatch")
        record = self.task_store.get(task_id)
        if record is None:
            with self._lock:
                if len(self._pending_cancellations) >= 1024:
                    self._pending_cancellations.pop(next(iter(self._pending_cancellations)))
                self._pending_cancellations[task_id] = (envelope.public_key, self.manifest.package_id)
            self.adapter.cancel(task_id)
            return True
        bindings = dict(record.get("bindings") or {})
        if bindings.get("consumer_peer_id") != envelope.public_key:
            raise TaskProtocolError("cancellation sender is not the task consumer")
        return self.cancel(task_id)


_MAX_PEER_RESPONSE_BYTES = 2 * 1024 * 1024

# A publishing provider refreshes its discovery record every 30 seconds; a
# record this old means the provider stopped refreshing (crashed or paused).
_DISCOVERY_STALE_AFTER_S = 180.0


def _record_is_stale(updated_at: Any) -> bool:
    text = str(updated_at or "").strip()
    if not text:
        return False  # older records without a timestamp stay orderable
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() > _DISCOVERY_STALE_AFTER_S


def _peer_post_json(
    endpoint: str, path: str, payload: dict[str, Any], *, timeout_s: float,
) -> dict[str, Any]:
    """POST bounded JSON through the peer client's configured Transport."""
    # Late import avoids the peer_http -> install_llm_routes import cycle while
    # retaining one authoritative peer transport/error implementation.
    from rynmesh.peer_http import HttpPeerClient

    return HttpPeerClient(endpoint, timeout_s=timeout_s).post_json(
        path, payload, max_bytes=_MAX_PEER_RESPONSE_BYTES,
    )


def _upload_relay_ciphertext(store: RynmeshStore, envelope: dict[str, Any], *,
                             relay_url: str, filename: str, home: Path) -> str:
    """Write a ciphertext envelope through the path-based relay API.

    Owns the whole tempfile lifecycle in one place (it exists only because the
    relay client takes a path); returns the uploaded blob's content hash.
    """
    fd, name = tempfile.mkstemp(prefix="rynmesh-llm-request-", suffix=".ciphertext", dir=home)
    os.close(fd)
    path = Path(name)
    try:
        path.write_text(json.dumps(envelope), encoding="utf-8")
        uploaded = store.upload_relay_artifact(
            path, relay_url=relay_url,
            media_type="application/vnd.rynmesh.llm-ciphertext+json",
            filename=filename,
        )
        return str(uploaded["blob"]["content_hash"])
    finally:
        path.unlink(missing_ok=True)


def _download_relay_ciphertext(store: RynmeshStore, reference: str, *,
                               relay_url: str, home: Path) -> dict[str, Any]:
    fd, name = tempfile.mkstemp(prefix="rynmesh-llm-response-", suffix=".ciphertext", dir=home)
    os.close(fd)
    path = Path(name)
    try:
        store.download_relay_artifact(reference, path, relay_url=relay_url)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TaskProtocolError("relayed ciphertext must be a JSON object")
        return value
    finally:
        path.unlink(missing_ok=True)


def dispatch_settlement(store: RynmeshStore, *, task_id: str, provider_peer_id: str,
                        service_id: str, amount: float, network_id: str,
                        endpoint: str = "") -> bool:
    """Sign and deliver the body-free settlement acknowledgement.

    One implementation for the live order path and crash recovery — the two
    used to hand-build the same envelope separately and had already diverged.
    """
    if provider_peer_id == store.peer_id and amount == 0:
        return True  # Local computation has no transfer to another provider.
    settlement = sign_payload({
        "kind": "llm_settlement", "task_id": task_id, "from_peer_id": store.peer_id,
        "to_peer_id": provider_peer_id, "amount": amount, "service_id": service_id,
        "settlement_id": "settle:" + task_id,
    }, private_key_bytes=store.private_key_bytes)
    if endpoint:
        try:
            _peer_post_json(
                endpoint, "/api/peer/llm/settlements", settlement.to_dict(), timeout_s=15,
            )
            return True
        except Exception:
            pass
    try:
        store.submit_work_order(
            provider_peer_id=provider_peer_id, capability=CAPABILITY,
            operation=OPERATION + ".settlement",
            params={"signed_settlement": settlement.to_dict()}, network_id=network_id,
            idempotency_key="settle:" + task_id, expires_in_hours=1,
        )
        return True
    except Exception:
        return False


def _recover_consumer_orders(
    consumer_orders: TaskOrderStore, balance: TaskBalanceLedger,
    store: RynmeshStore | None = None,
) -> None:
    """Recover billing checkpoints, otherwise fail interrupted exchanges safely.

    A response received before a crash carries a body-free settlement checkpoint,
    so its local balance and provider settlement can be completed idempotently.
    Earlier interrupted exchanges are not resumable and release their holds.
    """
    for record in consumer_orders.list():
        task_id = str(record.get("task_id") or "")
        state = str(record.get("state") or "created")
        if not task_id:
            continue
        history = list(record.get("history") or [])
        pending = next(
            (dict(item) for item in reversed(history) if item.get("settlement_pending")),
            None,
        )
        if state not in TERMINAL_STATES and pending:
            bindings = dict(record.get("bindings") or {})
            balance.settle(
                task_id=task_id, amount=float(pending.get("amount") or 0),
                input_tokens=int(pending.get("input_tokens") or 0),
                output_tokens=int(pending.get("output_tokens") or 0),
                duration_ms=int(pending.get("duration_ms") or 0),
                service_id=str(pending.get("service_id") or bindings.get("service_id") or ""),
                provider_peer_id=str(
                    pending.get("provider_peer_id") or bindings.get("provider_peer_id") or ""
                ),
            )
            consumer_orders.transition(
                task_id=task_id, state="succeeded",
                metadata={key: value for key, value in pending.items()
                          if key not in {"at", "checkpoint", "state", "settlement_pending"}},
            )
            record = consumer_orders.get(task_id) or record
            state = "succeeded"
            history = list(record.get("history") or [])
        if state == "succeeded":
            dispatched = any(item.get("settlement_dispatched") for item in history)
            if store is not None and pending and not dispatched:
                bindings = dict(record.get("bindings") or {})
                provider_peer_id = str(
                    pending.get("provider_peer_id") or bindings.get("provider_peer_id") or ""
                )
                service_id = str(pending.get("service_id") or bindings.get("service_id") or "")
                if dispatch_settlement(
                    store, task_id=task_id, provider_peer_id=provider_peer_id,
                    service_id=service_id, amount=float(pending.get("amount") or 0),
                    network_id=str(pending.get("network_id") or "rynmesh-main"),
                ):
                    consumer_orders.checkpoint(
                        task_id=task_id, metadata={"settlement_dispatched": True},
                    )
            continue
        if state not in TERMINAL_STATES:
            consumer_orders.transition(
                task_id=task_id,
                state="failed",
                metadata={"error_code": "consumer_restarted_before_completion"},
            )
        try:
            balance.release(task_id=task_id, reason="consumer_restart_recovery")
        except TaskBalanceError as exc:
            if "task hold not found" not in str(exc):
                raise


def _missing_provider_response_code(record: dict[str, Any]) -> str:
    if any(event.get("error_code") == "provider_restarted_before_completion" for event in record.get("history", [])):
        return "provider_restarted_before_completion"
    return "result_expired"


def _recover_provider_orders(provider_orders: TaskOrderStore) -> None:
    # Called once when the node installs its routes, before accepting requests.
    # Old workers cannot resume; the external runtime may still be computing.
    for record in provider_orders.list():
        if record.get("state") not in TERMINAL_STATES:
            provider_orders.transition(task_id=record["task_id"], state="failed",
                metadata={"error_code": "provider_restarted_before_completion"})


def install_llm_routes(app: Any, *, store: RynmeshStore, home: Path, messaging_key: Any,
                       resolve_endpoint: Callable[[str], str], resolve_pubkey: Callable[[str], str]) -> ConsumerCommands:
    provider_orders = TaskOrderStore(home / "llm" / "provider-orders")
    _recover_provider_orders(provider_orders)
    consumer_orders = TaskOrderStore(home / "llm" / "consumer-orders")
    # Ledger-backed: every hold/settle/release/earning is a signed event in
    # the node's credit ledger (category dev:task_balance, invisible to
    # reputation scoring), with this file as the O(1) snapshot.
    balance = TaskBalanceLedger(
        home / "llm" / "task-balance.json",
        credit_ledger=store.credit_ledger, peer_id=store.peer_id,
        private_key_bytes=store.private_key_bytes,
    )
    _recover_consumer_orders(consumer_orders, balance, store)
    manager: ProviderService | None = None
    manager_lock = threading.Lock()
    p2p_sessions: set[str] = set()
    p2p_sessions_lock = threading.Lock()
    background_orders: dict[str, dict[str, Any]] = {}
    background_orders_lock = threading.Lock()
    pending_cancellations: set[str] = set()
    settings_path = home / "llm" / "provider-settings.json"
    consumer_settings_path = home / "llm" / "consumer-settings.json"
    setup_job_path = home / "llm" / "setup-job.json"
    setup_job_lock = threading.Lock()
    setup_operation_lock = threading.Lock()
    setup_cancel_events: dict[str, threading.Event] = {}
    manager_path = ""

    def write_setup_job(value: dict[str, Any]) -> dict[str, Any]:
        atomic_write_json(setup_job_path, value, indent=2, sort_keys=True)
        return value

    def read_setup_job() -> dict[str, Any]:
        if not setup_job_path.exists():
            return {"state": "idle", "stage": "idle", "progress": 0}
        try:
            value = dict(json.loads(setup_job_path.read_text(encoding="utf-8")) or {})
        except (OSError, json.JSONDecodeError, TypeError):
            return {"state": "failed", "stage": "recovery", "progress": 0,
                    "error_code": "setup_status_unreadable",
                    "message": "The previous setup status could not be read; retry setup."}
        return value

    previous_setup_job = read_setup_job()
    setup_recovery = SetupRecovery(home)
    try:
        recovered_setup = setup_recovery.restore()
    except SetupRecoveryError:
        recovered_setup = "failed"
    if previous_setup_job.get("state") in {"queued", "running", "cancelling"} or recovered_setup != "absent":
        previous_setup_job.update({
            "state": "failed",
            "stage": "recovery",
            "error_code": "setup_interrupted",
            "message": "Setup was interrupted when the node stopped. Review the model service and retry.",
            "retryable": True,
            "recovery_state": "restored_unchecked" if recovered_setup == "restored" else recovered_setup,
        })
        if recovered_setup == "restored":
            previous_setup_job["message"] = "Previous configuration restored after interrupted setup. Check model status or retry configuration."
        elif recovered_setup == "failed":
            previous_setup_job.update({"error_code": "setup_recovery_failed", "message": RECOVERY_FAILED})
        write_setup_job(previous_setup_job)

    def read_consumer_settings() -> dict[str, Any]:
        defaults = {"result_retention_seconds": 3600}
        if not consumer_settings_path.exists():
            return defaults
        try:
            value = json.loads(consumer_settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return defaults
        return {**defaults, **dict(value or {})}

    def write_consumer_settings(value: dict[str, Any]) -> dict[str, Any]:
        atomic_write_json(consumer_settings_path, value, indent=2, sort_keys=True)
        return value

    def response_retention() -> int:
        value = int(read_consumer_settings().get("result_retention_seconds") or 0)
        return value if value in {0, 3600, 86400, 604800} else 3600

    def read_provider_settings() -> dict[str, Any]:
        configured = os.environ.get("RYNMESH_LLM_SERVICE_MANIFEST", "").strip()
        defaults = {
            "manifest": configured,
            "publication_enabled": bool(configured),
            "network_id": os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"),
        }
        if not settings_path.exists():
            return defaults
        try:
            value = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return defaults
        saved = dict(value or {})
        if configured and str(saved.get("manifest") or "") != configured:
            # A deployment manifest is authoritative. This also makes profile
            # upgrades deterministic when a persistent data volume is reused.
            return defaults
        return {**defaults, **saved}

    def write_provider_settings(value: dict[str, Any]) -> dict[str, Any]:
        atomic_write_json(settings_path, value, indent=2, sort_keys=True)
        return value

    def active_manager(path: str = "", *, refresh: bool = False) -> ProviderService | None:
        nonlocal manager, manager_path
        # Serialized: this is reached concurrently from async routes, threadpool
        # sync routes, and both background loops. Without the lock two callers
        # can build two ProviderServices (doubling max_concurrent against one
        # runtime) or shut an adapter down while another thread is mid-handle.
        with manager_lock:
            if (setup_recovery.pending or setup_operation_lock.locked()) and not path:
                return None  # An incomplete or unrecovered configuration is not a service.
            settings = read_provider_settings()
            configured = str(path or settings.get("manifest") or "")
            if manager is not None and (refresh or (configured and configured != manager_path)):
                manager.adapter.shutdown()
                manager = None
            if manager is None and configured:
                manifest = load_manifest(configured)
                manager = ProviderService(
                    manifest=manifest, adapter=adapter_from_manifest(manifest), store=store,
                    task_store=provider_orders, balance=balance, messaging_key=messaging_key,
                    access_check=(lambda peer, service_id, permission: app.state.ai_access.store.authorize(peer, service_id, permission))
                    if getattr(app.state, "ai_access", None) else None,
                )
                manager.accepting_orders = bool(settings.get("publication_enabled"))
                manager_path = configured
            return manager

    def configure_llm(
        body: dict[str, Any],
        *,
        progress: Callable[[str, int, str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        mode = str(body.get("mode") or "").strip().lower().replace("_", "-")
        package_id = str(body.get("package_id") or "local-small")
        alias = str(body.get("alias") or "rynmesh-local")
        root = home / "llm"
        if mode == "managed":
            return install_managed(
                package_id=package_id, root=root,
                port=int(body.get("port") or 18080),
                accept_risk=bool(body.get("accept_risk", False)),
                runtime=str(body.get("runtime") or "auto"),
                profile=str(body.get("profile") or "auto"),
                progress=progress, cancel_check=cancel_check,
            )
        if mode == "import-gguf":
            return import_gguf(
                source=str(body.get("model_path") or ""), package_id=package_id,
                alias=alias, root=root, port=int(body.get("port") or 18080),
                accept_risk=bool(body.get("accept_risk", False)),
                runtime=str(body.get("runtime") or "auto"),
                progress=progress, cancel_check=cancel_check,
            )
        if mode in {"openai-compatible", "ollama"}:
            return connect_local_api(
                base_url=str(body.get("base_url") or "http://127.0.0.1:8080"),
                package_id=package_id, alias=alias, model=str(body.get("model") or ""),
                api_key_env=str(body.get("api_key_env") or ""),
                adapter="ollama" if mode == "ollama" else "openai_compatible",
                root=root, allow_non_loopback=bool(body.get("allow_non_loopback", False)),
                progress=progress, cancel_check=cancel_check,
            )
        raise LifecycleError("setup mode must be managed, import-gguf, openai-compatible, or ollama")

    def activate_configuration(result: dict[str, Any]) -> dict[str, Any]:
        configured = str(result["manifest"])
        settings = read_provider_settings()
        settings.update({"manifest": configured, "publication_enabled": False})
        write_provider_settings(settings)
        current = active_manager(configured, refresh=True)
        if current is not None:
            current.accepting_orders = False
        public_result = json.loads(json.dumps(result))
        if isinstance(public_result.get("self_test"), dict):
            public_result["self_test"].pop("output_preview", None)
        return {
            "configured": True,
            "publication_enabled": False,
            "setup": public_result,
            "status": current.public_status() if current else {},
        }

    def resolve_provider_endpoint(peer_id: str, *, cancellation: bool = False) -> str:
        friends_state = getattr(app.state, "friends", None)
        if friends_state:
            friends = friends_state.service
            relation = friends.store.relationship_for_peer(peer_id)
            if relation is None and cancellation:
                # A removed friendship may still have an already-issued task.
                # Its signed cancellation contains no new prompt or credentials.
                relation = next((row for row in friends.store.list_relationships() if row.get("peer_id") == peer_id), None)
            if relation:
                from rynmesh.friends.crypto import validate_endpoint

                return validate_endpoint(relation["endpoint"], allow_loopback=friends.allow_loopback)
        return resolve_endpoint(peer_id)

    def discover(network_id: str) -> list[dict[str, Any]]:
        services = []
        current = active_manager()
        if current is not None:
            local = current.public_status()
            local["service"] = {**local["service"], "pricing": {**local["service"]["pricing"], "input_per_1k": 0, "output_per_1k": 0, "minimum": 0}}
            services.append({**local, "online": local["ready"], "peer_id": store.peer_id,
                             "node_name": "This device", "access": "self", "updated_at": datetime.now(timezone.utc).isoformat()})
        access_state = getattr(app.state, "ai_access", None)
        if access_state and access_state.catalog:
            services.extend(row for row in access_state.catalog.records() if row.get("network_id", "rynmesh-main") == network_id)
        try:
            values = store.list_job_capacities(
                network_id=network_id, capability=CAPABILITY, max_age_hours=1,
            ).get("capacities", [])
        except Exception:
            if services:
                return services  # This device does not require a registry connection.
            raise
        for value in values:
            if value.get("peer_id") == store.peer_id:
                continue  # A stale published self-record cannot replace local health.
            public = dict(value.get("metadata") or {}).get("llm_service")
            if isinstance(public, dict):
                if public.get("access_policy") == "explicit_friends" or any(row["peer_id"] == value.get("peer_id") for row in services):
                    continue  # An invitation to discover metadata is not a grant.
                services.append({**public, "peer_id": value.get("peer_id"), "node_name": value.get("node_name"),
                                 "updated_at": value.get("updated_at")})
        return services

    def publish_once() -> dict[str, Any]:
        """Refresh the configured provider's short-lived discovery record."""
        current = active_manager()
        if current is None:
            return {"configured": False, "online": False}
        settings = read_provider_settings()
        current.accepting_orders = bool(settings.get("publication_enabled"))
        if not current.accepting_orders:
            return {**current.public_status(), "publication_enabled": False}
        return current.publish(
            network_id=str(settings.get("network_id") or "rynmesh-main"),
            benchmark=False,
        )

    def _run_p2p_provider(order: dict[str, Any], current: ProviderService) -> None:
        task_order_id = str(order.get("work_order_id") or "")
        requester = str(order.get("requester_peer_id") or "")
        network_id = str(order.get("network_id") or "rynmesh-main")
        params = dict(order.get("params") or {})

        def publish_answer(answer: IceSignal) -> None:
            store.publish_work_result(
                work_order_id=task_order_id,
                requester_peer_id=requester,
                status="accepted",
                message="ICE candidates exchanged; direct connectivity check starting",
                result_refs={
                    "transport": "ice_udp_direct",
                    "relay_allowed": False,
                    "session_id": str(params.get("session_id") or ""),
                    "ice_signal": answer.to_dict(),
                },
                network_id=network_id,
            )

        try:
            offer = IceSignal.from_dict(dict(params.get("ice_signal") or {}))
            evidence = asyncio.run(provider_exchange(
                offer=offer,
                publish_answer=publish_answer,
                handle_request=current.handle,
                timeout_s=float(params.get("timeout_seconds") or current.manifest.timeout_seconds + 30),
            ))
            store.publish_work_result(
                work_order_id=task_order_id,
                requester_peer_id=requester,
                status="completed",
                message="LLM task completed over a nominated direct ICE/UDP candidate pair",
                result_refs={"transport_evidence": evidence},
                network_id=network_id,
            )
        except Exception as exc:
            store.publish_work_result(
                work_order_id=task_order_id,
                requester_peer_id=requester,
                status="failed",
                message="strict P2P ICE/UDP connection or task processing failed",
                result_refs={
                    "transport": "ice_udp_direct",
                    "relay_used": False,
                    "error_code": _delivery_error_code(exc, transport="p2p"),
                },
                network_id=network_id,
            )
        finally:
            with p2p_sessions_lock:
                p2p_sessions.discard(task_order_id)

    def relay_once() -> int:
        """Process signaling, settlement, and optional legacy ciphertext relay work."""
        current = active_manager()
        relay_url = os.environ.get("RYNMESH_LLM_RELAY_URL", "").strip()
        if current is None:
            return 0
        settings = read_provider_settings()
        orders = store.poll_work_orders(
            network_id=str(settings.get("network_id") or "rynmesh-main"), capability=CAPABILITY,
        ).get("work_orders", [])
        processed = 0
        for order in orders:
            operation = str(order.get("operation") or "")
            if operation not in {
                OPERATION + ".p2p_offer",
                OPERATION + ".relay",
                OPERATION + ".settlement",
                OPERATION + ".cancel",
            }:
                continue
            task_order_id = str(order.get("work_order_id") or "")
            prior = store.list_work_results(work_order_id=task_order_id).get("work_results", [])
            if any(item.get("status") in {"completed", "failed", "cancelled"} for item in prior):
                continue
            reference = str(dict(order.get("params") or {}).get("encrypted_task_ref") or "")
            requester = str(order.get("requester_peer_id") or "")
            if not requester:
                continue
            if operation == OPERATION + ".p2p_offer":
                with p2p_sessions_lock:
                    if task_order_id in p2p_sessions:
                        continue
                    p2p_sessions.add(task_order_id)
                threading.Thread(
                    target=_run_p2p_provider,
                    args=(order, current),
                    name=f"rynmesh-llm-p2p-{task_order_id[-8:]}",
                    daemon=True,
                ).start()
                processed += 1
                continue
            if operation == OPERATION + ".cancel":
                try:
                    current.cancel_signed(
                        dict(dict(order.get("params") or {}).get("signed_cancel") or {})
                    )
                    store.publish_work_result(
                        work_order_id=task_order_id, requester_peer_id=requester,
                        status="completed", message="body-free LLM cancellation recorded",
                        network_id=str(order.get("network_id") or "rynmesh-main"),
                    )
                    processed += 1
                except Exception:
                    store.publish_work_result(
                        work_order_id=task_order_id, requester_peer_id=requester,
                        status="failed", message="body-free LLM cancellation failed",
                        network_id=str(order.get("network_id") or "rynmesh-main"),
                    )
                continue
            if operation == OPERATION + ".settlement":
                try:
                    current.settle_earning(
                        dict(dict(order.get("params") or {}).get("signed_settlement") or {})
                    )
                    store.publish_work_result(
                        work_order_id=task_order_id, requester_peer_id=requester,
                        status="completed", message="body-free LLM settlement recorded",
                        network_id=str(order.get("network_id") or "rynmesh-main"),
                    )
                    processed += 1
                except Exception:
                    store.publish_work_result(
                        work_order_id=task_order_id, requester_peer_id=requester,
                        status="failed", message="body-free LLM settlement failed",
                        network_id=str(order.get("network_id") or "rynmesh-main"),
                    )
                continue
            if not relay_url:
                store.publish_work_result(
                    work_order_id=task_order_id, requester_peer_id=requester,
                    status="failed", message="ciphertext relay is not configured",
                    network_id=str(order.get("network_id") or "rynmesh-main"),
                )
                continue
            if not reference:
                continue
            try:
                encrypted_request = _download_relay_ciphertext(
                    store, reference, relay_url=relay_url, home=home,
                )
                encrypted_response = current.handle(encrypted_request)
                response_ref = _upload_relay_ciphertext(
                    store, encrypted_response, relay_url=relay_url,
                    filename=f"{task_order_id}.ciphertext", home=home,
                )
                store.publish_work_result(
                    work_order_id=task_order_id, requester_peer_id=requester, status="completed",
                    message="encrypted LLM relay response ready",
                    result_refs={"encrypted_task_ref": response_ref},
                    network_id=str(order.get("network_id") or "rynmesh-main"),
                )
                processed += 1
            except Exception:
                store.publish_work_result(
                    work_order_id=task_order_id, requester_peer_id=requester, status="failed",
                    message="encrypted LLM relay processing failed", network_id=str(order.get("network_id") or "rynmesh-main"),
                )
        return processed

    registry = getattr(app.state, "background_workers", None)
    if registry is None:
        # Standalone package tests and embedders may install the routes on a
        # plain FastAPI app. The full node lifespan owns start/stop.
        registry = BackgroundWorkerRegistry()
        app.state.background_workers = registry
    if not isinstance(registry, BackgroundWorkerRegistry):
        raise TypeError("app.state.background_workers must be a BackgroundWorkerRegistry")
    registry.register(BackgroundWorkerSpec(
        name="llm.relay-poll",
        run_once=lambda: bool(relay_once()),
        initial_delay_s=1.0,
        policy=BackoffPolicy(
            busy_delay_s=1.0,
            idle_initial_s=1.0,
            idle_multiplier=1.5,
            idle_max_s=10.0,
            error_multiplier=2.0,
            error_max_s=30.0,
        ),
        error_sink=lambda value: setattr(app.state, "llm_relay_error", value),
    ))
    def recheck_permissions_once():
        current = active_manager()
        return bool(current and current.recheck_permissions())

    if getattr(app.state, "ai_access", None):
        app.state.ai_access.recheck = recheck_permissions_once
        def own_service_status():
            current = active_manager()
            return {**current.public_status(), "network_id": read_provider_settings().get("network_id", "rynmesh-main")} if current else None
        app.state.ai_access.provider = own_service_status
    registry.register(BackgroundWorkerSpec(
        name="llm.friend-permissions", run_once=recheck_permissions_once,
        initial_delay_s=1.0,
        policy=BackoffPolicy(busy_delay_s=1, idle_initial_s=1, idle_multiplier=1, idle_max_s=1,
                             error_multiplier=2, error_max_s=10),
    ))
    registry.register(BackgroundWorkerSpec(
        name="llm.publish-refresh",
        run_once=publish_once,
        initial_delay_s=1.0,
        policy=BackoffPolicy(
            busy_delay_s=30.0,
            idle_initial_s=30.0,
            idle_multiplier=1.0,
            idle_max_s=30.0,
            error_multiplier=2.0,
            error_max_s=120.0,
        ),
        error_sink=lambda value: setattr(app.state, "llm_publication_error", value),
    ))

    def shutdown_owned_runtimes() -> None:
        """Never orphan an inference server when the node exits.

        A native-runtime `llama-server` is a child of this process; without
        this it would outlive the node, keeping the loopback port bound and
        the model resident. `runtime_native` also registers the same call with
        `atexit`, for exits that never run an application shutdown at all.
        """
        nonlocal manager
        if manager is not None:
            manager.adapter.shutdown()
            manager = None
        runtime_native.stop_owned_children()

    # Two registrations, because Starlette runs `on_shutdown` only for an app
    # that kept the default lifespan: this covers embedders and the package
    # tests, while the node's own lifespan calls `app.state.llm_shutdown`.
    # `stop_owned_children` is idempotent, so running both is harmless.
    app.state.llm_shutdown = shutdown_owned_runtimes
    app.router.on_shutdown.append(shutdown_owned_runtimes)

    @app.get("/api/local/llm/hardware")
    def local_llm_hardware() -> dict[str, Any]:
        from .hardware import detect_hardware, recommend
        report = detect_hardware(home, runtime_root=home / "llm")
        return {"hardware": report.to_dict(), "recommendations": recommend(report)}

    @app.get("/api/local/llm/services")
    def local_llm_services(network_id: str = "rynmesh-main") -> dict[str, Any]:
        return {"network_id": network_id, "services": discover(network_id)}

    @app.post("/api/local/llm/services/publish")
    async def local_llm_publish(request: Request) -> dict[str, Any]:
        body = await request.json()
        settings = read_provider_settings()
        configured = str(body.get("manifest") or settings.get("manifest") or "")
        settings.update({
            "manifest": configured,
            "publication_enabled": True,
            "network_id": str(body.get("network_id") or settings.get("network_id") or "rynmesh-main"),
        })
        write_provider_settings(settings)
        current = active_manager(configured)
        if current is None:
            raise HTTPException(status_code=400, detail="LLM service manifest is not configured")
        current.accepting_orders = True
        try:
            # publish() runs a health probe and (by default) a real benchmark
            # inference — seconds of blocking work that must not sit on the
            # node's event loop.
            return await asyncio.to_thread(
                current.publish,
                network_id=str(settings["network_id"]),
                benchmark=bool(body.get("benchmark", True)),
            )
        except (TaskProtocolError, ValueError) as exc:
            settings["publication_enabled"] = False
            write_provider_settings(settings)
            current.accepting_orders = False
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/local/llm/services/pause")
    def local_llm_pause() -> dict[str, Any]:
        settings = read_provider_settings()
        settings["publication_enabled"] = False
        write_provider_settings(settings)
        current = active_manager()
        if current is None:
            return {"configured": False, "online": False, "publication_enabled": False}
        current.accepting_orders = False
        current.publish(
            network_id=str(settings.get("network_id") or "rynmesh-main"),
            benchmark=False, require_online=False,
        )
        return {**current.public_status(), "publication_enabled": False}

    @app.post("/api/local/llm/setup")
    async def local_llm_setup(request: Request) -> dict[str, Any]:
        if not setup_operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="another local model setup is already running")
        try:
            if setup_recovery.pending:
                raise HTTPException(status_code=409, detail=RECOVERY_FAILED)
            result = await asyncio.to_thread(configure_llm, dict(await request.json()))
            return activate_configuration(result)
        except (LifecycleError, AdapterError, ManifestError, OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            setup_operation_lock.release()

    @app.get("/api/local/llm/setup/status")
    def local_llm_setup_status() -> dict[str, Any]:
        with setup_job_lock:
            return dict(read_setup_job())

    @app.post("/api/local/llm/setup/async")
    async def local_llm_setup_async(request: Request) -> dict[str, Any]:
        body = dict(await request.json())
        resume_configuration = managed_resume_configuration(body)
        with setup_job_lock:
            current_job = read_setup_job()
            if current_job.get("state") in {"queued", "running", "cancelling"}:
                raise HTTPException(status_code=409, detail="another local model setup is already running")
            if not setup_operation_lock.acquire(blocking=False):
                raise HTTPException(status_code=409, detail="another local model setup is already running")
            job_id = "setup_" + uuid.uuid4().hex
            cancel_event = threading.Event()
            setup_cancel_events.clear()
            setup_cancel_events[job_id] = cancel_event
            job = {
                "job_id": job_id,
                "state": "queued",
                "stage": "queued",
                "progress": 0,
                "message": "Local model setup is queued",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "resume_configuration": resume_configuration,
            }
            try:
                write_setup_job(job)
            except BaseException:
                setup_operation_lock.release()
                raise

        def report(stage: str, percent: int, message: str) -> None:
            with setup_job_lock:
                latest = read_setup_job()
                if latest.get("job_id") != job_id:
                    return
                latest.update({
                    "state": "cancelling" if cancel_event.is_set() else "running",
                    "stage": "cancelling" if cancel_event.is_set() else stage,
                    "progress": percent,
                    "message": "Cancelling setup; waiting for the current step to stop" if cancel_event.is_set() else message,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                write_setup_job(latest)

        def run_setup() -> None:
            captured = False
            previous_manifest = ""
            try:
                report("starting", 1, "Starting local model setup")
                # A retry first finishes the original recovery. Never capture
                # a partially replaced manifest as the new rollback baseline.
                setup_recovery.restore()
                previous_manifest = str(read_provider_settings().get("manifest") or "")
                setup_recovery.capture(previous_manifest)
                captured = True
                result = configure_llm(
                    body, progress=report, cancel_check=cancel_event.is_set,
                )
                activation = activate_configuration(result)
                setup_recovery.commit()
                with setup_job_lock:
                    write_setup_job({
                        "job_id": job_id,
                        "state": "succeeded",
                        "stage": "completed",
                        "progress": 100,
                        "message": "Local model configured and self-tested; publishing remains off",
                        "resume_configuration": resume_configuration,
                        "configured": activation["configured"],
                        "publication_enabled": False,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
            except (LifecycleError, AdapterError, ManifestError, OSError, ValueError) as exc:
                cancelled = cancel_event.is_set() or "cancelled" in str(exc).lower()
                recovery_state = "failed" if isinstance(exc, SetupRecoveryError) else "not_started"
                if captured:
                    try:
                        recovery_state = setup_recovery.restore()
                    except SetupRecoveryError:
                        recovery_state = "failed"
                    if recovery_state == "restored":
                        try:
                            resumed = start_runtime(previous_manifest)
                            active_manager(previous_manifest, refresh=True)
                            if not resumed.get("health", {}).get("ok"):
                                recovery_state = "unavailable"
                        except (LifecycleError, AdapterError, ManifestError, OSError, ValueError):
                            recovery_state = "unavailable"
                message = "Setup cancelled." if cancelled else (str(exc).strip() or "Local model setup failed")
                if recovery_state == "failed":
                    message = RECOVERY_FAILED
                elif recovery_state in {"unavailable", "missing"}:
                    message += " Previous configuration is unavailable. Check the local model service and retry configuration."
                elif recovery_state == "restored":
                    message += " Previous configuration restored and its health check passed."
                elif recovery_state == "not_needed":
                    message += " No previous model configuration required restoration."
                recovery_error = recovery_state in {"failed", "unavailable", "missing"}
                with setup_job_lock:
                    write_setup_job({
                        "job_id": job_id,
                        "state": "cancelled" if cancelled and not recovery_error else "failed",
                        "stage": "recovery" if recovery_error else "cancelled" if cancelled else "failed",
                        "progress": 0,
                        "error_code": "setup_recovery_failed" if recovery_state == "failed" else
                            "setup_previous_unavailable" if recovery_error else "setup_cancelled" if cancelled else "setup_failed",
                        "message": message,
                        "recovery_state": recovery_state,
                        "resume_configuration": resume_configuration,
                        "retryable": True,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
            finally:
                body.clear()
                with setup_job_lock:
                    setup_cancel_events.pop(job_id, None)
                setup_operation_lock.release()

        threading.Thread(
            target=run_setup, name=f"rynmesh-llm-setup-{job_id[-8:]}", daemon=True,
        ).start()
        return job

    @app.post("/api/local/llm/setup/{job_id}/cancel")
    def local_llm_setup_cancel(job_id: str) -> dict[str, Any]:
        with setup_job_lock:
            current_job = read_setup_job()
            if current_job.get("job_id") != job_id:
                raise HTTPException(status_code=404, detail="setup job not found")
            if current_job.get("state") not in {"queued", "running", "cancelling"}:
                return current_job
            event = setup_cancel_events.get(job_id)
            if event is None:
                raise HTTPException(status_code=409, detail="setup worker is no longer running")
            event.set()
            current_job.update({
                "state": "cancelling",
                "stage": "cancelling",
                "message": "Cancelling setup; waiting for the current step to stop",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            return write_setup_job(current_job)

    @app.post("/api/local/llm/service/actions/{action}")
    async def local_llm_service_action(action: str, request: Request) -> dict[str, Any]:
        nonlocal manager, manager_path
        settings = read_provider_settings()
        configured = str(settings.get("manifest") or "")
        if not configured:
            raise HTTPException(status_code=400, detail="LLM service manifest is not configured")
        body = dict(await request.json())
        allowed = {"start", "stop", "restart", "update", "self-test", "uninstall"}
        if action not in allowed:
            raise HTTPException(status_code=404, detail="unsupported local model action")
        settings["publication_enabled"] = False
        write_provider_settings(settings)
        if manager is not None:
            manager.accepting_orders = False

        def execute() -> dict[str, Any]:
            if action == "start":
                return start_runtime(configured)
            if action == "stop":
                return stop_runtime(configured)
            if action == "restart":
                return restart_runtime(configured)
            if action == "update":
                return update_runtime(configured)
            if action == "self-test":
                return {"self_test": run_self_test(load_manifest(configured))}
            return uninstall_runtime(
                configured,
                delete_environment=bool(body.get("delete_environment", True)),
                delete_model=bool(body.get("delete_model", False)),
                confirm_model_delete=bool(body.get("confirm_model_delete", False)),
            )

        try:
            result = await asyncio.to_thread(execute)
            if action in {"stop", "uninstall"} and manager is not None:
                manager.adapter.shutdown()
                manager = None
                manager_path = ""
            public_result = json.loads(json.dumps(result))
            if isinstance(public_result.get("self_test"), dict):
                public_result["self_test"].pop("output_preview", None)
            return {"ok": True, "action": action, "result": public_result,
                    "publication_enabled": False}
        except (LifecycleError, AdapterError, ManifestError, OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/local/llm/service/status")
    def local_llm_service_status(request: Request) -> dict[str, Any]:
        # Background publication/relay failures are recorded on app.state by
        # the registered worker error sinks. Without surfacing them here, a
        # provider whose registry publication is failing looks healthy while
        # its discovery record silently expires.
        background = {
            "publication_error": str(getattr(request.app.state, "llm_publication_error", "") or ""),
            "relay_poll_error": str(getattr(request.app.state, "llm_relay_error", "") or ""),
        }
        current = active_manager()
        if current is None:
            return {"configured": False, "online": False, "publication_enabled": False,
                    "background": background}
        settings = read_provider_settings()
        current.accepting_orders = bool(settings.get("publication_enabled"))
        lifecycle = {}
        try:
            lifecycle = runtime_status(str(settings.get("manifest") or ""))
        except (LifecycleError, AdapterError, ManifestError, OSError, ValueError) as exc:
            lifecycle = {"error": str(exc)}
        return {**current.public_status(),
                "publication_enabled": current.accepting_orders,
                "network_id": str(settings.get("network_id") or "rynmesh-main"),
                "lifecycle": lifecycle,
                "background": background}

    @app.get("/api/local/task-balance")
    def local_task_balance() -> dict[str, Any]:
        return {**balance.summary(), "events": balance.events()}

    @app.get("/api/local/llm/orders")
    def local_llm_orders() -> dict[str, Any]:
        consumer_orders.purge_expired_responses()
        summaries = []
        for record in consumer_orders.list():
            final = order_state_metadata(record)
            summaries.append({
                "task_id": record.get("task_id"), "state": record.get("state"),
                "created_at": record.get("created_at"), "updated_at": record.get("updated_at"),
                "history": list(record.get("history") or []),
                **{key: value for key, value in final.items() if key not in {"at", "state"}},
            })
        summaries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return {"orders": summaries}

    @app.get("/api/local/llm/privacy")
    def local_llm_privacy() -> dict[str, Any]:
        return {
            "result_retention_seconds": response_retention(),
            "plaintext_persisted": False,
            "stored_results_encrypted": True,
            "compute_node_sees_plaintext": True,
        }

    @app.put("/api/local/llm/privacy")
    async def local_llm_privacy_update(request: Request) -> dict[str, Any]:
        value = int(dict(await request.json()).get("result_retention_seconds") or 0)
        if value not in {0, 3600, 86400, 604800}:
            raise HTTPException(status_code=400, detail="unsupported result retention period")
        write_consumer_settings({"result_retention_seconds": value})
        if value == 0:
            for order in consumer_orders.list():
                consumer_orders.purge_encrypted_response(str(order["task_id"]))
        return local_llm_privacy()

    @app.delete("/api/local/llm/orders")
    def local_llm_orders_clear() -> dict[str, Any]:
        removed = 0
        for record in consumer_orders.list():
            if record.get("state") in TERMINAL_STATES:
                removed += int(consumer_orders.delete(str(record["task_id"])))
        return {"ok": True, "removed": removed}

    @app.get("/api/local/llm/provider-orders")
    def local_llm_provider_orders() -> dict[str, Any]:
        return {"orders": provider_orders.list()}

    @app.post("/api/local/llm/orders")
    async def local_llm_order(request: Request) -> dict[str, Any]:
        return await execute_order(dict(await request.json()))

    async def execute_order(body: dict[str, Any]) -> dict[str, Any]:
        network_id = str(body.get("network_id") or "rynmesh-main")
        provider_peer_id = str(body.get("provider_peer_id") or "")
        service_id = str(body.get("service_id") or "")
        prompt = str(body.get("prompt") or "")
        prompt_format = body.get("prompt_format", "text")
        try:
            decode_chat_prompt(prompt, prompt_format)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        try:
            max_tokens = int(body.get("max_tokens") or 64)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="max_tokens must be a positive integer") from exc
        if max_tokens < 1:
            raise HTTPException(status_code=400, detail="max_tokens must be a positive integer")
        if not provider_peer_id or not service_id or not prompt:
            raise HTTPException(status_code=400, detail="provider_peer_id, service_id, and prompt are required")
        records = await asyncio.to_thread(discover, network_id)
        selected = next((item for item in records
                         if item.get("peer_id") == provider_peer_id
                         and dict(item.get("service") or {}).get("package_id") == service_id), None)
        if selected and (selected.get("ready") is False or dict(selected.get("health") or {}).get("ok") is False):
            health_code = dict(selected.get("health") or {}).get("error_code")
            code = health_code if isinstance(health_code, str) and health_code in RUNTIME_ERROR_CODES else "service_unhealthy"
            raise HTTPException(status_code=409, detail=code)
        if not selected or not selected.get("online"):
            raise HTTPException(status_code=409, detail="service is absent, stale, offline, or unhealthy")
        if _record_is_stale(selected.get("updated_at")):
            # The online flag is frozen at publish time and discovery keeps
            # records for up to an hour; a healthy provider republishes every
            # 30s, so anything older than a few minutes is a dead provider.
            raise HTTPException(status_code=409, detail="service discovery record is stale; the provider has stopped refreshing")
        capacity = dict(selected.get("capacity") or {})
        if capacity.get("available") is not None and int(capacity["available"]) < 1:
            raise HTTPException(status_code=409, detail="capacity_exhausted: Provider is busy")
        public_manifest = dict(selected["service"])
        if prompt_format == CHAT_FORMAT and CHAT_FORMAT not in public_manifest.get("capabilities", []):
            raise HTTPException(status_code=409, detail="provider no longer supports the reviewed prompt format")
        try:
            # Only package_id/model_alias/context_window/max_output_tokens/
            # timeout_seconds/pricing are load-bearing here; the rest are
            # pass-through with defaults so a cross-version provider record
            # yields a clean 409 instead of a KeyError 500.
            manifest = LLMPackageManifest.from_dict({
                "package_id": public_manifest["package_id"], "mode": "openai_compatible",
                "public_model_alias": public_manifest["model_alias"], "base_url": "http://127.0.0.1",
                "version": public_manifest.get("version", "0"),
                "protocol_version": public_manifest.get("protocol_version", "1"),
                "adapter": public_manifest.get("adapter", "openai_compatible"),
                "runtime": public_manifest.get("runtime", ""),
                "capabilities": public_manifest.get("capabilities", ["chat"]),
                "context_window": public_manifest["context_window"],
                "max_output_tokens": public_manifest["max_output_tokens"],
                "max_concurrent": public_manifest.get("max_concurrent", 1),
                "queue_limit": public_manifest.get("queue_limit", 0),
                "timeout_seconds": public_manifest["timeout_seconds"],
                "hardware_requirements": public_manifest.get("hardware_requirements", {}),
                "pricing": public_manifest["pricing"],
                "privacy": public_manifest.get("privacy", {}),
                "license_id": public_manifest.get("license_id", ""),
                "license_notice": public_manifest.get("license_notice", ""),
                "risk_labels": public_manifest.get("risk_labels", []),
                "content_rules": public_manifest.get("content_rules", []),
                "model_fingerprint": public_manifest.get("model_fingerprint", ""),
            })
        except (KeyError, TypeError, ValueError, ManifestError) as exc:
            raise HTTPException(
                status_code=409,
                detail="provider discovery record is missing required manifest fields",
            ) from exc
        max_tokens = min(max_tokens, manifest.max_output_tokens)
        if _estimate_input_tokens(prompt) + max_tokens > manifest.context_window:
            raise HTTPException(
                status_code=400,
                detail="estimated prompt and output exceed the Provider context window",
            )
        maximum = _estimate_price(manifest, prompt, max_tokens)
        if maximum > manifest.pricing.maximum_per_task:
            raise HTTPException(status_code=409, detail="estimated task cost exceeds provider maximum")
        if maximum > float(balance.summary().get("available") or 0):
            raise HTTPException(status_code=409, detail="insufficient development Task Balance")
        task_id = str(body.get("task_id") or ("task_" + uuid.uuid4().hex))
        idempotency_key = str(body.get("idempotency_key") or task_id)
        requested_transport = str(body.get("transport") or "auto").strip().lower()
        if requested_transport not in {"auto", "direct", "p2p", "relay"}:
            raise HTTPException(status_code=400, detail="transport must be auto, direct, p2p, or relay")
        transport_mode = requested_transport
        failure_transport = transport_mode
        request_fingerprint = _request_fingerprint({
            "task_id": task_id,
            "idempotency_key": idempotency_key,
            "provider_peer_id": provider_peer_id,
            "service_id": service_id,
            "prompt": prompt,
            **({"prompt_format": prompt_format} if prompt_format != "text" else {}),
            **({"ai_permission": body["ai_permission"]} if "ai_permission" in body else {}),
            "max_tokens": max_tokens,
            "max_amount": maximum,
            "transport": requested_transport,
        }, store.private_key_bytes)
        bindings = {
            "provider_peer_id": provider_peer_id,
            "service_id": service_id,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
        }
        try:
            existing, claimed = consumer_orders.claim(task_id=task_id, bindings=bindings)
        except TaskProtocolError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not claimed:
            encrypted = existing.get("encrypted_response")
            if isinstance(encrypted, dict):
                final = order_state_metadata(existing)
                try:
                    _, prior_result = _open_provider_response(
                        encrypted, recipient_peer_id=store.peer_id, messaging_key=messaging_key,
                        task_id=task_id,
                        provider_peer_id=provider_peer_id, service_id=service_id,
                    )
                except TaskProtocolError:
                    # The stored envelope outlived its own expiry (retention is
                    # longer than envelope lifetime). The order metadata is
                    # still authoritative; answer with it instead of a 500.
                    return {"task_id": task_id,
                            "state": str(existing.get("state") or "unknown"),
                            "result_expired": True,
                            "transport": str(final.get("transport") or "unknown"),
                            "amount": final.get("amount"),
                            "detail": "stored result envelope has expired"}
                prior_result["transport"] = str(final.get("transport") or "unknown")
                prior_result["transport_evidence"] = dict(final.get("transport_evidence") or {})
                return prior_result
            raise HTTPException(
                status_code=409,
                detail=f"task already exists in state {existing.get('state', 'unknown')}",
            )
        with background_orders_lock:
            cancelled_before_start = task_id in pending_cancellations
            pending_cancellations.discard(task_id)
        if cancelled_before_start:
            consumer_orders.transition(
                task_id=task_id, state="cancelled",
                metadata={"provider_peer_id": provider_peer_id, "service_id": service_id,
                          "reason": "consumer_cancelled_before_start"},
            )
            return {"task_id": task_id, "state": "cancelled"}
        try:
            balance.hold(task_id=task_id, amount=maximum, service_id=service_id,
                         provider_peer_id=provider_peer_id, idempotency_key=idempotency_key,
                         request_fingerprint=request_fingerprint)
            consumer_orders.transition(task_id=task_id, state="accepted",
                                       metadata={"provider_peer_id": provider_peer_id,
                                                 "service_id": service_id,
                                                 "network_id": network_id})
            recipient_pub = str(selected.get("node_messaging_pub") or "") or resolve_pubkey(provider_peer_id)
            from rynmesh.services import peer_box
            signed = seal_task(
                body={"task_id": task_id, "idempotency_key": idempotency_key,
                      "service_id": service_id, "prompt": prompt, "max_tokens": max_tokens,
                      **({"prompt_format": prompt_format} if prompt_format != "text" else {}),
                      **({"ai_permission": body["ai_permission"]} if "ai_permission" in body else {}),
                      "max_amount": maximum, "reply_messaging_pub": peer_box.public_key_b64(messaging_key)},
                task_id=task_id, kind="llm_request", sender_peer_id=store.peer_id,
                recipient_peer_id=provider_peer_id, sender_signing_key=store.private_key_bytes,
                recipient_messaging_pub=recipient_pub,
                expires_at=_expires(max(60, manifest.timeout_seconds + 30)),
            )
            own_request = provider_peer_id == store.peer_id
            endpoint = "" if own_request else resolve_provider_endpoint(provider_peer_id)
            encrypted_response = None
            direct_error: Exception | None = None
            transport_evidence: dict[str, Any] = {}
            configured_transport = os.environ.get("RYNMESH_LLM_TRANSPORT", "auto").strip().lower()
            transport_mode = requested_transport if configured_transport == "auto" else configured_transport
            if transport_mode not in {"auto", "direct", "p2p", "relay"}:
                raise TaskProtocolError("LLM transport must be auto, direct, p2p, or relay")
            force_relay = os.environ.get("RYNMESH_LLM_FORCE_RELAY", "").strip().lower() in {
                "1", "true", "yes",
            }
            if force_relay:
                transport_mode = "relay"
            if own_request:
                transport_mode = "local"
            failure_transport = transport_mode
            consumer_orders.transition(
                task_id=task_id,
                state="running",
                metadata={
                    "provider_peer_id": provider_peer_id,
                    "service_id": service_id,
                    "network_id": network_id,
                    "transport": transport_mode,
                },
            )
            if own_request:
                current = active_manager()
                if current is None or current.manifest.package_id != service_id:
                    raise TaskProtocolError("local service is no longer configured")
                encrypted_response = await asyncio.to_thread(current.handle, signed.to_dict())
                transport_evidence = {"transport": "local_runtime", "relay_used": False}
            elif transport_mode == "p2p":
                p2p_work_order_id = ""

                async def publish_offer(offer: IceSignal) -> IceSignal:
                    nonlocal p2p_work_order_id
                    submitted = await asyncio.to_thread(
                        store.submit_work_order,
                        provider_peer_id=provider_peer_id,
                        capability=CAPABILITY,
                        operation=OPERATION + ".p2p_offer",
                        params={
                            "session_id": task_id,
                            "ice_signal": offer.to_dict(),
                            "timeout_seconds": manifest.timeout_seconds + 30,
                        },
                        network_id=network_id,
                        idempotency_key="p2p:" + idempotency_key,
                        expires_in_hours=max(1, manifest.timeout_seconds / 3600),
                    )
                    p2p_work_order_id = str(submitted["order"]["work_order_id"])
                    deadline = time.monotonic() + manifest.timeout_seconds + 30
                    while time.monotonic() < deadline:
                        values = await asyncio.to_thread(
                            store.list_work_results,
                            work_order_id=p2p_work_order_id,
                            network_id=network_id,
                        )
                        results = values.get("work_results", [])
                        failed = next(
                            (item for item in results if item.get("status") in {"failed", "cancelled"}),
                            None,
                        )
                        if failed:
                            if dict(failed.get("result_refs") or {}).get("error_code") == "p2p_capacity_exhausted":
                                raise P2PCapacityError("provider fixed UDP port is busy; retry later")
                            raise TaskProtocolError("provider rejected strict P2P signaling")
                        accepted = next(
                            (item for item in results if item.get("status") == "accepted"),
                            None,
                        )
                        if accepted:
                            refs = dict(accepted.get("result_refs") or {})
                            if refs.get("relay_allowed") is not False:
                                raise TaskProtocolError("provider answer did not forbid relay")
                            return IceSignal.from_dict(dict(refs.get("ice_signal") or {}))
                        await asyncio.sleep(0.25)
                    raise TaskProtocolError("timed out waiting for provider ICE answer")

                encrypted_response, transport_evidence = await consumer_exchange(
                    signed_request=signed.to_dict(),
                    publish_offer=publish_offer,
                    timeout_s=manifest.timeout_seconds + 30,
                )
            elif endpoint and transport_mode in {"auto", "direct"}:
                failure_transport = "direct"
                try:
                    # Blocking I/O for the full inference duration — run it in
                    # a worker thread so the node's event loop stays live.
                    encrypted_response = await asyncio.to_thread(
                        _peer_post_json, endpoint, "/api/peer/llm/tasks",
                        signed.to_dict(), timeout_s=manifest.timeout_seconds + 30,
                    )
                    transport_evidence = {
                        "transport": "peer_http_direct",
                        "relay_used": False,
                    }
                except Exception as exc:
                    direct_error = exc
            if encrypted_response is None and transport_mode == "direct":
                raise TaskProtocolError("strict direct provider path failed") from direct_error
            if encrypted_response is None:
                relay_url = os.environ.get("RYNMESH_LLM_RELAY_URL", "").strip()
                if transport_mode == "p2p":
                    raise TaskProtocolError("strict P2P path failed; relay fallback is disabled")
                if not relay_url:
                    raise TaskProtocolError("direct provider path failed and no dedicated LLM relay is configured") from direct_error
                failure_transport = "relay"
                def _relay_exchange() -> dict[str, Any]:
                    reference = _upload_relay_ciphertext(
                        store, signed.to_dict(), relay_url=relay_url,
                        filename=f"{task_id}.ciphertext", home=home,
                    )
                    submitted = store.submit_work_order(
                        provider_peer_id=provider_peer_id, capability=CAPABILITY,
                        operation=OPERATION + ".relay",
                        params={"encrypted_task_ref": reference},
                        network_id=network_id, idempotency_key=idempotency_key,
                        expires_in_hours=max(1, manifest.timeout_seconds / 3600),
                    )
                    work_order_id = str(submitted["order"]["work_order_id"])
                    deadline = time.monotonic() + manifest.timeout_seconds + 60
                    while time.monotonic() < deadline:
                        results = store.list_work_results(work_order_id=work_order_id, network_id=network_id).get("work_results", [])
                        terminal = next((item for item in results if item.get("status") in {"completed", "failed", "cancelled"}), None)
                        if terminal:
                            if terminal.get("status") != "completed":
                                raise TaskProtocolError("encrypted relay task failed")
                            ref = str(dict(terminal.get("result_refs") or {}).get("encrypted_task_ref") or "")
                            return _download_relay_ciphertext(store, ref, relay_url=relay_url, home=home)
                        time.sleep(0.5)
                    raise TaskProtocolError("encrypted relay task timed out")

                # Minutes of registry polling and blob I/O — worker thread, not
                # the event loop.
                encrypted_response = await asyncio.to_thread(_relay_exchange)
                transport_evidence = {
                    "transport": "encrypted_relay",
                    "relay_used": True,
                }
            _, result = _open_provider_response(
                encrypted_response, recipient_peer_id=store.peer_id, messaging_key=messaging_key,
                task_id=task_id,
                provider_peer_id=provider_peer_id, service_id=service_id,
            )
            state = str(result.get("state") or "failed")
            result["transport"] = str(transport_evidence.get("transport") or "unknown")
            result["transport_evidence"] = transport_evidence
            if state != "succeeded":
                balance.release(task_id=task_id, reason=str(result.get("error_code") or state))
                retention = response_retention()
                consumer_orders.transition(
                    task_id=task_id, state=state,
                    metadata={"provider_peer_id": provider_peer_id,
                              "service_id": service_id,
                              "error_code": result.get("error_code", state),
                              "response_expires_at": _expires(retention) if retention else ""},
                    encrypted_response=encrypted_response if retention else None,
                )
                return result
            retention = response_retention()
            settlement_metadata = {
                "provider_peer_id": provider_peer_id, "service_id": service_id,
                "network_id": network_id, "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"], "duration_ms": result["duration_ms"],
                "amount": result["amount"], "transport": result["transport"],
                "relay_used": transport_evidence.get("relay_used"),
                "transport_evidence": transport_evidence,
                "response_expires_at": _expires(retention) if retention else "",
            }
            consumer_orders.checkpoint(
                task_id=task_id,
                metadata={**settlement_metadata, "settlement_pending": True},
                encrypted_response=encrypted_response if retention else None,
            )
            balance.settle(
                task_id=task_id, amount=float(result["amount"]), input_tokens=int(result["input_tokens"]),
                output_tokens=int(result["output_tokens"]), duration_ms=int(result["duration_ms"]),
                service_id=service_id, provider_peer_id=provider_peer_id,
            )
            consumer_orders.transition(
                task_id=task_id, state="succeeded",
                metadata=settlement_metadata,
                encrypted_response=encrypted_response if retention else None,
                allow_recovery=True,
            )
            settlement_delivered = await asyncio.to_thread(
                dispatch_settlement, store, task_id=task_id,
                provider_peer_id=provider_peer_id, service_id=service_id,
                amount=float(result["amount"]), network_id=network_id,
                endpoint=endpoint if transport_mode in {"auto", "direct"} else "",
            )
            if settlement_delivered:
                consumer_orders.checkpoint(
                    task_id=task_id, metadata={"settlement_dispatched": True},
                )
            return result
        except Exception as exc:
            error_code = _delivery_error_code(exc, transport=failure_transport)
            try:
                balance.release(task_id=task_id, reason=error_code)
                consumer_orders.transition(task_id=task_id, state="failed",
                                           metadata={"provider_peer_id": provider_peer_id,
                                                     "service_id": service_id,
                                                     "error_code": error_code})
            except (TaskBalanceError, TaskProtocolError):
                pass
            reason = str(exc).strip() or type(exc).__name__
            raise HTTPException(
                status_code=502,
                detail=f"private LLM task failed: {type(exc).__name__}: {reason}",
            ) from exc
        finally:
            prompt = ""

    def run_background_order(body: dict[str, Any], task_id: str) -> None:
        try:
            completed = asyncio.run(execute_order(body))
            if response_retention() == 0:
                with background_orders_lock:
                    background_orders[task_id] = {**completed, "ephemeral": True,
                                                  "_recorded_at": time.time()}
        except HTTPException as exc:
            with background_orders_lock:
                current = background_orders.get(task_id, {})
                current.update({"task_id": task_id, "state": "failed",
                                "error_code": _submission_error_code(str(exc.detail)),
                                "detail": str(exc.detail), "_recorded_at": time.time()})
                background_orders[task_id] = current
        except Exception as exc:
            with background_orders_lock:
                current = background_orders.get(task_id, {})
                current.update({"task_id": task_id, "state": "failed",
                                "error_code": "background_worker_failed",
                                "detail": str(exc).strip() or type(exc).__name__,
                                "_recorded_at": time.time()})
                background_orders[task_id] = current
        finally:
            body["prompt"] = ""
            if consumer_orders.get(task_id) is not None and response_retention() != 0:
                with background_orders_lock:
                    background_orders.pop(task_id, None)

    def _public_background(entry: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    def _prune_background_orders() -> None:
        """Drop stale terminal entries; without this, rejected submissions and
        retention=0 results (each keyed by a fresh task uuid) accumulate for
        the process lifetime. Caller holds background_orders_lock."""
        cutoff = time.time() - 900
        for key in [k for k, v in background_orders.items()
                    if isinstance(v, dict) and float(v.get("_recorded_at") or 0) < cutoff
                    and "_recorded_at" in v]:
            background_orders.pop(key, None)

    @app.post("/api/local/llm/orders/async")
    async def local_llm_order_async(request: Request) -> dict[str, Any]:
        return submit_order(dict(await request.json()))

    def submit_order(value: dict[str, Any]) -> dict[str, Any]:
        body = dict(value)
        if not str(body.get("provider_peer_id") or "") \
                or not str(body.get("service_id") or "") \
                or not str(body.get("prompt") or ""):
            raise HTTPException(
                status_code=400,
                detail="provider_peer_id, service_id, and prompt are required",
            )
        try:
            max_tokens = int(body.get("max_tokens") or 64)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="max_tokens must be a positive integer") from exc
        if max_tokens < 1:
            raise HTTPException(status_code=400, detail="max_tokens must be a positive integer")
        body["max_tokens"] = max_tokens
        task_id = str(body.get("task_id") or ("task_" + uuid.uuid4().hex))
        try:
            existing = consumer_orders.get(task_id)
        except TaskProtocolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with background_orders_lock:
            _prune_background_orders()
            pending = background_orders.get(task_id)
            if pending is not None:
                return _public_background(pending)
            if existing is not None:
                return {"task_id": task_id, "state": str(existing.get("state") or "unknown")}
            background_orders[task_id] = {"task_id": task_id, "state": "queued"}
        body["task_id"] = task_id
        threading.Thread(
            target=run_background_order, args=(body, task_id),
            name=f"rynmesh-llm-consumer-{task_id[-8:]}", daemon=True,
        ).start()
        return {"task_id": task_id, "state": "queued"}

    @app.get("/api/local/llm/orders/{task_id}")
    def local_llm_order_status(task_id: str) -> dict[str, Any]:
        return order_status(task_id, consume_ephemeral=True)

    def order_state_metadata(record: dict[str, Any]) -> dict[str, Any]:
        # Later settlement checkpoints supplement the current state's usage
        # and transport; metadata from earlier states must not leak through.
        return {key: value for event in record.get("history", []) if event.get("state") == record.get("state")
                for key, value in event.items() if key not in {"at", "checkpoint"}}

    def order_status(task_id: str, *, consume_ephemeral: bool = False) -> dict[str, Any]:
        try:
            record = consumer_orders.get(task_id)
        except TaskProtocolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if record is None:
            with background_orders_lock:
                pending = background_orders.get(task_id)
            if pending is None:
                raise HTTPException(status_code=404, detail="task not found")
            return _public_background(pending)
        # A settlement acknowledgement is a later checkpoint, not a replacement
        # for the terminal usage/transport metadata needed after restarting UI.
        final = order_state_metadata(record)
        result: dict[str, Any] = {
            "task_id": task_id,
            "state": str(record.get("state") or "unknown"),
            **{key: value for key, value in final.items() if key != "at"},
        }
        with background_orders_lock:
            ephemeral = background_orders.get(task_id)
            if consume_ephemeral and ephemeral and ephemeral.get("ephemeral"):
                background_orders.pop(task_id, None)
        if ephemeral and ephemeral.get("ephemeral"):
            return {key: value for key, value in _public_background(ephemeral).items()
                    if key != "ephemeral"}
        encrypted = record.get("encrypted_response")
        if not consume_ephemeral and ephemeral is not None and ephemeral.get("state") == "queued":
            # A terminal ledger entry can precede the background thread's
            # transient result. Archive only after that result becomes visible.
            result["result_pending"] = True
        bindings = dict(record.get("bindings") or {})
        if isinstance(encrypted, dict):
            try:
                _, response = _open_provider_response(
                    encrypted, recipient_peer_id=store.peer_id, messaging_key=messaging_key,
                    task_id=task_id, provider_peer_id=str(bindings.get("provider_peer_id") or ""),
                    service_id=str(bindings.get("service_id") or ""),
                )
                result.update(response)
            except TaskProtocolError:
                result["error_code"] = "stored_response_invalid"
        return result

    @app.post("/api/local/llm/orders/{task_id}/cancel")
    def local_llm_cancel(task_id: str) -> dict[str, Any]:
        record = consumer_orders.get(task_id)
        if not record:
            with background_orders_lock:
                if task_id not in background_orders:
                    raise HTTPException(status_code=404, detail="task not found")
                pending_cancellations.add(task_id)
                background_orders[task_id] = {"task_id": task_id, "state": "cancelled"}
            return {"task_id": task_id, "state": "cancelled"}
        if record.get("state") not in TERMINAL_STATES:
            try:
                balance.release(task_id=task_id, reason="consumer_cancelled")
            except TaskBalanceError as exc:
                if "task hold not found" not in str(exc):
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
            consumer_orders.transition(task_id=task_id, state="cancelled",
                                       metadata={"reason": "consumer_cancelled"})
            bindings = dict(record.get("bindings") or {})
            provider_peer_id = str(bindings.get("provider_peer_id") or "")
            service_id = str(bindings.get("service_id") or "")
            history = list(record.get("history") or [])
            network_id = next(
                (str(item.get("network_id")) for item in reversed(history) if item.get("network_id")),
                "rynmesh-main",
            )
            signed_cancel = sign_payload({
                "kind": "llm_cancel", "task_id": task_id, "from_peer_id": store.peer_id,
                "to_peer_id": provider_peer_id, "service_id": service_id,
                "cancel_id": "cancel:" + task_id,
            }, private_key_bytes=store.private_key_bytes)
            delivered = False
            if provider_peer_id == store.peer_id:
                current = active_manager()
                if current is not None and current.manifest.package_id == service_id:
                    current.cancel_signed(signed_cancel.to_dict())
                return {"task_id": task_id, "state": (consumer_orders.get(task_id) or {}).get("state")}
            endpoint = resolve_provider_endpoint(provider_peer_id, cancellation=True)
            if endpoint:
                try:
                    _peer_post_json(
                        endpoint, "/api/peer/llm/cancellations",
                        signed_cancel.to_dict(), timeout_s=5,
                    )
                    delivered = True
                except Exception:
                    pass
            if not delivered and provider_peer_id and service_id:
                try:
                    store.submit_work_order(
                        provider_peer_id=provider_peer_id, capability=CAPABILITY,
                        operation=OPERATION + ".cancel",
                        params={"signed_cancel": signed_cancel.to_dict()}, network_id=network_id,
                        idempotency_key="cancel:" + task_id, expires_in_hours=1,
                    )
                except Exception:
                    pass
        return {"task_id": task_id, "state": (consumer_orders.get(task_id) or {}).get("state")}

    @app.post("/api/peer/llm/tasks")
    async def peer_llm_task(request: Request) -> dict[str, Any]:
        current = active_manager()
        if current is None:
            raise HTTPException(status_code=503, detail="LLM service not configured")
        try:
            body = await request.json()
            return await asyncio.to_thread(current.handle, body)
        except TaskProtocolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/peer/llm/settlements")
    async def peer_llm_settlement(request: Request) -> dict[str, Any]:
        current = active_manager()
        if current is None:
            raise HTTPException(status_code=503, detail="LLM service not configured")
        try:
            earning = current.settle_earning(await request.json())
            return {"ok": True, "event_id": earning["event_id"]}
        except TaskProtocolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/peer/llm/cancellations")
    async def peer_llm_cancellation(request: Request) -> dict[str, Any]:
        current = active_manager()
        if current is None:
            raise HTTPException(status_code=503, detail="LLM service not configured")
        try:
            current.cancel_signed(await request.json())
            return {"ok": True}
        except TaskProtocolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    app.state.llm_provider = active_manager()

    def acknowledge_result(task_id: str) -> None:
        with background_orders_lock:
            prior = background_orders.get(task_id)
            if prior and prior.get("ephemeral"):
                background_orders.pop(task_id, None)

    def erase_results(task_ids):
        from .privacy import erase_consumer_results

        return erase_consumer_results(consumer_orders, background_orders, background_orders_lock, task_ids)

    return ConsumerCommands(submit_order, order_status, local_llm_cancel, acknowledge_result, discover, erase_results)
