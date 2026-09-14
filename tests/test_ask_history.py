from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.ask_ryn.routes import install_ask_ryn
from rynmesh.ask_ryn.store import CHANNEL, ConversationError, ConversationStore
from rynmesh.atomic_io import atomic_write_json, read_json
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services import peer_box


def sample():
    return {"id": "conversation-1", "title": "中文私有会话", "serviceKey": "provider::model", "serviceName": "Model",
            "providerPeerId": "provider", "networkId": "network", "createdAt": "2026-09-11T00:00:00Z", "updatedAt": "2026-09-11T00:00:00Z",
            "messages": [{"id": "message-1", "role": "user", "content": "秘密文章正文", "createdAt": "2026-09-11T00:00:00Z", "status": "complete", "taskId": "task-original"}]}


def test_encrypted_restart_and_response_loss(tmp_path):
    key = X25519PrivateKey.generate()
    history = ConversationStore(tmp_path, key)
    first = history.save(sample(), expected_revision=0)
    assert first["revision"] == 1
    assert "秘密" not in history.path.read_text()
    assert "provider" not in history.path.read_text()
    restarted = ConversationStore(tmp_path, key)
    assert restarted.get(first["id"]) == first
    assert restarted.save(sample(), expected_revision=0) == first
    with pytest.raises(ConversationError, match="ask_history_unreadable"):
        ConversationStore(tmp_path, X25519PrivateKey.generate()).list()


def test_parallel_views_conflict_and_service_binding_is_immutable(tmp_path):
    history = ConversationStore(tmp_path, X25519PrivateKey.generate())
    original = history.save(sample(), expected_revision=0)
    def change(title):
        try:
            return history.save({**original, "title": title}, expected_revision=1)
        except ConversationError as exc:
            return str(exc)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(change, ["First view", "Second view"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "ask_revision_conflict" in results
    with pytest.raises(ConversationError, match="ask_service_binding_mismatch"):
        history.save({**original, "providerPeerId": "other", "serviceKey": "other::model"}, expected_revision=2)
    assert history.get(original["id"])["providerPeerId"] == "provider"


def test_failed_migration_retries_without_duplicate_or_resurrection(tmp_path, monkeypatch):
    history = ConversationStore(tmp_path, X25519PrivateKey.generate())
    import rynmesh.ask_ryn.store as module
    writer = module.atomic_write_json
    def fail(*args, **kwargs):
        raise OSError("injected storage fault")
    monkeypatch.setattr(module, "atomic_write_json", fail)
    with pytest.raises(OSError):
        history.migrate(sample())
    monkeypatch.setattr(module, "atomic_write_json", writer)
    assert history.list() == []
    assert history.migrate(sample())["status"] == "imported"
    assert history.migrate(sample())["status"] == "already_imported"
    assert len(history.list()) == 1
    row = history.get("conversation-1")
    history.save({**row, "title": "Renamed on node"}, expected_revision=1)
    assert history.migrate(sample())["status"] == "already_imported"
    assert history.get(row["id"])["title"] == "Renamed on node"
    history.remove(row["id"], expected_revision=2)
    assert history.migrate(sample())["status"] == "deleted"
    with pytest.raises(ConversationError, match="ask_conversation_deleted"):
        history.save(sample(), expected_revision=0)
    assert history.list() == []


def test_unknown_fields_preserved_but_export_is_projected(tmp_path):
    history = ConversationStore(tmp_path, X25519PrivateKey.generate())
    history.save(sample(), expected_revision=0)
    envelope, data = history._read()
    envelope["futureEnvelopeField"] = "preserved"
    data["futureStoreField"] = {"preserved": True}
    row = data["conversations"]["conversation-1"]
    row["futureCredential"] = "never-export-this"
    row["messages"][0]["futureMetadata"] = "retained"
    history._write(envelope, data)
    history.save({**sample(), "title": "Updated"}, expected_revision=1)
    envelope, data = history._read()
    assert envelope["futureEnvelopeField"] == "preserved"
    assert data["futureStoreField"]["preserved"]
    assert data["conversations"]["conversation-1"]["messages"][0]["futureMetadata"] == "retained"
    assert "never-export-this" not in str(history.list())


def test_future_and_corrupt_records_are_never_overwritten(tmp_path):
    history = ConversationStore(tmp_path, X25519PrivateKey.generate())
    history.save(sample(), expected_revision=0)
    envelope = read_json(history.path)
    atomic_write_json(history.path, {**envelope, "version": "ryn.ask-history.v99"})
    before = history.path.read_bytes()
    with pytest.raises(ConversationError, match="version_unsupported"):
        history.save(sample(), expected_revision=0)
    assert history.path.read_bytes() == before
    envelope["ciphertext"] = "corrupt"
    atomic_write_json(history.path, envelope)
    with pytest.raises(ConversationError, match="unreadable"):
        history.list()


def test_message_budget_and_invalid_values_fail_before_write(tmp_path):
    history = ConversationStore(tmp_path, X25519PrivateKey.generate())
    for change in ({"cost": float("nan")}, {"content": "x" * (256 * 1024 + 1)}, {"taskId": "../outside"}):
        row = copy.deepcopy(sample())
        row["messages"][0].update(change)
        with pytest.raises(ConversationError):
            history.save(row, expected_revision=0)
    assert not history.path.exists()


def test_unassigned_draft_is_encrypted_restartable_and_revision_checked(tmp_path):
    key = X25519PrivateKey.generate()
    history = ConversationStore(tmp_path, key)
    assert history.draft() == {"text": "", "revision": 0}
    saved = history.save_draft("尚未发送的草稿", expected_revision=0)
    assert saved["revision"] == 1
    assert history.list() == []  # No fake conversation or model is invented.
    assert "尚未发送" not in history.path.read_text()
    restarted = ConversationStore(tmp_path, key)
    assert restarted.draft() == saved
    assert restarted.save_draft(saved["text"], expected_revision=0) == saved
    with pytest.raises(ConversationError, match="ask_revision_conflict"):
        restarted.save_draft("older view", expected_revision=0)
    assert restarted.save_draft("", expected_revision=1) == {"text": "", "revision": 2}


def test_conversation_draft_keeps_binding_and_future_fields(tmp_path):
    history = ConversationStore(tmp_path, X25519PrivateKey.generate())
    saved = history.save({**sample(), "draft": "Review before sending"}, expected_revision=0)
    assert history.get(saved["id"])["draft"] == "Review before sending"
    with pytest.raises(ConversationError, match="ask_service_binding_mismatch"):
        history.save({**saved, "providerPeerId": "new", "serviceKey": "new::model"}, expected_revision=1)
    envelope, data = history._read()
    data["draft"] = {"version": 99, "text": "future", "revision": 2}
    history._write(envelope, data)
    before = history.path.read_bytes()
    with pytest.raises(ConversationError, match="version_unsupported"):
        history.save_draft("overwrite", expected_revision=2)
    assert history.path.read_bytes() == before


def test_owner_routes_reinstall_uses_latest_store_and_auth(tmp_path):
    app = FastAPI()
    key = X25519PrivateKey.generate()
    workers = BackgroundWorkerRegistry()
    def auth(request):
        if request.headers.get("x-owner") != "owner":
            raise HTTPException(401, "unauthorized")
    old_home, home = tmp_path / "old", tmp_path / "node"
    install_ask_ryn(app, store=SimpleNamespace(home=old_home), home=tmp_path / "ignored", workers=workers, local_control=auth, messaging_key=key)
    install_ask_ryn(app, store=SimpleNamespace(home=home), home=tmp_path / "ignored", workers=workers, local_control=auth, messaging_key=key)
    client = TestClient(app)
    headers = {"x-owner": "owner"}
    denied = client.get("/api/local/ask/conversations")
    assert denied.status_code == 401
    assert denied.headers.get("cache-control") == "no-store"
    row = {**sample(), "api_key": "must-not-import"}
    migrated = client.post("/api/local/ask/migrate", headers=headers, json={"source": "ryn-private-ai-chat-v1", "conversation": row})
    assert migrated.status_code == 200, migrated.text
    response = client.get("/api/local/ask/export", headers=headers)
    assert response.headers.get("cache-control") == "no-store"
    exported = response.json()
    assert exported["conversations"][0]["messages"][0]["taskId"] == "task-original"
    assert "must-not-import" not in str(exported)
    assert not (old_home / "ask-ryn" / "history.json").exists()
    assert (home / "ask-ryn" / "history.json").exists()
    assert client.put("/api/local/ask/conversations/conversation-1", headers=headers, json={"conversation": {**row, "title": "Renamed"}, "expected_revision": 1}).json()["revision"] == 2
    conflict = client.request("DELETE", "/api/local/ask/conversations/conversation-1", headers=headers, json={"expected_revision": 1})
    assert conflict.status_code == 409
    removed = client.request("DELETE", "/api/local/ask/conversations/conversation-1", headers=headers, json={"expected_revision": 2})
    assert removed.json() == {"removed": 1}
    assert client.get("/api/local/ask/conversations", headers=headers).json() == {"conversations": []}
    # Ciphertext is domain-separated from peer messages.
    sealed = read_json(home / "ask-ryn" / "history.json")
    assert CHANNEL != peer_box._INFO
    with pytest.raises(InvalidTag):
        peer_box.open_sealed(key, peer_box.public_key_b64(key), sealed["nonce"], sealed["ciphertext"])
