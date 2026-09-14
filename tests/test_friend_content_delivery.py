from test_friend_mailbox import mailboxes
from test_friends import _pair

from rynmesh.friends.store import FriendStore


def test_card_mailbox_receipt_and_repeated_delivery_preserve_downloaded_state(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    alice_mail, bob_mail = mailboxes(tmp_path, alice, bob)
    mesh.online.clear()
    card = {"title": "A shared article", "source_url": "https://example.test/article", "kind": "article"}
    sent = alice.send_content_card(bob.peer_id, card, card_id="b" * 32)
    assert sent["delivery_state"] == "mailbox" and not sent["delivered"]
    assert bob_mail.poll_once() == 1
    received = bob.content_cards()[0]
    assert received["card"]["title"] == card["title"]
    assert received["fetch_state"] == "metadata_only"
    assert alice_mail.poll_once() == 1
    assert alice.content_cards()[0]["delivery_state"] == "delivered"
    again = alice.send_content_card(bob.peer_id, card, card_id="b" * 32)
    assert again["delivered"]
    assert len(alice.content_cards()) == len(bob.content_cards()) == 1


def test_duplicate_card_does_not_erase_download_and_collision_is_rejected(tmp_path):
    import pytest
    mesh, alice, bob = _pair(tmp_path)
    captures = []
    original = alice.post_json
    def post(endpoint, path, body, headers, **kwargs):
        captures.append(body)
        return original(endpoint, path, body, headers, **kwargs)
    alice.post_json = post
    alice.send_content_card(bob.peer_id, {"title": "Read once"}, card_id="c" * 32)
    bob.store.patch_card("c" * 32, {"fetch_state": "fetched", "fetched_library_id": "import:local"})
    bob.receive_content_card(captures[0])
    assert bob.store.card("c" * 32)["fetched_library_id"] == "import:local"
    assert bob.store.card("c" * 32)["fetch_state"] == "fetched"
    original_record = bob.store.card("c" * 32)
    with pytest.raises(ValueError, match="friend_card_id_conflict"):
        bob.store.put_card({**original_record, "from": "another-peer"})
    assert FriendStore(bob.home).card("c" * 32) == original_record
