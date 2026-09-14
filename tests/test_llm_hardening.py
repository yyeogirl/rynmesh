"""Regression tests for the post-merge hardening of the LLM service package.

Each test pins a defect found in review: a poisoned registry listing, plaintext
smuggled through allowed param keys, one service's publication wiping another's
capabilities, keyed-registry health probes, provider billing above the hold
after inference, duplicate retrieval of a purged result, and the P2P lost-ACK
deadlock.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import rynmesh.llm_package.p2p as llm_p2p
from rynmesh.crypto import sign_payload
from rynmesh.jobs import JobError, validate_llm_control_params, verify_work_order
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.p2p import receive_json, send_json
from rynmesh.llm_package.routes import ProviderService, _record_is_stale
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import TaskOrderStore, open_task, seal_task
from rynmesh.registry import _signed_payload_list
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def _expires(seconds: float = 300) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# ---- registry listing tolerance -----------------------------------------


def _work_order_dict(store: RynmeshStore, *, operation: str, params: dict) -> dict:
    payload = {
        "kind": "work_order", "work_order_id": "wo_" + operation.replace(".", "_"),
        "requester_peer_id": store.peer_id, "provider_peer_id": "peer-b",
        "capability": "rynmesh.llm.private.v1", "operation": operation,
        "params": params, "network_id": "net", "status": "open",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": _expires(3600),
        "idempotency_key": "idem-" + operation,
    }
    return sign_payload(payload, private_key_bytes=store.private_key_bytes).to_dict()


def test_one_bad_record_does_not_poison_the_work_order_listing(tmp_path):
    """A single hostile signed order used to abort every registry poll."""
    store = RynmeshStore(home=tmp_path / "a", network_dir=tmp_path / "net")
    good = _work_order_dict(
        store, operation="rynmesh.llm.private.infer.v1.settlement",
        params={"signed_settlement": {"payload": "..."}},
    )
    poison = _work_order_dict(
        store, operation="rynmesh.llm.private.infer.v1",
        params={"prompt": "smuggled plaintext"},
    )
    listed = _signed_payload_list([poison, good, {"not": "a record"}], verify_work_order)
    assert len(listed) == 1
    assert listed[0].payload["operation"].endswith(".settlement")


# ---- param value bounds --------------------------------------------------


def test_control_param_values_are_size_bounded():
    op = "rynmesh.llm.private.infer.v1.p2p_offer"
    validate_llm_control_params(op, {"session_id": "task_1", "ice_signal": {"candidates": []}})
    with pytest.raises(JobError, match="length limit"):
        validate_llm_control_params(op, {"session_id": "x" * 5000})
    with pytest.raises(JobError, match="size limit"):
        validate_llm_control_params(
            op, {"ice_signal": {"candidates": ["c" * 1000] * 40}},
        )


# ---- capability merge ----------------------------------------------------


def test_capacity_registration_merges_instead_of_clobbering(tmp_path):
    """The LLM 30s republish used to wipe every other advertised capability."""
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net")
    store.register_job_capacity(capabilities=["signal50.veo_motion.v1"], network_id="net",
                                metadata={"veo": {"kind": "video"}})
    second = store.register_job_capacity(capabilities=["rynmesh.llm.private.v1"], network_id="net",
                                         metadata={"llm_service": {"online": True}})
    caps = set(second["record"]["capabilities"])
    assert {"signal50.veo_motion.v1", "rynmesh.llm.private.v1"} <= caps
    assert "veo" in second["record"]["metadata"]
    assert "llm_service" in second["record"]["metadata"]


# ---- keyed registry health ----------------------------------------------


def test_registry_health_stays_probeable_with_network_key(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "test-mesh-key")
    from rynmesh.registry import FilePeerRegistry
    from rynmesh.registry_http import create_app
    from rynmesh.transport import network_key_header

    with TestClient(create_app(registry=FilePeerRegistry(tmp_path / "reg"))) as client:
        plain = client.get("/health")
        assert plain.status_code == 200
        assert "kind" not in plain.json()  # liveness without fingerprinting
        authed = client.get("/health", headers=network_key_header())
        assert authed.json().get("kind") == "rynmesh-registry"
        assert client.get("/api/v1/peers/list").status_code == 404


# ---- provider billing ----------------------------------------------------


class _DenseTokenizerAdapter:
    """Reports far more actual tokens than the consumer's chars/4 estimate."""

    def health(self): return {"ok": True, "model": "dense"}
    def models(self): return [{"id": "dense"}]
    def capabilities(self): return {"chat_completions": True}
    def metrics(self): return {"requests": 0}
    def shutdown(self): pass
    def cancel(self, task_id): return True

    def infer(self, *, prompt, max_tokens, task_id, timeout_s):
        return {"text": "ok", "model": "dense", "input_tokens": 50_000,
                "output_tokens": max_tokens, "duration_ms": 5}


def _provider(tmp_path, adapter):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "p-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "c-msg")
    manifest = LLMPackageManifest(
        package_id="svc", mode="openai_compatible", public_model_alias="alias",
        base_url="http://127.0.0.1:1",
    )
    service = ProviderService(
        manifest=manifest, adapter=adapter, store=provider,
        task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"),
        messaging_key=provider_msg, access_check=lambda *_: {},  # Admission is isolated from these protocol tests.
    )
    return service, provider, consumer, provider_msg, consumer_msg


def _request(consumer, provider, provider_msg, consumer_msg, *, task_id="task_x",
             max_amount=0.5):
    return seal_task(
        body={"task_id": task_id, "service_id": "svc", "prompt": "hello",
              "max_tokens": 8, "max_amount": max_amount,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id=task_id, kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg),
        expires_at=_expires(),
    ).to_dict()


def test_actual_usage_above_the_hold_is_clamped_not_failed(tmp_path):
    """Dense tokenizers (CJK) deterministically failed AFTER burning inference."""
    service, provider, consumer, p_msg, c_msg = _provider(tmp_path, _DenseTokenizerAdapter())
    sealed = service.handle(_request(consumer, provider, p_msg, c_msg, max_amount=0.01))
    _, result = open_task(sealed, recipient_peer_id=consumer.peer_id,
                          recipient_messaging_key=c_msg, expected_kind="llm_response")
    assert result["state"] == "succeeded"
    assert result["amount"] == pytest.approx(0.01)  # billed at the ceiling, not above


def test_missing_hold_is_rejected_before_inference(tmp_path):
    class _CountingAdapter(_DenseTokenizerAdapter):
        calls = 0

        def infer(self, **kwargs):
            type(self).calls += 1
            return super().infer(**kwargs)

    adapter = _CountingAdapter()
    service, provider, consumer, p_msg, c_msg = _provider(tmp_path, adapter)
    from rynmesh.llm_package.task_protocol import TaskProtocolError

    with pytest.raises(TaskProtocolError, match="positive hold"):
        service.handle(_request(consumer, provider, p_msg, c_msg, max_amount=0))
    assert adapter.calls == 0


def test_duplicate_of_a_purged_result_answers_immediately(tmp_path):
    """Used to busy-wait 20Hz for timeout+30s, then claim 'still in progress'."""
    class _SmallAdapter(_DenseTokenizerAdapter):
        def infer(self, *, prompt, max_tokens, task_id, timeout_s):
            return {"text": "ok", "model": "dense", "input_tokens": 5,
                    "output_tokens": 2, "duration_ms": 5}

    service, provider, consumer, p_msg, c_msg = _provider(tmp_path, _SmallAdapter())
    request = _request(consumer, provider, p_msg, c_msg, task_id="task_purge")
    service.handle(request)
    service.task_store.purge_encrypted_response("task_purge")
    started = time.monotonic()
    sealed = service.handle(request)
    assert time.monotonic() - started < 5
    _, result = open_task(sealed, recipient_peer_id=consumer.peer_id,
                          recipient_messaging_key=c_msg, expected_kind="llm_response")
    assert result["state"] == "failed"
    assert result["error_code"] == "result_expired"


# ---- discovery freshness -------------------------------------------------


def test_record_staleness_gate():
    from datetime import datetime, timedelta, timezone

    fresh = datetime.now(timezone.utc).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert _record_is_stale(fresh) is False
    assert _record_is_stale(stale) is True
    assert _record_is_stale("") is False  # legacy records stay orderable
    assert _record_is_stale("not-a-date") is False


# ---- P2P lost-ACK recovery ----------------------------------------------


class _ScriptedConnection:
    def __init__(self, packets):
        self.packets = list(packets)
        self.sent = []

    async def recv(self):
        if not self.packets:
            await asyncio.sleep(10)
        return self.packets.pop(0)

    async def send(self, packet):
        self.sent.append(packet)


def test_lost_ack_burst_no_longer_deadlocks_the_consumer():
    """Provider's ACKs all dropped; its response DATA must satisfy the send."""
    response_payload = json.dumps({"answer": 42}, separators=(",", ":")).encode()
    _response_id, response_frames = llm_p2p._encode_frames(response_payload)
    connection = _ScriptedConnection(response_frames)

    async def scenario():
        pending: list[bytes] = []
        sent = await send_json(connection, {"q": 1}, timeout_s=3, pending_out=pending)
        assert sent > 0 and pending, "response DATA should implicitly ack the request"
        value, _ = await receive_json(connection, timeout_s=3, initial_packets=pending)
        return value

    assert asyncio.run(scenario()) == {"answer": 42}


def test_provider_reacks_retransmitted_request_frames():
    request_payload = json.dumps({"q": 1}, separators=(",", ":")).encode()
    request_id, request_frames = llm_p2p._encode_frames(request_payload)

    async def scenario():
        # Peer retransmits the request (lost ACK), then finally ACKs our response.
        connection = _ScriptedConnection(list(request_frames))

        async def delayed_ack():
            await asyncio.sleep(0.2)
            _, our_frames = llm_p2p._encode_frames(b"{}")
            # Find our response id from what we sent and ack it.
            for packet in connection.sent:
                kind, mid, _s, _t, digest, _b = llm_p2p._decode_header(packet)
                if kind == llm_p2p._DATA:
                    connection.packets.append(
                        llm_p2p._HEADER.pack(llm_p2p._MAGIC, llm_p2p._ACK, mid, 0, 0, digest)
                    )
                    return

        task = asyncio.ensure_future(delayed_ack())
        await send_json(connection, {"a": 2}, timeout_s=3, reack_message_id=request_id)
        await task
        reacks = [p for p in connection.sent
                  if llm_p2p._decode_header(p)[0] == llm_p2p._ACK
                  and llm_p2p._decode_header(p)[1] == request_id]
        assert reacks, "retransmitted request frames must be re-acked"

    asyncio.run(scenario())
