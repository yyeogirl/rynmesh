from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.digest import DIGEST_SCHEMA_VERSION
from rynmesh.services.first_success import FIRST_SUCCESS_VERSION, FirstSuccessStore
from rynmesh.store import RynmeshStore


def test_store_is_atomic_monotonic_and_body_free(tmp_path):
    path = tmp_path / "first-success.json"
    store = FirstSuccessStore(path)
    assert store.get()["version"] == FIRST_SUCCESS_VERSION

    store.record("node_ready", now_unix=10)
    store.record("node_ready", now_unix=20)
    store.sync(
        node_ready=True,
        content_ready=True,
        first_item_opened=True,
        first_signal_recorded=True,
        now_unix=30,
    )
    data = store.get()
    assert data["milestones"]["node_ready"] == 10
    assert data["milestones"]["completed"] == 30
    serialized = path.read_text(encoding="utf-8")
    assert "PRIVATE_ARTICLE_MARKER" not in serialized
    assert not path.with_suffix(".json.tmp").exists()


def test_store_rejects_unknown_milestone_and_recovers_corruption(tmp_path):
    path = tmp_path / "first-success.json"
    store = FirstSuccessStore(path)
    with pytest.raises(ValueError, match="first_success_milestone_invalid"):
        store.record("article_title")
    path.write_text("not json", encoding="utf-8")
    assert store.get()["milestones"]["completed"] == 0
    path.write_text(json.dumps({"version": "unknown", "completed": True}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="version_unsupported"):
        store.get()
    with pytest.raises(ValueError, match="version_unsupported"):
        store.reset()
    assert path.read_bytes() == before


def test_reset_starts_a_fresh_display_run_without_erasing_other_data(tmp_path):
    store = FirstSuccessStore(tmp_path / "first-success.json")
    store.sync(
        node_ready=True,
        content_ready=True,
        first_item_opened=True,
        first_signal_recorded=True,
        now_unix=10,
    )
    reset = store.reset(now_unix=50)
    assert reset["replay_started_at_unix"] == 50
    assert not any(reset["milestones"].values())


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    app = create_app(RynmeshStore())
    return TestClient(app), app


def _seed_ready_digest(app, *, all_failed: bool = False):
    service = app.state.digest_service
    service._save(
        "sources.json",
        [{
            "id": "source-1",
            "title": "Source",
            "kind": "rss",
            "feed_url": "https://example.test/feed",
            "weight": 1.0,
            "tags": [],
        }],
    )
    service._save(
        "health.json",
        [{
            "id": "source-1",
            "title": "Source",
            "ok": not all_failed,
            "using_cached_items": not all_failed,
            "item_count": 1 if not all_failed else 0,
        }],
    )
    service._save(
        "last_digest.json",
        {
            "schema_version": DIGEST_SCHEMA_VERSION,
            "generated_at_unix": 1,
            "items": [] if all_failed else [{"item_id": "item-1", "title": "Real item"}],
            "sources": [],
        },
    )
    service._save(
        "discovery-state.json",
        {"phase": "error" if all_failed else "ready", "message": "safe"},
    )


def _item(marker: str = "PRIVATE_ARTICLE_MARKER"):
    return {
        "item_id": "item-1",
        "source_id": "source-1",
        "source_title": "Source",
        "source_kind": "rss",
        "title": marker,
        "link": "https://example.test/article",
        "summary": marker,
        "content_kind": "document",
        "content_type": "text/html",
        "tags": ["test"],
        "reasons": ["source you follow"],
    }


def test_first_success_api_tracks_real_open_and_signal(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    initial = client.get("/api/local/first-success").json()
    assert initial["phase"] == "checking_sources"
    assert initial["node_ready"] is True

    _seed_ready_digest(app)
    ready = client.get("/api/local/first-success").json()
    assert ready["phase"] == "ready"
    assert ready["content_ready"] is True
    assert ready["using_cache"] is True

    opened = client.post(
        "/api/local/consumption", json={"item": _item(), "action": "opened"}
    )
    assert opened.status_code == 200
    assert client.get("/api/local/first-success").json()["phase"] == "awaiting_signal"

    bookmarked = client.post(
        "/api/local/consumption", json={"item": _item(), "action": "bookmark"}
    )
    assert bookmarked.status_code == 200
    complete = client.get("/api/local/first-success").json()
    assert complete["phase"] == "completed"
    assert complete["completed"] is True

    milestone_text = (tmp_path / "node" / "first_run" / "state.json").read_text(encoding="utf-8")
    assert "PRIVATE_ARTICLE_MARKER" not in milestone_text


def test_first_success_dismiss_reset_export_and_erase(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    _seed_ready_digest(app)
    client.post("/api/local/consumption", json={"item": _item(), "action": "opened"})
    client.post("/api/local/consumption", json={"item": _item(), "action": "bookmark"})

    dismissed = client.post("/api/local/first-success/dismiss").json()
    assert dismissed["completed"] is True
    assert dismissed["dismissed"] is True
    assert client.get("/api/local/privacy/export").json()["first_success"]["dismissed"] is True

    reset = client.post("/api/local/first-success/reset").json()
    assert reset["phase"] == "ready"
    assert reset["completed"] is False
    assert client.get("/api/local/consumption").json()

    client.post("/api/local/consumption", json={"item": _item(), "action": "opened"})
    client.post("/api/local/consumption", json={"item": _item(), "action": "bookmark"})
    assert client.get("/api/local/first-success").json()["completed"] is True

    erased = client.post("/api/local/privacy/erase", json={"scopes": ["onboarding"]})
    assert erased.status_code == 200
    assert erased.json()["erased"] == ["onboarding"]
    assert client.get("/api/local/consumption").json()


def test_first_success_reports_recoverable_failure_without_model_or_peer(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    _seed_ready_digest(app, all_failed=True)
    failed = client.get("/api/local/first-success").json()
    assert failed["phase"] == "needs_action"
    assert failed["safe_error"] == "discovery_unavailable"
    assert failed["recoverable_actions"] == ["retry_discovery"]
    assert "model" not in failed
    assert "peer" not in failed


def test_first_success_progress_write_failure_does_not_break_consumption(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    _seed_ready_digest(app)

    def fail_record(_milestone):
        raise OSError("PRIVATE_PROGRESS_PATH")

    monkeypatch.setattr(app.state.first_run.store, "record", fail_record)
    response = client.post(
        "/api/local/consumption", json={"item": _item(), "action": "opened"}
    )
    assert response.status_code == 200
    audit = json.dumps(client.get("/api/local/activity").json())
    assert "PRIVATE_PROGRESS_PATH" not in audit
    assert "PRIVATE_ARTICLE_MARKER" not in audit


def test_first_success_feature_flag_can_disable_new_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_FIRST_SUCCESS_V1_ENABLED", "0")
    client, _app = _client(tmp_path, monkeypatch)
    response = client.get("/api/local/first-success")
    assert response.status_code == 404
    assert response.json()["detail"] == "first_success_disabled"


def test_store_preserves_unknown_fields_and_backup_before_corruption_repair(tmp_path):
    path = tmp_path / "state.json"
    store = FirstSuccessStore(path)
    state = store.record("node_ready", now_unix=10)
    state["future_metadata"] = {"enabled": True}
    state["milestones"]["future_step"] = 20
    path.write_text(json.dumps(state), encoding="utf-8")
    store.dismiss(now_unix=30)
    assert store.get()["future_metadata"] == {"enabled": True}
    assert store.get()["milestones"]["future_step"] == 20
    path.write_text("broken", encoding="utf-8")
    assert not store.get()["milestones"]["completed"]
    assert path.with_suffix(".json.corrupt").read_text() == "broken"
    assert json.loads(path.read_text())["version"] == FIRST_SUCCESS_VERSION


def test_legacy_progress_migrates_once_without_losing_fields(tmp_path, monkeypatch):
    home = tmp_path / "node"
    home.mkdir()
    legacy = home / "first-success.json"
    FirstSuccessStore(legacy).dismiss(now_unix=20)
    original = legacy.read_bytes()
    client, app = _client(tmp_path, monkeypatch)
    assert client.get("/api/local/first-success").json()["dismissed"]
    assert legacy.with_suffix(".json.migrated").read_bytes() == original
    app.state.first_run.store.reset(now_unix=30)
    client, _ = _client(tmp_path, monkeypatch)
    assert not client.get("/api/local/first-success").json()["dismissed"]
    assert legacy.read_bytes() == original


def test_unsupported_legacy_progress_does_not_prevent_node_start(tmp_path, monkeypatch):
    home = tmp_path / "node"
    home.mkdir()
    legacy = home / "first-success.json"
    legacy.write_text('{"version":"future"}', encoding="utf-8")
    client, _ = _client(tmp_path, monkeypatch)
    assert client.get("/health").status_code == 200
    response = client.get("/api/local/first-success")
    assert response.status_code == 503
    assert response.json()["detail"] == "first_success_store_unavailable"
    assert not (home / "first_run" / "state.json").exists()


def test_first_run_routes_are_independent_authenticated_and_reinstallable(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI, HTTPException

    from rynmesh.background_workers import BackgroundWorkerRegistry
    from rynmesh.first_run_routes import install_first_run

    app = FastAPI()
    checked = []

    def auth(request):
        checked.append(request.url.path)
        if request.headers.get("x-test-owner") != "yes":
            raise HTTPException(401, "owner_required")

    kwargs = dict(store=SimpleNamespace(home=tmp_path / "owner"), home=tmp_path / "wrong",
                  workers=BackgroundWorkerRegistry(), local_control=auth,
                  discovery=lambda: SimpleNamespace(discovery_status=lambda: {}),
                  consumption=lambda: SimpleNamespace(list=lambda: []),
                  profile=lambda: SimpleNamespace(public=lambda: {}), audit=lambda: None)
    first = install_first_run(app, **kwargs)
    second = install_first_run(app, **kwargs)
    assert first is not second
    assert sum(route.path == "/api/local/first-success" for route in app.routes) == 1
    client = TestClient(app)
    for method, path in [("get", ""), ("post", "/dismiss"), ("post", "/reset")]:
        assert getattr(client, method)("/api/local/first-success" + path).status_code == 401
    result = client.get("/api/local/first-success", headers={"x-test-owner": "yes"})
    assert result.status_code == 200
    assert len(checked) == 4
    assert second.store.path.parent == tmp_path / "owner" / "first_run"
    assert not (tmp_path / "wrong").exists()


def test_completed_milestones_survive_reload_but_content_readiness_is_current(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    _seed_ready_digest(app)
    client.post("/api/local/consumption", json={"item": _item(), "action": "opened"})
    client.post("/api/local/consumption", json={"item": _item(), "action": "bookmark"})
    assert client.get("/api/local/first-success").json()["completed"]
    _seed_ready_digest(app, all_failed=True)
    client, _ = _client(tmp_path, monkeypatch)
    current = client.get("/api/local/first-success").json()
    assert current["completed"]
    assert current["content_ready"] is False
