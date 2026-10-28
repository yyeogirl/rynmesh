import time

import pytest
from test_friends import _node, _pair

from rynmesh.ai_access.catalog import PATH, FriendAICatalog
from rynmesh.ai_access.store import AIAccessError, AIAccessStore
from rynmesh.crypto import canonical_json
from rynmesh.llm_package.manifest import LLMPackageManifest


def setup(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    grants = AIAccessStore(alice.store.root.parent, relationship=alice.store.relationship)
    manifest = LLMPackageManifest(package_id="model-x", mode="openai_compatible", public_model_alias="private-model-alias",
                                  base_url="http://127.0.0.1:1").public_dict()
    status = {"configured": True, "service": manifest, "capacity": {"available": 1}, "online": True, "ready": True}
    publisher = FriendAICatalog(friends=lambda: alice, grants=lambda: grants, provider=lambda: status)
    clock = [time.time()]
    consumer = FriendAICatalog(friends=lambda: bob, grants=lambda: None, provider=lambda: None, clock=lambda: clock[0])
    original_post = mesh.post
    captured = []

    def post(endpoint, path, body, headers, **kwargs):
        if path != PATH:
            return original_post(endpoint, path, body, headers, **kwargs)
        relation = alice.verify_request(path=PATH, body=canonical_json(body), headers=headers)
        response = publisher.respond(body, relation)
        captured.append(response)
        return response

    bob.post_json = post
    return mesh, alice, bob, grants, publisher, consumer, clock, captured, post


def test_private_catalog_requires_scoped_grant_and_expires_cached_availability(tmp_path):
    _, alice, bob, grants, _, consumer, clock, captured, _ = setup(tmp_path)
    rid = alice.store.relationship_for_peer(bob.peer_id)["relationship_id"]
    assert consumer.refresh(alice.peer_id)["status"] == "not_authorized"
    grants.set("model-y", rid, allowed=True, expected_revision=0)
    assert consumer.refresh(alice.peer_id)["services"] == []
    grants.set("model-x", rid, allowed=True, expected_revision=0)
    result = consumer.refresh(alice.peer_id)
    assert result["status"] == "authorized"
    row = result["services"][0]
    assert row["peer_id"] == alice.peer_id and row["ai_permission"] == {"relationship_id": rid, "revision": 1}
    assert row["node_messaging_pub"] == bob.store.relationship_for_peer(alice.peer_id)["messaging_pub"]
    assert set(captured[-1]) == {"nonce", "ciphertext"}
    assert b"private-model-alias" not in canonical_json(captured[-1])
    clock[0] += 121
    assert consumer.snapshots()[0]["status"] == "stale"
    assert consumer.records()[0]["online"] is False
    grants.set("model-x", rid, allowed=False, expected_revision=1)
    revoked = consumer.refresh(alice.peer_id)
    assert revoked["status"] == "revoked" and revoked["services"] == []
    assert consumer.records() == []


def test_catalog_rejects_replayed_response_and_forgets_revoked_friend(tmp_path):
    _, alice, bob, grants, _, consumer, _, captured, post = setup(tmp_path)
    rid = alice.store.relationship_for_peer(bob.peer_id)["relationship_id"]
    grants.set("model-x", rid, allowed=True, expected_revision=0)
    consumer.refresh(alice.peer_id)
    replay = captured[-1]
    bob.post_json = lambda *args, **kwargs: replay
    with pytest.raises(AIAccessError, match="unreachable"):
        consumer.refresh(alice.peer_id)
    assert consumer.records() == []
    bob.post_json = post
    consumer.refresh(alice.peer_id)
    bob.revoke(rid, notify=False)
    assert consumer.records() == []


def test_grant_to_one_friend_never_discloses_service_to_another(tmp_path):
    mesh, alice, bob, grants, _, consumer, _, _, post = setup(tmp_path)
    rid = alice.store.relationship_for_peer(bob.peer_id)["relationship_id"]
    grants.set("model-x", rid, allowed=True, expected_revision=0)
    carol = _node(tmp_path, "Carol", 18083, mesh)
    carol.join(alice.create_invite()["invite_uri"])
    carol.post_json = post
    other = FriendAICatalog(friends=lambda: carol, grants=lambda: None, provider=lambda: None)
    assert consumer.refresh(alice.peer_id)["services"]
    assert other.refresh(alice.peer_id)["services"] == []
