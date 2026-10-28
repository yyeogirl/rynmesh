"""recommendation_service: node content view -> webapp Recommendation[] payload."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.recommendation_service import recommend_from_items
from rynmesh.store import RynmeshStore

NOW = 1_800_000_000.0


def _item(**overrides):
    base = {
        "content_id": "cid_x",
        "title": "A clip",
        "description": "",
        "tags": [],
        "publisher_peer_id": "peer_a",
        "distribution_weight": 1.0,
        "credit_score": 1.0,
        "safety_outcome": "passed",
        "provenance_head_hash": "sha256:head",
        "fetch_status": "discovered",
        "review_basis": "metadata",
        "published": "2027-01-15T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_empty_items_returns_empty():
    assert recommend_from_items([], now_unix=NOW) == []


def test_excludes_local_fetched_and_blocked():
    items = [
        _item(content_id="mine", fetch_status="local"),
        _item(content_id="have", fetch_status="fetched_full"),
        _item(content_id="bad", safety_outcome="blocked"),
        _item(content_id="ok"),
    ]
    recs = recommend_from_items(items, now_unix=NOW)
    assert [rec["contentId"] for rec in recs] == ["ok"]


def test_tag_affinity_ranks_matching_content_higher():
    items = [
        # Owner interest: a fetched item tagged "gardens".
        _item(
            content_id="have",
            fetch_status="fetched_full",
            tags=["gardens"],
            publisher_peer_id="peer_me",
        ),
        _item(
            content_id="other",
            tags=["finance"],
            publisher_peer_id="peer_b",
            distribution_weight=1.0,
        ),
        _item(
            content_id="match",
            tags=["gardens"],
            publisher_peer_id="peer_c",
            distribution_weight=1.0,
        ),
    ]
    recs = recommend_from_items(items, now_unix=NOW)
    assert [rec["contentId"] for rec in recs][0] == "match"
    match_rec = recs[0]
    assert "tag_match" in match_rec["evidence"]
    assert "gardens" in match_rec["reason"]


def test_generic_publish_tags_carry_no_signal():
    items = [
        _item(
            content_id="have",
            fetch_status="fetched_full",
            tags=["ai-generated", "rynmesh", "category:general"],
        ),
        _item(content_id="a", tags=["ai-generated", "rynmesh"], publisher_peer_id="peer_b"),
    ]
    recs = recommend_from_items(items, now_unix=NOW)
    assert recs and "tag_match" not in recs[0]["evidence"]


def test_trust_orders_by_distribution_weight():
    items = [
        _item(
            content_id="weak", publisher_peer_id="peer_w", distribution_weight=0.1, credit_score=0.1
        ),
        _item(
            content_id="strong",
            publisher_peer_id="peer_s",
            distribution_weight=5.0,
            credit_score=5.0,
        ),
    ]
    recs = recommend_from_items(items, now_unix=NOW)
    assert [rec["contentId"] for rec in recs][0] == "strong"
    assert "peer_trust" in recs[0]["evidence"]
    assert "peer_reputation" in recs[0]["evidence"]


def test_query_filters_candidates_and_marks_evidence():
    items = [
        _item(content_id="hit", title="Urban garden tour"),
        _item(content_id="miss", title="Quarterly report"),
    ]
    recs = recommend_from_items(items, now_unix=NOW, query="garden")
    assert [rec["contentId"] for rec in recs] == ["hit"]
    assert "query_match" in recs[0]["evidence"]


def test_limit_priority_and_deterministic_ids():
    items = [
        _item(content_id=f"cid_{i}", publisher_peer_id=f"peer_{i}", distribution_weight=float(i))
        for i in range(1, 10)
    ]
    recs = recommend_from_items(items, now_unix=NOW, limit=4)
    assert len(recs) == 4
    assert all(0.0 <= rec["priority"] <= 1.0 for rec in recs)
    assert recs[0]["priority"] == 1.0
    ids = [rec["id"] for rec in recs]
    assert len(set(ids)) == 4
    again = recommend_from_items(items, now_unix=NOW, limit=4)
    assert [rec["id"] for rec in again] == ids


def test_external_slate_represents_each_available_content_format():
    kinds = ["document", "report", "audio", "video", "image"]
    items = [
        _item(
            content_id=f"digest:{kind}",
            content_kind=kind,
            external=True,
            source_platform=kind,
            publisher_peer_id=f"discovery:{kind}",
            distribution_weight=10.0 if kind in {"document", "report"} else 0.2,
            credit_score=0.0,
            safety_outcome="unscanned",
            provenance_head_hash=None,
        )
        for kind in kinds
    ]
    items.extend(
        _item(
            content_id=f"digest:article-{index}",
            content_kind="document",
            external=True,
            source_platform="news",
            publisher_peer_id=f"discovery:article-{index}",
            distribution_weight=20.0,
            credit_score=0.0,
        )
        for index in range(30)
    )

    recommendations = recommend_from_items(items, now_unix=NOW, limit=5)

    assert {item["item"]["content_kind"] for item in recommendations} == set(kinds)


def test_newcomer_gets_exploration_slot():
    # Six strong-publisher items would fill the slate; the zero-credit
    # newcomer still gets the reserved exploration slot (vision OQ-2).
    items = [
        _item(
            content_id=f"cid_{i}",
            publisher_peer_id="peer_big",
            distribution_weight=9.0,
            credit_score=9.0,
        )
        for i in range(6)
    ]
    items.append(
        _item(
            content_id="cid_new",
            publisher_peer_id="peer_new",
            distribution_weight=0.0,
            credit_score=0.0,
        )
    )
    recs = recommend_from_items(items, now_unix=NOW, limit=6)
    picked = {rec["contentId"] for rec in recs}
    assert "cid_new" in picked
    newcomer = next(rec for rec in recs if rec["contentId"] == "cid_new")
    assert "diversity" in newcomer["evidence"]


def test_uncertainty_novelty_and_review_basis():
    items = [
        _item(content_id="have", fetch_status="fetched_full", publisher_peer_id="peer_known"),
        _item(
            content_id="warned",
            safety_outcome="flagged",
            publisher_peer_id="peer_known",
            provenance_head_hash=None,
        ),
        _item(
            content_id="fresh",
            safety_outcome="unscanned",
            publisher_peer_id="peer_new2",
            credit_score=0.0,
        ),
    ]
    recs = {rec["contentId"]: rec for rec in recommend_from_items(items, now_unix=NOW)}
    assert "flagged" in recs["warned"]["uncertainty"]
    assert recs["warned"]["novelty"] is None
    assert "provenance_signed" not in recs["warned"]["evidence"]
    assert recs["fresh"]["uncertainty"] == "Not yet safety-scanned."
    assert recs["fresh"]["novelty"] is not None
    assert all(rec["review_basis"] == "metadata" for rec in recs.values())


def test_external_recommendation_has_versioned_evidence_and_original_citation():
    item = _item(
        content_id="digest:public",
        external=True,
        external_url="https://example.com/original",
        source_peer_name="Example Feed",
        source_platform="rss",
        source_description="A feed-provided description.",
        safety_outcome="unscanned",
        provenance_status="unsigned",
        provenance_head_hash=None,
    )

    packet = recommend_from_items([item], now_unix=NOW)[0]["evidence_packet"]

    assert packet["version"] == 1
    assert packet["review_basis"] == "metadata"
    assert packet["source"] == {
        "name": "Example Feed",
        "platform": "rss",
        "url": "https://example.com/original",
    }
    assert packet["citations"] == [
        {
            "kind": "original",
            "label": "Original at Example Feed",
            "url": "https://example.com/original",
        }
    ]
    assert "Ryn reviewed metadata only, not the full content." in packet["limitations"]
    assert "This item has not completed a local safety scan." in packet["limitations"]
    assert "The source provenance is not fully verified." in packet["limitations"]


# ---------------------------------------------------------------- endpoint ----


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    # TestClient's synthetic host is not loopback; bypass the local-only guard.
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    store = RynmeshStore()
    return TestClient(create_app(store)), store


def test_endpoint_empty_store_returns_no_fabricated_recommendations(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    response = client.post("/api/local/recommendations", json={"limit": 6})
    assert response.status_code == 200
    recommendations = response.json()
    assert recommendations == []
    assert client.post("/api/local/recommendations/feedback",
        json={"contentId": "starter:agents", "action": "more"}).status_code == 404


def test_endpoint_prefers_real_digest_content_and_accepts_feedback(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    feed = b"""<rss version="2.0"><channel><title>Public Picks</title><item>
      <title>A real article</title><link>https://example.com/real</link>
      <description>Useful reporting</description>
      <pubDate>Wed, 29 Jul 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>"""
    service = client.app.state.digest_service
    service.fetcher = lambda *_: feed
    service.add_source("https://example.com/feed", tags=["technology", "platform:rss"])
    service.proactive_refresh(now_unix=1_800_000_000.0)

    recommendations = client.post("/api/local/recommendations", json={"limit": 6}).json()
    assert recommendations[0]["contentId"].startswith("digest:")
    assert recommendations[0]["item"]["external"] is True
    assert recommendations[0]["item"]["external_url"] == "https://example.com/real"
    assert "public discovery catalog" in recommendations[0]["reason"].lower()
    assert "peer_trust" not in recommendations[0]["evidence"]
    assert "newcomer" not in recommendations[0]["reason"].lower()
    assert not any(item["item"].get("starter") for item in recommendations)

    feedback = client.post(
        "/api/local/recommendations/feedback",
        json={"contentId": recommendations[0]["contentId"], "action": "more"},
    )
    assert feedback.status_code == 200
    shared_profile = client.get("/api/local/recommendations/profile").json()
    assert recommendations[0]["contentId"] in shared_profile["feedback"]


def test_endpoint_forwards_query_limit_and_items(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    captured = {}

    def fake(items, *, now_unix, query="", limit=6, profile=None):
        captured.update(
            items=list(items), query=query, limit=limit, now_unix=now_unix, profile=profile
        )
        return [{"id": "rec_sentinel"}]

    from rynmesh import recommendation_service

    monkeypatch.setattr(recommendation_service, "recommend_from_items", fake)
    response = client.post("/api/local/recommendations", json={"query": "garden", "limit": 3})
    assert response.status_code == 200
    assert response.json() == [{"id": "rec_sentinel"}]
    assert captured["query"] == "garden"
    assert captured["limit"] == 3
    assert captured["now_unix"] > 0
    assert isinstance(captured["items"], list)
    assert isinstance(captured["profile"], dict)


def test_endpoint_tolerates_empty_body(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    response = client.post("/api/local/recommendations")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_profile_direction_and_feedback_apply_to_feed_content_without_starter_fallback(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    service = client.app.state.digest_service
    service.fetcher = lambda *_: b'<rss version="2.0"><channel><title>Public Picks</title><item><title>Open source agents</title><link>https://example.com/agents</link><description>Open source agents research</description></item></channel></rss>'
    service.add_source("https://example.com/feed", tags=["ai-agents", "platform:github"])
    service.proactive_refresh(now_unix=NOW)
    profile = client.patch(
        "/api/local/recommendations/profile",
        json={"direction": "open source agents", "platforms": ["github"], "topics": ["ai-agents"]},
    )
    assert profile.status_code == 200
    assert profile.json()["platforms"] == ["github"]
    assert client.app.state.digest_service.get_steering()["text"] == "open source agents"

    before = client.post("/api/local/recommendations", json={"limit": 8}).json()
    assert len(before) == 1
    assert before[0]["contentId"].startswith("digest:")
    target = before[0]["contentId"]
    feedback = client.post(
        "/api/local/recommendations/feedback",
        json={"contentId": target, "action": "hide"},
    )
    assert feedback.status_code == 200
    after = client.post("/api/local/recommendations", json={"limit": 8}).json()
    assert after == []
