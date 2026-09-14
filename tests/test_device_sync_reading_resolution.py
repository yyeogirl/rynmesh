from copy import deepcopy

import pytest
from test_device_sync_reading import ITEM
from test_device_sync_transfer import Mesh

from rynmesh.device_sync import records
from rynmesh.device_sync.records import SyncError
from rynmesh.services import consumption
from rynmesh.services.consumption import ConsumptionStore


def conflicted(tmp_path, scope='reading', *, deletion=False):
    source = ConsumptionStore(tmp_path / 'consumption.json')
    source.enable_sync('a' * 64, [scope])
    left = {'item': ITEM, 'progress': .2, 'completed': False, 'content_version': 'version-one'} if scope == 'reading' else {'item': ITEM, 'bookmarked': True}
    right = {**left, 'progress': .8, 'content_version': 'version-two'} if scope == 'reading' else {'item': ITEM, 'bookmarked': False}
    if deletion:
        right = None
    for actor, value in (('b' * 64, left), ('c' * 64, right)):
        record = records.write(scope, ITEM['item_id'], records.empty(), actor, value)
        source.sync_receive([{'scope': scope, 'id': ITEM['item_id'], 'record': record}], scopes=[scope])
    return source, source.sync_issues()[0]


def choose(source, issue, candidate):
    return source.sync_resolve(issue['scope'], issue['id'], choice_id=candidate['choice_id'], expected_revision=issue['revision'])


def test_choose_earlier_position_keeps_version_and_retries_after_restart(tmp_path):
    source, issue = conflicted(tmp_path)
    candidate = next(row for row in issue['candidates'] if row['value']['progress'] == .2)
    result = choose(source, issue, candidate)
    assert not result['conflict'] and result['value']['progress'] == .2
    assert result['value']['content_version'] == 'version-one'
    assert source.list()[0]['open_count'] == 0
    original = source.path.read_bytes()
    restarted = ConsumptionStore(source.path)
    assert choose(restarted, issue, candidate) == result
    assert source.path.read_bytes() == original
    assert restarted.sync_issues() == []
    assert 'resolution' not in str(restarted.sync_export(['reading']))


def test_stale_review_and_unknown_candidate_preserve_current_record(tmp_path):
    source, issue = conflicted(tmp_path)
    before = source.path.read_bytes()
    with pytest.raises(SyncError, match='sync_reading_choice_invalid'):
        choose(source, issue, {'choice_id': 'not-a-candidate'})
    assert source.path.read_bytes() == before
    source.record(ITEM, 'progress', progress=.4)
    current = source.path.read_bytes()
    with pytest.raises(SyncError, match='sync_revision_conflict'):
        choose(source, issue, issue['candidates'][0])
    assert source.path.read_bytes() == current


def test_new_changes_after_resolution_invalidate_old_success_retry(tmp_path):
    source, issue = conflicted(tmp_path)
    choose(source, issue, issue['candidates'][0])
    source.record(ITEM, 'progress', progress=.5)
    with pytest.raises(SyncError, match='sync_revision_conflict'):
        choose(source, issue, issue['candidates'][0])
    assert source.list()[0]['progress'] == .5


def test_failed_resolution_commit_keeps_candidates_and_retry_can_finish(tmp_path, monkeypatch):
    source, issue = conflicted(tmp_path)
    before = source.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(consumption, 'atomic_write_json', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):
            choose(source, issue, issue['candidates'][0])
    assert source.path.read_bytes() == before
    assert source.sync_issues() == [issue]
    assert not choose(source, issue, issue['candidates'][0])['conflict']


def test_deleted_position_can_be_chosen_without_removing_saved_content(tmp_path):
    source, issue = conflicted(tmp_path, deletion=True)
    source.enable_sync('a' * 64, ['bookmarks'])
    source.record(ITEM, 'bookmark')
    candidate = next(row for row in issue['candidates'] if row['value'] is None)
    choose(source, issue, candidate)
    row = source.list()[0]
    assert row['bookmarked'] and not row['sync_reading_available']
    assert row['progress'] == 0 and source.sync_issues() == []


def test_explicit_saved_choice_can_resolve_cancel_wins_conflict(tmp_path):
    source, issue = conflicted(tmp_path, 'bookmarks')
    assert not source.list()[0]['bookmarked']
    candidate = next(row for row in issue['candidates'] if row['value']['bookmarked'])
    assert choose(source, issue, candidate)['bookmarked']
    assert source.sync_issues() == []


def test_resolved_position_propagates_and_old_packets_cannot_recreate_conflict(tmp_path):
    mesh = Mesh(tmp_path, ['reading'])
    for node, position in ((mesh.a, .2), (mesh.b, .8)):
        mesh.reader(node).record(ITEM, 'progress', progress=position)
        (mesh.left if node is mesh.a else mesh.right).status(mesh.pair_id)
    old = mesh.right.prepare(mesh.pair_id, 'reading')
    mesh.left.send(mesh.pair_id, 'reading')
    mesh.right.send(mesh.pair_id, 'reading')
    source = mesh.reader(mesh.a)
    issue = source.sync_issues()[0]
    candidate = next(row for row in issue['candidates'] if row['value']['progress'] == .2)
    choose(source, issue, candidate)
    mesh.left.send(mesh.pair_id, 'reading')
    assert mesh.reader(mesh.b).sync_issues() == []
    assert mesh.reader(mesh.b).list()[0]['progress'] == .2
    mesh.left.receive(deepcopy(old))
    assert source.sync_issues() == []
    assert source.list()[0]['progress'] == .2
