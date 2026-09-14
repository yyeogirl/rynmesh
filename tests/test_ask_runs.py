from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.ask_ryn.context import AskContextService
from rynmesh.ask_ryn.routes import install_ask_ryn
from rynmesh.ask_ryn.runs import AskRunService
from rynmesh.ask_ryn.store import ConversationError, ConversationStore
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.file_transactions import file_transaction


class Orders:
    def __init__(self):
        self.sent, self.cancelled, self.acknowledged = [], [], []
        self.results = {}

    def submit(self, body):
        self.sent.append(copy.deepcopy(body))
        self.results[body["task_id"]] = {"state": "running"}
        return {"state": "queued"}

    def status(self, task_id):
        if task_id not in self.results:
            raise HTTPException(404)
        return self.results[task_id]

    def cancel(self, task_id):
        self.cancelled.append(task_id)

    def acknowledge(self, task_id):
        self.acknowledged.append(task_id)


def setup(tmp_path):
    history = ConversationStore(tmp_path, X25519PrivateKey.generate())
    context = AskContextService(lambda: None, lambda _: [{"peer_id": "provider", "service": {"package_id": "model", "context_window": 4096, "max_output_tokens": 256}}])
    row = history.save({"id": "conversation", "title": "New chat", "serviceKey": "provider::model", "serviceName": "Model", "providerPeerId": "provider", "networkId": "network", "createdAt": "2026-09-11T00:00:00Z", "updatedAt": "2026-09-11T00:00:00Z", "messages": []}, expected_revision=0)
    orders = Orders()
    runs = AskRunService(history, lambda: context, lambda: orders)
    preview = context.preview(row, "中文秘密问题")
    request = {"task_id": "task_" + "a" * 32, "conversation_id": row["id"], "expected_revision": row["revision"], "question": "中文秘密问题", "prompt_sha256": preview["prompt_sha256"]}
    return history, runs, orders, request


@pytest.mark.parametrize('code,reason,action', [
    ('runtime_busy', 'busy', 'Wait'),
    ('capacity_exhausted', 'busy', 'Wait'),
    ('p2p_capacity_exhausted', 'No connection session', 'Wait'),
    ('model_not_ready', 'not ready', 'model setup'),
    ('model_not_found', 'selected model is unavailable', 'configuration'),
    ('runtime_unavailable', 'runtime is unavailable', 'Check'),
    ('runtime_connection_failed', 'could not reach', 'local AI settings'),
    ('service_unhealthy', 'not ready', 'model setup'),
    ('provider_unavailable', 'provider is unavailable', 'connection'),
    ('direct_transport_failed', 'direct connection failed', 'original task'),
    ('p2p_transport_failed', 'peer connection failed', 'original task'),
    ('encrypted_relay_failed', 'relay connection failed', 'original task'),
    ('provider_restarted_before_completion', 'provider restarted', 'has not been submitted again'),
])
def test_actionable_failures_survive_restart_without_resubmission(tmp_path, code, reason, action):
    history, runs, orders, request = setup(tmp_path)
    runs.begin(request)
    runs.run_once()
    orders.results[request['task_id']] = {
        'state': 'failed', 'error_code': code, 'error': 'PRIVATE_ERROR_CANARY',
    }
    runs.run_once()
    content = history.get('conversation')['messages'][-1]['content']
    assert reason in content and action in content
    assert 'PRIVATE_ERROR_CANARY' not in content
    restarted_history = ConversationStore(history.root, history.key)
    restarted = AskRunService(restarted_history, runs.context, lambda: orders)
    assert restarted.begin(request)['error_code'] == code
    restarted.run_once()
    assert restarted_history.get('conversation')['messages'][-1]['content'] == content
    assert len(orders.sent) == 1


def test_node_dispatch_and_archive_survive_no_browser_and_restart(tmp_path):
    history, runs, orders, request = setup(tmp_path)
    accepted = runs.begin(request)
    assert accepted["state"] == "queued" and orders.sent == []
    assert runs.begin(request) == accepted  # Lost begin response.
    assert len(history.get("conversation")["messages"]) == 2
    assert "中文秘密问题" not in history.path.read_text()
    runs.run_once()
    assert len(orders.sent) == 1
    assert orders.sent[0]["task_id"] == orders.sent[0]["idempotency_key"] == request["task_id"]
    assert "中文秘密问题" in orders.sent[0]["prompt"]

    orders.results[request["task_id"]] = {"state": "succeeded", "output": "实际归档回答", "input_tokens": 20, "output_tokens": 8, "amount": 0.01}
    restarted = AskRunService(ConversationStore(history.root, history.key), runs.context, lambda: orders)
    restarted.run_once()
    row = history.get("conversation")
    assert row["messages"][-1]["content"] == "实际归档回答"
    assert row["messages"][-1]["cost"] == 0.01
    assert row["messages"][-1]["promptSha256"] == request["prompt_sha256"]
    assert restarted.begin(request)["state"] == "succeeded"
    restarted.run_once()
    assert len(orders.sent) == 1 and orders.acknowledged == [request["task_id"]]
    with file_transaction(history.lock):
        _, data = history._read()
        assert "body" not in data["runs"]["records"][request["task_id"]]


def test_reviewed_friend_permission_is_frozen_and_changes_require_new_review(tmp_path):
    history, runs, orders, request = setup(tmp_path)
    context = runs.context()
    original_catalog = context.catalog
    version = [1]
    def catalog(network):
        rows = original_catalog(network)
        rows[0]["ai_permission"] = {"relationship_id": "b" * 32, "revision": version[0]}
        return rows
    context.catalog = catalog
    request["ai_permission"] = {"relationship_id": "b" * 32, "revision": 1}
    version[0] = 2
    with pytest.raises(ConversationError, match="ask_preview_changed"):
        runs.begin(request)
    assert history.get("conversation")["messages"] == []
    request["ai_permission"]["revision"] = 2
    runs.begin(request)
    version[0] = 3
    runs.run_once()
    assert orders.sent[0]["ai_permission"]["revision"] == 2


def test_crash_at_dispatch_boundary_never_resubmits(tmp_path, monkeypatch):
    history, runs, orders, request = setup(tmp_path)
    runs.begin(request)
    def crash(_):
        raise SystemExit("simulated process death before consumer claim")
    monkeypatch.setattr(orders, "submit", crash)
    with pytest.raises(SystemExit):
        runs.run_once()
    restarted = AskRunService(ConversationStore(history.root, history.key), runs.context, lambda: orders)
    restarted.run_once()
    assert restarted.get(request["task_id"])["state"] == "interrupted"
    assert orders.sent == []
    assert "not been submitted again" in history.get("conversation")["messages"][-1]["content"]


def test_restart_before_dispatch_keeps_approved_intent_and_cancel_before_start_is_local(tmp_path):
    history, runs, orders, request = setup(tmp_path)
    runs.begin(request)
    restarted = AskRunService(ConversationStore(history.root, history.key), runs.context, lambda: orders)
    restarted.cancel(request["task_id"])
    restarted.run_once()
    assert restarted.get(request["task_id"])["state"] == "cancelled"
    assert orders.sent == [] and orders.cancelled == []
    row = history.get("conversation")
    next_request = {**request, "task_id": "task_" + "b" * 32, "expected_revision": row["revision"], "prompt_sha256": runs.context().preview(row, request["question"])["prompt_sha256"]}
    restarted.begin(next_request)
    next_process = AskRunService(ConversationStore(history.root, history.key), runs.context, lambda: orders)
    next_process.run_once()
    assert len(orders.sent) == 1 and orders.sent[0]["task_id"] == next_request["task_id"]


def test_lost_submit_response_checks_same_task_without_repeating(tmp_path, monkeypatch):
    _, runs, orders, request = setup(tmp_path)
    runs.begin(request)
    submit = orders.submit
    def lost(body):
        submit(body)
        raise OSError("prompt must never enter worker diagnostics")
    monkeypatch.setattr(orders, "submit", lost)
    with pytest.raises(ConversationError, match="^ask_run_check_unavailable$"):
        runs.run_once()
    runs.run_once()
    assert runs.get(request["task_id"])["state"] == "running" and len(orders.sent) == 1


def test_storage_failure_does_not_acknowledge_result_or_duplicate_output(tmp_path, monkeypatch):
    history, runs, orders, request = setup(tmp_path)
    runs.begin(request)
    runs.run_once()
    orders.results[request["task_id"]] = {"state": "succeeded", "output": "Keep until encrypted commit"}
    writer = history._write
    writes = 0
    def fail_archive(envelope, data):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        writer(envelope, data)
    monkeypatch.setattr(history, "_write", fail_archive)
    with pytest.raises(OSError):
        runs.run_once()
    assert orders.acknowledged == []
    monkeypatch.setattr(history, "_write", writer)
    runs.run_once()
    assert len(history.get("conversation")["messages"]) == 2
    assert orders.acknowledged == [request["task_id"]]


@pytest.mark.parametrize("dispatched", [False, True])
def test_delete_removes_frozen_prompt_and_never_restores_history(tmp_path, dispatched):
    history, runs, orders, request = setup(tmp_path)
    runs.begin(request)
    if dispatched:
        runs.run_once()
    row = history.get("conversation")
    history.remove(row["id"], expected_revision=row["revision"])
    with file_transaction(history.lock):
        _, data = history._read()
        assert "body" not in data["runs"]["records"][request["task_id"]]
    orders.results[request["task_id"]] = {"state": "succeeded", "output": "Late answer"}
    runs.run_once()
    assert history.list() == []
    assert len(orders.sent) == int(dispatched)


def test_cancel_is_intent_until_original_order_confirms_and_late_success_is_saved(tmp_path):
    history, runs, orders, request = setup(tmp_path)
    runs.begin(request)
    runs.run_once()
    state = runs.cancel(request["task_id"])
    assert state["cancel_requested"] and state["state"] == "running"
    orders.results[request["task_id"]] = {"state": "cancelled", "result_pending": True}
    runs.run_once()
    assert history.get("conversation")["messages"][-1]["status"] == "cancel_requested"
    orders.results[request["task_id"]] = {"state": "succeeded", "output": "Completed before cancellation", "amount": 0.1}
    runs.run_once()
    assert runs.get(request["task_id"])["state"] == "succeeded"
    assert history.get("conversation")["messages"][-1]["cost"] == 0.1


def test_parallel_edits_keep_title_and_cannot_replace_active_messages(tmp_path):
    history, runs, orders, request = setup(tmp_path)
    runs.begin(request)
    row = history.get("conversation")
    with pytest.raises(ConversationError, match="ask_run_busy"):
        history.save({**row, "messages": []}, expected_revision=row["revision"])
    history.save({**row, "title": "Renamed while running"}, expected_revision=row["revision"])
    runs.run_once()
    orders.results[request["task_id"]] = {"state": "succeeded", "output": "Answer"}
    runs.run_once()
    assert history.get("conversation")["title"] == "Renamed while running"
    with pytest.raises(ConversationError, match="ask_run_identity_conflict"):
        runs.begin({**request, "question": "Changed question"})


def test_preview_revision_budget_changes_and_future_run_format_fail_closed(tmp_path):
    history, runs, orders, request = setup(tmp_path)
    with pytest.raises(ConversationError, match="ask_preview_changed"):
        runs.begin({**request, "prompt_sha256": "b" * 64})
    with pytest.raises(ConversationError, match="ask_revision_conflict"):
        runs.begin({**request, "expected_revision": 0})
    assert history.get("conversation")["messages"] == [] and orders.sent == []
    with file_transaction(history.lock):
        envelope, data = history._read()
        data["runs"] = {"version": 999, "records": {}}
        history._write(envelope, data)
    before = history.path.read_bytes()
    with pytest.raises(ConversationError, match="ask_history_version_unsupported"):
        runs.begin(request)
    assert history.path.read_bytes() == before


def test_owner_http_and_reinstalled_worker_use_current_node(tmp_path):
    history, _, _, request = setup(tmp_path / "seed")
    app, workers = FastAPI(), BackgroundWorkerRegistry()
    def owner(req):
        if req.headers.get("x-owner") != "yes":
            raise HTTPException(401)
    def install(home):
        state = install_ask_ryn(app, store=SimpleNamespace(home=home), home=home, workers=workers, local_control=owner, messaging_key=history.key)
        state.context = AskContextService(lambda: None, lambda _: [{"peer_id": "provider", "service": {"package_id": "model", "context_window": 4096, "max_output_tokens": 256}}])
        state.orders = Orders()
        state.conversations.save(history.get("conversation"), expected_revision=0)
        return state
    old = install(tmp_path / "old")
    current = install(tmp_path / "current")
    client = TestClient(app)
    assert client.post("/api/local/ask/runs", json=request).status_code == 401
    assert client.get(f"/api/local/ask/runs/{request['task_id']}").status_code == 401
    assert client.post(f"/api/local/ask/runs/{request['task_id']}/cancel").status_code == 401
    accepted = client.post("/api/local/ask/runs", json=request, headers={"x-owner": "yes"})
    assert accepted.status_code == 200, accepted.text
    worker = next(spec for spec in workers.specs() if spec.name == "ask-ryn-runs")
    worker.run_once()
    assert old.orders.sent == [] and len(current.orders.sent) == 1
    result = client.get(f"/api/local/ask/runs/{request['task_id']}", headers={"x-owner": "yes"}).json()
    assert result["state"] == "running" and "prompt" not in str(result)
    assert len(current.conversations.get("conversation")["messages"]) == 2
