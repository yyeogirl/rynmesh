"""Source erasure does not resurrect history or erase post-review work."""
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_ask_history import sample
from test_ask_runs import setup as run_setup
from test_device_sync_conversations import append, deliver, device

from rynmesh.ask_ryn import store as storage
from rynmesh.ask_ryn.privacy import ConversationPrivacy
from rynmesh.ask_ryn.runs import AskRunService
from rynmesh.ask_ryn.store import ConversationError, ConversationStore


def history(tmp_path):
    source = ConversationStore(tmp_path / 'ask', X25519PrivateKey.generate())
    source.save(sample(), expected_revision=0)
    return source


def test_erasure_removes_all_local_branches_and_old_delivery_cannot_restore_them(tmp_path):
    a, b = [device(tmp_path / name) for name in ('a', 'b')]
    a.source.save(sample(), expected_revision=0)
    deliver(a, b)
    append(a.source, sample()['id'], 'a')
    append(b.source, sample()['id'], 'b')
    stale = b.source.sync_export()
    deliver(a, b)
    deliver(b, a)
    assert a.source.sync_conflicts()
    a.source.save_draft('unassigned draft', expected_revision=0)
    privacy = ConversationPrivacy(a.source)
    preview = privacy.preview()
    assert preview['recovery_items'] == 2
    assert preview['has_unassigned_draft'] is True
    assert sample()['title'] not in str(preview)
    receipt = privacy.erase_source(review_token=preview['review_token'])
    assert receipt['source_erased'] and not receipt['remote_confirmed']
    assert a.source.list() == a.source.sync_conflicts() == []
    assert a.source.draft()['text'] == ''
    # Source reconciliation clears the replica before acknowledgements. Until
    # that separate step runs, this primitive does not claim replica erasure.
    deliver(a, b)
    assert b.source.list() == b.source.sync_conflicts() == []
    a.receive(stale)
    restarted = ConversationStore(a.source.root, a.source.key)
    assert restarted.list() == restarted.sync_conflicts() == []
    assert sample()['messages'][0]['content'] not in str(restarted._read()[1])
    assert restarted.migrate(sample())['status'] == 'deleted'


@pytest.mark.parametrize('enable', [False, True])
def test_committed_retry_after_restart_does_not_erase_new_work(tmp_path, enable):
    source = history(tmp_path)
    if enable:
        source.enable_sync()
    privacy = ConversationPrivacy(source)
    token = privacy.preview()['review_token']
    receipt = privacy.erase_source(review_token=token)
    new = {**sample(), 'id': 'new-conversation', 'title': 'Keep new work'}
    source.save(new, expected_revision=0)
    source.save_draft('new draft', expected_revision=source.draft()['revision'])
    before = source.path.read_bytes()
    restarted = ConversationPrivacy(ConversationStore(source.root, source.key))
    assert restarted.erase_source(review_token=token) == receipt
    assert source.path.read_bytes() == before
    assert source.get(new['id'])['title'] == new['title']
    assert source.draft()['text'] == 'new draft'


@pytest.mark.parametrize('change', ['draft', 'conversation', 'recovery'])
def test_review_is_invalidated_by_new_source_data(tmp_path, change):
    a = device(tmp_path / 'a')
    a.source.save(sample(), expected_revision=0)
    privacy = ConversationPrivacy(a.source)
    token = privacy.preview()['review_token']
    if change == 'draft':
        a.source.save_draft('do not delete', expected_revision=0)
    elif change == 'conversation':
        append(a.source, sample()['id'], 'new')
    else:
        b = device(tmp_path / 'b')
        deliver(a, b)
        append(a.source, sample()['id'], 'a')
        append(b.source, sample()['id'], 'b')
        deliver(b, a)
    before = a.source.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_privacy_review_changed'):
        privacy.erase_source(review_token=token)
    assert a.source.path.read_bytes() == before


@pytest.mark.parametrize('state', ['queued', 'dispatching', 'running', 'unknown_future_state'])
def test_active_or_unknown_tasks_prevent_erasure_without_claiming_cancellation(tmp_path, state):
    source = history(tmp_path)
    envelope, data = source._read()
    run = {'task_id': 'task-one', 'conversation_id': sample()['id'], 'fingerprint': 'a' * 64,
           'state': state, 'cancel_requested': False, 'body': {'prompt': 'keep until reconciled'}}
    data['runs'] = {'version': 1, 'records': {'task-one': run}}
    source._write(envelope, data)
    privacy = ConversationPrivacy(source)
    preview = privacy.preview()
    assert preview['active_tasks'] == 1
    before = source.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_privacy_tasks_active'):
        privacy.erase_source(review_token=preview['review_token'])
    assert source.path.read_bytes() == before


def test_terminal_receipts_survive_but_payloads_and_known_private_sections_are_erased(tmp_path):
    source = history(tmp_path)
    source.migrate(sample())
    source.enable_sync()
    envelope, data = source._read()
    envelope['extension'] = {'preserved': True}
    data['extension'] = {'preserved': True}
    data['runs'] = {'version': 1, 'records': {'task-one': {
        'task_id': 'task-one', 'conversation_id': sample()['id'], 'fingerprint': 'a' * 64,
        'state': 'succeeded', 'cancel_requested': False,
        'body': {'prompt': 'terminal prompt'}, 'extension': 'terminal private extension'}}}
    data['conversations'][sample()['id']]['private_extension'] = 'old source secret'
    data['device_sync']['entities'][sample()['id']]['private_extension'] = 'old recovery secret'
    source._write(envelope, data, capture_sync=False)
    privacy = ConversationPrivacy(source)
    privacy.erase_source(review_token=privacy.preview()['review_token'])
    envelope, data = source._read()
    assert envelope['extension'] == data['extension'] == {'preserved': True}
    run = data['runs']['records']['task-one']
    assert run['fingerprint'] == 'a' * 64 and run['state'] == 'succeeded'
    assert 'body' not in run and 'extension' not in run
    for secret in ('old source secret', 'old recovery secret', 'terminal prompt', 'terminal private extension'):
        assert secret not in str(data)
    assert source.migrate(sample())['status'] == 'deleted'
    # Existing migration backup remains explicitly outside this source step.
    assert source.path.with_name('history.json.migrated').exists()


def test_failed_atomic_commit_preserves_every_source_byte_and_review_can_retry(tmp_path, monkeypatch):
    source = history(tmp_path)
    source.enable_sync()
    privacy = ConversationPrivacy(source)
    token = privacy.preview()['review_token']
    before = source.path.read_bytes()
    write = storage.atomic_write_json
    def fail(*args, **kwargs):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(storage, 'atomic_write_json', fail)
    with pytest.raises(OSError):
        privacy.erase_source(review_token=token)
    assert source.path.read_bytes() == before
    monkeypatch.setattr(storage, 'atomic_write_json', write)
    assert privacy.erase_source(review_token=token)['source_erased']


def test_future_privacy_receipt_version_is_not_overwritten(tmp_path):
    source = history(tmp_path)
    envelope, data = source._read()
    data['privacy_erasure'] = {'version': 'future', 'unknown': deepcopy(sample())}
    source._write(envelope, data)
    before = source.path.read_bytes()
    privacy = ConversationPrivacy(source)
    with pytest.raises(ConversationError, match='version_unsupported'):
        privacy.preview()
    with pytest.raises(ConversationError, match='version_unsupported'):
        privacy.erase_source(review_token='a' * 64)
    assert source.path.read_bytes() == before


def test_erased_terminal_run_can_be_retried_without_dispatching_or_archiving_again(tmp_path):
    source, runs, orders, request = run_setup(tmp_path)
    source.enable_sync()
    runs.begin(request)
    runs.run_once()
    orders.results[request['task_id']] = {'state': 'succeeded', 'output': 'erase this completed answer'}
    runs.run_once()
    assert len(orders.sent) == 1
    privacy = ConversationPrivacy(source)
    privacy.erase_source(review_token=privacy.preview()['review_token'])
    restarted = AskRunService(ConversationStore(source.root, source.key), runs.context, lambda: orders)
    assert restarted.begin(request)['state'] == 'succeeded'
    restarted._finish(request['task_id'], {'state': 'succeeded', 'output': 'late private answer'})
    restarted.run_once()
    assert len(orders.sent) == 1
    assert source.list() == source.sync_conflicts() == []
    assert 'erase this completed answer' not in str(source._read()[1])
    assert 'late private answer' not in str(source._read()[1])


def test_whole_source_scope_includes_restored_copies_and_deleted_local_drafts(tmp_path):
    a, b = [device(tmp_path / name) for name in ('a', 'b')]
    a.source.save(sample(), expected_revision=0)
    deliver(a, b)
    local = b.source.get(sample()['id'])
    b.source.save({**local, 'draft': 'private unsent draft'}, expected_revision=local['revision'])
    a.source.remove(sample()['id'], expected_revision=1)
    deliver(a, b)
    issue = b.source.sync_conflicts()[0]
    assert issue['local_draft']['draft'] == 'private unsent draft'
    copy = b.source.sync_restore(sample()['id'], choice_id='local-draft', new_id='kept-copy',
                                 expected_revision=issue['revision'])
    assert copy['id'] == 'kept-copy'
    stale = b.source.sync_export()
    privacy = ConversationPrivacy(b.source)
    privacy.erase_source(review_token=privacy.preview()['review_token'])
    assert b.source.list() == b.source.sync_conflicts() == []
    assert b.source._read()[1]['device_sync']['restores'] == {}
    b.receive(stale)
    assert b.source.list() == b.source.sync_conflicts() == []
    assert 'private unsent draft' not in str(b.source._read()[1])


def test_identity_limit_failure_is_atomic_instead_of_losing_replay_barriers(tmp_path):
    source = history(tmp_path)
    envelope, data = source._read()
    data['tombstones'] = {f'deleted-{index}': {'revision': 1} for index in range(10000)}
    source._write(envelope, data)
    privacy = ConversationPrivacy(source)
    token = privacy.preview()['review_token']
    before = source.path.read_bytes()
    with pytest.raises(ConversationError, match='ask_history_limit'):
        privacy.erase_source(review_token=token)
    assert source.path.read_bytes() == before
