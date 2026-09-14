"""Real consumer state/crypto/ledger, with only peer transport simulated."""
import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import rynmesh.llm_package.routes as consumer
from rynmesh.ask_ryn.context import AskContextService
from rynmesh.ask_ryn.runs import AskRunService
from rynmesh.ask_ryn.store import ConversationStore
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.task_protocol import open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


@pytest.mark.parametrize('health,expected', [
    ({'ok': False, 'error_code': 'model_not_ready'}, 'model_not_ready'),
    ({'ok': False, 'error_code': 'runtime_connection_failed'}, 'runtime_connection_failed'),
    ({'ok': False, 'error_code': 'PRIVATE_ERROR_CANARY'}, 'service_unhealthy'),
    ({'ok': False, 'error_code': {'bad': 'shape'}}, 'service_unhealthy'),
    ({'ok': True}, 'provider_unavailable'),
])
def test_unready_preflight_preserves_reason_without_reserving_or_delivering(tmp_path, monkeypatch, health, expected):
    store = RynmeshStore(home=tmp_path / 'node', network_dir=tmp_path / 'network')
    key = peer_box.load_or_create_messaging_key(store.home / 'messaging.x25519')
    public = {'online': False, 'ready': health['ok'], 'health': health,
              'service': {'package_id': 'model'}}
    monkeypatch.setattr(store, 'list_job_capacities', lambda **_: {'capacities': [{
        'peer_id': 'synthetic-provider', 'updated_at': datetime.now(timezone.utc).isoformat(),
        'metadata': {'llm_service': public}}]})
    app = FastAPI()
    commands = consumer.install_llm_routes(app, store=store, home=store.home, messaging_key=key,
        resolve_endpoint=lambda _: pytest.fail('Unready provider must not receive a task'),
        resolve_pubkey=lambda _: pytest.fail('Unready provider must not receive a task'))
    client = TestClient(app)
    before = client.get('/api/local/task-balance').json()
    task_id = 'task_' + 'd' * 32
    commands.submit({'task_id': task_id, 'provider_peer_id': 'synthetic-provider',
                     'service_id': 'model', 'prompt': 'Synthetic preflight request'})
    deadline = time.monotonic() + 5
    result = {}
    while time.monotonic() < deadline:
        result = commands.status(task_id)
        if result.get('state') == 'failed':
            break
        time.sleep(0.01)
    assert result.get('error_code') == expected, result
    assert client.get('/api/local/llm/orders').json()['orders'] == []
    assert client.get('/api/local/task-balance').json() == before


@pytest.mark.parametrize('relay_configured', [False, True])
def test_auto_connection_reports_attempted_route_without_disabling_fallback(tmp_path, monkeypatch, relay_configured):
    monkeypatch.setenv('RYNMESH_LLM_TRANSPORT', 'auto')
    monkeypatch.setenv('RYNMESH_LLM_FORCE_RELAY', '0')
    monkeypatch.setenv('RYNMESH_LLM_RELAY_URL', 'http://synthetic-relay.invalid' if relay_configured else '')
    store = RynmeshStore(home=tmp_path / 'node', network_dir=tmp_path / 'network')
    peer = RynmeshStore(home=tmp_path / 'peer', network_dir=tmp_path / 'network')
    key = peer_box.load_or_create_messaging_key(store.home / 'messaging.x25519')
    peer_key = peer_box.load_or_create_messaging_key(peer.home / 'messaging.x25519')
    manifest = LLMPackageManifest(package_id='model', mode='openai_compatible', public_model_alias='Fault test', base_url='http://127.0.0.1')
    public = {'online': True, 'service': manifest.public_dict(), 'node_messaging_pub': peer_box.public_key_b64(peer_key), 'capacity': {'available': 1}}
    monkeypatch.setattr(store, 'list_job_capacities', lambda **_: {'capacities': [{
        'peer_id': peer.peer_id, 'updated_at': datetime.now(timezone.utc).isoformat(), 'metadata': {'llm_service': public}}]})
    attempts = []
    def direct(*_, **__):
        attempts.append('direct')
        raise ConnectionRefusedError('Synthetic closed endpoint')
    def relay(*_, **__):
        attempts.append('relay')
        raise ConnectionRefusedError('Synthetic closed relay')
    monkeypatch.setattr(consumer, '_peer_post_json', direct)
    monkeypatch.setattr(consumer, '_upload_relay_ciphertext', relay)
    app = FastAPI()
    commands = consumer.install_llm_routes(app, store=store, home=store.home, messaging_key=key,
        resolve_endpoint=lambda _: 'http://synthetic-peer.invalid', resolve_pubkey=lambda _: peer_box.public_key_b64(peer_key))
    client = TestClient(app)
    before = client.get('/api/local/task-balance').json()
    task_id = 'task_' + 'e' * 32
    request = {'task_id': task_id, 'provider_peer_id': peer.peer_id, 'service_id': 'model', 'prompt': 'Synthetic connection test', 'transport': 'auto'}
    commands.submit(request)
    deadline = time.monotonic() + 5
    result = {}
    while time.monotonic() < deadline:
        result = commands.status(task_id)
        if result.get('state') == 'failed':
            break
        time.sleep(0.01)
    expected = 'encrypted_relay_failed' if relay_configured else 'direct_transport_failed'
    assert result.get('error_code') == expected, result
    # The durable order can finish just before its background receipt updates.
    # Resubmitting that identity in either window must never run it again.
    repeated = commands.submit(request)
    while repeated['state'] != 'failed' and time.monotonic() < deadline:
        time.sleep(0.01)
        repeated = commands.submit(request)
    assert repeated['state'] == 'failed'
    assert attempts == (['direct', 'relay'] if relay_configured else ['direct'])
    balance = client.get('/api/local/task-balance').json()
    assert balance['held'] == 0 and balance['available'] == before['available']
    assert [event['kind'] for event in balance['events'] if event.get('task_id') == task_id] == ['hold', 'release']


@pytest.mark.parametrize("retention", [0, 3600])
def test_existing_consumer_result_is_archived_before_transient_ack(tmp_path, monkeypatch, retention):
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "network")
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    provider_key = peer_box.load_or_create_messaging_key(provider.home / "messaging.x25519")
    manifest = LLMPackageManifest(package_id="model", mode="openai_compatible", public_model_alias="Synthetic transport test", base_url="http://127.0.0.1")
    public = {"online": True, "service": manifest.public_dict(), "node_messaging_pub": peer_box.public_key_b64(provider_key), "capacity": {"available": 1}}
    monkeypatch.setattr(store, "list_job_capacities", lambda **_: {"capacities": [{"peer_id": provider.peer_id, "updated_at": datetime.now(timezone.utc).isoformat(), "metadata": {"llm_service": public}}]})
    requests = []
    def peer_post(endpoint, path, signed, **_):
        if path.endswith("settlements"):
            return {"ok": True}
        _, body = open_task(signed, recipient_peer_id=provider.peer_id, recipient_messaging_key=provider_key, expected_kind="llm_request")
        requests.append(body)
        return seal_task(body={"task_id": body["task_id"], "service_id": "model", "state": "succeeded", "output": "Synthetic signed answer", "input_tokens": 10, "output_tokens": 5, "duration_ms": 20, "amount": 0.001}, task_id=body["task_id"], kind="llm_response", sender_peer_id=provider.peer_id, recipient_peer_id=store.peer_id, sender_signing_key=provider.private_key_bytes, recipient_messaging_pub=peer_box.public_key_b64(key), expires_at=consumer._expires(300)).to_dict()
    monkeypatch.setattr(consumer, "_peer_post_json", peer_post)
    app = FastAPI()
    commands = consumer.install_llm_routes(app, store=store, home=store.home, messaging_key=key, resolve_endpoint=lambda _: "http://synthetic.invalid", resolve_pubkey=lambda _: peer_box.public_key_b64(provider_key))
    client = TestClient(app)
    assert client.put("/api/local/llm/privacy", json={"result_retention_seconds": retention}).status_code == 200
    history = ConversationStore(store.home / "ask-ryn", key)
    row = history.save({"id": "chat", "title": "New chat", "serviceKey": provider.peer_id + "::model", "serviceName": "Model", "providerPeerId": provider.peer_id, "networkId": "network", "createdAt": "2026-09-11T00:00:00Z", "updatedAt": "2026-09-11T00:00:00Z", "messages": []}, expected_revision=0)
    context = AskContextService(lambda: None, lambda _: [{**public, "peer_id": provider.peer_id}])
    preview = context.preview(row, "A private test question")
    runs = AskRunService(history, lambda: context, lambda: commands)
    request = {"task_id": "task_" + "c" * 32, "conversation_id": "chat", "expected_revision": 1, "question": "A private test question", "prompt_sha256": preview["prompt_sha256"]}
    runs.begin(request)
    runs.run_once()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = commands.status(request["task_id"])
        if result.get("state") == "succeeded" and not result.get("result_pending"):
            break
        time.sleep(0.01)
    assert result.get("output") == "Synthetic signed answer", result
    assert commands.status(request["task_id"])["output"] == "Synthetic signed answer"  # Peek does not consume it.
    runs.run_once()
    assert history.get("chat")["messages"][-1]["content"] == "Synthetic signed answer"
    assert len(requests) == 1
    assert runs.begin(request)["state"] == "succeeded"
    balance = client.get("/api/local/task-balance").json()
    assert balance["held"] == 0 and balance["available"] == pytest.approx(99.999)
    if retention == 0:
        assert "output" not in commands.status(request["task_id"])
    commands.erase_results([request['task_id']])
    assert 'output' not in commands.status(request['task_id'])
    assert commands.status(request['task_id'])['state'] == 'succeeded'
    assert runs.begin(request)['state'] == 'succeeded'
    assert len(requests) == 1
    assert client.get('/api/local/task-balance').json() == balance
