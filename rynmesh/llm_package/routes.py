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
import urllib.request
import uuid
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from fastapi import HTTPException, Request

from rynmesh.crypto import SignedPayload, sign_payload, verify_signed_payload
from rynmesh.store import RynmeshStore
from rynmesh.transport import network_key_header

from .adapters import AdapterError, LLMAdapter, adapter_from_manifest
from .chat import validate_chat
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
from .p2p import IceSignal, P2PError, consumer_exchange, provider_exchange
from .streaming import events, peer_stream
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
                 balance: TaskBalanceLedger, messaging_key: Any) -> None:
        self.manifest = manifest
        self.adapter = adapter
        self.store = store
        self.task_store = task_store
        self.balance = balance
        self.messaging_key = messaging_key
        self._slots = threading.BoundedSemaphore(manifest.max_concurrent)
        self._lock = threading.Lock()
        self._running = 0
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
            "service": self.manifest.public_dict(),
            "online": bool(health.get("ok")) and self.accepting_orders,
            "accepting_orders": self.accepting_orders,
            "health": {k: v for k, v in health.items() if k not in {"base_url", "path"}},
            "capacity": {"max_concurrent": self.manifest.max_concurrent,
                         "running": self._running, "available": max(0, self.manifest.max_concurrent - self._running),
                         "queue_limit": self.manifest.queue_limit, "queue_policy": "reject_when_full"},
        }
        result["chat_protocol"] = "rynmesh.chat.v1" if hasattr(self.adapter, "chat") else None
        from rynmesh.services import peer_box

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

    def handle(self, signed_request: dict[str, Any], on_event: Any = None) -> dict[str, Any]:
        outer, body = open_task(
            signed_request, recipient_peer_id=self.store.peer_id,
            recipient_messaging_key=self.messaging_key, expected_kind="llm_request",
        )
        task_id = str(outer["task_id"])
        if str(body.get("service_id")) != self.manifest.package_id:
            raise TaskProtocolError("requested service is not available")
        chat = validate_chat(body["chat"]) if "chat" in body else None
        prompt = json.dumps(chat, ensure_ascii=False) if chat else str(body.get("prompt") or "")
        if not prompt:
            raise TaskProtocolError("prompt is required")
        max_tokens = min(int(body.get("max_tokens") or 64), self.manifest.max_output_tokens)
        if max_tokens < 1:
            raise TaskProtocolError("max_tokens is invalid")
        reply_pub = str(body.get("reply_messaging_pub") or "")
        _validate_messaging_pub(reply_pub)
        max_amount = float(body.get("max_amount") or 0)
        if max_amount <= 0:
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
            if not self.accepting_orders:
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
                    task_id, reply_pub, str(outer["from_peer_id"]), "failed", "result_expired",
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
                        task_id, reply_pub, str(outer["from_peer_id"]), "failed", "result_expired",
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
        try:
            self.task_store.transition(task_id=task_id, state="accepted", metadata=metadata)
            self.task_store.transition(task_id=task_id, state="running", metadata=metadata)
            started = time.monotonic()
            if chat:
                if not hasattr(self.adapter, "chat"):
                    raise AdapterError("structured_chat_not_supported")
                if chat.get("stream") and on_event is None:
                    raise AdapterError("stream_transport_not_supported")
                sequence = 0

                def emit(chunk: dict[str, Any]) -> None:
                    nonlocal sequence
                    if (self.task_store.get(task_id) or {}).get("state") == "cancelled":
                        raise AdapterError("task_cancelled")
                    envelope = seal_task(
                        body={"task_id": task_id, "service_id": self.manifest.package_id,
                              "sequence": sequence, "chunk": chunk},
                        task_id=task_id, kind="llm_stream", sender_peer_id=self.store.peer_id,
                        recipient_peer_id=str(outer["from_peer_id"]),
                        sender_signing_key=self.store.private_key_bytes, recipient_messaging_pub=reply_pub,
                        expires_at=_expires(max(300, self.manifest.timeout_seconds * 2)),
                    ).to_dict()
                    on_event(envelope)
                    sequence += 1

                chat["max_tokens"] = max_tokens
                result = self.adapter.chat(chat, task_id=task_id, timeout_s=self.manifest.timeout_seconds,
                                           on_event=emit if chat.get("stream") else None)
            else:
                result = self.adapter.infer(
                    prompt=prompt, max_tokens=max_tokens, task_id=task_id,
                    timeout_s=self.manifest.timeout_seconds,
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
            amount = min(
                _price(self.manifest, int(result["input_tokens"]), int(result["output_tokens"])),
                max_amount,
            )
            response_body = {
                "task_id": task_id, "state": "succeeded", "service_id": self.manifest.package_id,
                "model_alias": self.manifest.public_model_alias, "output": str(result["text"]),
                "input_tokens": int(result["input_tokens"]), "output_tokens": int(result["output_tokens"]),
                "duration_ms": duration_ms, "amount": amount, "currency": "DEV_TASK_BALANCE",
            }
            if chat:
                response_body.update(message=result["message"], finish_reason=result["finish_reason"])
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
            already_cancelled = (self.task_store.get(task_id) or {}).get("state") == "cancelled"
            state = "cancelled" if already_cancelled or "task_cancelled" in message else (
                "timed_out" if "timed out" in message else "failed"
            )
            return self._failure(task_id, reply_pub, outer["from_peer_id"], state,
                                 "consumer_cancelled" if state == "cancelled" else (
                                     "inference_timeout" if state == "timed_out" else "inference_failed"
                                 ))
        finally:
            prompt = ""  # best-effort reference cleanup; see privacy documentation
            with self._lock:
                self._running -= 1
            self._slots.release()

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


def _peer_post_json(url: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    """POST JSON to a peer endpoint with a bounded response read.

    Raw json.load over a peer socket would let a hostile provider stream an
    unbounded body into memory; every peer response in this module is small.
    """
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **network_key_header()}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        raw = response.read(_MAX_PEER_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_PEER_RESPONSE_BYTES:
        raise TaskProtocolError("peer response exceeds size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TaskProtocolError("peer response must be a JSON object")
    return value


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
    settlement = sign_payload({
        "kind": "llm_settlement", "task_id": task_id, "from_peer_id": store.peer_id,
        "to_peer_id": provider_peer_id, "amount": amount, "service_id": service_id,
        "settlement_id": "settle:" + task_id,
    }, private_key_bytes=store.private_key_bytes)
    if endpoint:
        try:
            _peer_post_json(endpoint + "/api/peer/llm/settlements",
                            settlement.to_dict(), timeout_s=15)
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


def install_llm_routes(app: Any, *, store: RynmeshStore, home: Path, messaging_key: Any,
                       resolve_endpoint: Callable[[str], str], resolve_pubkey: Callable[[str], str]) -> None:
    provider_orders = TaskOrderStore(home / "llm" / "provider-orders")
    consumer_orders = TaskOrderStore(home / "llm" / "consumer-orders")
    balance = TaskBalanceLedger(home / "llm" / "task-balance.json")
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
    setup_cancel_events: dict[str, threading.Event] = {}
    manager_path = ""

    def write_setup_job(value: dict[str, Any]) -> dict[str, Any]:
        setup_job_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = setup_job_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(setup_job_path)
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
    if previous_setup_job.get("state") in {"queued", "running", "cancelling"}:
        previous_setup_job.update({
            "state": "failed",
            "stage": "recovery",
            "error_code": "setup_interrupted",
            "message": "Setup was interrupted when the node stopped. Review the model service and retry.",
            "retryable": True,
        })
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
        consumer_settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = consumer_settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(consumer_settings_path)
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
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(settings_path)
        return value

    def active_manager(path: str = "") -> ProviderService | None:
        nonlocal manager, manager_path
        # Serialized: this is reached concurrently from async routes, threadpool
        # sync routes, and both background loops. Without the lock two callers
        # can build two ProviderServices (doubling max_concurrent against one
        # runtime) or shut an adapter down while another thread is mid-handle.
        with manager_lock:
            settings = read_provider_settings()
            configured = str(path or settings.get("manifest") or "")
            if manager is not None and configured and configured != manager_path:
                manager.adapter.shutdown()
                manager = None
            if manager is None and configured:
                manifest = load_manifest(configured)
                manager = ProviderService(
                    manifest=manifest, adapter=adapter_from_manifest(manifest), store=store,
                    task_store=provider_orders, balance=balance, messaging_key=messaging_key,
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
                progress=progress, cancel_check=cancel_check,
            )
        if mode == "import-gguf":
            return import_gguf(
                source=str(body.get("model_path") or ""), package_id=package_id,
                alias=alias, root=root, port=int(body.get("port") or 18080),
                accept_risk=bool(body.get("accept_risk", False)),
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
        current = active_manager(configured)
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

    def discover(network_id: str) -> list[dict[str, Any]]:
        values = store.list_job_capacities(
            network_id=network_id, capability=CAPABILITY, max_age_hours=1,
        ).get("capacities", [])
        services = []
        for value in values:
            public = dict(value.get("metadata") or {}).get("llm_service")
            if isinstance(public, dict):
                services.append({"peer_id": value.get("peer_id"), "node_name": value.get("node_name"),
                                 "updated_at": value.get("updated_at"), **public})
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
                handle_stream_request=current.handle,
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

    app.state.llm_relay_once = relay_once
    app.state.llm_publish_once = publish_once

    @app.get("/api/local/llm/hardware")
    def local_llm_hardware() -> dict[str, Any]:
        from .hardware import detect_hardware, recommend
        report = detect_hardware(home)
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
        try:
            result = await asyncio.to_thread(configure_llm, dict(await request.json()))
            return activate_configuration(result)
        except (LifecycleError, AdapterError, ManifestError, OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/local/llm/setup/status")
    def local_llm_setup_status() -> dict[str, Any]:
        with setup_job_lock:
            return dict(read_setup_job())

    @app.post("/api/local/llm/setup/async")
    async def local_llm_setup_async(request: Request) -> dict[str, Any]:
        body = dict(await request.json())
        with setup_job_lock:
            current_job = read_setup_job()
            if current_job.get("state") in {"queued", "running", "cancelling"}:
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
            }
            write_setup_job(job)

        def report(stage: str, percent: int, message: str) -> None:
            with setup_job_lock:
                latest = read_setup_job()
                if latest.get("job_id") != job_id:
                    return
                latest.update({
                    "state": "running",
                    "stage": stage,
                    "progress": percent,
                    "message": message,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                write_setup_job(latest)

        def run_setup() -> None:
            previous_settings = read_provider_settings()
            previous_manifest = Path(str(previous_settings.get("manifest") or ""))
            previous_manifest_bytes = None
            if previous_manifest.is_file():
                try:
                    previous_manifest_bytes = previous_manifest.read_bytes()
                except OSError:
                    previous_manifest_bytes = None
            try:
                report("starting", 1, "Starting local model setup")
                result = configure_llm(
                    body, progress=report, cancel_check=cancel_event.is_set,
                )
                activation = activate_configuration(result)
                with setup_job_lock:
                    write_setup_job({
                        "job_id": job_id,
                        "state": "succeeded",
                        "stage": "completed",
                        "progress": 100,
                        "message": "Local model configured and self-tested; publishing remains off",
                        "configured": activation["configured"],
                        "publication_enabled": False,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
            except (LifecycleError, AdapterError, ManifestError, OSError, ValueError) as exc:
                cancelled = cancel_event.is_set() or "cancelled" in str(exc).lower()
                if previous_manifest_bytes is not None:
                    try:
                        previous_manifest.parent.mkdir(parents=True, exist_ok=True)
                        previous_manifest.write_bytes(previous_manifest_bytes)
                        start_runtime(previous_manifest)
                    except (LifecycleError, AdapterError, ManifestError, OSError, ValueError):
                        pass
                with setup_job_lock:
                    write_setup_job({
                        "job_id": job_id,
                        "state": "cancelled" if cancelled else "failed",
                        "stage": "cancelled" if cancelled else "failed",
                        "progress": 0,
                        "error_code": "setup_cancelled" if cancelled else "setup_failed",
                        "message": "Setup cancelled; the previous private configuration was restored."
                        if cancelled else (str(exc).strip() or "Local model setup failed"),
                        "retryable": True,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
            finally:
                body.clear()
                with setup_job_lock:
                    setup_cancel_events.pop(job_id, None)

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
                "message": "Cancelling setup safely; existing configuration will be preserved",
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
        # the lifespan loops; without surfacing them here a provider whose
        # registry publication is failing looks healthy while its discovery
        # record silently expires.
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
            final = {}
            for item in record.get("history") or []:
                final.update(item)
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

    async def execute_order(body: dict[str, Any], on_event: Any = None) -> dict[str, Any]:
        network_id = str(body.get("network_id") or "rynmesh-main")
        provider_peer_id = str(body.get("provider_peer_id") or "")
        service_id = str(body.get("service_id") or "")
        chat = validate_chat(body["chat"]) if "chat" in body else None
        prompt = json.dumps(chat, ensure_ascii=False) if chat else str(body.get("prompt") or "")
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
        if not selected or not selected.get("online"):
            raise HTTPException(status_code=409, detail="service is absent, stale, offline, or unhealthy")
        if chat and selected.get("chat_protocol") != "rynmesh.chat.v1":
            raise HTTPException(status_code=400, detail="provider requires an update for structured chat")
        if _record_is_stale(selected.get("updated_at")):
            # The online flag is frozen at publish time and discovery keeps
            # records for up to an hour; a healthy provider republishes every
            # 30s, so anything older than a few minutes is a dead provider.
            raise HTTPException(status_code=409, detail="service discovery record is stale; the provider has stopped refreshing")
        capacity = dict(selected.get("capacity") or {})
        # API/agent follow-ups arrive faster than registry publication. The provider's
        # semaphore is authoritative; a stale busy snapshot must not reject them.
        if not chat and capacity.get("available") is not None and int(capacity["available"]) < 1:
            raise HTTPException(status_code=409, detail="capacity_exhausted: Provider is busy")
        public_manifest = dict(selected["service"])
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
        request_fingerprint = _request_fingerprint({
            "task_id": task_id,
            "idempotency_key": idempotency_key,
            "provider_peer_id": provider_peer_id,
            "service_id": service_id,
            "prompt": prompt,
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
                final = dict((existing.get("history") or [{}])[-1])
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
                      **({"chat": chat} if chat else {}),
                      "max_amount": maximum, "reply_messaging_pub": peer_box.public_key_b64(messaging_key)},
                task_id=task_id, kind="llm_request", sender_peer_id=store.peer_id,
                recipient_peer_id=provider_peer_id, sender_signing_key=store.private_key_bytes,
                recipient_messaging_pub=recipient_pub,
                expires_at=_expires(max(60, manifest.timeout_seconds + 30)),
            )
            endpoint = resolve_endpoint(provider_peer_id)
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
            if requested_transport == "p2p":
                # An explicit strict route must never be weakened by deployment
                # overrides, including legacy HTTP/relay test settings.
                transport_mode = "p2p"
            elif force_relay:
                transport_mode = "relay"
            if chat and chat.get("stream") and transport_mode == "relay":
                raise TaskProtocolError("streaming requires direct HTTP or P2P transport")
            sequence = 0

            def receive_event(envelope: dict[str, Any]) -> None:
                nonlocal sequence
                outer, event = open_task(envelope, recipient_peer_id=store.peer_id,
                                         recipient_messaging_key=messaging_key, expected_kind="llm_stream")
                if outer.get("from_peer_id") != provider_peer_id or outer.get("task_id") != task_id or event.get("service_id") != service_id or event.get("sequence") != sequence:
                    raise TaskProtocolError("stream identity or sequence mismatch")
                if (consumer_orders.get(task_id) or {}).get("state") == "cancelled":
                    raise TaskProtocolError("task_cancelled")
                sequence += 1
                if on_event:
                    on_event(event["chunk"])
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
            if transport_mode == "p2p":
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
                    **({"on_event": receive_event} if chat and chat.get("stream") else {}),
                )
            elif endpoint and transport_mode in {"auto", "direct"}:
                try:
                    # Blocking I/O for the full inference duration — run it in
                    # a worker thread so the node's event loop stays live.
                    if chat and chat.get("stream"):
                        encrypted_response = await asyncio.to_thread(
                            peer_stream, endpoint + "/api/peer/llm/tasks/stream", signed.to_dict(),
                            timeout_s=manifest.timeout_seconds + 30, on_event=receive_event,
                        )
                    else:
                        encrypted_response = await asyncio.to_thread(
                            _peer_post_json, endpoint + "/api/peer/llm/tasks",
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
                if chat and chat.get("stream"):
                    raise TaskProtocolError("streaming direct path failed; buffered relay is not supported") from direct_error
                relay_url = os.environ.get("RYNMESH_LLM_RELAY_URL", "").strip()
                if transport_mode == "p2p":
                    raise TaskProtocolError("strict P2P path failed; relay fallback is disabled")
                if not relay_url:
                    raise TaskProtocolError("direct provider path failed and no dedicated LLM relay is configured") from direct_error
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
            error_code = _delivery_error_code(exc, transport=transport_mode)
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
        body = dict(await request.json())
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
        final = dict((record.get("history") or [{}])[-1])
        result: dict[str, Any] = {
            "task_id": task_id,
            "state": str(record.get("state") or "unknown"),
            **{key: value for key, value in final.items() if key != "at"},
        }
        with background_orders_lock:
            ephemeral = background_orders.get(task_id)
            if ephemeral and ephemeral.get("ephemeral"):
                background_orders.pop(task_id, None)
        if ephemeral and ephemeral.get("ephemeral"):
            return {key: value for key, value in _public_background(ephemeral).items()
                    if key != "ephemeral"}
        encrypted = record.get("encrypted_response")
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
            endpoint = resolve_endpoint(provider_peer_id)
            if endpoint:
                try:
                    _peer_post_json(endpoint + "/api/peer/llm/cancellations",
                                    signed_cancel.to_dict(), timeout_s=5)
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

    @app.post("/api/peer/llm/tasks/stream")
    async def peer_llm_stream(request: Request):
        from fastapi.responses import StreamingResponse

        current = active_manager()
        if current is None:
            raise HTTPException(status_code=503, detail="LLM service not configured")
        body = await request.json()
        # Authenticate before sending headers and before accepting a cancellation id.
        outer, _ = open_task(body, recipient_peer_id=store.peer_id,
                             recipient_messaging_key=messaging_key, expected_kind="llm_request")
        task_id = outer["task_id"]

        async def run(emit):
            return await asyncio.to_thread(current.handle, body, emit)

        async def generate():
            async for _, value in events(run, lambda: current.cancel(task_id)):
                yield "data: " + json.dumps(value, separators=(",", ":")) + "\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

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
    from .api import install_inference_api

    install_inference_api(app, home=home, store=store, active_manager=active_manager,
                          discover=discover, execute_order=execute_order, cancel_order=local_llm_cancel)
