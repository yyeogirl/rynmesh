"""Measure SEARCH10 using 10,000 synthetic records in the real node stores.

Use a new, empty --home (the script refuses an existing directory). The data
is synthetic, seeded through store APIs, and never sent to a remote service.
--serve keeps that node open for browser checks after writing the measurement.
This is local HTTP/ASGI evidence, not packaged-desktop acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path


def configure(home: Path, port: int):
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_AUTO_REGISTER="0", RYNMESH_DISABLE_DISCOVERY="1",
        RYNMESH_MODEL_PROVIDER="none", RYNMESH_REGISTRY_URL="", RYNMESH_REGISTRY_DIR=str(home / "network" / "registry"),
        RYNMESH_RELAY_URL="", RYNMESH_LLM_RELAY_URL="", RYNMESH_LLM_SERVICE_MANIFEST="",
        RYNMESH_FRIEND_ENDPOINT=f"http://127.0.0.1:{port}", RYNMESH_FRIEND_ALLOW_LOOPBACK="1",
        RYNMESH_WEBUI_DIR=str(Path(__file__).resolve().parents[1] / "webapp" / "dist"))


def create(home: Path):
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore
    return create_app(RynmeshStore(home=home, network_dir=home / "network", node_name="Search scale acceptance"))


def seed(app, home: Path):
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore
    friend = RynmeshStore(home=home / "synthetic-friend", network_dir=home / "network", node_name="Synthetic friend")
    key = peer_box.load_or_create_messaging_key(friend.home / "messaging.x25519")
    rid = "9" * 32
    stamp = "2026-09-11T00:00:00+00:00"
    shared = "城市漫步 Python guide 离线阅读 中文检索 testing"
    app.state.friends.service.store.put_relationship({"relationship_id": rid, "peer_id": friend.peer_id,
        "node_name": "Synthetic friend", "endpoint": "http://127.0.0.1:19999", "created_at": stamp,
        "status": "active", "permissions": ["friend.message", "friend.content"],
        "messaging_pub": peer_box.public_key_b64(key)}, os.urandom(32))
    for i in range(1000):
        item = {"item_id": f"scale-article-{i}", "title": f"{shared} article {i}", "source_title": "Scale journal",
                "link": f"https://example.test/scale/{i}", "content_kind": "article"}
        app.state.consumption_store.record(item, "bookmark", now_unix=1789084800 + i)
        app.state.consumption_store.record(item, "opened", now_unix=1789084800 + i)
        app.state.reader_cache.put(item["link"], {"blocks": [{"text": f"{shared} 本地正文 {i}"}]}, now=1)
    print("seeded saved/history: 1000", flush=True)
    service = app.state.friends.service
    for i in range(500):
        card_id, message_id = f"{i:032x}", f"{i + 1000:032x}"
        card = service._clean_card({"title": f"{shared} card {i}", "source": "Scale friend journal", "kind": "article"})
        service.store.put_card({"card_id": card_id, "card": card, "relationship_id": rid, "from": friend.peer_id,
            "to": service.peer_id, "dir": "in", "created_at": stamp, "fetch_state": "metadata_only"})
        service.messages.append(friend.peer_id, {"msg_id": message_id, "text": f"{shared} message {i}",
            "dir": "in", "from": friend.peer_id, "to": service.peer_id, "kind": "text", "ts": stamp, "delivered": True})
    print("seeded friend cards/messages: 1000", flush=True)
    for i in range(8):
        app.state.ask_ryn.conversations.save({"id": f"scale-conversation-{i}", "title": f"{shared} conversation {i}",
            "serviceKey": "scale-provider::scale-model", "serviceName": "Scale model", "providerPeerId": "scale-provider",
            "networkId": "scale", "createdAt": stamp, "updatedAt": stamp,
            "messages": [{"id": f"message-{j}", "role": "user" if j % 2 == 0 else "assistant", "status": "complete",
                          "content": f"{shared} archive {i}:{j}", "createdAt": stamp} for j in range(1000)]}, expected_revision=0)
    app.state.first_run.store.dismiss()
    print("seeded Ask messages: 8000", flush=True)


def measure(app) -> dict:
    from fastapi.testclient import TestClient
    existing_cache = app.state.local_search.index.path.exists()
    start = time.perf_counter()
    app.state.local_search.index.rebuild(force=True)
    cold = time.perf_counter() - start
    durations, partial = [], 0
    queries = ["城市", "Python", "中文检索", "阅读 guide", "testing"] * 4
    with TestClient(app) as client:
        for number, query in enumerate(queries):
            start = time.perf_counter()
            response = client.post("/api/local/search/query", json={"query": query})
            durations.append(time.perf_counter() - start)
            response.raise_for_status()
            result = response.json()
            assert result["total"] == 10_000 and len(result["results"]) == 20
            partial += int(result["partial"])
            print(f"query {number + 1}/20: {durations[-1]:.3f}s", flush=True)
        # Walk one complete result set and check identities, not text equality.
        identities, cursor = [], ""
        while True:
            result = client.post("/api/local/search/query", json={"query": "testing", "limit": 100, "cursor": cursor}).json()
            identities.extend(row["id"] for row in result["results"])
            cursor = result["next_cursor"]
            if not cursor:
                break
        assert len(identities) == len(set(identities)) == 10_000
    return {"recorded_at": datetime.now(timezone.utc).isoformat(), "scope": "Real node stores and Owner ASGI HTTP with live background workers; no TCP/browser or packaged desktop timing",
        "platform": platform.platform(), "python": platform.python_version(), "cpu_threads": os.cpu_count(),
        "record_count": 10_000, "distribution": {"saved_and_history": 1000, "friend_cards": 500, "friend_messages": 500, "ask_messages": 8000},
        ("forced_rebuild_seconds" if existing_cache else "cold_build_seconds"): cold,
        "existing_cache_at_start": existing_cache, "first_page_size": 20, "query_seconds": durations,
        "query_p95_seconds": sorted(durations)[18], "p95_target_seconds": 2,
        "partial_responses": partial, "pagination_count": len(identities), "pagination_unique_count": len(set(identities)),
        "pagination_identity_sha256": hashlib.sha256("\n".join(identities).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--reuse", action="store_true", help="Reuse only a dedicated scale acceptance home, without reseeding")
    parser.add_argument("--port", type=int, default=18852)
    args = parser.parse_args()
    if args.reuse:
        if not args.home.resolve().name.startswith("rynmesh-search-scale-") or not (args.home / "local-search" / "index.json").is_file():
            raise SystemExit("Reuse requires a completed dedicated scale acceptance home.")
    elif args.home.exists():
        raise SystemExit("Refusing an existing node home; choose a new acceptance directory.")
    configure(args.home, args.port)
    app = create(args.home)
    if not args.reuse:
        seed(app, args.home)
    result = measure(app)
    from rynmesh.atomic_io import atomic_write_json
    atomic_write_json(args.output, result)
    print(json.dumps({"query_p95_seconds": result["query_p95_seconds"], "pagination_count": result["pagination_count"]}), flush=True)
    if args.serve:
        import uvicorn
        uvicorn.run(create(args.home), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
