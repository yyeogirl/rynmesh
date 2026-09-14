"""Per-source recovery must preserve healthy content and suppress error secrets."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.digest import DigestError, DigestService
from rynmesh.store import RynmeshStore

FEED = b'<rss><channel><title>Reading</title><item><title>Read me</title><link>https://example.test/read</link><description>Actual feed content.</description></item></channel></rss>'


def sources(service):
    service._save("sources.json", [
        {"id": key, "title": key, "kind": "rss", "feed_url": f"https://{key}.test/feed", "tags": [], "weight": 1}
        for key in ("one", "two")
    ])


def test_single_source_retry_preserves_other_source_and_failure_history(tmp_path):
    calls = []
    fail = False

    def fetch(url, timeout):
        calls.append(url)
        if fail:
            raise OSError("SECRET_QUERY_TOKEN private/path")
        return FEED

    service = DigestService(tmp_path, fetcher=fetch)
    sources(service)
    assert all(row["status"] == "not_checked" for row in service.source_health())
    service.refresh()
    before = service.source_health()
    calls.clear()
    fail = True
    service.refresh(source_id="one")
    after = service.source_health()
    assert calls == ["https://one.test/feed"]
    assert after[1] == before[1]
    assert after[0]["status"] == "cached"
    assert after[0]["consecutive_failures"] == 1
    assert after[0]["last_success_unix"] == before[0]["last_success_unix"]
    assert after[0]["item_count"] == before[0]["item_count"]
    assert "SECRET_QUERY_TOKEN" not in json.dumps(service.discovery_status())
    assert "SECRET_QUERY_TOKEN" not in (service.dir / "health.json").read_text()
    fail = False
    service.refresh(source_id="one")
    assert service.source_health()[0]["consecutive_failures"] == 0
    assert service.source_health()[0]["status"] == "healthy"
    with pytest.raises(DigestError, match="source_not_found"):
        service.refresh(source_id="missing")


def test_overlapping_refresh_returns_busy_without_second_fetch(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        started.set()
        assert release.wait(5)
        return FEED

    service = DigestService(tmp_path, fetcher=fetch)
    sources(service)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.refresh, source_id="one")
        try:
            assert started.wait(3)
            with pytest.raises(DigestError, match="discovery_busy"):
                service.refresh(source_id="two")
        finally:
            release.set()
        future.result()
    assert calls == ["https://one.test/feed"]


def test_source_retry_api_rebuilds_recommendations_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    app = create_app(RynmeshStore())
    app.state.digest_service.fetcher = lambda url, timeout: FEED
    sources(app.state.digest_service)
    client = TestClient(app)
    response = client.post("/api/local/sources/one/retry")
    assert response.status_code == 200
    assert response.json()["digest"]["items"]
    health = client.get("/api/local/sources/health").json()
    assert health[0]["status"] == "healthy"
    assert health[1]["status"] == "not_checked"
    assert client.post("/api/local/sources/missing/retry").status_code == 404
    rebooted = TestClient(create_app(RynmeshStore()))
    assert rebooted.get("/api/local/sources/health").json() == health
    assert rebooted.get("/api/local/first-success").json()["phase"] == "ready"
