import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from test_friends import _pair

from rynmesh.friends import card_cleanup
from rynmesh.friends.card_cleanup import CardCleanup
from rynmesh.friends.service import FriendError
from rynmesh.friends.store import FriendStore


def seeded(tmp_path):
    mesh, alice, bob = _pair(tmp_path)
    mesh.online.remove(alice.endpoint)
    bob.send_content_card(alice.peer_id, {'title': 'Private cleared metadata'}, card_id='a' * 32)
    snapshot = deepcopy(bob.store.card('a' * 32))
    mesh.online.add(alice.endpoint)
    bob.retry_card('a' * 32)
    return alice, bob, snapshot


def test_clear_real_cards_legacy_and_backup_without_revoking_or_losing_saved_data(tmp_path):
    alice, bob, sent = seeded(tmp_path)
    before = alice.store._read(alice.store.state_path, alice.store._empty())
    legacy = alice.store.root / 'content-cards.jsonl'
    backup = alice.store.root / 'state.json.migrated'
    legacy.write_text(json.dumps({'card_id': 'b' * 32, 'card': {'title': 'Legacy private card'}}) + '\n')
    backup.write_text(json.dumps({**before, 'unknown_extension': 'keep this field'}))
    independent = alice.home / 'library-imports' / 'keep.txt'
    independent.parent.mkdir(exist_ok=True)
    independent.write_text('Independent downloaded document')
    cleanup = CardCleanup(alice.store)
    preview = cleanup.preview()
    assert preview['cards'] == preview['legacy_files'] == 2
    assert 'Private' not in str(preview)
    result = cleanup.begin(preview['review_token'])
    assert result['complete'] and not result['remote_confirmed']
    assert alice.content_cards() == [] and legacy.read_bytes() == b''
    cleaned_backup = json.loads(backup.read_text())
    assert cleaned_backup['cards'] == {} and cleaned_backup['unknown_extension'] == 'keep this field'
    for key in ('relationships', 'secrets', 'invites', 'nonces'):
        assert alice.store._read(alice.store.state_path, alice.store._empty())[key] == before[key]
        assert cleaned_backup[key] == before[key]
    assert independent.read_text() == 'Independent downloaded document'
    assert 'Private cleared metadata' not in alice.store.state_path.read_text()
    alice.store = FriendStore(alice.home)
    assert alice.receive_content_card(sent['wire']) == {'card_id': 'a' * 32, 'erased': True}
    assert alice.content_cards() == []
    assert CardCleanup(alice.store).begin(preview['review_token']) == result
    bob.send_content_card(alice.peer_id, {'title': 'A new share'}, card_id='c' * 32)
    assert len(alice.content_cards()) == 1


def test_review_change_and_source_write_failure_preserve_cards(tmp_path, monkeypatch):
    alice, bob, _ = seeded(tmp_path)
    cleanup = CardCleanup(alice.store)
    old = cleanup.preview()
    bob.send_content_card(alice.peer_id, {'title': 'Arrived after review'}, card_id='c' * 32)
    before = alice.store.state_path.read_bytes()
    with pytest.raises(ValueError, match='review_changed'):
        cleanup.begin(old['review_token'])
    assert alice.store.state_path.read_bytes() == before
    preview = cleanup.preview()
    def fail(*args, **kwargs):
        raise OSError('private disk failure')
    monkeypatch.setattr(alice.store, '_write', fail)
    with pytest.raises(OSError):
        cleanup.begin(preview['review_token'])
    assert alice.store.state_path.read_bytes() == before


def test_legacy_failure_restart_rereview_and_old_retry_keep_later_cards(tmp_path, monkeypatch):
    alice, bob, _ = seeded(tmp_path)
    legacy = alice.store.root / 'content-cards.jsonl'
    legacy.write_text(json.dumps({'card_id': 'a' * 32, 'card': {'title': 'old'}}) + '\n')
    cleanup = CardCleanup(alice.store)
    preview = cleanup.preview()
    original = card_cleanup.atomic_write_bytes
    def fail(*args, **kwargs):
        raise OSError('file held open')
    monkeypatch.setattr(card_cleanup, 'atomic_write_bytes', fail)
    with pytest.raises(OSError):
        cleanup.begin(preview['review_token'])
    assert alice.content_cards() == [] and cleanup.preview()['resuming']
    bob.send_content_card(alice.peer_id, {'title': 'Keep later card'}, card_id='c' * 32)
    legacy.write_text(legacy.read_text() + json.dumps({'card_id': 'd' * 32, 'card': {'title': 'Keep new legacy'}}) + '\n')
    monkeypatch.setattr(card_cleanup, 'atomic_write_bytes', original)
    cleanup = CardCleanup(FriendStore(alice.home))
    with pytest.raises(ValueError, match='review_changed'):
        cleanup.begin(preview['review_token'])
    reviewed = cleanup.preview()
    assert reviewed['resuming'] and reviewed['cards'] == 1
    result = cleanup.begin(reviewed['review_token'])
    assert result['complete']
    assert {r['card_id'] for r in alice.content_cards()} == {'c' * 32, 'd' * 32}
    assert cleanup.begin(reviewed['review_token']) == result
    with pytest.raises(ValueError, match='review_changed'):
        cleanup.begin(preview['review_token'])


def test_outgoing_snapshot_and_old_owner_request_do_not_resend_after_cleanup(tmp_path):
    _, bob, sent = seeded(tmp_path)
    cleanup = CardCleanup(bob.store)
    cleanup.begin(cleanup.preview()['review_token'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Cleared card was sent again')
    bob.post_json = forbidden
    with pytest.raises(FriendError, match='friend_card_erased'):
        bob._attempt_delivery(sent['to'], sent)
    with pytest.raises(ValueError, match='friend_card_erased'):
        bob.send_content_card(sent['to'], {'title': 'Private cleared metadata'}, card_id=sent['card_id'])


def test_future_cleanup_format_is_not_overwritten(tmp_path):
    alice, _, _ = seeded(tmp_path)
    data = alice.store._read(alice.store.state_path, alice.store._empty())
    data['card_erasure']['version'] = 999
    alice.store._write(alice.store.state_path, data)
    before = alice.store.state_path.read_bytes()
    with pytest.raises(ValueError, match='version_unsupported'):
        CardCleanup(alice.store).preview()
    assert alice.store.state_path.read_bytes() == before


def test_lost_completion_write_reuses_committed_legacy_file_without_erasing_new_work(tmp_path, monkeypatch):
    alice, bob, _ = seeded(tmp_path)
    legacy = alice.store.root / 'content-cards.jsonl'
    legacy.write_text(json.dumps({'card_id': 'a' * 32, 'card': {'title': 'old'}}) + '\n')
    cleanup = CardCleanup(alice.store)
    reviewed = cleanup.preview()
    original = alice.store._write
    calls = 0
    def lose_receipt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('completion was not committed')
        return original(*args, **kwargs)
    monkeypatch.setattr(alice.store, '_write', lose_receipt)
    with pytest.raises(OSError):
        cleanup.begin(reviewed['review_token'])
    assert legacy.read_bytes() == b''
    monkeypatch.setattr(alice.store, '_write', original)
    bob.send_content_card(alice.peer_id, {'title': 'Keep new work'}, card_id='c' * 32)
    result = CardCleanup(FriendStore(alice.home)).begin(reviewed['review_token'])
    assert result['complete'] and result['cards'] == 1
    assert [row['card_id'] for row in alice.content_cards()] == ['c' * 32]


def test_owner_routes_guard_and_actual_cleanup(tmp_path, monkeypatch):
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore
    monkeypatch.setenv('RYNMESH_AUTO_REGISTER', '0')
    monkeypatch.setenv('RYNMESH_DISABLE_DISCOVERY', '1')
    monkeypatch.setenv('RYNMESH_LOCAL_TOKEN', 'owner')
    monkeypatch.setenv('RYNMESH_MODEL_PROVIDER', 'none')
    app = create_app(RynmeshStore(home=tmp_path / 'node', network_dir=tmp_path / 'network'))
    app.state.friends.service.store.put_card({'card_id': 'f' * 32, 'card': {'title': 'Private owner card'}})
    client = TestClient(app)
    path = '/api/local/friends/card-cleanup'
    assert client.get(path).status_code == client.post(path, json={}).status_code == 403
    owner = {'x-ryn-local-token': 'owner'}
    response = client.get(path, headers=owner)
    assert response.headers['cache-control'] == 'no-store'
    preview = response.json()
    assert preview['cards'] == 1
    assert client.post(path, headers=owner, json={'review_token': preview['review_token']}).json()['complete']
    assert app.state.friends.service.content_cards() == []
    assert client.post(path, headers=owner, content=b'x' * 1025).status_code == 413
