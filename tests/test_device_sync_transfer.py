"""Real encrypted sources, pairing and wire batches with injected delivery faults."""
from copy import deepcopy

import pytest
from test_ask_history import sample
from test_device_sync_pairing import Devices
from test_device_sync_reading import ITEM

from rynmesh.ask_ryn.store import ConversationStore
from rynmesh.device_sync.records import SyncError
from rynmesh.device_sync.store import ReplicaStore
from rynmesh.device_sync.transfer import DeviceTransfer
from rynmesh.services.consumption import ConsumptionStore


class Mesh:
    def __init__(self, home, scopes=None):
        self.devices = Devices(home)
        self.a, self.b, self.pair_id = self.devices.pair(scopes)
        self.wires, self.transfers, self.readers, self.histories = [], {}, {}, {}
        self.fail, self.after_receive = False, None
        self.left, self.right = self.install(self.a), self.install(self.b)

    def install(self, node):
        home = self.devices.home / node.identity['name']
        reader = self.readers.setdefault(node.identity['actor'], ConsumptionStore(home / 'consumption.json'))
        history = self.histories.setdefault(node.identity['actor'], ConversationStore(home / 'ask', node.messaging_key))
        transfer = DeviceTransfer(pairing=lambda: node, replica=ReplicaStore(home, messaging_key=node.messaging_key),
            reading=lambda: reader, conversations=lambda: history, post_json=self.post)
        self.transfers[node.identity['endpoint']] = transfer
        return transfer

    def post(self, endpoint, path, wire):
        assert path.endswith('/batch')
        self.wires.append(deepcopy(wire))
        response = self.transfers[endpoint].receive(wire)
        if self.after_receive:
            self.after_receive()
        if self.fail:
            raise TimeoutError('private response lost')
        return response

    def reader(self, node):
        return self.readers[node.identity['actor']]

    def history(self, node):
        return self.histories[node.identity['actor']]


def test_three_scopes_merge_original_sources_and_confirm_only_after_ack(tmp_path):
    mesh = Mesh(tmp_path)
    a, b, pair_id = mesh.a, mesh.b, mesh.pair_id
    mesh.reader(a).record(ITEM, 'bookmark')
    mesh.reader(a).record(ITEM, 'progress', progress=.6)
    mesh.reader(b).record({**ITEM, 'item_id': 'other', 'title': 'B keeps this'}, 'bookmark')
    mesh.history(a).save(sample(), expected_revision=0)
    assert mesh.left.status(pair_id)['pending'] == 3
    assert mesh.left.status(pair_id)['state'] == 'pending'
    for scope in ['bookmarks', 'reading', 'conversations']:
        wire = mesh.left.prepare(pair_id, scope)
        response = mesh.right.receive(wire)
        assert mesh.left.status(pair_id)['state'] == 'pending'
        mesh.left.acknowledge(wire, response)
    assert mesh.left.status(pair_id)['state'] == 'confirmed'
    assert mesh.reader(b).sync_read('reading', ITEM['item_id'])['value']['progress'] == .6
    assert mesh.history(b).get(sample()['id'])['serviceKey'] == sample()['serviceKey']
    assert {row['item_id'] for row in mesh.reader(b).list()} == {ITEM['item_id'], 'other'}
    mesh.right.send(pair_id, 'bookmarks')
    assert mesh.reader(a).sync_read('bookmarks', 'other')['bookmarked']
    assert sample()['messages'][0]['content'] not in str(mesh.wires)


def test_unselected_sources_are_not_enabled_read_or_sent(tmp_path):
    mesh = Mesh(tmp_path, ['bookmarks'])
    mesh.reader(mesh.a).record(ITEM, 'progress', progress=.8)
    mesh.reader(mesh.a).record(ITEM, 'bookmark')
    mesh.history(mesh.a).save(sample(), expected_revision=0)
    before = mesh.history(mesh.a).path.read_bytes()
    mesh.left.send(mesh.pair_id, 'bookmarks')
    assert mesh.history(mesh.a).path.read_bytes() == before
    assert not mesh.history(mesh.b).path.exists()
    with pytest.raises(SyncError):
        mesh.reader(mesh.b).sync_read('reading', ITEM['item_id'])
    with pytest.raises(SyncError, match='sync_scope_denied'):
        mesh.left.prepare(mesh.pair_id, 'conversations')


def test_receiver_rejects_signed_unapproved_scope_before_source_opt_in(tmp_path):
    from rynmesh.device_sync import records
    mesh = Mesh(tmp_path, ['bookmarks'])
    wire = mesh.left.prepare(mesh.pair_id, 'bookmarks')
    row = mesh.left._pair(mesh.pair_id)
    payload = mesh.left._outgoing(row, wire)
    payload['scope'] = 'reading'
    record = records.write('reading', ITEM['item_id'], records.empty(), mesh.a.identity['actor'],
        {'item': ITEM, 'progress': .5, 'completed': False, 'content_version': ''})
    payload['records'] = [{'scope': 'reading', 'id': ITEM['item_id'], 'record': record}]
    forbidden = mesh.left._seal(row, payload)
    with pytest.raises(SyncError, match='sync_scope_denied'):
        mesh.right.receive(forbidden)
    assert not mesh.reader(mesh.b).path.exists()
    assert not mesh.history(mesh.b).path.exists()


def test_lost_response_restart_and_new_local_change_do_not_duplicate_or_false_confirm(tmp_path):
    mesh = Mesh(tmp_path, ['bookmarks'])
    mesh.reader(mesh.a).record(ITEM, 'bookmark')
    mesh.fail = True
    with pytest.raises(TimeoutError):
        mesh.left.send(mesh.pair_id, 'bookmarks')
    assert mesh.left.status(mesh.pair_id)['state'] == 'waiting'
    assert mesh.reader(mesh.b).sync_read('bookmarks', ITEM['item_id'])['bookmarked']
    mesh.left = mesh.install(mesh.devices.node('A'))
    mesh.fail = False
    mesh.after_receive = lambda: mesh.reader(mesh.a).record(ITEM, 'unbookmark')
    assert mesh.left.send(mesh.pair_id, 'bookmarks')['outdated'] == 1
    assert mesh.left.status(mesh.pair_id)['pending'] == 1
    mesh.after_receive = None
    mesh.left.send(mesh.pair_id, 'bookmarks')
    assert mesh.left.status(mesh.pair_id)['state'] == 'confirmed'
    assert not mesh.reader(mesh.b).sync_read('bookmarks', ITEM['item_id'])['bookmarked']


def test_pause_resume_invalidates_old_packet_and_old_receipt(tmp_path):
    mesh = Mesh(tmp_path, ['bookmarks'])
    mesh.reader(mesh.a).record(ITEM, 'bookmark')
    wire = mesh.left.prepare(mesh.pair_id, 'bookmarks')
    response = mesh.right.receive(wire)
    mesh.b.configure(mesh.pair_id, expected_revision=1, scopes=['bookmarks'], paused=True)
    with pytest.raises(SyncError, match='sync_paused'):
        mesh.right.receive(wire)
    mesh.b.exchange_policy(mesh.pair_id)
    assert mesh.left.status(mesh.pair_id)['state'] == 'paused'
    mesh.b.configure(mesh.pair_id, expected_revision=2, scopes=['bookmarks'], paused=False)
    mesh.b.exchange_policy(mesh.pair_id)
    with pytest.raises(SyncError, match='sync_policy_changed'):
        mesh.right.receive(wire)
    with pytest.raises(SyncError, match='sync_policy_changed'):
        mesh.left.acknowledge(wire, response)
    assert mesh.left.status(mesh.pair_id)['pending'] == 1
    mesh.left.send(mesh.pair_id, 'bookmarks')
    assert mesh.left.status(mesh.pair_id)['state'] == 'confirmed'


def test_remove_during_network_wait_prevents_ack_and_new_pair_resends(tmp_path):
    mesh = Mesh(tmp_path, ['bookmarks'])
    old = mesh.pair_id
    mesh.reader(mesh.a).record(ITEM, 'bookmark')
    mesh.after_receive = lambda: mesh.a.revoke(old, expected_revision=1)
    with pytest.raises(SyncError, match='sync_device_not_active'):
        mesh.left.send(old, 'bookmarks')
    mesh.a.retry_removal(old)
    new = mesh.b.join(mesh.a.create_invite(['bookmarks'])['uri'], ['bookmarks'])['id']
    mesh.a.approve(new, review_token=new, scopes=['bookmarks'])
    mesh.b.retry(new)
    mesh.after_receive = None
    assert mesh.left.status(new)['pending'] == 1
    mesh.left.send(new, 'bookmarks')
    with pytest.raises(SyncError, match='sync_device_not_active'):
        mesh.right.receive(mesh.wires[0])
    assert mesh.left.status(new)['state'] == 'confirmed'


@pytest.mark.parametrize('location', ['source', 'replica'])
def test_failed_receiver_commit_never_returns_receipt(tmp_path, monkeypatch, location):
    from rynmesh.device_sync import store as replica_store
    from rynmesh.services import consumption
    mesh = Mesh(tmp_path, ['bookmarks'])
    mesh.reader(mesh.a).record(ITEM, 'bookmark')
    wire = mesh.left.prepare(mesh.pair_id, 'bookmarks')
    # Finish opt-in first so the fault exercises the receiving commit.
    mesh.right.status(mesh.pair_id)
    module = consumption if location == 'source' else replica_store
    with monkeypatch.context() as patch:
        patch.setattr(module, 'atomic_write_json', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):
            mesh.right.receive(wire)
    assert mesh.left.status(mesh.pair_id)['pending'] == 1
    mesh.left.acknowledge(wire, mesh.right.receive(wire))
    assert mesh.left.status(mesh.pair_id)['state'] == 'confirmed'


def test_forged_wrong_request_and_wrong_scope_receipts_fail_closed(tmp_path):
    mesh = Mesh(tmp_path, ['bookmarks'])
    mesh.reader(mesh.a).record(ITEM, 'bookmark')
    first = mesh.left.prepare(mesh.pair_id, 'bookmarks')
    second = mesh.left.prepare(mesh.pair_id, 'bookmarks')
    response = mesh.right.receive(first)
    with pytest.raises(SyncError, match='sync_receipt_invalid'):
        mesh.left.acknowledge(second, response)
    row, payload = mesh.left._open(response, reply=True)
    payload['receipts'] = []
    remote = mesh.right._pair(mesh.pair_id)
    wrong = mesh.right._seal(remote, payload, reply=True)
    with pytest.raises(SyncError, match='sync_receipt_invalid'):
        mesh.left.acknowledge(first, wrong)
    assert mesh.left.status(mesh.pair_id)['pending'] == 1


def test_scope_change_requires_both_peers_and_new_confirmation(tmp_path):
    mesh = Mesh(tmp_path, ['bookmarks'])
    mesh.reader(mesh.a).record(ITEM, 'bookmark')
    mesh.reader(mesh.a).record(ITEM, 'progress', progress=.4)
    mesh.left.send(mesh.pair_id, 'bookmarks')
    mesh.a.configure(mesh.pair_id, expected_revision=1, scopes=['bookmarks', 'reading'], paused=False)
    mesh.a.exchange_policy(mesh.pair_id)
    with pytest.raises(SyncError, match='sync_scope_denied'):
        mesh.left.prepare(mesh.pair_id, 'reading')
    mesh.b.configure(mesh.pair_id, expected_revision=1, scopes=['bookmarks', 'reading'], paused=False)
    mesh.b.exchange_policy(mesh.pair_id)
    assert mesh.left.status(mesh.pair_id)['pending'] == 2
    assert mesh.left.status(mesh.pair_id)['last_success_at'] is None
    mesh.left.send(mesh.pair_id, 'bookmarks')
    mesh.left.send(mesh.pair_id, 'reading')
    assert mesh.left.status(mesh.pair_id)['state'] == 'confirmed'


def test_disabling_conversation_sync_preserves_history_and_resumes_without_duplicates(tmp_path):
    mesh = Mesh(tmp_path, ['conversations'])
    original = sample()
    mesh.history(mesh.a).save(original, expected_revision=0)
    mesh.left.send(mesh.pair_id, 'conversations')
    retained = mesh.history(mesh.b).get(original['id'])
    old_wire = mesh.left.prepare(mesh.pair_id, 'conversations')

    mesh.a.configure(mesh.pair_id, expected_revision=1, scopes=[], paused=False)
    mesh.a.exchange_policy(mesh.pair_id)
    current = mesh.history(mesh.a).get(original['id'])
    message = {'id': 'completed-while-disabled', 'role': 'assistant', 'status': 'complete',
               'content': 'A completed answer kept locally while conversation sync is disabled.',
               'createdAt': current['updatedAt']}
    mesh.history(mesh.a).save({**current, 'messages': [*current['messages'], message]},
                             expected_revision=current['revision'])
    with pytest.raises(SyncError, match='sync_scope_denied'):
        mesh.left.send(mesh.pair_id, 'conversations')
    with pytest.raises(SyncError):
        mesh.right.receive(old_wire)
    assert mesh.history(mesh.b).get(original['id']) == retained

    mesh.a.configure(mesh.pair_id, expected_revision=2, scopes=['conversations'], paused=False)
    mesh.a.exchange_policy(mesh.pair_id)
    mesh.left.send(mesh.pair_id, 'conversations')
    updated = mesh.history(mesh.b).get(original['id'])
    assert updated['messages'] == [*retained['messages'], message]
    assert updated['serviceKey'] == retained['serviceKey']
    assert mesh.left.status(mesh.pair_id)['state'] == 'confirmed'
    mesh.left.send(mesh.pair_id, 'conversations')
    assert mesh.history(mesh.b).get(original['id']) == updated


def test_conflicts_remain_visible_in_sources_and_transfer_summary(tmp_path):
    mesh = Mesh(tmp_path)
    mesh.history(mesh.a).save(sample(), expected_revision=0)
    mesh.left.send(mesh.pair_id, 'conversations')
    for node, text in ((mesh.a, 'A branch'), (mesh.b, 'B branch')):
        history = mesh.history(node)
        current = history.get(sample()['id'])
        history.save({**current, 'messages': [*current['messages'], {'id': text.replace(' ', '-'), 'role': 'assistant', 'status': 'complete',
            'content': text, 'createdAt': current['updatedAt']}]}, expected_revision=current['revision'])
    mesh.left.send(mesh.pair_id, 'conversations')
    mesh.right.send(mesh.pair_id, 'conversations')
    assert len(mesh.history(mesh.a).sync_conflicts()) == 1
    assert mesh.left.status(mesh.pair_id)['state'] == 'conflict'
