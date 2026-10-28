from concurrent.futures import ThreadPoolExecutor

import pytest

from rynmesh.services.consumption import ConsumptionError, ConsumptionStore

ITEM = {"item_id": "mesh:article", "title": "A mesh article", "link": "rynmesh://content/mesh%3Aarticle"}


def test_mesh_reading_and_position_survive_restart_without_duplicate_bookmarks(tmp_path):
    store = ConsumptionStore(tmp_path / "history.json")
    store.record(ITEM, "opened")
    store.record(ITEM, "bookmark")
    store.record(ITEM, "bookmark")
    store.record(ITEM, "progress", progress=0.85)
    store.record(ITEM, "progress", progress=0.4)  # Re-reading an earlier paragraph.
    rows = ConsumptionStore(store.path).list()
    assert len(rows) == 1 and rows[0]["bookmarked"]
    assert rows[0]["progress"] == 0.4
    assert rows[0]["item"]["link"] == ITEM["link"]
    for value in (float("nan"), float("inf")):
        with pytest.raises(ConsumptionError, match="progress_invalid"):
            store.record(ITEM, "progress", progress=value)


def test_concurrent_history_updates_do_not_lose_other_items(tmp_path):
    store = ConsumptionStore(tmp_path / "history.json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda key: store.record({**ITEM, "item_id": str(key),
            "link": f"https://example.test/{key}"}, "bookmark"), range(20)))
    assert len(store.list()) == 20


def test_failed_write_retry_and_corrupt_history_preserve_previous_data(tmp_path, monkeypatch):
    store = ConsumptionStore(tmp_path / "history.json")
    store.record(ITEM, "opened")
    before = store.path.read_bytes()
    write = store._write
    monkeypatch.setattr(store, "_write", lambda data: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.record(ITEM, "bookmark")
    assert store.path.read_bytes() == before
    monkeypatch.setattr(store, "_write", write)
    store.record(ITEM, "bookmark")
    assert len(store.list()) == 1 and store.list()[0]["bookmarked"]
    store.path.write_text('{"incomplete":')
    with pytest.raises(OSError):
        store.record(ITEM, "progress", progress=0.5)
    assert store.path.read_text() == '{"incomplete":'
