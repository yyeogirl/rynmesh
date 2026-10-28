import json

import pytest

from rynmesh.services.library_imports import LibraryImportError, LibraryImportStore


def test_private_document_uses_bounded_extraction_and_is_idempotent_after_restart(tmp_path):
    store = LibraryImportStore(tmp_path / "imports")
    data = "# 私人阅读\r\n\r\n重新启动仍然可以阅读。".encode()
    source = {"peer_id": "friend", "card_id": "card", "title": "Shared reading"}
    record = store.save(data, filename="../../reading.md", mime="text/markdown", source=source)
    assert record["filename"] == "reading.md"
    restarted = LibraryImportStore(store.root)
    assert restarted.save(data, filename="reading.md", mime="text/markdown", source=source) == record
    assert restarted.read_bytes(record["import_id"]) == data
    assert "\r" not in restarted.body(record["import_id"])["text"]
    assert len(restarted.list()) == 1


def test_import_crash_before_metadata_can_retry_without_deleting_other_work(tmp_path, monkeypatch):
    import rynmesh.services.library_imports as module
    store = LibraryImportStore(tmp_path)
    other = tmp_path / ".imp_other.tmp"
    other.mkdir()
    (other / "inflight").write_bytes(b"other transfer")
    original = module.atomic_write_json
    def fail_metadata(path, value, **kwargs):
        if path.name == "metadata.json":
            raise OSError("disk full")
        return original(path, value, **kwargs)
    monkeypatch.setattr(module, "atomic_write_json", fail_metadata)
    with pytest.raises(OSError):
        store.save(b"An interrupted import", filename="article.txt", mime="text/plain")
    assert store.list() == []
    monkeypatch.setattr(module, "atomic_write_json", original)
    record = LibraryImportStore(tmp_path).save(b"An interrupted import", filename="article.txt", mime="text/plain")
    assert store.body(record["import_id"])["text"] == "An interrupted import"
    assert (other / "inflight").read_bytes() == b"other transfer"


def test_corrupt_bytes_or_future_metadata_are_not_reported_as_readable(tmp_path):
    store = LibraryImportStore(tmp_path)
    record = store.save(b"Original content", filename="article.txt", mime="text/plain")
    directory = store._directory(record["import_id"])
    (directory / record["blob_name"]).write_bytes(b"Tampered content")
    with pytest.raises(LibraryImportError, match="hash_mismatch"):
        store.body(record["import_id"])
    path = directory / "metadata.json"
    path.write_text(json.dumps({**record, "version": "future"}))
    before = path.read_bytes()
    with pytest.raises(LibraryImportError, match="version_unsupported"):
        store.save(b"Original content", filename="article.txt", mime="text/plain")
    assert path.read_bytes() == before


def test_rejects_mislabelled_pdf_before_extraction(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("MIME check must happen before parsing")
    monkeypatch.setattr("rynmesh.services.library_imports.extract_document", unexpected)
    with pytest.raises(LibraryImportError, match="mime_mismatch"):
        LibraryImportStore(tmp_path).save(b"%PDF-malformed", filename="article.txt", mime="text/plain")
