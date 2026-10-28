import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from rynmesh.atomic_io import atomic_write_json, read_json
from rynmesh.local_search import index as module
from rynmesh.local_search.index import CHANNEL, VERSION, LocalSearchIndex, SearchError
from rynmesh.services import peer_box


def document(identifier="article:1", *, text="春天城市漫步 Python guide", **changes):
    return {"id": identifier, "title": "城市漫步", "text": text, "source": "Local journal",
            "timestamp": 100, "kinds": ["saved", "history", "share"], "friend_ids": ["friend-a"],
            "targets": [{"label": "Read", "href": "/item/one"}], **changes}


def fixture(tmp_path, rows=None):
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.x25519")
    records = rows if rows is not None else [document()]
    return LocalSearchIndex(tmp_path / "local-search", messaging_key=key, source=lambda: records), records, key


def test_multilingual_search_and_identity_deduplication(tmp_path):
    engine, rows, _ = fixture(tmp_path, [document(), document("message:1", kinds=["chat"]), document("message:2", kinds=["chat"])])
    assert engine.query("")["results"] == []
    assert engine.query("城市")["partial"] is True
    engine.rebuild()
    result = engine.query("城市 Python")
    assert result["total"] == 3  # Different messages must not merge by identical text.
    article = next(row for row in result["results"] if row["id"] == "article:1")
    assert article["kinds"] == ["saved", "history", "share"]
    snippet = article["snippet"]
    assert [snippet["text"][start:end] for start, end in snippet["matches"]] == ["城市", "Python"]
    assert engine.query("PYTHON", kind="saved")["total"] == 1
    assert engine.query("城市", friend_id="different")["total"] == 0
    assert engine.query("城市", after=101)["total"] == 0
    assert engine.query("城市", before=100, source="Local journal")["total"] == 3
    assert engine.query("城市", source="Other")["total"] == 0
    rows[0]["text"] = "Straße 😀道路"
    engine.rebuild()
    snippet = engine.query("STRASSE", kind="saved")["results"][0]["snippet"]
    assert snippet["text"][slice(*snippet["matches"][0])] == "Straße"
    snippet = engine.query("道路", kind="saved")["results"][0]["snippet"]
    assert snippet["offset_unit"] == "unicode_codepoints"
    assert snippet["text"][slice(*snippet["matches"][0])] == "道路"


def test_stale_index_cannot_disclose_deleted_revoked_or_modified_content(tmp_path):
    engine, rows, _ = fixture(tmp_path, [document("copy"), document("protected"), document("edited")])
    engine.rebuild()
    rows.pop(1)  # Source authorization no longer permits the protected share.
    rows[1]["text"] = "Replacement body"
    result = engine.query("Python")
    assert [row["id"] for row in result["results"]] == ["copy"]
    assert result["partial"] is True
    assert engine.query("Replacement")["total"] == 0
    engine.rebuild()
    assert engine.query("Replacement")["total"] == 1
    rows.clear()
    assert engine.query("Python")["total"] == 0
    assert engine.query("Replacement")["total"] == 0


def test_encrypted_restart_corruption_rebuild_and_future_version_preservation(tmp_path):
    marker = "private_phrase_秘密_92746"
    engine, rows, key = fixture(tmp_path, [document(text=marker)])
    engine.rebuild()
    for path in engine.root.iterdir():
        if path.is_file():
            assert marker.encode() not in path.read_bytes()
    if os.name != "nt":
        assert engine.path.stat().st_mode & 0o777 == 0o600
    restarted = LocalSearchIndex(engine.root, messaging_key=key, source=lambda: rows)
    assert restarted.query(marker)["total"] == 1
    assert marker not in json.dumps(restarted.status())
    engine.path.write_bytes(b"{damaged")
    damaged = LocalSearchIndex(engine.root, messaging_key=key, source=lambda: rows)
    assert damaged.status()["state"] == "needs_rebuild"
    assert damaged.query(marker)["partial"]
    assert damaged.rebuild()
    assert rows[0]["text"] == marker
    assert LocalSearchIndex(engine.root, messaging_key=key, source=lambda: rows).query(marker)["total"] == 1
    envelope = read_json(engine.path)
    data = json.loads(peer_box.open_sealed(key, peer_box.public_key_b64(key), envelope["nonce"], envelope["ciphertext"], info=CHANNEL))
    data["future_extension"] = {"retain": True}
    nonce, ciphertext = peer_box.seal(key, peer_box.public_key_b64(key), json.dumps(data).encode(), info=CHANNEL)
    atomic_write_json(engine.path, {**envelope, "nonce": nonce, "ciphertext": ciphertext, "extension": 42})
    rows.append(document("new"))
    damaged.rebuild()
    envelope = read_json(engine.path)
    data = json.loads(peer_box.open_sealed(key, peer_box.public_key_b64(key), envelope["nonce"], envelope["ciphertext"], info=CHANNEL))
    assert envelope["extension"] == 42 and data["future_extension"] == {"retain": True}
    atomic_write_json(engine.path, {**envelope, "version": "ryn.local-search.v999"})
    before = engine.path.read_bytes()
    future = LocalSearchIndex(engine.root, messaging_key=key, source=lambda: rows)
    assert future.status()["state"] == "unsupported"
    with pytest.raises(SearchError, match="version_unsupported"):
        future.rebuild()
    assert before == engine.path.read_bytes()


def test_query_and_failed_rebuild_do_not_log_private_errors(tmp_path, caplog):
    engine, _, _ = fixture(tmp_path)
    engine.rebuild()
    marker = "PRIVATE_BODY_不应记录"
    def failed():
        raise OSError(marker)
    engine.source = failed
    for operation in (engine.rebuild, lambda: engine.query(marker)):
        with pytest.raises(SearchError) as error:
            operation()
        assert marker not in str(error.value)
    assert marker not in json.dumps(engine.status()) and marker not in caplog.text


@pytest.mark.parametrize("force", [False, True])
def test_rebuild_in_progress_is_visible_and_never_serves_removed_snippets(tmp_path, force):
    engine, rows, _ = fixture(tmp_path)
    engine.rebuild()
    entered, release = threading.Event(), threading.Event()
    def source():
        if threading.current_thread().name.startswith("builder"):
            entered.set()
            assert release.wait(5)
        return rows
    engine.source = source
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="builder") as pool:
        future = pool.submit(engine.rebuild, force=force)
        assert entered.wait(3)
        try:
            assert engine.status()["state"] == ("building" if force else "ready")
            assert engine.query("Python")["partial"] is force
            with pytest.raises(SearchError, match="busy"):
                engine.rebuild()
            rows.clear()
            result = engine.query("Python")
            assert result["partial"] and result["total"] == 0
        finally:
            release.set()
        future.result(timeout=5)
    assert engine.status()["state"] == "ready"


def test_explicit_rebuild_regenerates_unchanged_index_but_periodic_check_does_not(tmp_path):
    engine, _, _ = fixture(tmp_path)
    engine.rebuild()
    before = engine.path.read_bytes()
    assert engine.rebuild() is False
    assert engine.path.read_bytes() == before
    assert engine.rebuild(force=True) is True
    assert engine.path.read_bytes() != before
    assert engine.query("Python")["total"] == 1


def test_pagination_rejects_changed_results_instead_of_skipping_or_repeating(tmp_path):
    engine, rows, _ = fixture(tmp_path, [document(str(i), timestamp=100 + i) for i in range(45)])
    engine.rebuild()
    first = engine.query("Python", sort="recent")
    second = engine.query("Python", sort="recent", cursor=first["next_cursor"])
    third = engine.query("Python", sort="recent", cursor=second["next_cursor"])
    ids = [row["id"] for page in (first, second, third) for row in page["results"]]
    assert ids == [str(i) for i in reversed(range(45))]
    assert third["next_cursor"] == ""
    rows.pop()
    with pytest.raises(SearchError, match="results_changed"):
        engine.query("Python", sort="recent", cursor=first["next_cursor"])


def test_invalid_filters_and_cursors_do_not_fall_back_to_unfiltered_results(tmp_path):
    engine, _, _ = fixture(tmp_path)
    engine.rebuild()
    for options in ({"kind": "unknown"}, {"after": None}, {"after": float("nan")},
                    {"before": -1}, {"after": 2, "before": 1}, {"limit": True},
                    {"cursor": "not-a-cursor"}, {"sort": "unknown"}):
        with pytest.raises(SearchError):
            engine.query("Python", **options)


def test_source_bounds_and_pathological_postings_fallback(tmp_path, monkeypatch):
    engine, rows, _ = fixture(tmp_path)
    monkeypatch.setattr(module, "MAX_POSTINGS", 1)
    engine.rebuild()
    assert engine.postings is None and engine.query("Python")["total"] == 1
    monkeypatch.setattr(module, "MAX_DOCUMENTS", 1)
    before = engine.path.read_bytes()
    rows.append(document("too-many"))
    with pytest.raises(SearchError, match="index_limit"):
        engine.rebuild()
    assert engine.path.read_bytes() == before
    rows.pop()
    rows[0]["targets"][0]["href"] = "//untrusted.example"
    with pytest.raises(SearchError, match="source_invalid"):
        engine.rebuild()


def test_ten_thousand_record_query_measurement(tmp_path, record_property):
    rows = [document(str(i), title=f"城市漫步 {i}", text=f"离线阅读 Python guide {i} 中文检索 testing",
                     source=f"Source {i % 10}", kinds=[sorted(module.KINDS)[i % 4]], timestamp=i) for i in range(10_000)]
    engine, _, _ = fixture(tmp_path, rows)
    started = time.perf_counter()
    engine.rebuild()
    cold = time.perf_counter() - started
    durations = []
    for query in ["城市", "Python", "中文检索", "阅读 guide", "testing"] * 4:
        started = time.perf_counter()
        result = engine.query(query)
        durations.append(time.perf_counter() - started)
        assert result["total"] == 10_000 and len(result["results"]) == 20 and not result["partial"]
    p95 = sorted(durations)[18]
    record_property("search_reference_rows", 10_000)
    record_property("search_cold_build_seconds", cold)
    record_property("search_query_p95_seconds", p95)
    # This core measurement includes fresh source validation, but not the four
    # production store adapters or HTTP/UI. It is not full SEARCH10 acceptance.
    assert p95 < 2
    assert engine.status()["version"] == VERSION
