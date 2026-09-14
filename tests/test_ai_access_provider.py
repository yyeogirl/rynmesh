import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.ai_access.routes import install_ai_access
from rynmesh.friends.store import FriendStore
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.routes import ProviderService
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import TaskOrderStore, open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore

RID = "a" * 32


class Model:
    def __init__(self):
        self.calls = 0
        self.cancelled = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def health(self):
        return {"ok": True}

    def infer(self, **kwargs):
        self.calls += 1
        self.started.set()
        assert self.release.wait(5)
        return {"text": "answer", "input_tokens": 1, "output_tokens": 1, "duration_ms": 1}

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        return False  # Simulate an engine that cannot immediately stop compute.


def fixture(tmp_path):
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "net")
    friend = RynmeshStore(home=tmp_path / "friend", network_dir=tmp_path / "net")
    stranger = RynmeshStore(home=tmp_path / "stranger", network_dir=tmp_path / "net")
    pkey = peer_box.load_or_create_messaging_key(provider.home / "messaging.x25519")
    fkey = peer_box.load_or_create_messaging_key(friend.home / "messaging.x25519")
    relationships = FriendStore(provider.home)
    relationships.put_relationship({"relationship_id": RID, "peer_id": friend.peer_id, "status": "active"}, b"a" * 32)
    model = Model()

    def manager(service_id="model-x"):
        return ProviderService(manifest=LLMPackageManifest(package_id=service_id, mode="openai_compatible",
                                 public_model_alias=service_id, base_url="http://127.0.0.1:1"),
            adapter=model, store=provider, task_store=TaskOrderStore(provider.home / "orders" / service_id),
            balance=TaskBalanceLedger(provider.home / "balance.json"), messaging_key=pkey)

    service = manager()
    app = FastAPI()

    def control(request):
        if request.headers.get("x-test-owner") != "owner":
            raise HTTPException(401)

    grants = install_ai_access(app, home=provider.home, local_control=control, relationship=relationships.relationship)
    app.state.ai_access.recheck = service.recheck_permissions
    client = TestClient(app)

    def request(task_id, *, revision=None, sender=friend, service_id="model-x"):
        body = {"task_id": task_id, "service_id": service_id, "prompt": "question", "max_tokens": 8, "max_amount": 1,
                "reply_messaging_pub": peer_box.public_key_b64(fkey)}
        if revision is not None:
            body["ai_permission"] = {"relationship_id": RID, "revision": revision}
        return seal_task(body=body, task_id=task_id, kind="llm_request", sender_peer_id=sender.peer_id,
            recipient_peer_id=provider.peer_id, sender_signing_key=sender.private_key_bytes,
            recipient_messaging_pub=peer_box.public_key_b64(pkey),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()).to_dict()

    def result(wire, current=service, sender=friend):
        return open_task(current.handle(wire), recipient_peer_id=sender.peer_id,
                         recipient_messaging_key=fkey, expected_kind="llm_response")[1]

    return service, manager, model, client, grants, relationships, friend, stranger, request, result


def test_owner_grants_gate_signed_provider_requests_and_replays(tmp_path):
    service, manager, model, client, grants, relationships, friend, stranger, request, result = fixture(tmp_path)
    path = f"/api/local/ai-access/model-x/{RID}"
    body = {"allowed": True, "expected_revision": 0}
    assert client.put(path, json=body).status_code == 401
    assert client.get("/api/local/ai-access").status_code == 401
    assert result(request("missing"))["error_code"] == "ai_permission_denied"
    assert result(request("invented", revision=1))["error_code"] == "ai_permission_denied"
    assert model.calls == 0
    saved = client.put(path, json=body, headers={"x-test-owner": "owner"})
    assert saved.status_code == 200 and saved.json()["grant"]["effective"]
    original = request("accepted", revision=1)
    assert result(original)["state"] == "succeeded"
    assert result(original)["state"] == "succeeded" and model.calls == 1
    assert result(request("stranger", revision=1, sender=stranger), sender=stranger)["error_code"] == "ai_permission_denied"
    other_service = manager("model-y")
    assert result(request("wrong-service", revision=1, service_id="model-y"), current=other_service)["error_code"] == "ai_permission_denied"
    revoked = client.put(path, json={"allowed": False, "expected_revision": 1}, headers={"x-test-owner": "owner"})
    assert revoked.json()["grant"]["allowed"] is False
    assert result(original)["error_code"] == "ai_permission_denied"
    restarted = manager()
    assert result(request("after-restart", revision=1), current=restarted)["error_code"] == "ai_permission_denied"
    grants.set("model-x", RID, allowed=True, expected_revision=2)
    assert result(request("old-version", revision=1), current=restarted)["error_code"] == "ai_permission_denied"
    assert result(request("new-version", revision=3), current=restarted)["state"] == "succeeded"
    assert model.calls == 2
    relationships.revoke(RID, datetime.now(timezone.utc).isoformat())
    assert result(request("friend-revoked", revision=3), current=restarted)["error_code"] == "ai_permission_denied"


def test_revoke_during_inference_does_not_claim_the_engine_stopped(tmp_path):
    service, _, model, client, grants, _, _, _, request, result = fixture(tmp_path)
    grants.set("model-x", RID, allowed=True, expected_revision=0)
    model.release.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(result, request("running", revision=1))
        assert model.started.wait(3)
        try:
            revoked = client.put(f"/api/local/ai-access/model-x/{RID}",
                json={"allowed": False, "expected_revision": 1}, headers={"x-test-owner": "owner"})
            assert revoked.status_code == 200 and revoked.json()["cancellation"] == "requested"
            assert model.cancelled == ["running"]
            assert not running.done() and service.public_status()["capacity"]["running"] == 1
            assert result(request("new-after-revoke", revision=1))["error_code"] == "ai_permission_denied"
        finally:
            model.release.set()
        assert running.result(timeout=3)["state"] == "cancelled"
    assert model.calls == 1
    assert service.public_status()["capacity"]["running"] == 0


def test_reinstall_routes_uses_current_store_and_failed_cancel_keeps_revocation(tmp_path):
    _, _, _, client, grants, relationships, _, _, _, _ = fixture(tmp_path)
    grants.set("model-x", RID, allowed=True, expected_revision=0)
    def unavailable():
        raise OSError("runtime unavailable")
    client.app.state.ai_access.recheck = unavailable
    reply = client.put(f"/api/local/ai-access/model-x/{RID}", json={"allowed": False, "expected_revision": 1},
                       headers={"x-test-owner": "owner"})
    assert reply.json()["cancellation"] == "pending"
    assert grants.list()[0]["allowed"] is False
    install_ai_access(client.app, home=tmp_path / "replacement", local_control=lambda _: None,
                      relationship=relationships.relationship)
    assert client.get("/api/local/ai-access").json() == {"grants": []}


def test_http_provider_uses_current_permission_state_after_route_reinstall(tmp_path, monkeypatch):
    from rynmesh.llm_package import routes
    from rynmesh.llm_package.manifest import save_manifest

    service, _, model, client, grants, relationships, friend, _, request, _ = fixture(tmp_path)
    manifest_path = save_manifest(service.manifest, tmp_path / "manifest.json")
    monkeypatch.setenv("RYNMESH_LLM_SERVICE_MANIFEST", str(manifest_path))
    monkeypatch.setattr(routes, "adapter_from_manifest", lambda _: model)
    routes.install_llm_routes(client.app, store=service.store, home=service.store.home,
        messaging_key=service.messaging_key, resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    friend_key = peer_box.load_or_create_messaging_key(friend.home / "messaging.x25519")

    def send(task_id):
        response = client.post("/api/peer/llm/tasks", json=request(task_id, revision=1))
        assert response.status_code == 200
        return open_task(response.json(), recipient_peer_id=friend.peer_id,
                          recipient_messaging_key=friend_key, expected_kind="llm_response")[1]

    assert send("http-denied")["error_code"] == "ai_permission_denied"
    grants.set("model-x", RID, allowed=True, expected_revision=0)
    assert send("http-allowed")["state"] == "succeeded"
    install_ai_access(client.app, home=tmp_path / "replacement", local_control=lambda _: None,
                      relationship=relationships.relationship)
    assert send("http-after-reinstall")["error_code"] == "ai_permission_denied"
    assert model.calls == 1
