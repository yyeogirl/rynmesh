import socket
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_ask_history import sample
from test_friends import _pair
from test_offline_reading import BODY as OFFLINE_TEXT
from test_offline_reading import download as download_offline
from test_offline_reading import fixture as offline_fixture

from rynmesh.ask_ryn.store import ConversationStore
from rynmesh.atomic_io import atomic_write_json, read_json
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.local_search.index import LocalSearchIndex, SearchError
from rynmesh.local_search.routes import install_local_search
from rynmesh.local_search.sources import LocalSearchSources
from rynmesh.services import peer_box
from rynmesh.services.consumption import ConsumptionStore
from rynmesh.services.library_imports import LibraryImportStore
from rynmesh.services.reader import ReaderCache


def sources(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    history = ConsumptionStore(alice.home / "consumption.json")
    imports = LibraryImportStore(alice.home / "library-imports")
    reader = ReaderCache(alice.home / "reader", ttl_s=1)
    conversations = ConversationStore(alice.home / "ask", alice.messaging_private)
    adapter = LocalSearchSources(consumption=lambda: history, imports=lambda: imports,
        reader=lambda: reader, friends=lambda: alice, conversations=lambda: conversations)
    return adapter, history, imports, reader, conversations, alice, bob, mesh


def test_synced_position_is_searchable_history_without_fabricated_local_open(tmp_path):
    from rynmesh.device_sync import records
    adapter, history, _, _, _, _, _, _ = sources(tmp_path)
    history.enable_sync('a' * 64, ['reading'])
    item = {'item_id': 'remote-position', 'title': 'Remote reading position', 'source_title': 'Journal',
            'link': 'https://example.test/read', 'content_kind': 'article'}
    value = {'item': item, 'progress': .65, 'completed': False, 'content_version': ''}
    state = records.write('reading', item['item_id'], records.empty(), 'b' * 64, value)
    history.sync_receive([{'scope': 'reading', 'id': item['item_id'], 'record': state}], scopes=['reading'])
    result = adapter.snapshot()[0]
    assert result['kinds'] == ['history']
    assert result['reading_record']['progress'] == .65
    assert result['reading_record']['open_count'] == result['timestamp'] == 0
    state = records.write('reading', item['item_id'], state, 'b' * 64, None)
    history.sync_receive([{'scope': 'reading', 'id': item['item_id'], 'record': state}], scopes=['reading'])
    assert adapter.snapshot() == []


def test_offline_body_search_clear_revalidation_and_independent_copy(tmp_path):
    adapter, history, imports, reader, _, alice, _, _ = sources(tmp_path)
    offline = offline_fixture(alice.home, images=False)
    adapter.offline = lambda: offline.service
    engine = LocalSearchIndex(alice.home / 'local-search', messaging_key=alice.messaging_private, source=adapter.snapshot)
    engine.rebuild()
    assert engine.query('Offline-only')['total'] == 0
    download_offline(offline)
    engine.rebuild()
    result = engine.query('Offline-only')
    assert result['total'] == 1
    document = engine.resolve(result['results'][0]['id'])
    assert document['text'] == OFFLINE_TEXT and document['offline_key']
    assert not (alice.home / 'reader-cache').exists()  # Download does not create another cache copy.
    before = history.path.read_bytes()
    offline.service.clear(review_token=offline.service.clear_preview()['review_token'])
    assert engine.query('Offline-only')['total'] == 0  # Immediately gated, before rebuild.
    assert not engine.resolve(document['id']).get('offline_key')
    assert history.path.read_bytes() == before
    engine.rebuild()
    assert engine.query('Saved article')['total'] == 1  # Bookmark metadata remains searchable.
    download_offline(offline)
    independent = imports.save(OFFLINE_TEXT.encode(), filename='independent.txt', mime='text/plain')
    engine.rebuild()
    assert engine.query('Offline-only')['total'] == 2
    offline.service.clear(review_token=offline.service.clear_preview()['review_token'])
    assert engine.query('Offline-only')['total'] == 1
    assert imports.body(independent['import_id'])['text'] == OFFLINE_TEXT


def test_offline_search_retains_download_without_history_and_refuses_corrupt_body(tmp_path):
    adapter, history, _, _, _, alice, _, _ = sources(tmp_path)
    offline = offline_fixture(alice.home, images=False)
    adapter.offline = lambda: offline.service
    body = download_offline(offline)
    history.clear()
    engine = LocalSearchIndex(alice.home / 'local-search', messaging_key=alice.messaging_private, source=adapter.snapshot)
    engine.rebuild()
    result = engine.query('Offline-only')
    assert result['total'] == 1
    document = engine.resolve(result['results'][0]['id'])
    assert document['reading_record']['item']['item_id'] == offline.item['item_id']
    atomic_write_json(offline.service.store._path(body['job_id']), {'version': 'ryn.offline-reading.v1', 'ciphertext': 'broken'})
    assert engine.query('Offline-only')['total'] == 0
    with pytest.raises(SearchError, match='search_result_unavailable'):
        engine.resolve(document['id'])


def test_corrupt_offline_metadata_does_not_hide_independent_search_sources(tmp_path):
    adapter, _, imports, _, _, alice, _, _ = sources(tmp_path)
    offline = offline_fixture(alice.home, images=False)
    adapter.offline = lambda: offline.service
    download_offline(offline)
    imports.save(b'Independent healthy document', filename='healthy.txt', mime='text/plain')
    engine = LocalSearchIndex(alice.home / 'local-search', messaging_key=alice.messaging_private, source=adapter.snapshot)
    engine.rebuild()
    atomic_write_json(offline.service.store.path, {'version': 'ryn.offline-reading.v999'})
    assert engine.query('Offline-only')['total'] == 0
    assert engine.query('Independent healthy')['total'] == 1


def test_real_local_stores_search_all_types_without_fetching_and_revalidate_open(tmp_path, monkeypatch):
    adapter, history, imports, reader, conversations, alice, bob, mesh = sources(tmp_path)
    article = {"item_id": "article-one", "title": "Saved-title", "source_title": "Notebook", "link": "https://example.test/story"}
    history.record(article, "bookmark")
    history.record(article, "opened")
    reader.put(article["link"], {"blocks": [{"text": "春天山谷 Python"}]}, now=1)
    imported = imports.save(b"Private local paper", filename="paper.txt", mime="text/plain")
    bob.send_content_card(alice.peer_id, {"title": "Friend-card", "source": "Bob journal"}, card_id="a" * 32)
    bob.send_message(alice.peer_id, text="Friend-message", message_id="b" * 32)
    conversation = sample()
    conversations.save(conversation, expected_revision=0)
    mesh.online.clear()  # Searching must not need a peer, registry or model.
    def forbidden(*args, **kwargs):
        raise AssertionError("Search attempted a remote request")
    alice.post_json = forbidden
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    engine = LocalSearchIndex(alice.home / "local-search", messaging_key=alice.messaging_private, source=adapter.snapshot)
    engine.rebuild()
    for query, kind in (("山谷 Python", "saved"), ("山谷", "history"), ("Friend-card", "share"),
                        ("Friend-message", "chat"), ("秘密文章正文", "chat"), ("Private local paper", "saved")):
        result = engine.query(query, kind=kind)
        assert result["total"] == 1 and not result["partial"]
        assert result["results"][0]["targets"][0]["href"].startswith(("/search?", "/friends?", "/ask?"))
    assert len(engine.query("山谷")["results"]) == 1
    before = engine.query("Friend-card")["results"][0]
    assert engine.resolve(before["id"])["body_state"] == "not_downloaded"
    assert engine.query("nonexistent-downloaded-body")["total"] == 0
    alice.revoke(alice.store.relationship_for_peer(bob.peer_id)["relationship_id"], notify=False)
    assert engine.query("Friend-card")["total"] == engine.query("Friend-message")["total"] == 0
    with pytest.raises(SearchError, match="result_unavailable"):
        engine.resolve(before["id"])
    assert engine.query("Private local paper")["total"] == 1
    imports.remove(imported["import_id"])
    assert engine.query("Private local paper")["total"] == 0
    conversations.remove(conversation["id"], expected_revision=1)
    assert engine.query("秘密文章正文")["total"] == 0


def test_same_verified_article_merges_sources_but_a_different_revision_does_not(tmp_path):
    adapter, history, imports, reader, _, alice, bob, _ = sources(tmp_path)
    url = "https://example.test/article"
    article = {"item_id": "same", "title": "Shared exact body", "source_title": "Journal", "link": url}
    history.record(article, "opened")
    reader.put(url, {"blocks": [{"text": "Same verified article"}]}, now=1)
    saved = imports.save(b"Same verified article", filename="article.txt", mime="text/plain", source={"source_url": url})
    bob.send_content_card(alice.peer_id, {"title": "A card", "source_url": url}, card_id="c" * 32)
    alice.store.patch_card("c" * 32, {"fetched_library_id": "import:" + saved["import_id"], "fetch_state": "fetched"})
    engine = LocalSearchIndex(alice.home / "local-search", messaging_key=alice.messaging_private, source=adapter.snapshot)
    engine.rebuild()
    result = engine.query("Same verified article")
    assert result["total"] == 1
    assert set(result["results"][0]["kinds"]) == {"saved", "history", "share"}
    assert len(result["results"][0]["targets"]) == 2
    metadata_path = imports.root / saved["import_id"] / "metadata.json"
    metadata = read_json(metadata_path)
    atomic_write_json(metadata_path, {**metadata, "extraction_status": "truncated"})
    engine.rebuild()
    assert engine.query("Same verified article")["results"][0]["text_truncated"]
    imports.save(b"New revision", filename="article.txt", mime="text/plain", source={"source_url": url})
    engine.rebuild()
    assert engine.query("New revision")["total"] == 1
    assert engine.query("Same verified article")["total"] == 1
    # The explicit copy survives friendship removal, with share provenance gone.
    alice.revoke(alice.store.relationship_for_peer(bob.peer_id)["relationship_id"], notify=False)
    engine.rebuild()
    assert set(engine.query("Same verified article")["results"][0]["kinds"]) == {"saved", "history"}


def test_routes_auth_reinstallation_current_source_and_safe_post_logging(tmp_path, caplog):
    adapter, _, _, _, conversations, alice, _, _ = sources(tmp_path)
    conversations.save(sample(), expected_revision=0)
    workers = BackgroundWorkerRegistry()
    app = FastAPI()
    def guard(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(401)
    def install(root):
        return install_local_search(app, store=SimpleNamespace(home=root), home=tmp_path / "wrong-home",
            workers=workers, messaging_key=alice.messaging_private, local_control=guard, source=adapter.snapshot)
    engine = install(alice.home)
    install(alice.home)
    assert len([route for route in app.routes if route.path == "/api/local/search/query"]) == 1
    assert [spec.name for spec in workers.specs()] == ["local-search.index"]
    spec = workers.specs()[0]
    assert spec.initial_delay_s == 1 and spec.policy.busy_delay_s == 1
    client = TestClient(app)
    for path, method in (("status", "get"), ("query", "post"), ("rebuild", "post"), ("open?identifier=x", "get")):
        assert getattr(client, method)("/api/local/search/" + path).status_code == 401
    owner = {"x-owner": "yes"}
    assert client.post("/api/local/search/rebuild", headers=owner).status_code == 200
    marker = "秘密文章正文"
    result = client.post("/api/local/search/query", headers=owner, json={"query": marker})
    assert result.status_code == 200 and result.json()["total"] == 1
    status = client.get("/api/local/search/status", headers=owner).json()
    assert set(status) == {"version", "state", "error_code", "indexed_count", "updated_at", "unavailable_sources", "worker"}
    assert marker not in caplog.text and marker not in str(status)
    replacement = install(tmp_path / "replacement")
    assert replacement.path != engine.path and replacement.status()["state"] == "needs_rebuild"
    workers.specs()[0].run_once()
    assert replacement.path.is_file() and not (tmp_path / "wrong-home" / "local-search").exists()
    assert client.post("/api/local/search/query", headers=owner, json={"query": marker}).json()["total"] == 1
    assert client.post("/api/local/search/query", headers=owner, json={}).status_code == 400
    assert client.post("/api/local/search/query", headers=owner, content=b"x" * 8193).status_code == 413
    assert peer_box.public_key_b64(alice.messaging_private) not in str(status)


def test_search_private_responses_never_cache_and_recheck_deleted_content(tmp_path):
    adapter, _, _, _, conversations, alice, _, _ = sources(tmp_path)
    conversation = sample()
    conversations.save(conversation, expected_revision=0)
    app = FastAPI()

    def guard(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(401)

    engine = install_local_search(app, store=SimpleNamespace(home=alice.home),
        home=tmp_path, workers=BackgroundWorkerRegistry(),
        messaging_key=alice.messaging_private, local_control=guard, source=adapter.snapshot)
    engine.rebuild()
    client = TestClient(app)
    owner = {"x-owner": "yes"}
    result = client.post("/api/local/search/query", headers=owner, json={"query": "秘密文章正文"})
    assert result.status_code == 200
    identifier = result.json()["results"][0]["id"]
    opened = client.get("/api/local/search/open", headers=owner, params={"identifier": identifier})
    assert opened.status_code == 200 and opened.json()["text"]
    responses = [result, opened,
        client.get("/api/local/search/status", headers=owner),
        client.post("/api/local/search/rebuild", headers=owner),
        client.get("/api/local/search/open", params={"identifier": identifier}),
        client.post("/api/local/search/query", headers=owner, json={}),
        client.get("/api/local/search/open", headers=owner),
        client.get("/api/local/search/open", headers=owner, params={"identifier": "missing"})]
    assert [response.status_code for response in responses] == [200, 200, 200, 200, 401, 400, 422, 409]
    # Delete the original while its old index entry still exists.
    conversations.remove(conversation["id"], expected_revision=1)
    denied = client.get("/api/local/search/open", headers=owner, params={"identifier": identifier})
    assert denied.status_code == 409
    refreshed = client.post("/api/local/search/query", headers=owner, json={"query": "秘密文章正文"})
    assert refreshed.status_code == 200 and refreshed.json()["total"] == 0
    responses.extend([denied, refreshed])
    for response in responses:
        assert response.headers.get("cache-control") == "no-store"
