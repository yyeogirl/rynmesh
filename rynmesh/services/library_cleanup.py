"""Reviewed managed-file deletion, fenced before bytes become unavailable."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy

from ..atomic_io import _fsync_dir, atomic_write_json, migration_backup, read_json
from ..device_sync.records import MAX_COUNTER, fingerprint
from ..file_transactions import file_transaction
from .library_imports import _ID, VERSION, LibraryImportError

CONTROL_BYTES = 4 * 1024 * 1024
MAX_FILES = 16000
TOKEN = re.compile(r'[a-f0-9]{64}')
JOB_VERSION = 'ryn.library-cleanup.v1'


def control(store):
    path = store.root / 'control.json'
    value = read_json(path, max_bytes=CONTROL_BYTES) if path.exists() else {'version': 1, 'generation': 0}
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] not in (1, 2):
        raise LibraryImportError('library_import_version_unsupported')
    if type(value.get('generation')) is not int or not 0 <= value['generation'] <= MAX_COUNTER:
        raise LibraryImportError('library_import_corrupt')
    job = value.get('cleanup')
    if value['version'] == 1 and job is not None:
        raise LibraryImportError('library_import_version_unsupported')
    if job is not None:
        if not isinstance(job, dict) or job.get('version') != JOB_VERSION:
            raise LibraryImportError('library_import_version_unsupported')
        if (not isinstance(job.get('id'), str) or not TOKEN.fullmatch(job['id'])
                or job.get('done') not in (['source'], ['source', 'files'])
                or type(job.get('documents')) is not int or not 0 <= job['documents'] <= 2000
                or type(job.get('sequence')) is not int or not 1 <= job['sequence'] <= value['generation']
                or 'scope' not in job or (job['scope'] is not None and (not isinstance(job['scope'], str) or not _ID.fullmatch(job['scope'])))
                or not isinstance(job.get('targets'), list) or len(job['targets']) > 2000
                or any(not isinstance(key, str) or not _ID.fullmatch(key) for key in job['targets'])
                or len(set(job['targets'])) != len(job['targets'])
                or (job['done'] == ['source'] and len(job['targets']) != job['documents'])
                or not isinstance(job.get('files'), list) or len(job['files']) > MAX_FILES):
            raise LibraryImportError('library_import_corrupt')
        seen = set()
        for row in job['files']:
            if (not isinstance(row, dict) or row.get('directory') not in job['targets']
                    or not isinstance(row.get('name'), str) or not safe_name(row['name'])
                    or not isinstance(row.get('sha256'), str) or not TOKEN.fullmatch(row['sha256'])
                    or type(row.get('bytes')) is not int or row['bytes'] < 0
                    or (row['directory'], row['name']) in seen):
                raise LibraryImportError('library_import_corrupt')
            seen.add((row['directory'], row['name']))
    return value


def safe_name(name):
    # Store-generated basenames only. User document names live in metadata.
    return bool(re.fullmatch(r'(?:original\.(?:bin|txt|md|markdown|pdf)|metadata\.json|extracted\.json)(?:\.(?:repaired|migrated))?', name)
                or re.fullmatch(r'\.(?:original\.(?:bin|txt|md|markdown|pdf)|metadata\.json|extracted\.json)(?:\.(?:repaired|migrated))?\.[a-f0-9]{32}\.tmp', name))


def pending_target(store, identifier):
    job = control(store).get('cleanup')
    return bool(job and job['done'] == ['source'] and identifier in job['targets'])


class LibraryCleanup:
    def __init__(self, store):
        self.store = store
        self.lock = store.root / '.imports.lock'

    def _path(self, identifier, name):
        directory = self.store._directory(identifier)
        path = directory / name
        if not safe_name(name) or path.is_symlink() or path.resolve().parent != directory or not path.is_file():
            raise LibraryImportError('library_cleanup_files_changed')
        return path

    def _files(self, targets):
        result = []
        for identifier in targets:
            directory = self.store._directory(identifier)
            paths = sorted(directory.iterdir()) if directory.exists() else []
            for path in paths:
                self._path(identifier, path.name)
            metadata = directory / 'metadata.json'
            for path in (metadata, directory / 'extracted.json'):
                if path.exists():
                    try:
                        value = read_json(path, max_bytes=8 * 1024 * 1024)
                    except OSError:
                        value = None  # Explicit review can remove damaged copies.
                    versions = {VERSION, 'ryn.library-import.v1'} if path == metadata else {1}
                    if isinstance(value, dict) and value.get('version') not in versions:
                        raise LibraryImportError('library_import_version_unsupported')
            for path in paths:
                path = self._path(identifier, path.name)
                with path.open('rb') as handle:
                    digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                result.append({'directory': identifier, 'name': path.name, 'sha256': digest, 'bytes': path.stat().st_size})
                if len(result) > MAX_FILES:
                    raise LibraryImportError('library_cleanup_limit')
        return result

    def _plan(self, scope):
        if scope is not None and (not isinstance(scope, str) or not _ID.fullmatch(scope)):
            raise LibraryImportError('library_import_not_found')
        state = control(self.store)
        if state.get('cleanup', {}).get('done') == ['source']:
            raise LibraryImportError('library_cleanup_pending')
        if state['generation'] >= MAX_COUNTER:
            raise LibraryImportError('library_cleanup_limit')
        targets = ([self.store._directory(scope).name] if scope is not None else
                   sorted(path.name for path in self.store.root.glob('imp_*') if _ID.fullmatch(path.name)))
        if len(targets) > 2000:
            raise LibraryImportError('library_cleanup_limit')
        return {'scope': scope, 'generation': state['generation'], 'targets': targets, 'files': self._files(targets)}

    def preview(self, scope=None):
        with file_transaction(self.lock):
            plan = self._plan(scope)
            return {'review_token': fingerprint(plan), 'scope': scope, 'documents': len(plan['targets']),
                    'files': len(plan['files']), 'bytes': sum(row['bytes'] for row in plan['files'])}

    @staticmethod
    def _public(job):
        return {'id': job['id'], 'sequence': job['sequence'], 'scope': job['scope'],
                'done': deepcopy(job['done']), 'pending': [] if job['done'] == ['source', 'files'] else ['files'],
                'cancelled': False, 'local_copies_complete': job['done'] == ['source', 'files'],
                'remote_confirmed': False, 'removed': job['documents'], 'generation': job['sequence']}

    def status(self):
        with file_transaction(self.lock):
            job = control(self.store).get('cleanup')
            return self._public(job) if job else None

    def _write(self, value):
        path = self.store.root / 'control.json'
        prior = control(self.store)
        if prior['version'] == 1 and path.exists():
            if migration_backup(path, suffix='.v1.migrated', max_bytes=CONTROL_BYTES) is None:
                raise LibraryImportError('library_cleanup_backup_failed')
        atomic_write_json(path, value, max_bytes=CONTROL_BYTES)

    def begin(self, *, review_token, scope=None):
        with file_transaction(self.lock):
            state = control(self.store)
            current = state.get('cleanup')
            if current and current['id'] == review_token:
                if current['scope'] != scope:
                    raise LibraryImportError('library_cleanup_review_changed')
                return self.resume(review_token)
            plan = self._plan(scope)
            if not isinstance(review_token, str) or fingerprint(plan) != review_token:
                raise LibraryImportError('library_cleanup_review_changed')
            generation = state['generation'] + 1
            job = {'version': JOB_VERSION, 'id': review_token, 'sequence': generation, 'scope': scope,
                   'done': ['source'], 'documents': len(plan['targets']), 'targets': plan['targets'], 'files': plan['files']}
            self._write({**state, 'version': 2, 'generation': generation, 'cleanup': job})
            return self.resume(review_token)

    def _current(self, identifier):
        state = control(self.store)
        if state.get('cleanup', {}).get('id') != identifier:
            raise LibraryImportError('library_cleanup_not_found')
        return state

    def resume(self, identifier):
        with file_transaction(self.lock):
            state = self._current(identifier)
            job = state['cleanup']
            if job['done'] == ['source', 'files']:
                return self._public(job)
            # Missing reviewed files are already removed; new/changed files
            # need explicit re-review, never a wider recursive delete.
            current = self._files(job['targets'])
            if any(row not in job['files'] for row in current):
                raise LibraryImportError('library_cleanup_files_changed')
            for row in current:
                self._path(row['directory'], row['name']).unlink()
            for target in job['targets']:
                directory = self.store._directory(target)
                if directory.exists():
                    directory.rmdir()
            _fsync_dir(self.store.root)
            job.update(done=['source', 'files'], targets=[], files=[])
            self._write(state)
            return self._public(job)

    def review_files(self, identifier):
        with file_transaction(self.lock):
            job = self._current(identifier)['cleanup']
            if job['done'] != ['source']:
                raise LibraryImportError('library_cleanup_not_pending')
            files = self._files(job['targets'])
            return {'review_token': fingerprint([identifier, files]), 'files': len(files), 'bytes': sum(row['bytes'] for row in files)}

    def approve_files(self, identifier, *, review_token):
        with file_transaction(self.lock):
            state = self._current(identifier)
            job = state['cleanup']
            if job.get('file_approval') != review_token:
                review = self.review_files(identifier)
                if not isinstance(review_token, str) or review['review_token'] != review_token:
                    raise LibraryImportError('library_cleanup_files_changed')
                job.update(files=self._files(job['targets']), file_approval=review_token)
                self._write(state)
            return self.resume(identifier)
