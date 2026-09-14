import os
import stat
from copy import deepcopy

import pytest

from rynmesh.ai_access.store import AIAccessError, AIAccessStore
from rynmesh.atomic_io import atomic_write_json, read_json

RID = "a" * 32


def setup(tmp_path):
    relationships = {RID: {"relationship_id": RID, "peer_id": "friend-b", "status": "active"}}
    store = AIAccessStore(tmp_path, relationship=lambda rid: deepcopy(relationships.get(rid)))
    return store, relationships


def test_friendship_is_not_permission_and_grants_are_peer_and_service_scoped(tmp_path):
    store, _ = setup(tmp_path)
    permission = {"relationship_id": RID, "revision": 1}
    with pytest.raises(AIAccessError, match="denied"):
        store.authorize("friend-b", "model-x", permission)
    grant = store.set("model-x", RID, allowed=True, expected_revision=0)
    assert grant["effective"]
    assert store.authorize("friend-b", "model-x", permission) == permission
    for peer, service in (("friend-c", "model-x"), ("friend-b", "model-y")):
        with pytest.raises(AIAccessError, match="denied"):
            store.authorize(peer, service, permission)
    with pytest.raises(AIAccessError, match="required"):
        store.authorize("friend-b", "model-x", None)


def test_revoke_regrant_restart_and_new_relationship_never_revive_old_revision(tmp_path):
    store, relationships = setup(tmp_path)
    store.set("model-x", RID, allowed=True, expected_revision=0)
    store.set("model-x", RID, allowed=False, expected_revision=1)
    store.set("model-x", RID, allowed=True, expected_revision=2)
    restarted = AIAccessStore(tmp_path, relationship=lambda rid: relationships.get(rid))
    with pytest.raises(AIAccessError, match="revoked"):
        restarted.authorize("friend-b", "model-x", {"relationship_id": RID, "revision": 1})
    assert restarted.authorize("friend-b", "model-x", {"relationship_id": RID, "revision": 3})["revision"] == 3
    relationships[RID]["status"] = "revoked"
    relationships["b" * 32] = {"relationship_id": "b" * 32, "peer_id": "friend-b", "status": "active"}
    assert restarted.list()[0]["effective"] is False
    with pytest.raises(AIAccessError, match="inactive"):
        restarted.authorize("friend-b", "model-x", {"relationship_id": RID, "revision": 3})
    with pytest.raises(AIAccessError, match="denied"):
        restarted.authorize("friend-b", "model-x", {"relationship_id": "b" * 32, "revision": 3})


def test_revision_conflicts_lost_responses_and_failed_writes(tmp_path, monkeypatch):
    import rynmesh.ai_access.store as module

    store, _ = setup(tmp_path)
    saved = store.set("model-x", RID, allowed=True, expected_revision=0)
    assert store.set("model-x", RID, allowed=True, expected_revision=0) == saved
    with pytest.raises(AIAccessError, match="revision_conflict"):
        store.set("model-x", RID, allowed=False, expected_revision=0)
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(module, "atomic_write_json", fail)
    with pytest.raises(OSError):
        store.set("model-x", RID, allowed=False, expected_revision=1)
    assert store.list()[0]["allowed"] is True


def test_unknown_fields_preserved_and_future_permissions_refused(tmp_path):
    store, _ = setup(tmp_path)
    store.set("model-x", RID, allowed=True, expected_revision=0)
    value = read_json(store.path)
    value["future_annotation"] = "keep"
    row = next(iter(value["grants"].values()))
    row["future_annotation"] = "also keep"
    atomic_write_json(store.path, value)
    store.set("model-x", RID, allowed=False, expected_revision=1)
    updated = read_json(store.path)
    assert updated["future_annotation"] == "keep"
    assert next(iter(updated["grants"].values()))["future_annotation"] == "also keep"
    assert "future_annotation" not in store.list()[0]
    if os.name != "nt":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    updated["version"] = "ryn.ai-access.v999"
    atomic_write_json(store.path, updated)
    before = store.path.read_bytes()
    with pytest.raises(AIAccessError, match="version_unsupported"):
        store.set("model-x", RID, allowed=True, expected_revision=2)
    assert store.path.read_bytes() == before
