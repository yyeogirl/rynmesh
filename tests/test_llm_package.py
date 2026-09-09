from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import rynmesh.llm_package.lifecycle as llm_lifecycle
import rynmesh.llm_package.p2p as llm_p2p
import rynmesh.llm_package.routes as llm_routes
from rynmesh.crypto import SignatureError, sign_payload
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter, validate_local_url
from rynmesh.llm_package.lifecycle import LifecycleError, connect_local_api, validate_gguf
from rynmesh.llm_package.manifest import (
    LLMPackageManifest,
    ManifestError,
    Pricing,
    fingerprint_file,
)
from rynmesh.llm_package.p2p import (
    IceSignal,
    P2PError,
    apply_remote_signal,
    gather_signal,
    new_connection,
    receive_json,
    selected_pair,
    send_json,
    validate_distinct_public_egress,
)
from rynmesh.llm_package.routes import (
    ProviderService,
    _delivery_error_code,
    _open_provider_response,
    _recover_consumer_orders,
    install_llm_routes,
)
from rynmesh.llm_package.task_balance import TaskBalanceError, TaskBalanceLedger
from rynmesh.llm_package.task_protocol import (
    TaskOrderStore,
    TaskProtocolError,
    open_task,
    seal_task,
)
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


class _OpenAIHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):
        if self.path == "/v1/models":
            self._send({"object": "list", "data": [{"id": "test-real-api"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        type(self).calls += 1
        if body["stream"] is True:
            self._send({"choices": [{"delta": {"content": "stream supported"}}]})
            return
        self._send({
            "choices": [{"message": {"content": "test adapter completion"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        })

    def log_message(self, *_args):
        pass

    def _send(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def openai_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_openai_compatible_health_and_real_request(openai_server):
    adapter = OpenAICompatibleAdapter(base_url=openai_server, model="test-real-api")
    assert adapter.health()["ok"] is True
    result = adapter.infer(prompt="private prompt", max_tokens=8, task_id="t1", timeout_s=5)
    assert result["text"] == "test adapter completion"
    assert result["input_tokens"] == 4 and result["output_tokens"] == 3
    assert adapter.metrics()["requests"] == 1
    assert adapter.capabilities()["streaming"] is False


def test_openai_adapter_blocks_non_loopback_by_default():
    with pytest.raises(AdapterError, match="non-loopback"):
        validate_local_url("http://192.0.2.10:8080")
    assert validate_local_url("http://192.0.2.10:8080", allow_non_loopback=True)


def test_lifecycle_rejects_package_path_traversal_before_writing(tmp_path, openai_server):
    with pytest.raises(LifecycleError, match="lowercase slug"):
        connect_local_api(
            base_url=openai_server, package_id="../../outside", alias="safe-alias",
            root=tmp_path / "llm",
        )
    assert not (tmp_path / "outside").exists()

    with pytest.raises(LifecycleError, match="environment-variable name"):
        connect_local_api(
            base_url=openai_server, package_id="safe-package", alias="safe-alias",
            api_key_env="secret value pasted here", root=tmp_path / "llm",
        )


def test_managed_runtime_and_model_are_immutably_pinned():
    assert "@sha256:" in llm_lifecycle.DEFAULT_IMAGE
    assert "/resolve/main/" not in llm_lifecycle.DEFAULT_MODEL_URL
    assert llm_lifecycle.DEFAULT_MODEL_REVISION in llm_lifecycle.DEFAULT_MODEL_URL
    assert len(llm_lifecycle.DEFAULT_MODEL_SHA256) == 64
    manifest = LLMPackageManifest(
        package_id="unsafe", mode="managed", public_model_alias="unsafe",
        install_source={"runtime_image": "example.invalid/runtime:latest"},
    )
    with pytest.raises(LifecycleError, match="pinned by SHA-256"):
        llm_lifecycle._pinned_runtime_image(manifest)
    manifest.install_source["runtime_image"] = "ghcr.io/ggml-org/llama.cpp:server"
    assert llm_lifecycle._pinned_runtime_image(manifest) == llm_lifecycle.DEFAULT_IMAGE


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.001])
def test_manifest_rejects_unsafe_public_pricing(value):
    manifest = LLMPackageManifest(
        package_id="priced", mode="openai_compatible", public_model_alias="priced",
        base_url="http://127.0.0.1:8080", pricing=Pricing(input_per_1k=value),
    )
    with pytest.raises(ManifestError, match="finite and non-negative"):
        manifest.validate()


def test_managed_container_drops_privileges(monkeypatch, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF" + b"0" * 64)
    manifest = LLMPackageManifest(
        package_id="safe", mode="managed", public_model_alias="safe",
        runtime="docker_llama_cpp", model_path=str(model),
        checksum=fingerprint_file(model), base_url="http://127.0.0.1:18080",
        install_source={"runtime_image": llm_lifecycle.DEFAULT_IMAGE},
    )
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(llm_lifecycle, "_docker", lambda: "docker")
    monkeypatch.setattr(llm_lifecycle.subprocess, "run", fake_run)
    llm_lifecycle._run_container(manifest)
    run_command = commands[-1]
    assert run_command[0:2] == ["docker", "run"]
    assert run_command[run_command.index("--cap-drop") + 1] == "ALL"
    assert run_command[run_command.index("--security-opt") + 1] == "no-new-privileges"
    assert llm_lifecycle.DEFAULT_IMAGE in run_command


def test_local_setup_publish_pause_flow_is_explicit_and_persistent(tmp_path, openai_server):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )

    with TestClient(app) as client:
        setup = client.post("/api/local/llm/setup", json={
            "mode": "openai-compatible", "package_id": "configured-api",
            "alias": "public-safe-alias", "base_url": openai_server,
            "model": "test-real-api",
        })
        assert setup.status_code == 200
        setup_body = setup.json()
        assert setup_body["configured"] is True
        assert setup_body["publication_enabled"] is False
        assert "output_preview" not in setup_body["setup"]["self_test"]

        offline = client.get("/api/local/llm/service/status").json()
        assert offline["configured"] is True
        assert offline["online"] is False
        assert offline["accepting_orders"] is False

        published = client.post("/api/local/llm/services/publish", json={
            "network_id": "provider-test", "benchmark": False,
        })
        assert published.status_code == 200
        assert published.json()["record"]["metadata"]["llm_service"]["online"] is True

        online = client.get("/api/local/llm/service/status").json()
        assert online["online"] is True
        assert online["publication_enabled"] is True
        assert online["network_id"] == "provider-test"

        paused = client.post("/api/local/llm/services/pause").json()
        assert paused["online"] is False
        assert paused["accepting_orders"] is False
        assert paused["publication_enabled"] is False

        started = time.monotonic()
        accepted = client.post("/api/local/llm/orders/async", json={
            "provider_peer_id": "missing-provider", "service_id": "missing-service",
            "prompt": "private prompt that must not be stored", "max_tokens": 8,
        })
        assert accepted.status_code == 200
        assert time.monotonic() - started < 2
        task_id = accepted.json()["task_id"]
        deadline = time.monotonic() + 3
        status = {}
        while time.monotonic() < deadline:
            status = client.get(f"/api/local/llm/orders/{task_id}").json()
            if status.get("state") in {"failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert status["state"] == "failed"
        assert "private prompt" not in json.dumps(status)

        invalid_tokens = client.post("/api/local/llm/orders/async", json={
            "provider_peer_id": "missing-provider", "service_id": "missing-service",
            "prompt": "private prompt", "max_tokens": "not-a-number",
        })
        assert invalid_tokens.status_code == 400
        assert invalid_tokens.json()["detail"] == "max_tokens must be a positive integer"

        privacy = client.put("/api/local/llm/privacy", json={
            "result_retention_seconds": 0,
        })
        assert privacy.status_code == 200
        assert privacy.json()["result_retention_seconds"] == 0
        assert privacy.json()["plaintext_persisted"] is False

        history_store = TaskOrderStore(home / "llm" / "consumer-orders")
        history_store.claim(task_id="history_cleanup", bindings={"request": "test"})
        history_store.transition(task_id="history_cleanup", state="accepted")
        history_store.transition(task_id="history_cleanup", state="running")
        history_store.transition(
            task_id="history_cleanup", state="succeeded",
            encrypted_response={"ciphertext": "encrypted-only"},
        )
        privacy = client.put("/api/local/llm/privacy", json={
            "result_retention_seconds": 0,
        })
        assert privacy.status_code == 200
        assert "encrypted_response" not in history_store.get("history_cleanup")
        cleared = client.delete("/api/local/llm/orders").json()
        assert cleared["removed"] >= 1
        assert history_store.get("history_cleanup") is None

    settings = json.loads((home / "llm" / "provider-settings.json").read_text(encoding="utf-8"))
    assert Path(settings["manifest"]).parts[-2:] == ("configured-api", "manifest.json")
    assert settings["publication_enabled"] is False
    assert settings["network_id"] == "provider-test"


def test_async_setup_reports_progress_and_exposes_safe_lifecycle_actions(tmp_path, openai_server):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )

    with TestClient(app) as client:
        queued = client.post("/api/local/llm/setup/async", json={
            "mode": "openai-compatible",
            "package_id": "async-local-api",
            "alias": "async-safe-alias",
            "base_url": openai_server,
            "model": "test-real-api",
        })
        assert queued.status_code == 200
        assert queued.json()["state"] == "queued"
        deadline = time.monotonic() + 5
        setup_status = {}
        while time.monotonic() < deadline:
            setup_status = client.get("/api/local/llm/setup/status").json()
            if setup_status.get("state") in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert setup_status["state"] == "succeeded"
        assert setup_status["progress"] == 100
        assert "output_preview" not in json.dumps(setup_status)

        provider = client.get("/api/local/llm/service/status").json()
        assert provider["configured"] is True
        assert provider["lifecycle"]["runtime"]["managed"] is False

        tested = client.post("/api/local/llm/service/actions/self-test", json={})
        assert tested.status_code == 200
        assert tested.json()["result"]["self_test"]["ok"] is True
        assert "output_preview" not in tested.text

        stopped = client.post("/api/local/llm/service/actions/stop", json={})
        assert stopped.status_code == 200
        assert stopped.json()["publication_enabled"] is False


def test_async_setup_can_be_cancelled_without_replacing_existing_configuration(
    tmp_path, monkeypatch,
):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    started = threading.Event()

    def slow_connect(**kwargs):
        progress = kwargs["progress"]
        cancel_check = kwargs["cancel_check"]
        progress("connect", 20, "Checking the local model API")
        started.set()
        while not cancel_check():
            time.sleep(0.01)
        raise LifecycleError("setup cancelled")

    monkeypatch.setattr(llm_routes, "connect_local_api", slow_connect)
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )

    with TestClient(app) as client:
        queued = client.post("/api/local/llm/setup/async", json={
            "mode": "openai-compatible", "package_id": "cancelled-api",
            "alias": "cancelled-alias", "base_url": "http://127.0.0.1:8080",
        }).json()
        assert started.wait(timeout=2)
        cancelling = client.post(f"/api/local/llm/setup/{queued['job_id']}/cancel").json()
        assert cancelling["state"] == "cancelling"
        deadline = time.monotonic() + 3
        final = {}
        while time.monotonic() < deadline:
            final = client.get("/api/local/llm/setup/status").json()
            if final.get("state") == "cancelled":
                break
            time.sleep(0.02)
        assert final["state"] == "cancelled"
        assert final["retryable"] is True
        assert not (home / "llm" / "provider-settings.json").exists()


def test_interrupted_setup_status_is_recovered_as_retryable_failure(tmp_path):
    home = tmp_path / "node"
    job_path = home / "llm" / "setup-job.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "job_id": "setup_interrupted", "state": "running",
        "stage": "download_model", "progress": 42,
    }), encoding="utf-8")
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )

    with TestClient(app) as client:
        status = client.get("/api/local/llm/setup/status").json()
    assert status["state"] == "failed"
    assert status["error_code"] == "setup_interrupted"
    assert status["retryable"] is True


def test_manifest_public_view_has_no_paths_urls_or_key_names(tmp_path):
    manifest = LLMPackageManifest(
        package_id="private-model", mode="import_gguf", public_model_alias="private-alias",
        base_url="http://127.0.0.1:8080", api_key_env="VERY_SECRET_KEY",
        model_path=str(tmp_path / "commercial-secret.gguf"), runtime_command=["secret-bin"],
        model_fingerprint="sha256:" + "a" * 64,
    )
    public = json.dumps(manifest.public_dict())
    assert "commercial-secret" not in public
    assert "VERY_SECRET_KEY" not in public
    assert "127.0.0.1" not in public
    assert "secret-bin" not in public
    assert "private-alias" in public


def test_gguf_import_is_read_only_and_fingerprinted(tmp_path):
    model = tmp_path / "owned.gguf"
    original = b"GGUF" + b"\x00" * 64
    model.write_bytes(original)
    details = validate_gguf(model, allow_risk=True)
    assert details["format"] == "GGUF"
    assert details["fingerprint"] == fingerprint_file(model)
    assert model.read_bytes() == original
    with pytest.raises(LifecycleError, match="expected GGUF"):
        bad = tmp_path / "bad.bin"
        bad.write_bytes(b"nope" * 10)
        validate_gguf(bad, allow_risk=True)


def test_task_balance_hold_settle_release_and_dedupe(tmp_path):
    ledger = TaskBalanceLedger(tmp_path / "task-balance.json", initial_dev_balance=10)
    first = ledger.hold(task_id="one", amount=2, service_id="svc", provider_peer_id="p")
    assert ledger.hold(task_id="one", amount=2, service_id="svc", provider_peer_id="p") == first
    settled = ledger.settle(task_id="one", amount=1.25, input_tokens=3, output_tokens=4,
                            duration_ms=5, service_id="svc", provider_peer_id="p")
    assert settled["state"] == "settled"
    assert ledger.settle(task_id="one", amount=1.25, input_tokens=3, output_tokens=4,
                         duration_ms=5, service_id="svc", provider_peer_id="p")["settled_amount"] == 1.25
    ledger.hold(task_id="two", amount=3, service_id="svc", provider_peer_id="p")
    released = ledger.release(task_id="two", reason="cancelled")
    assert released["state"] == "released"
    reheld = ledger.hold(task_id="two", amount=3, service_id="svc", provider_peer_id="p")
    assert reheld["state"] == "held"
    ledger.release(task_id="two", reason="retry_failed")
    with pytest.raises(TaskBalanceError):
        ledger.settle(task_id="two", amount=1, input_tokens=1, output_tokens=1,
                      duration_ms=1, service_id="svc", provider_peer_id="p")
    summary = ledger.summary()
    assert summary["development_only"] is True
    assert summary["available"] == 8.75 and summary["held"] == 0
    assert all("prompt" not in json.dumps(event) for event in ledger.events())


def test_task_balance_rejects_task_id_reuse_for_different_request(tmp_path):
    ledger = TaskBalanceLedger(tmp_path / "task-balance.json", initial_dev_balance=10)
    ledger.hold(
        task_id="same", amount=2, service_id="svc", provider_peer_id="provider-a",
        idempotency_key="key", request_fingerprint="fingerprint-a",
    )
    with pytest.raises(TaskBalanceError, match="idempotency conflict"):
        ledger.hold(
            task_id="same", amount=2, service_id="svc", provider_peer_id="provider-b",
            idempotency_key="key", request_fingerprint="fingerprint-b",
        )


def test_task_store_rejects_state_rollback_and_binding_changes(tmp_path):
    orders = TaskOrderStore(tmp_path / "orders")
    bindings = {
        "consumer_peer_id": "consumer", "service_id": "svc",
        "idempotency_key": "key", "request_fingerprint": "fingerprint",
    }
    _, claimed = orders.claim(task_id="bound", bindings=bindings)
    assert claimed is True
    _, claimed_again = orders.claim(task_id="bound", bindings=bindings)
    assert claimed_again is False
    orders.transition(task_id="bound", state="accepted")
    orders.transition(task_id="bound", state="running")
    with pytest.raises(Exception, match="invalid task transition"):
        orders.transition(task_id="bound", state="accepted")
    with pytest.raises(Exception, match="idempotency conflict"):
        orders.claim(task_id="bound", bindings={**bindings, "service_id": "other"})


def test_task_envelope_is_ciphertext_and_authenticated(tmp_path):
    sender = RynmeshStore(home=tmp_path / "s", network_dir=tmp_path / "net")
    recipient = RynmeshStore(home=tmp_path / "r", network_dir=tmp_path / "net")
    recipient_msg = peer_box.load_or_create_messaging_key(tmp_path / "r-msg")
    signed = seal_task(
        body={"task_id": "task_one", "prompt": "NEVER VISIBLE IN RELAY"},
        task_id="task_one", kind="llm_request", sender_peer_id=sender.peer_id,
        recipient_peer_id=recipient.peer_id, sender_signing_key=sender.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(recipient_msg), expires_at=_expires(),
    )
    wire = json.dumps(signed.to_dict())
    assert "NEVER VISIBLE IN RELAY" not in wire
    _, body = open_task(signed, recipient_peer_id=recipient.peer_id,
                        recipient_messaging_key=recipient_msg, expected_kind="llm_request")
    assert body["prompt"] == "NEVER VISIBLE IN RELAY"
    tampered = signed.to_dict()
    tampered["payload"]["ciphertext"] += "A"
    with pytest.raises(SignatureError):
        open_task(tampered, recipient_peer_id=recipient.peer_id,
                  recipient_messaging_key=recipient_msg, expected_kind="llm_request")


def test_ice_udp_direct_transport_exchanges_chunked_json_without_relay(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_PUBLIC", raising=False)

    async def scenario():
        consumer = new_connection(controlling=True)
        provider = new_connection(controlling=False)
        try:
            consumer_signal, provider_signal = await asyncio.gather(
                gather_signal(consumer), gather_signal(provider),
            )
            await asyncio.gather(
                apply_remote_signal(consumer, provider_signal),
                apply_remote_signal(provider, consumer_signal),
            )
            await asyncio.gather(consumer.connect(), provider.connect())
            consumer_evidence = selected_pair(consumer)
            provider_evidence = selected_pair(provider)
            assert consumer_evidence["transport"] == "ice_udp_direct"
            assert provider_evidence["relay_used"] is False
            assert consumer_evidence["remote"]["type"] != "relay"

            request = {"ciphertext": "x" * 5000}
            response = {"ciphertext": "y" * 7000}

            async def provider_side():
                received, request_bytes = await receive_json(provider, timeout_s=5)
                assert received == request and request_bytes > 5000
                await send_json(provider, response, timeout_s=5)

            provider_task = asyncio.create_task(provider_side())
            await send_json(consumer, request, timeout_s=5)
            received, response_bytes = await receive_json(consumer, timeout_s=5)
            await provider_task
            assert received == response and response_bytes > 7000
        finally:
            await consumer.close()
            await provider.close()

    asyncio.run(scenario())


def test_public_nat_mode_refuses_to_fall_back_to_host_candidate(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_PUBLIC", "1")

    async def scenario():
        connection = new_connection(controlling=True)
        try:
            with pytest.raises(P2PError, match="server-reflexive STUN candidate"):
                await gather_signal(connection)
        finally:
            await connection.close()

    asyncio.run(scenario())


def test_public_nat_mode_accepts_peer_reflexive_nominated_candidate(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_PUBLIC", "1")
    local = SimpleNamespace(type="srflx", transport="udp", host="117.50.189.73", port=3000)
    remote = SimpleNamespace(type="prflx", transport="udp", host="14.154.222.55", port=52066)
    pair = SimpleNamespace(local_candidate=local, remote_candidate=remote)
    connection = SimpleNamespace(_nominated={1: pair})

    evidence = selected_pair(connection)

    assert evidence["transport"] == "ice_udp_direct"
    assert evidence["relay_used"] is False
    assert evidence["peer_public_mapping_nominated"] is True
    assert evidence["path_kind"] == "peer_reflexive"


def test_strict_p2p_connection_never_passes_turn_configuration(monkeypatch):
    monkeypatch.delenv("RYNMESH_P2P_BIND_PORT", raising=False)
    monkeypatch.setenv("RYNMESH_P2P_STUN", "stun.example.test:3478")
    monkeypatch.setenv("RYNMESH_P2P_TURN", "turn.example.test:3478")
    monkeypatch.setenv("RYNMESH_P2P_TURN_USERNAME", "must-be-ignored")
    monkeypatch.setenv("RYNMESH_P2P_TURN_PASSWORD", "must-be-ignored")
    captured = {}
    sentinel = object()

    def fake_connection(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(llm_p2p.aioice, "Connection", fake_connection)

    assert llm_p2p.new_connection(controlling=True) is sentinel
    assert captured == {
        "ice_controlling": True,
        "components": 1,
        "stun_server": ("stun.example.test", 3478),
        "use_ipv4": True,
        "use_ipv6": True,
    }
    assert all("turn" not in key.lower() for key in captured)


def test_provider_can_bind_ice_to_one_firewall_port(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.setenv("RYNMESH_P2P_BIND_PORT", "3000")
    connection = new_connection(controlling=False)
    assert connection._rynmesh_bind_port == 3000


def test_invalid_fixed_ice_port_fails_closed(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_BIND_PORT", "70000")
    with pytest.raises(P2PError, match="between 1 and 65535"):
        new_connection(controlling=False)


def test_ice_signal_rejects_turn_relay_candidate_before_connecting():
    with pytest.raises(P2PError, match="TURN/relay"):
        IceSignal.from_dict({
            "username": "remote",
            "password": "remote-password",
            "candidates": [
                "relay 1 udp 1677734910 203.0.113.10 50000 typ relay "
                "raddr 0.0.0.0 rport 0",
            ],
        })

    with pytest.raises(P2PError, match="non-UDP"):
        IceSignal.from_dict({
            "username": "remote",
            "password": "remote-password",
            "candidates": [
                "tcp 1 tcp 1518280447 192.0.2.1 9 typ host tcptype active",
            ],
        })

    async def scenario():
        connection = new_connection(controlling=True)
        signal = IceSignal(
            username="remote",
            password="remote-password",
            candidates=(
                "relay 1 udp 1677734910 203.0.113.10 50000 typ relay "
                "raddr 0.0.0.0 rport 0",
            ),
        )
        try:
            with pytest.raises(P2PError, match="TURN/relay"):
                await apply_remote_signal(connection, signal)
        finally:
            await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("candidate", "error"),
    [
        (
            "host-name 1 udp 2130706431 example.invalid 50000 typ host",
            "host must be an IP literal",
        ),
        (
            "related-name 1 udp 1694498815 203.0.113.10 50000 typ srflx "
            "raddr internal.invalid rport 50000",
            "related host must be an IP literal",
        ),
        (
            "component 2 udp 2130706431 192.0.2.10 50000 typ host",
            "component must be 1",
        ),
        (
            "zero-port 1 udp 2130706431 192.0.2.10 0 typ host",
            "port is invalid",
        ),
        (
            "unspecified 1 udp 2130706431 0.0.0.0 50000 typ host",
            "not a usable unicast address",
        ),
        (
            "multicast 1 udp 2130706431 239.1.2.3 50000 typ host",
            "not a usable unicast address",
        ),
        (
            "broadcast 1 udp 2130706431 255.255.255.255 50000 typ host",
            "not a usable unicast address",
        ),
    ],
)
def test_ice_signal_rejects_non_literal_or_non_unicast_destination(candidate, error):
    with pytest.raises(P2PError, match=error):
        IceSignal.from_dict({
            "username": "strict",
            "password": "strict-password",
            "candidates": [candidate],
        })


@pytest.mark.parametrize(
    "candidate",
    [
        "ipv4 1 udp 2130706431 192.0.2.10 50000 typ host",
        "ipv6 1 udp 2130706431 2001:db8::10 50000 typ host",
        "srflx 1 udp 1694498815 203.0.113.10 50000 typ srflx "
        "raddr 10.0.0.10 rport 50000",
    ],
)
def test_ice_signal_accepts_bounded_ip_literal_candidates(candidate):
    signal = IceSignal.from_dict({
        "username": "strict",
        "password": "strict-password",
        "candidates": [candidate],
    })
    assert signal.candidates == (candidate,)


def test_distinct_public_egress_acceptance_fails_fast_for_shared_mapping(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC", "1")
    local = IceSignal(
        username="local",
        password="local-password",
        candidates=(
            "local 1 udp 1694498815 98.158.108.218 50001 typ srflx raddr 10.0.0.2 rport 50001",
        ),
    )
    same_egress = IceSignal(
        username="remote",
        password="remote-password",
        candidates=(
            "remote 1 udp 1694498815 98.158.108.218 50002 typ srflx raddr 192.168.1.2 rport 50002",
        ),
    )
    other_egress = IceSignal(
        username="remote",
        password="remote-password",
        candidates=(
            "remote 1 udp 1694498815 203.0.113.8 50002 typ srflx raddr 192.168.1.2 rport 50002",
        ),
    )

    with pytest.raises(P2PError, match="distinct public egress"):
        validate_distinct_public_egress(local, same_egress)
    validate_distinct_public_egress(local, other_egress)
    assert _delivery_error_code(
        P2PError("strict P2P acceptance requires distinct public egress addresses"),
        transport="p2p",
    ) == "p2p_distinct_public_egress_required"


def test_restart_recovery_fails_interrupted_order_and_releases_hold(tmp_path):
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "balance.json")
    orders.transition(task_id="task_interrupted", state="created")
    orders.transition(task_id="task_interrupted", state="accepted")
    orders.transition(task_id="task_interrupted", state="running")
    balance.hold(
        task_id="task_interrupted",
        amount=0.25,
        service_id="private-service",
        provider_peer_id="provider-peer",
    )

    _recover_consumer_orders(orders, balance)
    _recover_consumer_orders(orders, balance)

    assert orders.get("task_interrupted")["state"] == "failed"
    assert balance.summary()["held"] == 0.0
    assert balance.summary()["available"] == 100.0
    releases = [event for event in balance.events() if event["kind"] == "release"]
    assert len(releases) == 1
    assert releases[0]["reason"] == "consumer_restart_recovery"


def test_restart_recovery_completes_received_result_settlement(tmp_path):
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "balance.json")
    orders.claim(task_id="task_received", bindings={
        "provider_peer_id": "provider-peer", "service_id": "private-service",
        "idempotency_key": "task_received", "request_fingerprint": "fingerprint",
    })
    orders.transition(task_id="task_received", state="accepted")
    orders.transition(task_id="task_received", state="running")
    orders.checkpoint(task_id="task_received", metadata={
        "settlement_pending": True, "provider_peer_id": "provider-peer",
        "service_id": "private-service", "network_id": "rynmesh-main",
        "amount": 0.1, "input_tokens": 4, "output_tokens": 2, "duration_ms": 7,
    })
    balance.hold(
        task_id="task_received", amount=0.25, service_id="private-service",
        provider_peer_id="provider-peer", idempotency_key="task_received",
        request_fingerprint="fingerprint",
    )

    _recover_consumer_orders(orders, balance)
    _recover_consumer_orders(orders, balance)

    assert orders.get("task_received")["state"] == "succeeded"
    assert balance.summary()["held"] == 0.0
    assert balance.summary()["available"] == 99.9
    settlements = [event for event in balance.events() if event["kind"] == "settle"]
    assert len(settlements) == 1


class _FakeAdapter:
    def __init__(self):
        self.calls = 0
        self.cancelled = []

    def health(self): return {"ok": True, "model": "fake-test-only"}
    def models(self): return [{"id": "fake-test-only"}]
    def capabilities(self): return {"chat_completions": True}
    def metrics(self): return {"requests": self.calls}
    def shutdown(self): pass
    def cancel(self, task_id): self.cancelled.append(task_id); return True
    def infer(self, *, prompt, max_tokens, task_id, timeout_s):
        self.calls += 1
        return {"text": "provider output", "model": "fake-test-only", "input_tokens": 5,
                "output_tokens": 2, "duration_ms": 7}


class _BlockingAdapter(_FakeAdapter):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def infer(self, *, prompt, max_tokens, task_id, timeout_s):
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=5)
        return {"text": "provider output", "model": "fake-test-only", "input_tokens": 5,
                "output_tokens": 2, "duration_ms": 7}


def test_provider_executes_and_settles_once_without_persisting_bodies(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _FakeAdapter()
    manifest = LLMPackageManifest(
        package_id="svc", mode="openai_compatible", public_model_alias="alias",
        base_url="http://127.0.0.1:1",
    )
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "provider-balance.json")
    service = ProviderService(manifest=manifest, adapter=adapter, store=provider,
                              task_store=orders, balance=balance, messaging_key=provider_msg)
    request = seal_task(
        body={"task_id": "task_same", "service_id": "svc", "prompt": "TOP SECRET PROMPT",
              "max_tokens": 8, "max_amount": 1, "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_same", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    first = service.handle(request)
    second = service.handle(request)
    assert first == second and adapter.calls == 1
    changed = seal_task(
        body={"task_id": "task_same", "service_id": "svc", "prompt": "CHANGED PROMPT",
              "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_same", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    with pytest.raises(TaskProtocolError, match="idempotency conflict"):
        service.handle(changed)
    _, result = open_task(first, recipient_peer_id=consumer.peer_id,
                          recipient_messaging_key=consumer_msg, expected_kind="llm_response")
    assert result["output"] == "provider output" and result["state"] == "succeeded"
    disk = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    assert "TOP SECRET PROMPT" not in disk
    assert "provider output" not in disk
    settlement = sign_payload({
        "kind": "llm_settlement", "task_id": "task_same", "from_peer_id": consumer.peer_id,
        "to_peer_id": provider.peer_id, "amount": result["amount"], "service_id": "svc",
        "settlement_id": "settle:task_same",
    }, private_key_bytes=consumer.private_key_bytes).to_dict()
    attacker = RynmeshStore(home=tmp_path / "attacker", network_dir=net)
    forged = sign_payload({
        "kind": "llm_settlement", "task_id": "task_same", "from_peer_id": attacker.peer_id,
        "to_peer_id": provider.peer_id, "amount": result["amount"], "service_id": "svc",
        "settlement_id": "settle:task_same",
    }, private_key_bytes=attacker.private_key_bytes).to_dict()
    with pytest.raises(Exception, match="not the task consumer"):
        service.settle_earning(forged)
    one = service.settle_earning(settlement)
    two = service.settle_earning(settlement)
    assert one["event_id"] == two["event_id"] == "earning:task_same"
    assert balance.summary()["earned"] == result["amount"]


def test_provider_bounds_retained_records_and_skips_paused_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("RYNMESH_LLM_MAX_PROVIDER_RECORDS_PER_PEER", "1")
    monkeypatch.setenv("RYNMESH_LLM_MAX_PROVIDER_RECORDS", "2")
    monkeypatch.setenv("RYNMESH_LLM_REQUESTS_PER_MINUTE", "10")
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    orders = TaskOrderStore(tmp_path / "orders")
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc", mode="openai_compatible", public_model_alias="alias",
            base_url="http://127.0.0.1:1",
        ),
        adapter=_FakeAdapter(), store=provider, task_store=orders,
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg,
    )

    def request(task_id: str, prompt: str, reply_key: str | None = None) -> dict:
        return seal_task(
            body={"task_id": task_id, "service_id": "svc", "prompt": prompt,
                  "max_tokens": 8, "max_amount": 1,
                  "reply_messaging_pub": reply_key or peer_box.public_key_b64(consumer_msg)},
            task_id=task_id, kind="llm_request", sender_peer_id=consumer.peer_id,
            recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
            recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
        ).to_dict()

    service.handle(request("first", "one"))
    with pytest.raises(TaskProtocolError, match="peer_task_record_limit"):
        service.handle(request("second", "two"))
    assert len(list((tmp_path / "orders").glob("*.json"))) == 1

    service.accepting_orders = False
    rejected = service.handle(request("paused", "three"))
    _, result = open_task(
        rejected, recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_msg, expected_kind="llm_response",
    )
    assert result["error_code"] == "service_paused"
    assert len(list((tmp_path / "orders").glob("*.json"))) == 1

    with pytest.raises(TaskProtocolError, match="messaging key is invalid"):
        service.handle(request("invalid-key", "four", "not-base64"))
    assert len(list((tmp_path / "orders").glob("*.json"))) == 1


def test_task_store_prunes_expired_terminal_records(tmp_path):
    store = TaskOrderStore(tmp_path / "orders")
    store.transition(task_id="old", state="created")
    store.transition(task_id="old", state="failed")
    path = tmp_path / "orders" / "old.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    path.write_text(json.dumps(record), encoding="utf-8")
    store.transition(task_id="new", state="created")
    store.transition(task_id="new", state="failed")
    removed = store.prune_terminal(
        older_than=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert removed == 1
    assert store.get("old") is None
    assert store.get("new") is not None


class _PacketConnection:
    def __init__(self, packets):
        self.packets = iter(packets)

    async def recv(self):
        return next(self.packets)


def _p2p_packet(message_id: bytes, *, sequence: int, total: int, body: bytes = b"x") -> bytes:
    return llm_p2p._HEADER.pack(
        llm_p2p._MAGIC, llm_p2p._DATA, message_id, sequence, total, b"d" * 32,
    ) + body


def test_p2p_receiver_rejects_oversized_chunk_declarations():
    packet = _p2p_packet(
        b"a" * 16, sequence=0, total=llm_p2p._MAX_CHUNKS + 1,
    )
    with pytest.raises(P2PError, match="declaration exceeds safe limits"):
        asyncio.run(receive_json(_PacketConnection([packet]), timeout_s=1))


def test_p2p_receiver_bounds_simultaneous_messages():
    packets = [
        _p2p_packet(index.to_bytes(16), sequence=0, total=2)
        for index in range(llm_p2p._MAX_IN_FLIGHT_MESSAGES + 1)
    ]
    with pytest.raises(P2PError, match="too many simultaneous"):
        asyncio.run(receive_json(_PacketConnection(packets), timeout_s=1))


def test_reliable_p2p_send_bounds_each_udp_burst_to_its_window():
    class Connection:
        def __init__(self) -> None:
            self.acks: list[bytes] = []
            self.sent_since_recv = 0
            self.peak_burst = 0

        async def send(self, packet: bytes) -> None:
            kind, message_id, sequence, total, digest, _body = llm_p2p._decode_header(
                packet
            )
            assert kind == llm_p2p._DATA
            self.sent_since_recv += 1
            self.peak_burst = max(self.peak_burst, self.sent_since_recv)
            self.acks.append(
                llm_p2p._HEADER.pack(
                    llm_p2p._MAGIC,
                    llm_p2p._ACK,
                    message_id,
                    sequence,
                    total,
                    digest,
                )
            )

        async def recv(self) -> bytes:
            self.sent_since_recv = 0
            return self.acks.pop(0)

    connection = Connection()
    payload = b"x" * (llm_p2p._CHUNK_BYTES * (llm_p2p._SEND_WINDOW + 2))
    sent = asyncio.run(llm_p2p.send_bytes(connection, payload, timeout_s=2))
    assert sent == len(payload)
    assert connection.peak_burst == llm_p2p._SEND_WINDOW == 8


def test_provider_concurrent_duplicate_executes_once(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _BlockingAdapter()
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc", mode="openai_compatible", public_model_alias="alias",
            base_url="http://127.0.0.1:1", timeout_seconds=2,
        ),
        adapter=adapter, store=provider, task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg,
    )
    request = seal_task(
        body={"task_id": "task_concurrent", "idempotency_key": "same-request",
              "service_id": "svc", "prompt": "private", "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_concurrent", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    responses = []
    threads = [threading.Thread(target=lambda: responses.append(service.handle(request))) for _ in range(2)]
    threads[0].start()
    assert adapter.started.wait(timeout=2)
    threads[1].start()
    adapter.release.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert adapter.calls == 1
    assert len(responses) == 2 and responses[0] == responses[1]
    assert service.task_store.get("task_concurrent")["state"] == "succeeded"


def test_signed_cancel_reaches_running_provider_and_rejects_other_identity(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    attacker = RynmeshStore(home=tmp_path / "attacker", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _BlockingAdapter()
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc", mode="openai_compatible", public_model_alias="alias",
            base_url="http://127.0.0.1:1", timeout_seconds=2,
        ),
        adapter=adapter, store=provider, task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg,
    )
    request = seal_task(
        body={"task_id": "task_cancel_running", "idempotency_key": "cancel-running",
              "service_id": "svc", "prompt": "private", "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_cancel_running", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    responses = []
    worker = threading.Thread(target=lambda: responses.append(service.handle(request)))
    worker.start()
    assert adapter.started.wait(timeout=2)

    def cancellation(sender: RynmeshStore) -> dict:
        return sign_payload({
            "kind": "llm_cancel", "task_id": "task_cancel_running",
            "from_peer_id": sender.peer_id, "to_peer_id": provider.peer_id,
            "service_id": "svc", "cancel_id": "cancel:task_cancel_running",
        }, private_key_bytes=sender.private_key_bytes).to_dict()

    with pytest.raises(Exception, match="not the task consumer"):
        service.cancel_signed(cancellation(attacker))
    assert service.cancel_signed(cancellation(consumer)) is True
    assert service.task_store.get("task_cancel_running")["state"] == "cancelled"
    adapter.release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    _, result = open_task(
        responses[0], recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_msg, expected_kind="llm_response",
    )
    assert result["state"] == "cancelled"
    assert result["error_code"] == "consumer_cancelled"
    assert adapter.cancelled == ["task_cancel_running"]


def test_consumer_rejects_response_signed_by_another_provider(tmp_path):
    net = tmp_path / "net"
    expected = RynmeshStore(home=tmp_path / "expected", network_dir=net)
    rogue = RynmeshStore(home=tmp_path / "rogue", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    response = seal_task(
        body={"task_id": "task_response", "service_id": "svc", "state": "succeeded",
              "input_tokens": 1, "output_tokens": 1, "duration_ms": 1, "amount": 0.001,
              "output": "rogue"},
        task_id="task_response", kind="llm_response", sender_peer_id=rogue.peer_id,
        recipient_peer_id=consumer.peer_id, sender_signing_key=rogue.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(consumer_msg), expires_at=_expires(),
    ).to_dict()
    with pytest.raises(Exception, match="not the selected provider"):
        _open_provider_response(
            response, recipient_peer_id=consumer.peer_id, messaging_key=consumer_msg,
            task_id="task_response", provider_peer_id=expected.peer_id, service_id="svc",
        )


def test_provider_explicitly_rejects_capacity_and_cancel_is_terminal(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _FakeAdapter()
    manifest = LLMPackageManifest(
        package_id="svc", mode="openai_compatible", public_model_alias="alias",
        base_url="http://127.0.0.1:1", max_concurrent=1,
    )
    orders = TaskOrderStore(tmp_path / "orders")
    service = ProviderService(
        manifest=manifest, adapter=adapter, store=provider, task_store=orders,
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg,
    )
    request = seal_task(
        body={"task_id": "busy_task", "service_id": "svc", "prompt": "body",
              "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="busy_task", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    assert service._slots.acquire(blocking=False)
    try:
        encrypted = service.handle(request)
    finally:
        service._slots.release()
    _, result = open_task(encrypted, recipient_peer_id=consumer.peer_id,
                          recipient_messaging_key=consumer_msg, expected_kind="llm_response")
    assert result["state"] == "rejected" and result["error_code"] == "capacity_exhausted"
    assert adapter.calls == 0
    orders.transition(task_id="cancel_me", state="created", metadata={"service_id": "svc"})
    assert service.cancel("cancel_me") is True
    assert orders.get("cancel_me")["state"] == "cancelled"
    assert adapter.cancelled == ["cancel_me"]
