"""Causal decisions, independent of transport timing and computer clocks."""
from copy import deepcopy
from itertools import permutations

import pytest

from rynmesh.device_sync import records as r

A, B, C = 'a' * 64, 'b' * 64, 'c' * 64
ITEM = {'item_id': 'article', 'title': 'A local article', 'source_title': 'Journal', 'source_id': 'journal',
        'content_kind': 'document', 'link': 'https://example.test/article'}


def bookmark(enabled=True):
    return {'item': ITEM, 'bookmarked': enabled}


def reading(progress):
    return {'item': ITEM, 'progress': progress, 'completed': False, 'content_version': 'sha256:' + 'd' * 64}


def test_validation_fingerprints_bind_scope_identifier_and_exact_encoded_value():
    cache = set()
    record = r.write('reading', 'article', r.empty(), A, reading(0))
    assert r.validate_cached('reading', 'article', record, cache, max_entries=3) == r.fingerprint(record)
    altered = deepcopy(record)
    altered['heads'][0]['value']['progress'] = False
    with pytest.raises(r.SyncError, match='sync_value_invalid'):
        r.validate_cached('reading', 'article', altered, cache, max_entries=3)
    with pytest.raises(r.SyncError, match='sync_item_invalid'):
        r.validate_cached('reading', 'different-article', record, cache, max_entries=3)
    with pytest.raises(r.SyncError):
        r.validate_cached('bookmarks', 'article', record, cache, max_entries=3)
    assert len(cache) == 1


def test_validation_fingerprints_are_bounded_and_do_not_retain_record_values():
    cache = set()
    for index in range(20):
        record = r.write('reading', 'article', r.empty(), A, reading(index / 20))
        r.validate_cached('reading', 'article', record, cache, max_entries=3)
        assert len(cache) <= 3
    assert ITEM['title'] not in repr(cache) and ITEM['link'] not in repr(cache)


def conversation(*messages):
    stamp = '2026-09-11T00:00:00Z'
    return {'id': 'chat', 'title': 'Conversation', 'serviceKey': 'provider::model', 'serviceName': 'Model',
            'providerPeerId': 'provider', 'networkId': 'network', 'createdAt': stamp, 'updatedAt': stamp,
            'messages': [{'id': str(index), 'role': 'user' if index % 2 == 0 else 'assistant', 'content': text,
                          'createdAt': stamp, 'status': 'complete'} for index, text in enumerate(messages)]}


def test_bookmark_remove_wins_concurrently_but_explicit_observed_readd_is_allowed():
    a = r.write('bookmarks', 'article', r.empty(), A, bookmark())
    b = r.write('bookmarks', 'article', r.empty(), B, bookmark(False))
    merged = r.merge('bookmarks', 'article', a, b)
    assert r.view('bookmarks', 'article', merged)['bookmarked'] is False
    assert r.view('bookmarks', 'article', merged)['conflict'] is True
    added = r.write('bookmarks', 'article', merged, A, bookmark())
    for old in [a, b, merged]:
        assert r.merge('bookmarks', 'article', added, old) == added
    assert r.view('bookmarks', 'article', added)['bookmarked'] is True


def test_reading_goes_back_when_causally_newer_and_keeps_concurrent_positions():
    first = r.write('reading', 'article', r.empty(), A, reading(0.9))
    reread = r.write('reading', 'article', first, B, reading(0.2))
    assert r.view('reading', 'article', r.merge('reading', 'article', reread, first))['value']['progress'] == 0.2
    concurrent = r.write('reading', 'article', first, A, reading(0.7))
    joined = r.merge('reading', 'article', reread, concurrent)
    view = r.view('reading', 'article', joined)
    assert view['value'] is None and {row['value']['progress'] for row in view['candidates']} == {0.2, 0.7}
    chosen = r.write('reading', 'article', joined, A, view['candidates'][0]['value'])
    assert len(r.view('reading', 'article', r.merge('reading', 'article', joined, chosen))['candidates']) == 1


def test_reordered_duplicate_and_transitive_snapshots_converge():
    a = r.write('bookmarks', 'article', r.empty(), A, bookmark())
    b = r.write('bookmarks', 'article', a, B, bookmark(False))
    c = r.write('bookmarks', 'article', a, C, bookmark())
    expected = r.merge('bookmarks', 'article', b, c)
    for ordering in permutations([a, b, c, b]):
        result = r.empty()
        for row in ordering:
            result = r.merge('bookmarks', 'article', result, row)
        assert result == expected
    assert r.merge('bookmarks', 'article', expected, expected) == expected


def test_conversation_branches_keep_common_history_and_original_provider():
    first = r.write('conversations', 'chat', r.empty(), A, conversation('Question', 'Answer'))
    a = r.write('conversations', 'chat', first, A, conversation('Question', 'Answer', 'Next from A'))
    b = r.write('conversations', 'chat', first, B, conversation('Question', 'Answer', 'Next from B'))
    merged = r.merge('conversations', 'chat', a, b)
    view = r.view('conversations', 'chat', merged)
    assert [message['content'] for message in view['common_messages']] == ['Question', 'Answer']
    assert len(view['branches']) == 2 and not view['deleted']
    assert {row['value']['serviceKey'] for row in view['branches']} == {'provider::model'}
    changed = conversation('Question') | {'providerPeerId': 'someone-else', 'serviceKey': 'someone-else::model'}
    with pytest.raises(r.SyncError, match='sync_service_binding_mismatch'):
        r.write('conversations', 'chat', first, A, changed)


def test_concurrent_delete_keeps_recovery_but_erase_removes_it_and_blocks_old_snapshots():
    first = r.write('conversations', 'chat', r.empty(), A, conversation('Original'))
    removed = r.write('conversations', 'chat', first, A, None)
    late = r.write('conversations', 'chat', first, B, conversation('Original', 'Offline answer'))
    merged = r.merge('conversations', 'chat', removed, late)
    view = r.view('conversations', 'chat', merged)
    assert view['deleted'] and not view['branches'] and len(view['recovery']) == 1
    with pytest.raises(r.SyncError, match='sync_conversation_deleted'):
        r.write('conversations', 'chat', merged, A, conversation('Restore silently'))
    erased = r.write('conversations', 'chat', merged, A, None, erase=True)
    for old in [first, removed, late, merged]:
        result = r.merge('conversations', 'chat', erased, old)
        assert result['erased'] and result['heads'] == []
    assert b'Offline answer' not in r.canonical_json(erased)
    with pytest.raises(r.SyncError, match='sync_entity_erased'):
        r.write('conversations', 'chat', erased, B, conversation('New work must use a new ID'))


def test_only_declared_metadata_and_final_history_enter_sync_values():
    local = bookmark() | {'api_key': 'secret', 'body': 'private body'}
    local['item'] = ITEM | {'summary': 'private summary', 'thumbnail': 'https://tracking.test/image'}
    clean = r.clean_value('bookmarks', 'article', local)
    assert clean == bookmark()
    chat = conversation('Finished', 'Finished answer', 'Running request', 'Pending reply')
    chat['draft'] = 'private draft'
    chat['credentials'] = {'token': 'secret'}
    chat['messages'][2]['taskId'] = 'running-task'
    chat['messages'][3].update(taskId='running-task', status='running')
    clean = r.clean_value('conversations', 'chat', chat)
    assert len(clean['messages']) == 2 and 'draft' not in clean and 'credentials' not in clean


def test_identical_dots_with_different_values_are_not_silently_overwritten():
    first = r.write('bookmarks', 'article', r.empty(), A, bookmark())
    forged = deepcopy(first)
    forged['heads'][0]['value']['bookmarked'] = False
    with pytest.raises(r.SyncError, match='sync_dot_conflict'):
        r.merge('bookmarks', 'article', first, forged)


@pytest.mark.parametrize('value', [True, 0, -1, 1.5, 2**53])
def test_invalid_clock_values_are_rejected(value):
    first = r.write('bookmarks', 'article', r.empty(), A, bookmark())
    first['clock'][A] = first['heads'][0]['counter'] = value
    with pytest.raises(r.SyncError):
        r.validate('bookmarks', 'article', first)


@pytest.mark.parametrize('link', ['javascript:alert(1)', 'file:///private', 'http://owner:password@example.test', 'https://'])
def test_metadata_cannot_supply_active_links_or_url_credentials(link):
    value = bookmark() | {'item': ITEM | {'link': link}}
    with pytest.raises(r.SyncError, match='sync_item_link_invalid'):
        r.clean_value('bookmarks', 'article', value)


def test_same_operation_cannot_change_its_numeric_encoding_and_hash_on_merge():
    first = r.write('reading', 'article', r.empty(), A, reading(0))
    changed = deepcopy(first)
    changed['heads'][0]['value']['progress'] = 0.0
    assert r.fingerprint(first) != r.fingerprint(changed)
    for left, right in [(first, changed), (changed, first)]:
        with pytest.raises(r.SyncError, match='sync_dot_conflict'):
            r.merge('reading', 'article', left, right)


def test_entity_key_memo_preserves_canonical_scope_binding_and_rejects_invalid_inputs():
    for scope in ('bookmarks', 'reading', 'conversations'):
        for identifier in ('article', '中文内容', 'a' * 256):
            assert r.entity_key(scope, identifier) == r.fingerprint([scope, identifier])
    for scope, identifier, error in (([], 'article', 'scope'), ('bookmarks', [], 'entity'),
                                     ('bookmarks', True, 'entity'), ('bookmarks', 'bad\nname', 'entity')):
        with pytest.raises(r.SyncError, match=error):
            r.entity_key(scope, identifier)
    # A long-lived node never retains unlimited identifiers in the memo.
    for index in range(30001):
        r.entity_key('bookmarks', f'memo-{index}')
    assert r._entity_key.cache_info().currsize == 30000


def test_conflict_count_keeps_identical_concurrent_values_together():
    first = r.write('bookmarks', 'article', r.empty(), A, bookmark())
    identical = r.merge('bookmarks', 'article', first, r.write('bookmarks', 'article', r.empty(), B, bookmark()))
    different = r.merge('bookmarks', 'article', first, r.write('bookmarks', 'article', r.empty(), B, bookmark(False)))
    assert r.conflict_count([{'record': row} for row in (r.empty(), first, identical, different)]) == 1
