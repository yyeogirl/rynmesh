"""Product acceptance evidence for explanation and individual feedback undo."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.recommendation_profile import RecommendationProfileStore
from rynmesh.services.digest import DigestService
from rynmesh.store import RynmeshStore


def item(key, **extra):
    return {"content_id": key, "title": f"Article {key}", "tags": [key],
            "publisher_peer_id": "source:one", "source_platform": "rss", **extra}


def test_individual_undo_restores_prior_action_and_survives_restart(tmp_path):
    store = RecommendationProfileStore(tmp_path / "profile.json")
    store.patch({"topics": ["research"]})
    store.feedback(item("first"), "more")
    first = store.history()["items"][0]
    store.feedback(item("second"), "less")
    store.feedback(item("first"), "hide")
    hidden = store.history()["items"][0]
    assert "first" in store.signals()["hidden_content_ids"]
    assert hidden["tags"] == ["first"] and hidden["updated_at"]
    store.undo(hidden["event_id"])
    store.undo(hidden["event_id"])  # A lost HTTP reply is safe to retry.
    store = RecommendationProfileStore(store.path)
    assert store.signals()["hidden_content_ids"] == []
    assert store.signals()["tag_weights"]["first"] > 0
    assert store.signals()["tag_weights"]["second"] < 0
    assert store.public()["topics"] == ["research"]
    store.undo(first["event_id"])
    assert "first" not in store.signals()["tag_weights"]
    assert store.history()["total"] == 3
    assert len(store.history(offset=1, limit=1)["items"]) == 1


def test_retry_concurrent_updates_and_failed_write_do_not_lose_actions(tmp_path, monkeypatch):
    store = RecommendationProfileStore(tmp_path / "profile.json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda key: store.feedback(item(str(key)), "more"), range(20)))
    store.feedback(item("0"), "more")
    assert store.history()["total"] == 20
    before = store.path.read_bytes()
    event_id = store.history()["items"][0]["event_id"]
    original = store._write
    monkeypatch.setattr(store, "_write", lambda data: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        store.undo(event_id)
    with pytest.raises(OSError):
        store.feedback(item("new"), "less")
    assert store.path.read_bytes() == before
    monkeypatch.setattr(store, "_write", original)
    store.undo(event_id)
    assert store.history()["items"][0]["undone_at"]


def test_legacy_backup_unknown_fields_future_and_corrupt_records(tmp_path):
    path = tmp_path / "profile.json"
    legacy = {"version": 1, "extension": {"retain": True}, "feedback": {
        "old": {"action": "more", "tags": ["science"], "extension": "private-value"}
    }}
    path.write_text(json.dumps(legacy))
    store = RecommendationProfileStore(path)
    store.patch({"topics": ["research"]})
    assert json.loads(path.with_name("profile.json.migrated").read_text()) == legacy
    assert store.get()["extension"] == {"retain": True}
    assert store.get()["feedback_events"][0]["extension"] == "private-value"
    assert "private-value" not in json.dumps(store.history())
    assert "private-value" not in json.dumps(store.public())
    assert store.history()["items"][0]["migrated"]
    for raw in ('{"version":999}', '{"damaged":'):
        path.write_text(raw)
        with pytest.raises((ValueError, OSError)):
            store.patch({"direction": "new"})
        with pytest.raises((ValueError, OSError)):
            store.clear()
        assert path.read_text() == raw


def prepare(service):
    service._save("sources.json", [{"id": "one", "title": "One", "kind": "rss",
        "feed_url": "https://example.test/feed", "tags": ["science"], "weight": 1.0}])
    service._save("items.json", {"one": [{"item_id": "a", "source_id": "one",
        "title": "Science reading", "summary": "Research", "link": "https://example.test/a",
        "published_unix": 1800000000}]})


def test_undo_removes_all_ranker_influence_including_legacy_derived_weights(tmp_path):
    profile = RecommendationProfileStore(tmp_path / "profile.json")
    service = DigestService(tmp_path, profile_store=profile)
    prepare(service)
    service._save("prefs.json", {"source_weight": {"one": 2.4}, "tag_affinity": {"science": 5}})
    before = service.build(now_unix=1800001000)["items"]
    service.feedback("a", "hide")
    assert service.build(now_unix=1800001000)["items"] == []
    profile.undo(profile.history()["items"][0]["event_id"])
    assert service.build(now_unix=1800001000)["items"] == before
    assert service._load("prefs.json", {})["source_weight"]["one"] == 2.4


def test_explanation_undo_routes_missing_item_restart_export_erase(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    app = create_app(RynmeshStore())
    prepare(app.state.digest_service)
    client = TestClient(app)
    assert client.post("/api/local/digest/feedback", json={"item_id": "a", "action": "hide"}).status_code == 200
    rows = client.get("/api/local/recommendations/signals").json()
    event_id = rows["items"][0]["event_id"]
    assert rows["items"][0]["action"] == "hide"
    assert client.get("/api/local/recommendations/signals?limit=101").status_code == 422
    app.state.digest_service._save("items.json", {})  # Undo does not need the original feed.
    assert client.post(f"/api/local/recommendations/feedback/{event_id}/undo").status_code == 200
    assert client.post("/api/local/recommendations/feedback/missing/undo").status_code == 404
    restarted = TestClient(create_app(RynmeshStore()))
    assert restarted.get("/api/local/recommendations/signals").json()["items"][0]["undone_at"]
    export = client.get("/api/local/privacy/export").json()
    assert export["recommendation_profile"]["feedback_events"][0]["undone_at"]
    assert client.post("/api/local/privacy/erase", json={"scopes": ["profile"]}).status_code == 200
    assert client.get("/api/local/recommendations/signals").json()["total"] == 0
