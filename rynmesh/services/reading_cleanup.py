"""Durable owner-reviewed cleanup of local reading sources and derived copies."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path

from ..atomic_io import _fsync_dir, atomic_write_json, read_json
from ..crypto import canonical_json
from ..device_sync import records
from ..device_sync.reading import SCOPES
from ..file_transactions import file_transaction
from ..local_search.index import SearchError
from . import peer_box
from .consumption import PRIVACY_VERSION, ConsumptionError
from .reading_privacy import ReadingPrivacy

VERSION = 'ryn.reading-cleanup.v1'
CHANNEL = b'rynmesh-reading-cleanup-v1'
MAX_FILE = 4 * 1024 * 1024
STEPS = ('source', 'replica', 'backups', 'search')


class ReadingCleanup:
    def __init__(self, home, *, source, replica, pairing_lock, search):
        self.path = Path(home).resolve() / 'privacy' / 'reading-cleanup.json'
        self.lock = self.path.parent / '.reading-cleanup.lock'
        self.source, self.replica, self.pairing_lock, self.search = source, replica, pairing_lock, search
        self.privacy = ReadingPrivacy(source, actor=replica.actor)
        self.paths = {'source': source.path.resolve(), 'replica': replica.path.resolve(), 'search': search.path.resolve()}

    def _read(self):
        if not self.path.exists():
            return {}, {'version': VERSION, 'sequence': 0, 'job': None}
        envelope = read_json(self.path, max_bytes=MAX_FILE)
        if not isinstance(envelope, dict) or envelope.get('version') != VERSION:
            raise ConsumptionError('reading_cleanup_version_unsupported')
        try:
            data = json.loads(peer_box.open_sealed(self.replica.key, self.replica.pub,
                                                 envelope['nonce'], envelope['ciphertext'], info=CHANNEL))
        except Exception:
            raise ConsumptionError('reading_cleanup_unreadable') from None
        if not isinstance(data, dict) or data.get('version') != VERSION:
            raise ConsumptionError('reading_cleanup_version_unsupported')
        if type(data.get('sequence')) is not int or not 0 <= data['sequence'] <= records.MAX_COUNTER:
            raise ConsumptionError('reading_cleanup_unreadable')
        job = data.get('job')
        if job is not None and (not isinstance(job, dict) or not isinstance(job.get('id'), str)
                or not re.fullmatch('[a-f0-9]{64}', job['id']) or not isinstance(job.get('done'), list)
                or job['done'] != list(STEPS[:len(job['done'])]) or not isinstance(job.get('plan'), dict)
                or type(job.get('cancelled', False)) is not bool or job.get('cancelled') and job['done']):
            raise ConsumptionError('reading_cleanup_unreadable')
        if job is not None:
            plan = job['plan']
            source = plan.get('source')
            if (type(plan.get('sequence')) is not int or plan['sequence'] != data['sequence'] or data['sequence'] < 1
                    or not isinstance(source, dict) or not isinstance(source.get('review_token'), str)
                    or not re.fullmatch('[a-f0-9]{64}', source['review_token'])
                    or not isinstance(plan.get('replica_revision'), str)
                    or not re.fullmatch('[a-f0-9]{64}', plan['replica_revision'])
                    or not isinstance(plan.get('backups'), list) or len(plan['backups']) > 3075):
                raise ConsumptionError('reading_cleanup_unreadable')
            seen = set()
            for row in plan['backups']:
                if (not isinstance(row, dict) or not isinstance(row.get('scope'), str) or row['scope'] not in self.paths
                        or not isinstance(row.get('name'), str) or not isinstance(row.get('versions'), list)
                        or not 1 <= len(row['versions']) <= 2 or (row['scope'], row['name']) in seen):
                    raise ConsumptionError('reading_cleanup_unreadable')
                seen.add((row['scope'], row['name']))
                self._artifact(row['scope'], row['name'])
                for version in row['versions']:
                    if (not isinstance(version, dict) or not isinstance(version.get('sha256'), str)
                            or not re.fullmatch('[a-f0-9]{64}', version['sha256'])
                            or type(version.get('bytes')) is not int or version['bytes'] < 0):
                        raise ConsumptionError('reading_cleanup_unreadable')
        return envelope, data

    def _save(self, envelope, data):
        nonce, ciphertext = peer_box.seal(self.replica.key, self.replica.pub, canonical_json(data), info=CHANNEL)
        atomic_write_json(self.path, {**envelope, 'version': VERSION, 'nonce': nonce, 'ciphertext': ciphertext}, max_bytes=MAX_FILE)

    def _locks(self):
        stack = ExitStack()
        try:
            if not self.search.writer.acquire(blocking=False):
                raise SearchError('search_index_busy')
            stack.callback(self.search.writer.release)
            for path in (self.search.root / '.index.lock', self.pairing_lock, self.source.lock_path, self.replica.lock):
                stack.enter_context(file_transaction(path))
            return stack
        except BaseException:
            stack.close()
            raise

    def _artifact(self, scope, name):
        base = self.paths[scope]
        allowed = scope == 'source' and name in {base.name + suffix for suffix in ('.migrated', '.v2.migrated', '.v3.migrated')}
        allowed = allowed or bool(re.fullmatch(r'\.' + re.escape(base.name) + r'\.[a-f0-9]{32}\.tmp', name))
        path = base.parent / name
        if not allowed or path.is_symlink() or path.resolve().parent != base.parent:
            raise ConsumptionError('reading_cleanup_backup_changed')
        return path

    @staticmethod
    def _version(path):
        with path.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        return {'sha256': digest, 'bytes': path.stat().st_size}

    def _backups(self):
        rows = {}
        for scope, base in self.paths.items():
            candidates = list(base.parent.glob(f'.{base.name}.*.tmp'))
            if len(candidates) > 1024:
                raise ConsumptionError('reading_cleanup_backup_limit')
            if scope == 'source':
                candidates += [base.with_name(base.name + suffix) for suffix in ('.migrated', '.v2.migrated', '.v3.migrated')]
            for path in candidates:
                if path.name.endswith('.tmp') and not re.fullmatch(r'\.' + re.escape(base.name) + r'\.[a-f0-9]{32}\.tmp', path.name):
                    continue
                path = self._artifact(scope, path.name)
                if path.exists():
                    rows[(scope, path.name)] = {'scope': scope, 'name': path.name, 'versions': [self._version(path)]}
        document, _, _ = self.source._document()
        version = document.get('version')
        if version != PRIVACY_VERSION and self.source.path.exists():
            suffix = '.v2.migrated' if version == 'ryn.consumption.v2' else '.v3.migrated' if version == 'ryn.consumption.v3' else '.migrated'
            name = self.source.path.name + suffix
            row = rows.setdefault(('source', name), {'scope': 'source', 'name': name, 'versions': []})
            upcoming = self._version(self.source.path)
            if upcoming not in row['versions']:
                row['versions'].append(upcoming)
        return [rows[key] for key in sorted(rows)]

    def _replica_snapshot(self):
        _, replica = self.replica._read()
        extra = [{key: row[key] for key in ('scope', 'id', 'record')} for row in replica['records'].values() if row['scope'] in SCOPES]
        return replica, extra

    def _plan(self, sequence):
        replica, extra = self._replica_snapshot()
        return {'sequence': sequence, 'source': self.privacy.preview(additional_records=extra),
                'replica_revision': records.fingerprint(replica), 'backups': self._backups()}

    @staticmethod
    def _terminal(job):
        return job is None or job.get('cancelled') or job['done'] == list(STEPS)

    @staticmethod
    def _public(data):
        job = data['job']
        if job is None:
            return None
        return {'id': job['id'], 'sequence': data['sequence'], 'done': job['done'],
                'pending': [] if job.get('cancelled') else list(STEPS[len(job['done']):]),
                'cancelled': bool(job.get('cancelled')), 'local_copies_complete': job['done'] == list(STEPS),
                'remote_confirmed': False, 'scope': 'reviewed_reading_copies'}

    def status(self):
        with file_transaction(self.lock):
            return self._public(self._read()[1])

    def preview(self):
        with file_transaction(self.lock), self._locks():
            _, data = self._read()
            if not self._terminal(data['job']):
                raise ConsumptionError('reading_cleanup_pending')
            plan = self._plan(data['sequence'] + 1)
            return {**plan['source'], 'review_token': records.fingerprint(plan), 'scope': 'reviewed_reading_copies',
                    'backup_files': len(plan['backups']), 'remote_confirmed': False}

    def begin(self, *, review_token):
        with file_transaction(self.lock):
            envelope, data = self._read()
            if data['job'] is None or data['job']['id'] != review_token:
                if not self._terminal(data['job']):
                    raise ConsumptionError('reading_cleanup_pending')
                if data['sequence'] >= records.MAX_COUNTER:
                    raise ConsumptionError('reading_cleanup_limit')
                with self._locks():
                    plan = self._plan(data['sequence'] + 1)
                    if records.fingerprint(plan) != review_token:
                        raise ConsumptionError('reading_privacy_review_changed')
                    data['sequence'] += 1
                    data['job'] = {'id': review_token, 'plan': plan, 'done': []}
                    self._save(envelope, data)
                    self._step_source(data['job'])
                    data['job']['done'].append('source')
                    self._save(envelope, data)
            return self.resume(review_token)

    def _receipt(self, job):
        document, _, _ = self.source._document()
        receipt = document.get('privacy_erasure') if document.get('version') == PRIVACY_VERSION else None
        return receipt if receipt and receipt['review_token'] == job['plan']['source']['review_token'] else None

    def _step_source(self, job):
        if self._receipt(job):
            return
        replica, extra = self._replica_snapshot()
        if records.fingerprint(replica) != job['plan']['replica_revision']:
            raise ConsumptionError('reading_privacy_review_changed')
        self.privacy.erase_source(review_token=job['plan']['source']['review_token'], additional_records=extra)

    def _step_replica(self, job):
        receipt = self._receipt(job)
        if receipt is None:
            raise ConsumptionError('reading_privacy_review_changed')

        def clear(data):
            for key, row in receipt['barriers'].items():
                old = data['records'].get(key, {}).get('record', records.empty())
                merged = records.merge(row['scope'], row['id'], old, row['record'])
                data['records'][key] = {**deepcopy(row), 'record': merged}
        self.replica._mutate(clear)

    def _step_backups(self, job):
        for row in job['plan']['backups']:
            path = self._artifact(row['scope'], row['name'])
            if not path.exists():
                continue
            if self._version(path) not in row['versions']:
                raise ConsumptionError('reading_cleanup_backup_changed')
            path.unlink()
            _fsync_dir(path.parent)

    def _remaining(self, job):
        if job['done'] != ['source', 'replica']:
            raise ConsumptionError('reading_cleanup_backup_review_unavailable')
        result = []
        for row in job['plan']['backups']:
            path = self._artifact(row['scope'], row['name'])
            if path.exists():
                result.append({**row, 'versions': [self._version(path)]})
        return result

    def review_backups(self, identifier):
        with file_transaction(self.lock), self._locks():
            _, data = self._read()
            job = data['job']
            if job is None or job['id'] != identifier:
                raise ConsumptionError('reading_cleanup_not_found')
            remaining = self._remaining(job)
            return {'review_token': records.fingerprint([identifier, remaining]), 'files': len(remaining),
                    'bytes': sum(row['versions'][0]['bytes'] for row in remaining)}

    def approve_backups(self, identifier, *, review_token):
        if not isinstance(review_token, str) or not re.fullmatch('[a-f0-9]{64}', review_token):
            raise ConsumptionError('reading_cleanup_backup_changed')
        with file_transaction(self.lock), self._locks():
            envelope, data = self._read()
            job = data['job']
            if job is None or job['id'] != identifier:
                raise ConsumptionError('reading_cleanup_not_found')
            if job.get('backup_approval') != review_token:
                remaining = self._remaining(job)
                if records.fingerprint([identifier, remaining]) != review_token:
                    raise ConsumptionError('reading_cleanup_backup_changed')
                job['plan']['backups'] = remaining
                job['backup_approval'] = review_token
                self._save(envelope, data)
            if job['done'] == ['source', 'replica']:
                self._step_backups(job)
                job['done'].append('backups')
                self._save(envelope, data)
        return self.resume(identifier)

    def resume(self, identifier):
        with file_transaction(self.lock):
            envelope, data = self._read()
            job = data['job']
            if job is None or identifier != job['id']:
                raise ConsumptionError('reading_cleanup_not_found')
            if job.get('cancelled'):
                return self._public(data)
            for step in STEPS[len(job['done']):]:
                if step == 'search':
                    self.search.rebuild(force=True)
                else:
                    with self._locks():
                        getattr(self, '_step_' + step)(job)
                job['done'].append(step)
                self._save(envelope, data)
            return self._public(data)

    def cancel_uncommitted(self, identifier):
        with file_transaction(self.lock), self._locks():
            envelope, data = self._read()
            job = data['job']
            if job is None or job['id'] != identifier:
                raise ConsumptionError('reading_cleanup_not_found')
            if job['done'] or self._receipt(job):
                raise ConsumptionError('reading_cleanup_already_started')
            job['cancelled'] = True
            self._save(envelope, data)
            return self._public(data)
