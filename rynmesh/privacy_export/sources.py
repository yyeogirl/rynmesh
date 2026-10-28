"""Read current owner data; no discovery, model calls or remote fetches."""
from __future__ import annotations

from ..device_sync import records
from ..file_transactions import file_transaction
from .archive import ExportError

CARD = ('version', 'library_id', 'content_id', 'title', 'summary', 'kind', 'source', 'source_url',
        'publisher_peer_id', 'manifest_ref', 'filename', 'mime', 'size_bytes', 'sha256',
        'fetch_available', 'content_truncated')


def pick(row, fields):
    """Only named scalar fields and scalar lists; never arbitrary nested objects."""
    if not isinstance(row, dict):
        raise ValueError('invalid record')
    def scalar(value):
        return value is None or type(value) in {str, bool, int, float}
    return {key: row[key] for key in fields if key in row and
            (scalar(row[key]) or isinstance(row[key], list) and all(scalar(value) for value in row[key]))}


def publication(row):
    if row is None:
        return None
    return pick(row, ('id', 'revision', 'published_at', 'updated_at')) | {
        'card': pick(row['card'], CARD), 'audience': pick(row['audience'], ('mode', 'relationship_ids'))}


class ProductDataSources:
    def __init__(self, app):
        self.app = app

    def __call__(self, scope):
        return getattr(self, scope)()

    def first_reading(self):
        data = self.app.state.first_run.export()
        if data.get('safe_error'):
            raise ExportError('privacy_export_source_unavailable', 'first_reading')
        yield 'progress.json', data

    def reading(self):
        store = self.app.state.consumption_store
        with file_transaction(store.lock_path):
            rows = []
            for row in store.list():
                clean = pick(row, ('item_id', 'first_opened_unix', 'last_opened_unix', 'last_activity_unix',
                    'open_count', 'bookmarked', 'progress', 'completed', 'content_version'))
                clean['item'] = pick(row['item'], ('item_id', 'source_id', 'source_title', 'source_kind', 'title',
                    'link', 'summary', 'ai_summary', 'author', 'thumbnail', 'media_url', 'content_kind',
                    'content_type', 'tags', 'published_unix', 'score', 'reasons'))
                rows.append(clean)
            issues = store.sync_issues()
        yield 'history.json', {'records': rows, 'sync_issues': issues}

    def conversations(self):
        yield 'history.json', self.app.state.ask_ryn.conversations.export_owner()

    def friends(self):
        service = self.app.state.friends.service
        data = service.store.state()
        friends = []
        for row in data['relationships'].values():
            clean = pick(row, ('relationship_id', 'peer_id', 'node_name', 'endpoint', 'status',
                              'created_at', 'revoked_at', 'revocation_delivery', 'permissions'))
            friends.append(clean)
        cards = []
        for row in data['cards'].values():
            clean = pick(row, ('version', 'card_id', 'from', 'to', 'dir', 'relationship_id', 'created_at',
                'delivery_state', 'delivered', 'fetch_state', 'fetched_library_id', 'sha256_verified', 'expires_at'))
            clean['card'] = pick(row['card'], CARD)
            cards.append(clean)
        yield 'relationships-and-cards.json', {'relationships': friends, 'cards': cards}

    def friend_feed(self):
        data = self.app.state.friend_feed.service.store.read()
        publications = [pick(row, ('id', 'revision', 'stopped')) | {
            'draft': publication(row['draft']), 'published': publication(row['published'])}
            for row in data['publications'].values()]
        subscriptions = []
        for row in data['subscriptions'].values():
            clean = pick(row, ('relationship_id', 'peer_id', 'enabled', 'revision', 'last_error'))
            clean['read'] = {key: value for key, value in row.get('read', {}).items() if type(value) is int}
            subscriptions.append(clean)
        inbox = []
        for rid, cached in data['inbox'].items():
            inbox.append({'relationship_id': rid, **pick(cached, ('checked_at',)),
                'records': [pick(row, ('id', 'revision', 'published_at', 'updated_at')) |
                    {'card': pick(row['card'], CARD)} for row in cached.get('records', {}).values()]})
        yield 'updates.json', {'publications': publications, 'subscriptions': subscriptions, 'inbox': inbox}

    def saved_documents(self):
        store = self.app.state.friends.content.imports
        with file_transaction(store.root / '.imports.lock'):
            snapshot = store.list()
        index = []
        for number, listed in enumerate(snapshot):
            with file_transaction(store.root / '.imports.lock'):
                row = store.get(listed['import_id'])
                if row != listed:
                    raise ExportError('privacy_export_source_changed', 'saved_documents')
                raw, body = store.read_bytes(row['import_id']), store.body(row['import_id'])
            clean = pick(row, ('import_id', 'filename', 'mime', 'kind', 'state', 'size_bytes', 'sha256',
                'extracted_sha256', 'extraction_status', 'extracted_bytes', 'created_at_unix'))
            clean['source'] = pick(row.get('source', {}), ('peer_id', 'card_id', 'title', 'source_url',
                                                         'publisher_peer_id', 'content_truncated'))
            # Fixed names, never a supplied filename or storage path.
            suffix = 'pdf' if row['mime'] == 'application/pdf' else 'txt'
            clean.update(original=f'{number}/original.{suffix}', extracted=f'{number}/text.json')
            yield clean['original'], raw
            yield clean['extracted'], pick(body, ('text', 'truncated', 'filename', 'mime'))
            index.append(clean)
        yield 'index.json', {'documents': index}

    def offline_reading(self):
        service = self.app.state.offline_reading.service
        store = service.store
        with file_transaction(store.lock):
            snapshot = store.read()['records']
        index = []
        for number, listed in enumerate(snapshot.values()):
            clean = pick(listed, ('key', 'item_id', 'state', 'error_code', 'verified_bytes'))
            clean['reference'] = pick(listed.get('reference', {}), ('item_id', 'title', 'source', 'url'))
            current = listed.get('current')
            clean['current'] = pick(current, ('job_id', 'downloaded_at', 'partial')) if current else None
            if current:
                with file_transaction(store.lock):
                    latest = store.read()['records'].get(listed['key'], {})
                    if latest.get('current') != current:
                        raise ExportError('privacy_export_source_changed', 'offline_reading')
                    body = service.read(listed['item_id'])
                    images = []
                    for image in body['images']:
                        metadata = pick(image, ('index', 'alt', 'state', 'error_code', 'mime'))
                        if image['state'] == 'verified':
                            raw, mime = service.image(listed['item_id'], image['index'], job_id=current['job_id'])
                            suffix = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}.get(mime, 'bin')
                            metadata['file'] = f"{number}/image-{image['index']}.{suffix}"
                            images.append((metadata['file'], raw))
                        metadata['mime'] = image.get('mime')
                        image.clear()
                        image.update(metadata)
                clean['article'] = f'{number}/article.json'
                yield clean['article'], body
                yield from images
            index.append(clean)
        yield 'index.json', {'records': index, 'unfinished_checkpoints_included': False}

    def devices(self):
        state = self.app.state.device_sync
        pairs = []
        for row in state.service.list():
            pairs.append(pick(row, ('id', 'role', 'status', 'expires', 'scopes', 'remote_scopes', 'paused',
                'remote_paused', 'revision', 'effective_scopes', 'removal_pending')) |
                {'device': pick(row['device'], ('name', 'peer_id', 'actor', 'endpoint'))})
        yield 'relationships.json', {'devices': pairs}
        replica = state.transfer.replica
        with file_transaction(replica.lock):
            _, data = replica._read()
        for number, row in enumerate(data['records'].values()):
            yield f'replicas/{number}.json', {'scope': row['scope'], **records.view(row['scope'], row['id'], row['record'])}

    def ai_permissions(self):
        yield 'grants.json', {'grants': self.app.state.ai_access.store.list()}
