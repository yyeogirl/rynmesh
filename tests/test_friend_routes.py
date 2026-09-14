from __future__ import annotations

from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore


def test_owner_pairing_message_receipt_and_signed_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "route-owner")
    monkeypatch.setenv("RYNMESH_FRIEND_ALLOW_LOOPBACK", "1")
    clients = {}
    apps = []
    auth = {"x-ryn-local-token": "route-owner"}
    for i, name in enumerate(("Alice", "Bob", "Carol")):
        home = tmp_path / name
        monkeypatch.setenv("RYNMESH_HOME", str(home))
        endpoint = f"http://127.0.0.1:{18901 + i}"
        monkeypatch.setenv("RYNMESH_FRIEND_ENDPOINT", endpoint)
        app = create_app(RynmeshStore(home=home, node_name=name, network_dir=tmp_path / "network"))
        client = TestClient(app)
        clients[endpoint] = client
        apps.append((app, client))

    calls = []
    def post(endpoint, path, payload, headers, **kwargs):
        calls.append(path)
        response = clients[endpoint].post(path, json=payload, headers=headers or {})
        response.raise_for_status()
        return response.json()

    for app, _ in apps:
        app.state.friends.service.post_json = post
    alice, bob, carol = [client for _, client in apps]
    assert alice.get("/api/local/friends").status_code in {401, 403}
    assert alice.get("/api/local/friends/abc%2Fdef/messages", headers=auth).json() == {"messages": []}
    invitation = alice.post("/api/local/friends/invites", json={}, headers=auth).json()
    uri = {"invite_uri": invitation["invite_uri"]}
    assert bob.post("/api/local/friends/invites/inspect", json=uri, headers=auth).status_code == 200
    assert calls == []
    joined = bob.post("/api/local/friends/join", json=uri, headers=auth)
    assert joined.status_code == 200, joined.text
    assert carol.post("/api/local/friends/join", json=uri, headers=auth).json()["detail"] == "invite_used"
    peer = joined.json()["peer_id"]
    request = {"message_id": "a" * 32, "text": "第一次分享"}
    sent = bob.post(f"/api/local/friends/{peer}/messages", json=request, headers=auth)
    assert sent.status_code == 200, sent.text
    assert sent.json()["delivery_state"] == "delivered"
    assert bob.post(f"/api/local/friends/{peer}/messages", json=request, headers=auth).json()["msg_id"] == "a" * 32
    sender = apps[1][0].state.friends.service.peer_id
    rows = alice.get(f"/api/local/friends/{sender}/messages", headers=auth).json()["messages"]
    assert len(rows) == 1 and rows[0]["text"] == "第一次分享"
    import base64
    attachment_bytes = b"x" * (5 * 1024 * 1024)
    attached = bob.post(f"/api/local/friends/{peer}/messages", json={
        "message_id": "d" * 32,
        "attachment": {"filename": "boundary.txt", "mime": "text/plain", "data_base64": base64.b64encode(attachment_bytes).decode()},
    }, headers=auth)
    assert attached.status_code == 200, attached.text
    assert attached.json()["delivery_state"] == "delivered"
    downloaded = alice.get(f"/api/local/friends/{sender}/attachments/{'d' * 32}", headers=auth)
    assert downloaded.content == attachment_bytes
    import time
    article_url = "https://example.test/private-reading"
    bob_app, alice_app = apps[1][0], apps[0][0]
    bob_app.state.reader_cache.put(article_url, {"title": "Reading to share", "source_url": article_url,
                                               "blocks": [{"tag": "p", "text": "Private article body, sent only on request."}]}, now=time.time())
    bob_app.state.consumption_store.record({"item_id": "reading-1", "title": "Reading to share", "link": article_url}, "opened")
    share_body = {"peer_id": peer, "item_id": "reading-1", "card_id": "b" * 32}
    shared = bob.post("/api/local/friends/share", json=share_body, headers=auth)
    assert shared.status_code == 200, shared.text
    assert shared.json()["delivery_state"] == "delivered"
    assert alice_app.state.friends.content.imports.list() == []
    received_card = alice.get("/api/local/friends/cards", headers=auth).json()["cards"][0]
    assert received_card["card"]["source_url"] == article_url
    assert received_card["fetch_state"] == "available"
    fetched = alice.post(f"/api/local/friends/cards/{'b' * 32}/fetch", headers=auth)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["sha256_verified"]
    imported_id = fetched.json()["library_id"].removeprefix("import:")
    document_url = f"/api/local/friends/documents/{imported_id}/body"
    assert alice.get(document_url, headers=auth).json()["text"] == "Private article body, sent only on request."
    assert len(alice_app.state.consumption_store.list()) == 1
    assert alice.delete(f"/api/local/friends/documents/{imported_id}", headers=auth).status_code == 409
    reviewed = alice.post('/api/local/privacy/documents/preview', json={'scope': imported_id}, headers=auth).json()
    cleared = alice.request('DELETE', f"/api/local/friends/documents/{imported_id}",
                            json={'review_token': reviewed['review_token']}, headers=auth)
    assert cleared.status_code == 200 and cleared.json()["removed"] == 1
    assert alice.get("/api/local/friends/cards", headers=auth).json()["cards"][0]["fetch_state"] == "unavailable"
    repaired = alice.post(f"/api/local/friends/cards/{'b' * 32}/fetch", json={"repair": True}, headers=auth)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["library_id"].removeprefix("import:") == imported_id
    assert len(alice_app.state.consumption_store.list()) == 1
    assert bob.post("/api/local/friends/share", json=share_body, headers=auth).json()["card_id"] == "b" * 32
    assert len(alice.get("/api/local/friends/cards", headers=auth).json()["cards"]) == 1
    assert bob.post("/api/local/friends/share", json={**share_body, "item_id": "another-article"}, headers=auth).status_code == 409
    denied = bob.post("/api/local/friends/share", json={**share_body, "card_id": "c" * 32}, headers=auth)
    assert denied.status_code == 200, denied.text
    relation = joined.json()["relationship_id"]
    assert bob.delete(f"/api/local/friends/{relation}", headers=auth).status_code == 200
    assert bob.post(f"/api/local/friends/{peer}/messages", json=request, headers=auth).status_code == 409
    assert alice.post(f"/api/local/friends/cards/{'c' * 32}/fetch", headers=auth).status_code == 409
    assert alice.get(document_url, headers=auth).status_code == 200  # An explicit saved copy remains local.
    assert alice.post("/api/peer/friends/accept", json={"invite": "bad"}).status_code == 200


def test_replay_capacity_never_evicts_still_valid_requests(tmp_path):
    from rynmesh.friends.store import FriendStore
    store = FriendStore(tmp_path)
    assert store.remember_nonce("rel", "a", now=100, timestamp=100, limit=2)
    assert store.remember_nonce("rel", "b", now=101, timestamp=101, limit=2)
    assert not store.remember_nonce("rel", "c", now=102, timestamp=102, limit=2)
    assert not store.remember_nonce("rel", "a", now=103, timestamp=100, limit=2)
    assert store.remember_nonce("rel", "c", now=221, timestamp=221, limit=2)


def test_private_card_fetch_returns_safe_denial_for_inactive_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    app = create_app(RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network"))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/peer/friends/content-card/fetch", json={"v": 1, "card_id": "a" * 32})
    assert response.status_code == 403
    assert response.json() == {"detail": "friend_request_rejected"}


def test_imports_belong_to_the_injected_node_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "ambient-home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    store = RynmeshStore(home=tmp_path / "injected-home", network_dir=tmp_path / "network")
    app = create_app(store)
    assert app.state.friends.content.imports.root == (store.home / "library-imports").resolve()
