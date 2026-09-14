import json

import pytest
from test_ask_history import sample
from test_local_search_sources import sources

from rynmesh.local_search.index import LocalSearchIndex, SearchError


@pytest.mark.parametrize('fault', ['metadata', 'body', 'control', 'timestamp'])
def test_damaged_saved_copy_does_not_break_healthy_reading_and_conversations(tmp_path, fault):
    adapter, history, imports, reader, conversations, alice, _, _ = sources(tmp_path)
    article = {'item_id': 'healthy', 'title': 'Healthy reading', 'link': 'https://example.test/healthy'}
    history.record(article, 'opened')
    reader.put(article['link'], {'blocks': [{'text': 'Healthy independent article'}]}, now=1)
    conversations.save(sample(), expected_revision=0)
    broken = imports.save(b'private damaged copy marker', filename='broken.txt', mime='text/plain')
    healthy = imports.save(b'healthy saved copy marker', filename='healthy.txt', mime='text/plain')
    engine = LocalSearchIndex(alice.home / 'local-search', messaging_key=alice.messaging_private, source=adapter.snapshot)
    engine.rebuild()
    identifier = engine.query('private damaged')['results'][0]['id']
    directory = imports._directory(broken['import_id'])
    path = directory / ('metadata.json' if fault in {'metadata', 'timestamp'} else 'original.txt')
    original = path.read_bytes()
    if fault == 'control':
        path = imports.root / 'control.json'
        path.write_text(json.dumps({'version': 999, 'generation': 0}))
    elif fault == 'timestamp':
        path.write_text(json.dumps({**broken, 'created_at_unix': 'invalid time'}))
    else:
        path.write_bytes(b'corrupt synthetic copy')
    unchanged = path.read_bytes()
    result = engine.query('Healthy independent')
    assert result['total'] == 1
    assert result['partial'] and result['unavailable_sources'] == ['saved_documents']
    assert engine.query('private damaged')['total'] == 0
    try:
        value = engine.resolve(identifier)
    except SearchError as exc:
        assert str(exc) == 'search_result_unavailable'
    else:
        assert value['text'] == '' and value['body_state'] == 'unavailable'
    assert engine.query('秘密文章正文')['total'] == 1
    if fault != 'control':
        assert imports.body(healthy['import_id'])['text'] == 'healthy saved copy marker'
        assert engine.query('healthy saved')['total'] == 1
    engine.rebuild(force=True)
    result = engine.query('Healthy independent')
    assert result['total'] == 1 and result['partial'] and not result['indexing_pending']
    assert path.read_bytes() == unchanged
    if fault == 'control':
        path.unlink()
    else:
        path.write_bytes(original)
    engine.rebuild(force=True)
    repaired = engine.query('private damaged')
    assert repaired['total'] == 1 and not repaired['partial']
    assert repaired['unavailable_sources'] == []


def test_pending_document_cleanup_never_leaks_old_index_body(tmp_path, monkeypatch):
    from pathlib import Path

    from rynmesh.services.library_cleanup import LibraryCleanup

    adapter, _, imports, _, _, alice, _, _ = sources(tmp_path)
    target = imports.save(b'old private cleanup marker', filename='old.txt', mime='text/plain')
    imports.save(b'healthy retained marker', filename='healthy.txt', mime='text/plain')
    engine = LocalSearchIndex(alice.home / 'local-search', messaging_key=alice.messaging_private, source=adapter.snapshot)
    engine.rebuild()
    original_index = engine.path.read_bytes()
    identifier = engine.query('old private')['results'][0]['id']
    unlink = Path.unlink
    def held(path, *args, **kwargs):
        if path.parent.name == target['import_id'] and path.name == 'original.txt':
            raise PermissionError('synthetic held file')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', held)
    cleanup = LibraryCleanup(imports)
    review = cleanup.preview(target['import_id'])
    with pytest.raises(PermissionError):
        cleanup.begin(scope=target['import_id'], review_token=review['review_token'])
    assert engine.path.read_bytes() == original_index
    assert engine.query('old private')['total'] == 0
    assert engine.query('healthy retained')['total'] == 1
    with pytest.raises(SearchError, match='result_unavailable'):
        engine.resolve(identifier)
    engine.rebuild(force=True)
    assert 'old private' not in str(engine._read()[1])
    assert cleanup.status()['pending'] == ['files']
