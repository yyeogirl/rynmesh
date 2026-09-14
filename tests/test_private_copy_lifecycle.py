import json

import pytest
from test_friends import _pair

from rynmesh.friends.content import FriendContent
from rynmesh.friends.service import FriendError
from rynmesh.services.consumption import ConsumptionStore
from rynmesh.services.library_imports import LibraryImportError, LibraryImportStore


def shared_copy(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    content = FriendContent(store=None, imports=LibraryImportStore(bob.home / "library-imports"),
                            cache=lambda: None, consumption=lambda: ConsumptionStore(bob.home / "reading.json"))
    bob.import_content = content.import_card
    bob.import_generation = content.imports.generation
    bob.verify_import = lambda key: content.imports.body(key.removeprefix("import:"))
    alice.resolve_content = lambda key: {"data": b"A verifiable private copy", "filename": "copy.txt", "mime": "text/plain"}
    card = alice.send_content_card(bob.peer_id, {"library_id": "fixture", "title": "Private reading"})
    return mesh, alice, bob, content, card["card_id"]


def test_clear_during_download_prevents_late_file_resurrection(tmp_path):
    _, _, bob, content, card_id = shared_copy(tmp_path)
    post = bob.post_json
    def clear_before_response(endpoint, path, body, headers, **kwargs):
        result = post(endpoint, path, body, headers, **kwargs)
        content.imports.remove()
        return result
    bob.post_json = clear_before_response
    with pytest.raises(LibraryImportError, match="cancelled_by_cleanup"):
        bob.fetch_content_card(card_id)
    assert content.imports.list() == []
    assert list(content.imports.root.glob("imp_*/original.*")) == []
    assert bob.store.card(card_id)["fetch_state"] == "available"
    bob.post_json = post
    assert bob.fetch_content_card(card_id)["sha256_verified"]


def test_damaged_copy_requires_explicit_repair_and_preserves_unknown_metadata(tmp_path):
    _, alice, bob, content, card_id = shared_copy(tmp_path)
    fetched = bob.fetch_content_card(card_id)
    import_id = fetched["library_id"].removeprefix("import:")
    record = content.imports.get(import_id)
    metadata = content.imports._directory(import_id) / "metadata.json"
    metadata.write_text(json.dumps({**record, "future_note": {"keep": True}}))
    content.imports._blob(record).write_bytes(b"damaged")
    with pytest.raises(FriendError, match="friend_copy_unavailable"):
        bob.fetch_content_card(card_id)
    assert bob.fetch_content_card(card_id, repair=True)["library_id"] == fetched["library_id"]
    assert content.imports.read_bytes(import_id) == b"A verifiable private copy"
    repaired = content.imports.get(import_id)
    assert repaired["future_note"] == {"keep": True}
    assert repaired["created_at_unix"] == record["created_at_unix"]
    assert metadata.with_name("metadata.json.repaired").exists()
    alice.revoke(alice.list_friends()[0]["relationship_id"])
    assert bob.fetch_content_card(card_id)["already_fetched"]
    with pytest.raises(FriendError, match="active_friend_required"):
        bob.fetch_content_card(card_id, repair=True)


def test_removed_copy_can_be_explicitly_downloaded_again_without_duplicate_bookmark(tmp_path):
    _, _, bob, content, card_id = shared_copy(tmp_path)
    fetched = bob.fetch_content_card(card_id)
    import_id = fetched["library_id"].removeprefix("import:")
    assert content.imports.remove(import_id)["removed"] == 1
    with pytest.raises(FriendError, match="friend_copy_unavailable"):
        bob.fetch_content_card(card_id)
    assert bob.fetch_content_card(card_id, repair=True)["library_id"] == fetched["library_id"]
    assert len(content.consumption().list()) == 1


def test_cleanup_rejects_future_versions_and_paths_outside_the_managed_root(tmp_path):
    store = LibraryImportStore(tmp_path / "imports")
    record = store.save(b"Keep me", filename="copy.txt", mime="text/plain")
    metadata = store._directory(record["import_id"]) / "metadata.json"
    metadata.write_text(json.dumps({**record, "version": "future"}))
    protected = tmp_path / "owner-file.txt"
    protected.write_bytes(b"not an import")
    with pytest.raises(LibraryImportError, match="version_unsupported"):
        store.remove()
    with pytest.raises(LibraryImportError):
        store.remove("../owner-file.txt")
    assert protected.read_bytes() == b"not an import"
    assert metadata.exists()


def test_explicit_repair_recovers_corrupt_metadata_but_retains_its_backup(tmp_path):
    store = LibraryImportStore(tmp_path)
    record = store.save(b"Recover metadata", filename="copy.txt", mime="text/plain")
    metadata = store._directory(record["import_id"]) / "metadata.json"
    metadata.write_text("broken")
    restored = store.save(b"Recover metadata", filename="copy.txt", mime="text/plain", repair=True)
    assert restored["import_id"] == record["import_id"]
    assert metadata.with_name("metadata.json.repaired").read_text() == "broken"


def test_explicit_cleanup_can_remove_corrupt_metadata_without_contacting_a_friend(tmp_path):
    store = LibraryImportStore(tmp_path)
    record = store.save(b"Erase damaged copy", filename="copy.txt", mime="text/plain")
    directory = store._directory(record["import_id"])
    (directory / "metadata.json").write_text("corrupt")
    assert store.remove(record["import_id"])["removed"] == 1
    assert not directory.exists()
