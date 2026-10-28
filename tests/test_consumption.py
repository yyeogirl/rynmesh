from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.consumption import (
    _ITEM_FIELDS,
    MAX_HISTORY_BYTES,
    ConsumptionError,
    ConsumptionStore,
)
from rynmesh.store import RynmeshStore

ITEM = {
    "item_id": "item-1",
    "source_id": "source-1",
    "source_title": "Example",
    "source_kind": "news",
    "title": "A useful article",
    "link": "https://example.com/article",
    "content_kind": "document",
    "tags": ["technology"],
    "reasons": ["fresh"],
}


def test_consumption_store_records_history_bookmarks_and_progress(tmp_path):
    store = ConsumptionStore(tmp_path / "consumption.json")
    opened = store.record(ITEM, "opened", now_unix=10)
    assert opened["open_count"] == 1
    assert opened["first_opened_unix"] == 10

    saved = store.record(ITEM, "bookmark", now_unix=11)
    assert saved["bookmarked"] is True
    progress = store.record(ITEM, "progress", progress=0.72, now_unix=12)
    assert progress["progress"] == 0.72
    assert progress["completed"] is False

    completed = store.record(ITEM, "progress", progress=0.96, now_unix=13)
    assert completed["completed"] is True
    assert store.list()[0]["item"]["title"] == "A useful article"


def test_consumption_store_is_bounded_and_can_be_cleared(tmp_path):
    store = ConsumptionStore(tmp_path / "consumption.json", max_items=2)
    for index in range(3):
        store.record(
            {**ITEM, "item_id": f"item-{index}", "link": f"https://example.com/{index}"},
            "opened",
            now_unix=index,
        )
    assert [record["item_id"] for record in store.list()] == ["item-2", "item-1"]
    store.clear()
    assert store.list() == []


def test_consumption_store_rejects_invalid_actions_and_items(tmp_path):
    store = ConsumptionStore(tmp_path / "consumption.json")
    try:
        store.record(ITEM, "unknown")
        raise AssertionError("invalid action accepted")
    except ConsumptionError as exc:
        assert str(exc) == "consumption_action_invalid"
    try:
        store.record({**ITEM, "link": "file:///secret"}, "opened")
        raise AssertionError("invalid link accepted")
    except ConsumptionError as exc:
        assert str(exc) == "consumption_item_link_invalid"


def test_consumption_store_worst_case_stays_under_atomic_cap(tmp_path):
    """`max_items` records, every capped field maxed out, must fit `MAX_HISTORY_BYTES`.

    `ConsumptionStore` writes its whole history as one JSON record via
    `atomic_write_json`, passing its own `MAX_HISTORY_BYTES` cap (sized for
    this store specifically, not the generic `atomic_io.MAX_RECORD_BYTES`
    default). Nothing ties `max_items` to the per-item field caps
    automatically, so this test is the guard: it fills a store to its
    declared limits and asserts the serialized file still fits under this
    store's own budget. Every field `_clean_item` truncates is maxed here,
    including `item_id` (at its real 256-char cap, not a short synthetic id)
    and `link` (at its real 4000-char cap) — both are truncated exactly like
    the other string fields and can reach their caps in production, so
    leaving either short would let this guard pass while understating the
    true worst case. `item_id` also doubles as the record's dict key, so it
    is built as a 256-char string with a unique numeric suffix rather than
    repeating one value, to keep all `max_items` records distinct.
    Re-run this after raising `max_items` or any `_ITEM_FIELDS` truncation
    length in `rynmesh/services/consumption.py`, and raise
    `MAX_HISTORY_BYTES` together with it if the measured worst case grows
    past it.
    """
    store = ConsumptionStore(tmp_path / "consumption.json")
    long_string = "x" * 5000  # longer than the 4000-char per-field cap
    long_tag = "y" * 300  # longer than the 160-char per-tag cap
    many_tags = [long_tag] * 100  # longer than the 64-entry cap
    long_link = "https://" + "z" * 5000  # longer than the 4000-char cap; keeps its prefix once truncated

    # Build the full worst-case history directly (via the real `_clean_item`
    # truncation) and write it in one shot, rather than calling `.record()`
    # `max_items` times: each `.record()` call re-reads and re-serializes the
    # *whole* growing file, so doing this 1000 times over multi-MB payloads
    # would make the test itself needlessly slow without exercising anything
    # `_write`'s single `atomic_write_json` call doesn't already cover.
    records = {}
    for index in range(store.max_items):
        suffix = f"{index:06d}"
        item_id = ("w" * (256 - len(suffix))) + suffix  # exactly 256 chars, unique per index
        raw_item = {"item_id": item_id, "link": long_link}
        for field in _ITEM_FIELDS - {"item_id", "link", "tags", "reasons", "published_unix", "score"}:
            raw_item[field] = long_string
        raw_item["tags"] = many_tags
        raw_item["reasons"] = many_tags
        raw_item["published_unix"] = 1234567890.123456
        raw_item["score"] = 0.999999
        clean_item = ConsumptionStore._clean_item(raw_item)
        assert len(clean_item["item_id"]) == 256
        assert len(clean_item["link"]) == 4000
        records[clean_item["item_id"]] = {
            "item_id": clean_item["item_id"],
            "first_opened_unix": 0.0,
            "last_opened_unix": 0.0,
            "open_count": 1,
            "bookmarked": False,
            "progress": 1.0,
            "completed": True,
            "last_activity_unix": float(index),
            "item": clean_item,
        }
    assert len(records) == store.max_items  # every item_id must be unique
    store._write(records)

    assert len(store.list()) == store.max_items
    raw_bytes = (tmp_path / "consumption.json").read_bytes()
    assert len(raw_bytes) < MAX_HISTORY_BYTES, (
        f"worst-case consumption history ({len(raw_bytes)} bytes) exceeds "
        f"MAX_HISTORY_BYTES ({MAX_HISTORY_BYTES}); lower max_items or the "
        "per-field caps in ConsumptionStore, or raise MAX_HISTORY_BYTES if "
        "the larger history is intentional"
    )


def test_consumption_http_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    client = TestClient(create_app(RynmeshStore()))

    saved = client.post(
        "/api/local/consumption",
        json={"item": ITEM, "action": "bookmark"},
    )
    assert saved.status_code == 200
    assert saved.json()["bookmarked"] is True
    assert client.get("/api/local/consumption").json()[0]["item_id"] == "item-1"

    bad = client.post(
        "/api/local/consumption",
        json={"item": ITEM, "action": "invalid"},
    )
    assert bad.status_code == 400
    assert client.delete("/api/local/consumption").json() == {"ok": True}
    assert client.get("/api/local/consumption").json() == []
