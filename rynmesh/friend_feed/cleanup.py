"""Reviewed feed-state cleanup with publication and refresh replay barriers."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy

from ..atomic_io import _fsync_dir
from ..device_sync.records import MAX_COUNTER, fingerprint
from ..file_transactions import file_transaction
from .store import MAX_ERASED_PUBLICATIONS, PREVIOUS_VERSION, VERSION, FeedError, identity, revision

CLEANUP_VERSION = 'ryn.friend-feed-cleanup.v1'
STEPS = ('source', 'backups')
TOKEN = re.compile('[a-f0-9]{64}')
MAX_BACKUPS = 1025


def validate_cleanup(value):
    if not isinstance(value, dict) or value.get('version') != CLEANUP_VERSION:
        raise FeedError('feed_cleanup_version_unsupported')
    if (not isinstance(value.get('id'), str) or not TOKEN.fullmatch(value['id'])
            or type(value.get('sequence')) is not int or not 1 <= value['sequence'] <= MAX_COUNTER
            or value.get('done') not in (['source'], list(STEPS))
            or not isinstance(value.get('backups'), list) or len(value['backups']) > MAX_BACKUPS):
        raise FeedError('feed_cleanup_unreadable')
    seen = set()
    for row in value['backups']:
        if (not isinstance(row, dict) or not isinstance(row.get('name'), str)
                or row['name'] in seen or not isinstance(row.get('versions'), list)
                or not 1 <= len(row['versions']) <= 2):
            raise FeedError('feed_cleanup_unreadable')
        if row['name'] != 'state.json.v1.migrated' and not re.fullmatch(r'\.state\.json\.[a-f0-9]{32}\.tmp', row['name']):
            raise FeedError('feed_cleanup_unreadable')
        seen.add(row['name'])
        for version in row['versions']:
            if (not isinstance(version, dict) or not isinstance(version.get('sha256'), str)
                    or not TOKEN.fullmatch(version['sha256']) or type(version.get('bytes')) is not int or version['bytes'] < 0):
                raise FeedError('feed_cleanup_unreadable')
    return value


class FeedCleanup:
    def __init__(self, store):
        self.store = store

    def _artifact(self, name):
        if name != 'state.json.v1.migrated' and not re.fullmatch(r'\.state\.json\.[a-f0-9]{32}\.tmp', name):
            raise FeedError('feed_cleanup_backup_changed')
        path = self.store.path.parent / name
        if path.is_symlink() or path.resolve().parent != self.store.path.parent.resolve():
            raise FeedError('feed_cleanup_backup_changed')
        return path

    @staticmethod
    def _version(path):
        with path.open('rb') as handle:
            result = hashlib.file_digest(handle, 'sha256').hexdigest()
        return {'sha256': result, 'bytes': path.stat().st_size}

    def _backups(self, data):
        paths = list(self.store.path.parent.glob('.state.json.*.tmp'))
        if len(paths) > MAX_BACKUPS - 1:
            raise FeedError('feed_cleanup_backup_limit')
        paths.append(self.store.path.with_name('state.json.v1.migrated'))
        result = {}
        for path in paths:
            if path.name.endswith('.tmp') and not re.fullmatch(r'\.state\.json\.[a-f0-9]{32}\.tmp', path.name):
                continue
            path = self._artifact(path.name)
            if path.exists():
                result[path.name] = {'name': path.name, 'versions': [self._version(path)]}
        if data['version'] == PREVIOUS_VERSION:
            row = result.setdefault('state.json.v1.migrated', {'name': 'state.json.v1.migrated', 'versions': []})
            # Poll bookkeeping re-encrypts the v1 file without changing its
            # reviewed content. Bind the impending backup to that content;
            # capture its exact bytes under the commit lock after verification.
            row['from_reviewed_source'] = True
        return [result[name] for name in sorted(result)]

    @staticmethod
    def _source_revision(data):
        reviewed = {**data,
            'subscriptions': {key: {field: value for field, value in row.items()
                if field not in {'refresh_sequence', 'last_error'}} for key, row in data['subscriptions'].items()},
            'inbox': {key: {field: value for field, value in row.items()
                if field not in {'checked_at', 'next_cursor'}} for key, row in data['inbox'].items()}}
        return fingerprint(reviewed)

    def _plan(self, data):
        if data.get('cleanup') and data['cleanup']['done'] != list(STEPS):
            raise FeedError('feed_cleanup_pending')
        sequence = data.get('cleanup', {}).get('sequence', 0) + 1
        if sequence > MAX_COUNTER:
            raise FeedError('feed_cleanup_limit')
        backups = self._backups(data)
        return {'sequence': sequence, 'source_revision': self._source_revision(data), 'backups': backups}

    def preview(self):
        with file_transaction(self.store.lock):
            data = self.store.read()
            plan = self._plan(data)
            return {'review_token': fingerprint(plan), 'publications': len(data['publications']),
                    'active_publications': sum(bool(row.get('published')) and not row.get('stopped') for row in data['publications'].values()),
                    'subscriptions': sum(bool(row.get('enabled')) for row in data['subscriptions'].values()),
                    'received_updates': sum(len(row.get('records', {})) for row in data['inbox'].values()),
                    'backup_files': len(plan['backups'])}

    @staticmethod
    def _public(value):
        return {'id': value['id'], 'sequence': value['sequence'], 'done': deepcopy(value['done']),
                'pending': list(STEPS[len(value['done']):]), 'cancelled': False,
                'local_copies_complete': value['done'] == list(STEPS), 'remote_confirmed': False,
                'scope': 'reviewed_friend_feed_copies'}

    def status(self):
        value = self.store.read().get('cleanup')
        return self._public(value) if value else None

    def begin(self, *, review_token):
        if not isinstance(review_token, str) or not TOKEN.fullmatch(review_token):
            raise FeedError('feed_cleanup_review_changed')
        with file_transaction(self.store.lock):
            def clear(data):
                if data.get('cleanup', {}).get('id') == review_token:
                    return
                plan = self._plan(data)
                if fingerprint(plan) != review_token:
                    raise FeedError('feed_cleanup_review_changed')
                for backup in plan['backups']:
                    if backup.pop('from_reviewed_source', False):
                        current = self._version(self.store.path)
                        if current not in backup['versions']:
                            backup['versions'].append(current)
                erased = dict(data.get('erased_publications', {}))
                for identifier, row in data['publications'].items():
                    identity(identifier)
                    erased[identifier] = revision(row['revision']) + 1
                if len(erased) > MAX_ERASED_PUBLICATIONS or any(counter > MAX_COUNTER for counter in erased.values()):
                    raise FeedError('feed_cleanup_limit')
                subscriptions = {}
                for rid, row in data['subscriptions'].items():
                    identity(rid)
                    counter = revision(row['revision']) + 1
                    if counter > MAX_COUNTER:
                        raise FeedError('feed_cleanup_limit')
                    # Retain only identity and revision; old refresh tickets and
                    # old enable requests must not restore subscriptions/read marks.
                    subscriptions[rid] = {'relationship_id': rid, 'peer_id': row['peer_id'],
                                          'enabled': False, 'revision': counter}
                value = {'version': CLEANUP_VERSION, 'id': review_token, 'sequence': plan['sequence'],
                         'done': ['source'], 'backups': plan['backups']}
                validate_cleanup(value)
                data.update(version=VERSION, publications={}, subscriptions=subscriptions, inbox={},
                            erased_publications=erased, cleanup=value)
            self.store.mutate(clear)
            return self.resume(review_token)

    def _current(self, data, identifier):
        value = data.get('cleanup')
        if not value or value['id'] != identifier:
            raise FeedError('feed_cleanup_not_found')
        return value

    def resume(self, identifier):
        with file_transaction(self.store.lock):
            value = self._current(self.store.read(), identifier)
            if value['done'] == list(STEPS):
                return self._public(value)
            for row in value['backups']:
                path = self._artifact(row['name'])
                if not path.exists():
                    continue
                if self._version(path) not in row['versions']:
                    raise FeedError('feed_cleanup_backup_changed')
                path.unlink()
                _fsync_dir(path.parent)

            def finish(data):
                current = self._current(data, identifier)
                current['done'] = list(STEPS)
                return self._public(current)
            return self.store.mutate(finish)

    def _remaining(self, value):
        if value['done'] != ['source']:
            raise FeedError('feed_cleanup_backup_review_unavailable')
        result = []
        for row in value['backups']:
            path = self._artifact(row['name'])
            if path.exists():
                result.append({**row, 'versions': [self._version(path)]})
        return result

    def review_backups(self, identifier):
        with file_transaction(self.store.lock):
            rows = self._remaining(self._current(self.store.read(), identifier))
            return {'review_token': fingerprint([identifier, rows]), 'files': len(rows),
                    'bytes': sum(row['versions'][0]['bytes'] for row in rows)}

    def approve_backups(self, identifier, *, review_token):
        if not isinstance(review_token, str) or not TOKEN.fullmatch(review_token):
            raise FeedError('feed_cleanup_backup_changed')
        with file_transaction(self.store.lock):
            def approve(data):
                value = self._current(data, identifier)
                if value.get('backup_approval') == review_token:
                    return
                rows = self._remaining(value)
                if fingerprint([identifier, rows]) != review_token:
                    raise FeedError('feed_cleanup_backup_changed')
                value.update(backups=rows, backup_approval=review_token)
            self.store.mutate(approve)
            return self.resume(identifier)
