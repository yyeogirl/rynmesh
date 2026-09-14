"""Encrypted source history, real run archiving, branches and erased tombstones."""
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_ask_history import sample
from test_ask_runs import setup as run_setup

from rynmesh.ask_ryn import store as history_storage
from rynmesh.ask_ryn.runs import AskRunService
from rynmesh.ask_ryn.store import ConversationError, ConversationStore
from rynmesh.atomic_io import atomic_write_json, read_json
from rynmesh.device_sync import records
from rynmesh.device_sync import store as replica_storage
from rynmesh.device_sync.conversation_bridge import ConversationBridge
from rynmesh.device_sync.records import SyncError
from rynmesh.device_sync.store import ReplicaStore


def device(path, *, history=None, enable=True):
    history = history or ConversationStore(path / 'ask', X25519PrivateKey.generate())
    if enable:
        history.enable_sync()
    return ConversationBridge(history, ReplicaStore(path, messaging_key=history.key))


def deliver(a, b):
    batch = a.pending(b.replica.actor)
    receipts = b.receive(batch['records'])
    a.acknowledge(b.replica.actor, receipts)
    return receipts


def append(history, identifier, suffix):
    row = history.get(identifier)
    message = {'id': 'message-' + suffix, 'role': 'assistant', 'content': 'Answer ' + suffix,
               'createdAt': row['updatedAt'], 'status': 'complete'}
    return history.save({**row, 'messages': [*row['messages'], message]}, expected_revision=row['revision'])


def test_opt_in_backup_remains_encrypted_and_retains_unknown_local_fields(tmp_path):
    a = device(tmp_path / 'a', enable=False)
    a.source.save(sample(), expected_revision=0)
    envelope, data = a.source._read()
    envelope['extension'] = {'preserve': True}
    data['extension'] = {'preserve': True}
    data['conversations'][sample()['id']]['credential'] = 'private credential'
    a.source._write(envelope, data)
    original = a.source.path.read_bytes()
    a.source.enable_sync()
    assert a.source.path.with_name('history.json.migrated').read_bytes() == original
    assert sample()['title'] not in a.source.path.read_text()
    assert sample()['messages'][0]['content'] not in a.source.path.read_text()
    envelope, data = a.source._read()
    assert envelope['extension'] == data['extension'] == {'preserve': True}
    assert data['conversations'][sample()['id']]['credential'] == 'private credential'
    a.source.enable_sync()
    assert a.source.path.with_name('history.json.migrated').read_bytes() == original
    assert 'private credential' not in str(a.source.sync_export())
    b = device(tmp_path / 'b')
    deliver(a, b)
    assert b.source.get(sample()['id'])['serviceKey'] == sample()['serviceKey']
    assert b.source.get(sample()['id'])['messages'] == sample()['messages']
    assert 'runs' not in b.source._read()[1]


def test_same_save_after_response_loss_does_not_advance_sync_clock(tmp_path):
    a = device(tmp_path)
    first = a.source.save(sample(), expected_revision=0)
    snapshot = a.source.sync_export()
    assert a.source.save(sample(), expected_revision=0) == first
    assert a.source.sync_export() == snapshot
    a.source.save_draft('unassigned private draft', expected_revision=0)
    a.source.save({**first, 'draft': 'conversation private draft'}, expected_revision=1)
    assert a.source.sync_export() == snapshot
    assert 'private draft' not in str(snapshot)


def test_parallel_conversations_keep_branches_and_third_device_never_picks_one(tmp_path):
    a, b, c = [device(tmp_path / letter) for letter in ('a', 'b', 'c')]
    a.source.save(sample(), expected_revision=0)
    deliver(a, b)
    append(a.source, sample()['id'], 'a')
    append(b.source, sample()['id'], 'b')
    deliver(a, b)
    deliver(b, a)
    assert a.source.get(sample()['id'])['messages'][-1]['content'] == 'Answer a'
    assert b.source.get(sample()['id'])['messages'][-1]['content'] == 'Answer b'
    issue = a.source.sync_conflicts()[0]
    assert issue['common_messages'] == sample()['messages']
    assert len(issue['branches']) == 2
    assert {row['value']['serviceKey'] for row in issue['branches']} == {sample()['serviceKey']}
    deliver(a, c)
    assert c.source.list() == []
    assert len(c.source.sync_conflicts()[0]['branches']) == 2
    # Continuing the existing local branch retains the competing one.
    append(a.source, sample()['id'], 'a2')
    assert len(a.source.sync_conflicts()[0]['branches']) == 2


def test_delete_race_recovery_requires_new_identity_and_reviewed_revision(tmp_path):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.save(sample(), expected_revision=0)
    deliver(a, b)
    append(b.source, sample()['id'], 'b')
    a.source.remove(sample()['id'], expected_revision=1)
    deliver(b, a)
    deliver(a, b)
    assert a.source.list() == b.source.list() == []
    issue = a.source.sync_conflicts()[0]
    assert issue['deleted'] and len(issue['recovery']) == 1
    choice = issue['recovery'][0]
    with pytest.raises(SyncError, match='new_identity'):
        a.source.sync_restore(issue['id'], choice_id=choice['choice_id'], new_id=issue['id'], expected_revision=issue['revision'])
    with pytest.raises(SyncError, match='revision_conflict'):
        a.source.sync_restore(issue['id'], choice_id=choice['choice_id'], new_id='recovered', expected_revision='0' * 64)
    result = a.source.sync_restore(issue['id'], choice_id=choice['choice_id'], new_id='recovered', expected_revision=issue['revision'])
    assert result['id'] == 'recovered' and result['messages'][-1]['content'] == 'Answer b'
    assert result['serviceKey'] == sample()['serviceKey']
    assert a.source.sync_restore(issue['id'], choice_id=choice['choice_id'], new_id='recovered', expected_revision=issue['revision']) == result
    deliver(a, b)
    assert [row['id'] for row in b.source.list()] == ['recovered']
    with pytest.raises(ConversationError, match='deleted'):
        a.source.save(sample(), expected_revision=0)


def test_erase_removes_recovery_text_and_old_packets_cannot_restore_it(tmp_path):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.save(sample(), expected_revision=0)
    deliver(a, b)
    append(b.source, sample()['id'], 'b')
    stale = b.source.sync_export()
    a.source.remove(sample()['id'], expected_revision=1)
    a.source.sync_receive(stale)
    issue = a.source.sync_conflicts()[0]
    a.source.sync_erase(issue['id'], expected_revision=issue['revision'])
    a.source.sync_receive(stale)
    assert a.source.sync_conflicts() == [] and a.source.list() == []
    state = a.source._read()[1]['device_sync']
    assert 'Answer b' not in str(state) and sample()['messages'][0]['content'] not in str(state)
    assert state['entities'][issue['id']]['record']['erased']
    deliver(a, b)
    assert b.source.list() == [] and b.source.sync_conflicts() == []


def deleted_branch(tmp_path):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.save(sample(), expected_revision=0)
    deliver(a, b)
    append(b.source, sample()['id'], 'b')
    a.source.remove(sample()['id'], expected_revision=1)
    deliver(b, a)
    issue = a.source.sync_conflicts()[0]
    request = {'choice_id': issue['recovery'][0]['choice_id'], 'expected_revision': issue['revision']}
    return a, b, issue['id'], request


def test_discard_recovery_preserves_independent_copies_and_survives_restart_replay(tmp_path):
    a, b, identifier, request = deleted_branch(tmp_path)
    stale = b.source.sync_export()
    copy = a.source.sync_restore(identifier, new_id='independent', **request)
    issue = a.source.sync_conflicts()[0]
    result = a.source.sync_discard_recovery(identifier, review_token=issue['discard_token'])
    assert result['erased'] and result['deleted']
    assert a.source.sync_conflicts() == []
    assert a.source.get(copy['id']) == copy
    restarted = ConversationStore(a.source.root, a.source.key)
    assert restarted.sync_discard_recovery(identifier, review_token=issue['discard_token']) == result
    restarted.sync_receive(stale)
    assert restarted.sync_conflicts() == []
    assert [row['id'] for row in restarted.list()] == [copy['id']]
    deliver(a, b)
    b.source.sync_receive(stale)
    assert b.source.sync_conflicts() == []
    assert [row['id'] for row in b.source.list()] == [copy['id']]
    exported = next(row for row in restarted.sync_export() if row['id'] == identifier)
    assert exported['record']['erased'] and all(head['value'] is None for head in exported['record']['heads'])
    assert 'discarded_review' not in str(exported)


def test_discard_review_covers_local_draft_and_rejects_live_conversation(tmp_path):
    a, _, identifier, _ = deleted_branch(tmp_path)
    issue = a.source.sync_conflicts()[0]
    envelope, data = a.source._read()
    data['device_sync']['entities'][identifier]['local_draft'] = {**sample(), 'draft': 'Unsent local revision'}
    a.source._write(envelope, data, capture_sync=False)
    before = a.source.path.read_bytes()
    with pytest.raises(SyncError, match='revision_conflict'):
        a.source.sync_discard_recovery(identifier, review_token=issue['discard_token'])
    assert a.source.path.read_bytes() == before
    current = a.source.sync_conflicts()[0]
    assert current['discard_token'] != issue['discard_token']
    a.source.sync_discard_recovery(identifier, review_token=current['discard_token'])
    assert 'Unsent local revision' not in str(a.source._read()[1])
    a.source.save({**sample(), 'id': 'alive'}, expected_revision=0)
    state = a.source._sync_state(a.source._read()[1])
    before = a.source.path.read_bytes()
    with pytest.raises(SyncError, match='sync_recovery_not_deleted'):
        a.source.sync_discard_recovery('alive', review_token=state.recovery_review('alive'))
    assert a.source.path.read_bytes() == before


def test_discard_failure_keeps_recovery_for_retry(tmp_path, monkeypatch):
    a, _, identifier, _ = deleted_branch(tmp_path)
    issue = a.source.sync_conflicts()[0]
    before = a.source.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(history_storage, 'atomic_write_json', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('full')))
        with pytest.raises(OSError):
            a.source.sync_discard_recovery(identifier, review_token=issue['discard_token'])
    assert a.source.path.read_bytes() == before
    assert a.source.sync_discard_recovery(identifier, review_token=issue['discard_token'])['erased']


def test_deleted_recovery_copy_requires_explicit_replacement_and_restart_retry(tmp_path):
    a, b, identifier, request = deleted_branch(tmp_path)
    first = a.source.sync_restore(identifier, new_id='first-copy', **request)
    a.source.remove(first['id'], expected_revision=first['revision'])
    before = a.source.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_conversation_deleted'):
        a.source.sync_restore(identifier, new_id='first-copy', **request)
    assert a.source.path.read_bytes() == before
    second = a.source.sync_restore(identifier, new_id='second-copy', replaces='first-copy', **request)
    assert second['serviceKey'] == first['serviceKey']
    assert second['providerPeerId'] == first['providerPeerId']
    assert second['networkId'] == first['networkId']
    assert second['messages'] == first['messages']
    restarted = ConversationStore(a.source.root, a.source.key)
    assert restarted.sync_restore(identifier, new_id='second-copy', replaces='first-copy', **request) == second
    assert [row['id'] for row in restarted.list()] == ['second-copy']
    assert {identifier, 'first-copy'} <= restarted._read()[1]['tombstones'].keys()
    deliver(a, b)
    assert [row['id'] for row in b.source.list()] == ['second-copy']
    restarted.remove(second['id'], expected_revision=second['revision'])
    with pytest.raises(ConversationError, match='ask_conversation_deleted'):
        restarted.sync_restore(identifier, new_id='second-copy', replaces='first-copy', **request)
    third = restarted.sync_restore(identifier, new_id='third-copy', replaces='second-copy', **request)
    assert third['messages'] == first['messages']
    assert [row['id'] for row in restarted.list()] == ['third-copy']


def test_replacement_rejects_live_copy_wrong_intent_and_reused_identity(tmp_path):
    a, _, identifier, request = deleted_branch(tmp_path)
    first = a.source.sync_restore(identifier, new_id='first-copy', **request)
    before = a.source.path.read_bytes()
    with pytest.raises(SyncError, match='sync_restore_copy_not_deleted'):
        a.source.sync_restore(identifier, new_id='second-copy', replaces='first-copy', **request)
    assert a.source.path.read_bytes() == before
    a.source.remove(first['id'], expected_revision=first['revision'])
    before = a.source.path.read_bytes()
    for replaces, new_id, extra, error in (
        ('unknown-copy', 'second-copy', {}, 'identity_conflict'),
        ('first-copy', 'second-copy', {'choice_id': 'another-choice'}, 'identity_conflict'),
        ('first-copy', 'first-copy', {}, 'new_identity_required'),
        (identifier, 'second-copy', {}, 'new_identity_required'),
    ):
        with pytest.raises(SyncError, match=error):
            a.source.sync_restore(identifier, new_id=new_id, replaces=replaces, **{**request, **extra})
        assert a.source.path.read_bytes() == before


def test_replacement_failed_write_preserves_retry_and_stale_review_is_rejected(tmp_path, monkeypatch):
    a, _, identifier, request = deleted_branch(tmp_path)
    first = a.source.sync_restore(identifier, new_id='first-copy', **request)
    a.source.remove(first['id'], expected_revision=first['revision'])
    before = a.source.path.read_bytes()
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError('simulated full disk')
        patch.setattr(history_storage, 'atomic_write_json', fail)
        with pytest.raises(OSError):
            a.source.sync_restore(identifier, new_id='second-copy', replaces='first-copy', **request)
    assert a.source.path.read_bytes() == before
    second = a.source.sync_restore(identifier, new_id='second-copy', replaces='first-copy', **request)
    a.source.remove(second['id'], expected_revision=second['revision'])
    a.source.sync_erase(identifier, expected_revision=request['expected_revision'])
    before = a.source.path.read_bytes()
    with pytest.raises(SyncError, match='revision_conflict'):
        a.source.sync_restore(identifier, new_id='third-copy', replaces='second-copy', **request)
    assert a.source.path.read_bytes() == before


@pytest.mark.parametrize('location', ['source', 'replica'])
def test_failed_receive_does_not_acknowledge_and_can_retry(tmp_path, monkeypatch, location):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.save(sample(), expected_revision=0)
    batch = a.pending(b.replica.actor)
    with monkeypatch.context() as patch:
        module = history_storage if location == 'source' else replica_storage
        patch.setattr(module, 'atomic_write_json', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk failed')))
        with pytest.raises(OSError):
            b.receive(batch['records'])
    assert a.pending(b.replica.actor)['pending'] == 1
    assert bool(b.source.list()) is (location == 'replica')
    restarted = device(tmp_path / 'b', history=ConversationStore(b.source.root, b.source.key), enable=False)
    receipts = restarted.receive(batch['records'])
    saved = restarted.source.get(sample()['id'])
    assert restarted.receive(batch['records']) == receipts
    assert restarted.source.get(sample()['id']) == saved
    a.acknowledge(b.replica.actor, receipts)
    assert a.pending(b.replica.actor)['pending'] == 0


def test_late_receipt_cannot_acknowledge_newer_source_answer(tmp_path):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.save(sample(), expected_revision=0)
    receipts = b.receive(a.pending(b.replica.actor)['records'])
    append(a.source, sample()['id'], 'later')
    assert a.acknowledge(b.replica.actor, receipts) == {'acknowledged': 0, 'outdated': 1}
    deliver(a, b)
    assert b.source.get(sample()['id'])['messages'][-1]['content'] == 'Answer later'


def test_real_run_completion_is_captured_but_pending_question_title_and_drafts_are_not(tmp_path):
    history, runs, orders, request = run_setup(tmp_path / 'a')
    a = device(tmp_path / 'a', history=history)
    b = device(tmp_path / 'b')
    initial = history.sync_export()
    runs.begin(request)
    assert history.sync_export() == initial
    runs.run_once()
    assert history.sync_export() == initial
    assert request['question'] not in str(a.pending(b.replica.actor))
    orders.results[request['task_id']] = {'state': 'succeeded', 'output': 'Completed local answer'}
    restarted = AskRunService(ConversationStore(history.root, history.key), runs.context, lambda: orders)
    restarted.run_once()
    deliver(a, b)
    assert b.source.get('conversation')['messages'][-1]['content'] == 'Completed local answer'
    assert 'runs' not in b.source._read()[1]
    assert orders.acknowledged == [request['task_id']] and len(orders.sent) == 1


@pytest.mark.parametrize('erase', [False, True])
def test_remote_delete_during_run_hides_original_and_preserves_only_allowed_recovery(tmp_path, erase):
    history, runs, orders, request = run_setup(tmp_path / 'a')
    a, b = device(tmp_path / 'a', history=history), device(tmp_path / 'b')
    deliver(a, b)
    runs.begin(request)
    runs.run_once()
    row = b.source.get('conversation')
    if erase:
        b.source.sync_erase('conversation', expected_revision=row['sync']['revision'])
    else:
        b.source.remove('conversation', expected_revision=row['revision'])
    deliver(b, a)
    assert history.list() == []
    with pytest.raises(ConversationError, match='not_found'):
        history.get('conversation')
    with pytest.raises(ConversationError, match='not_found'):
        runs.begin({**request, 'task_id': 'task_' + 'b' * 32})
    assert runs.get(request['task_id'])['cancel_requested']
    assert 'body' not in history._read()[1]['runs']['records'][request['task_id']]
    if not erase:
        pending = history.sync_conflicts()[0]
        assert pending['deferred']
        with pytest.raises(SyncError, match='sync_recovery_busy'):
            history.sync_discard_recovery(pending['id'], review_token=pending['discard_token'])
    orders.results[request['task_id']] = {'state': 'succeeded', 'output': 'Concurrent result after delete'}
    runs.run_once()
    assert history.list() == []
    assert len(orders.sent) == 1 and orders.cancelled == [request['task_id']]
    issues = history.sync_conflicts()
    if erase:
        assert issues == [] and 'Concurrent result after delete' not in str(history._read()[1])
    else:
        assert issues[0]['deleted'] and not issues[0]['deferred']
        assert issues[0]['recovery'][0]['value']['messages'][-1]['content'] == 'Concurrent result after delete'
        deliver(a, b)
        assert b.source.sync_conflicts()[0]['recovery'][0]['value']['messages'][-1]['content'] == 'Concurrent result after delete'


def test_remote_update_does_not_replace_active_prompt_context(tmp_path):
    history, runs, orders, request = run_setup(tmp_path / 'a')
    a, b = device(tmp_path / 'a', history=history), device(tmp_path / 'b')
    deliver(a, b)
    runs.begin(request)
    runs.run_once()
    append(b.source, 'conversation', 'remote')
    deliver(b, a)
    assert history.get('conversation')['messages'][0]['content'] == request['question']
    assert history.get('conversation')['sync']['deferred']
    orders.results[request['task_id']] = {'state': 'succeeded', 'output': 'Local context answer'}
    runs.run_once()
    issue = history.sync_conflicts()[0]
    assert not issue['deferred'] and len(issue['branches']) == 2
    assert {row['value']['messages'][-1]['content'] for row in issue['branches']} == {'Local context answer', 'Answer remote'}


def test_future_sync_version_and_backup_failure_preserve_original(tmp_path, monkeypatch):
    a = device(tmp_path, enable=False)
    a.source.save(sample(), expected_revision=0)
    before = a.source.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(history_storage, 'migration_backup', lambda *args, **kwargs: None)
        with pytest.raises(ConversationError, match='backup_failed'):
            a.source.enable_sync()
    assert a.source.path.read_bytes() == before
    a.source.enable_sync()
    envelope = read_json(a.source.path)
    atomic_write_json(a.source.path, {**envelope, 'version': 'ryn.ask-history.future'})
    before = a.source.path.read_bytes()
    for operation in (a.source.enable_sync, lambda: a.source.save(sample(), expected_revision=0)):
        with pytest.raises(ConversationError, match='version_unsupported'):
            operation()
        assert a.source.path.read_bytes() == before


def test_invalid_batch_is_atomic_and_does_not_change_service_binding(tmp_path):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.save(sample(), expected_revision=0)
    deliver(a, b)
    before = b.source.path.read_bytes()
    bad = deepcopy(sample())
    bad.update(providerPeerId='another', serviceKey='another::model')
    record = records.write('conversations', bad['id'], records.empty(), 'c' * 64, bad)
    with pytest.raises(SyncError, match='service_binding_mismatch'):
        b.source.sync_receive([{'scope': 'conversations', 'id': bad['id'], 'record': record}])
    assert b.source.path.read_bytes() == before


def test_enable_during_pending_run_does_not_expose_question_as_title(tmp_path):
    history, runs, _, request = run_setup(tmp_path)
    runs.begin(request)
    history.enable_sync()
    wire = history.sync_export()
    assert request['question'] not in str(wire)
    assert wire[0]['record']['heads'][0]['value']['messages'] == []


def test_archive_failure_leaves_causal_source_and_order_acknowledgement_unchanged(tmp_path, monkeypatch):
    history, runs, orders, request = run_setup(tmp_path)
    history.enable_sync()
    initial = history.sync_export()
    runs.begin(request)
    runs.run_once()
    orders.results[request['task_id']] = {'state': 'succeeded', 'output': 'Durable final answer'}
    original = history._write

    def fail_final(envelope, data, **kwargs):
        if data['runs']['records'][request['task_id']]['state'] == 'succeeded':
            raise OSError('disk failed')
        return original(envelope, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(history, '_write', fail_final)
        with pytest.raises(OSError):
            runs.run_once()
    assert history.sync_export() == initial
    assert orders.acknowledged == []
    restarted = AskRunService(ConversationStore(history.root, history.key), runs.context, lambda: orders)
    restarted.run_once()
    assert orders.acknowledged == [request['task_id']]
    value = history.sync_export()[0]['record']['heads'][0]['value']
    assert [row['content'] for row in value['messages']] == [request['question'], 'Durable final answer']
    assert len(orders.sent) == 1


def test_owner_conflict_api_export_restore_and_auth(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from rynmesh.ask_ryn.routes import install_ask_ryn
    from rynmesh.background_workers import BackgroundWorkerRegistry

    def owner(request):
        if request.headers.get('x-owner') != 'yes':
            raise HTTPException(403)

    app = FastAPI()
    state = install_ask_ryn(app, store=SimpleNamespace(home=tmp_path / 'api'), home=tmp_path,
                           workers=BackgroundWorkerRegistry(), local_control=owner,
                           messaging_key=X25519PrivateKey.generate())
    a = device(tmp_path / 'api', history=state.conversations)
    b = device(tmp_path / 'b')
    a.source.save(sample(), expected_revision=0)
    deliver(a, b)
    append(a.source, sample()['id'], 'a')
    append(b.source, sample()['id'], 'b')
    deliver(b, a)
    client = TestClient(app)
    headers = {'x-owner': 'yes'}
    assert client.get('/api/local/ask/sync/conflicts').status_code == 403
    assert client.post('/api/local/ask/sync/restore', json={}).status_code == 403
    issue = client.get('/api/local/ask/sync/conflicts', headers=headers).json()['conflicts'][0]
    exported = client.get('/api/local/ask/export', headers=headers).json()
    assert exported['sync_conflicts'][0]['branches'] == issue['branches']
    request = {'conversation_id': issue['id'], 'choice_id': issue['branches'][0]['choice_id'],
               'new_id': 'restored', 'expected_revision': issue['revision']}
    stale = client.post('/api/local/ask/sync/restore', headers=headers, json={**request, 'expected_revision': '0' * 64})
    assert stale.status_code == 409
    restored = client.post('/api/local/ask/sync/restore', headers=headers, json=request)
    assert restored.status_code == 200
    assert restored.json()['serviceKey'] == sample()['serviceKey']
    assert client.post('/api/local/ask/sync/restore', headers=headers, json=request).json() == restored.json()
    replacement = {**request, 'new_id': 'second-copy', 'replaces': 'restored'}
    assert client.post('/api/local/ask/sync/restore', json=replacement).status_code == 403
    live = client.post('/api/local/ask/sync/restore', headers=headers, json=replacement)
    assert live.status_code == 409 and live.json()['detail'] == 'sync_restore_copy_not_deleted'
    a.source.remove('restored', expected_revision=restored.json()['revision'])
    deleted = client.post('/api/local/ask/sync/restore', headers=headers, json=request)
    assert deleted.json()['detail'] == 'ask_conversation_deleted'
    second = client.post('/api/local/ask/sync/restore', headers=headers, json=replacement)
    assert second.status_code == 200 and second.json()['id'] == 'second-copy'
    assert second.json()['serviceKey'] == sample()['serviceKey']
    assert client.post('/api/local/ask/sync/restore', headers=headers, json=replacement).json() == second.json()
    discard = {'conversation_id': issue['id'], 'review_token': issue['discard_token']}
    assert client.post('/api/local/ask/sync/discard', json=discard).status_code == 403
    live = client.post('/api/local/ask/sync/discard', headers=headers, json=discard)
    assert live.status_code == 409 and live.json()['detail'] == 'sync_recovery_not_deleted'
    original = a.source.get(issue['id'])
    a.source.remove(issue['id'], expected_revision=original['revision'])
    assert client.post('/api/local/ask/sync/discard', headers=headers, json=discard).status_code == 409
    current = client.get('/api/local/ask/sync/conflicts', headers=headers).json()['conflicts'][0]
    discard['review_token'] = current['discard_token']
    discarded = client.post('/api/local/ask/sync/discard', headers=headers, json=discard)
    assert discarded.status_code == 200 and discarded.json()['erased']
    assert client.post('/api/local/ask/sync/discard', headers=headers, json=discard).json() == discarded.json()
    assert a.source.get('second-copy')['id'] == 'second-copy'


def test_remote_delete_keeps_unsent_draft_local_and_explicit_restore_keeps_binding(tmp_path):
    a, b = device(tmp_path / 'a'), device(tmp_path / 'b')
    a.source.save(sample(), expected_revision=0)
    deliver(a, b)
    row = a.source.get(sample()['id'])
    a.source.save({**row, 'draft': 'Unsent draft only on A'}, expected_revision=row['revision'])
    row = b.source.get(sample()['id'])
    b.source.remove(row['id'], expected_revision=row['revision'])
    deliver(b, a)
    assert a.source.list() == []
    issue = a.source.sync_conflicts()[0]
    assert issue['deleted'] and not issue['conflict']
    assert issue['local_draft']['draft'] == 'Unsent draft only on A'
    assert 'Unsent draft only on A' not in str(a.source.sync_export())
    envelope, data = a.source._read()
    data['device_sync']['entities'][issue['id']]['local_draft']['futureCredential'] = 'private extension credential'
    a.source._write(envelope, data, capture_sync=False)
    assert 'private extension credential' not in str(a.source.export_owner())
    assert a.source.export_owner()['sync_conflicts'][0]['local_draft']['draft'] == 'Unsent draft only on A'
    restored = a.source.sync_restore(issue['id'], choice_id='local-draft', new_id='draft-recovered', expected_revision=issue['revision'])
    assert restored['draft'] == 'Unsent draft only on A' and restored['serviceKey'] == sample()['serviceKey']
    deliver(a, b)
    assert 'draft' not in b.source.get('draft-recovered')
    a.source.sync_erase(issue['id'], expected_revision=issue['revision'])
    assert a.source.sync_conflicts() == []
    assert 'local_draft' not in a.source._read()[1]['device_sync']['entities'][issue['id']]
