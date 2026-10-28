"""Reviewed offline-copy deletion with a durable receipt and exact-file retries."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy

from ..atomic_io import _fsync_dir
from ..crypto import canonical_json
from ..file_transactions import file_transaction
from .fetch import OfflineError
from .store import MAX_ITEM, MAX_RECORDS

VERSION = 'ryn.offline-cleanup.v1'
MAX_SEQUENCE = 2**53 - 1
ACTIVE = {'queued', 'downloading', 'verifying', 'cancel_requested'}


def receipt(data):
    value = data.get('cleanup')
    if value is None and 'cleanup' not in data:
        return None
    if not isinstance(value, dict) or value.get('version') != VERSION:
        raise OfflineError('offline_cleanup_version_unsupported')
    if (not isinstance(value.get('review_token'), str) or not re.fullmatch('[a-f0-9]{64}', value['review_token'])
            or type(value.get('sequence')) is not int or not 1 <= value['sequence'] <= MAX_SEQUENCE
            or type(value.get('done')) is not bool or not isinstance(value.get('reviewed'), dict)
            or not isinstance(value.get('files'), list) or len(value['files']) > MAX_RECORDS * 2):
        raise OfflineError('offline_cleanup_unreadable')
    item = value.get('item_id')
    if item is not None and (not isinstance(item, str) or not 1 <= len(item.encode()) <= 512):
        raise OfflineError('offline_cleanup_unreadable')
    seen = set()
    for row in value['files']:
        if (not isinstance(row, dict) or not isinstance(row.get('job_id'), str)
                or not re.fullmatch('[a-f0-9]{32}', row['job_id']) or row['job_id'] in seen
                or not isinstance(row.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', row['sha256'])
                or type(row.get('bytes')) is not int or not 0 <= row['bytes'] <= MAX_ITEM):
            raise OfflineError('offline_cleanup_unreadable')
        seen.add(row['job_id'])
    reviewed = value['reviewed']
    if (any(type(reviewed.get(key)) is not int or not 0 <= reviewed[key] <= MAX_RECORDS for key in ('copies', 'pending'))
            or type(reviewed.get('bytes')) is not int or reviewed['bytes'] != sum(row['bytes'] for row in value['files'])):
        raise OfflineError('offline_cleanup_unreadable')
    return value


def public(value):
    if value is None:
        return None
    return {'review_token': value['review_token'], 'item_id': value['item_id'], 'sequence': value['sequence'],
            'done': value['done'], 'copies': value['reviewed']['copies'], 'bytes': value['reviewed']['bytes']}


class OfflineCleanup:
    def __init__(self, store):
        self.store = store

    def _file(self, job_id):
        path = self.store._path(job_id)
        if not path.exists():
            return None
        with path.open('rb') as handle:
            raw = handle.read(MAX_ITEM + 1)
        if len(raw) > MAX_ITEM:
            raise OfflineError('offline_cleanup_copy_too_large')
        return {'job_id': job_id, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}

    def _plan(self, data, item_id):
        previous = receipt(data)
        if previous and not previous['done']:
            raise OfflineError('offline_cleanup_pending')
        sequence = (previous or {}).get('sequence', 0) + 1
        if sequence > MAX_SEQUENCE:
            raise OfflineError('offline_cleanup_limit')
        rows = [row for row in data['records'].values() if item_id is None or row['item_id'] == item_id]
        jobs = {(row.get('current') or {}).get('job_id') for row in rows}
        jobs.update((row.get('job') or {}).get('id') for row in rows)
        files = [file for job in sorted(jobs - {None}) if (file := self._file(job)) is not None]
        token = hashlib.sha256(canonical_json([item_id, sequence, rows, files])).hexdigest()
        reviewed = {'copies': sum(bool(row.get('current')) for row in rows), 'bytes': sum(row['bytes'] for row in files),
                    'pending': sum(row['state'] in ACTIVE for row in rows)}
        return {'version': VERSION, 'review_token': token, 'item_id': item_id, 'sequence': sequence,
                'reviewed': reviewed, 'files': files, 'done': False}, jobs - {None}

    def preview(self, item_id=None):
        with file_transaction(self.store.lock):
            plan, _ = self._plan(self.store.read(), item_id)
            return {'review_token': plan['review_token'], **plan['reviewed']}

    def clear(self, *, review_token, item_id=None):
        if not isinstance(review_token, str) or not re.fullmatch('[a-f0-9]{64}', review_token):
            raise OfflineError('offline_clear_review_required')
        with file_transaction(self.store.lock):
            existing = receipt(self.store.read())
            if existing and existing['review_token'] == review_token:
                if existing['item_id'] != item_id:
                    raise OfflineError('offline_clear_review_changed')
                return self._finish(existing)

            def prepare(data):
                plan, jobs = self._plan(data, item_id)
                if plan['review_token'] != review_token:
                    raise OfflineError('offline_clear_review_changed')
                self.store.ensure_supported(jobs)
                for row in data['records'].values():
                    if item_id is None or row['item_id'] == item_id:
                        row.update(current=None, job=None, state='cleared', error_code='', verified_bytes=0)
                # The worker fence and retry receipt commit in the same file.
                data['cleanup'] = plan
                return plan
            return self._finish(self.store.mutate(prepare))

    def _finish(self, plan):
        if not plan['done']:
            # Do not call generic GC: later downloads and unreviewed orphan files
            # are outside this operation, even after a lost response or restart.
            for old in plan['files']:
                current = self._file(old['job_id'])
                if current is None:
                    continue
                if any(current[key] != old[key] for key in ('job_id', 'sha256', 'bytes')):
                    raise OfflineError('offline_cleanup_copy_changed')
                self.store.ensure_supported([old['job_id']])
                self.store._path(old['job_id']).unlink()
                _fsync_dir(self.store.downloads)
            def complete(data):
                current = receipt(data)
                if current['review_token'] != plan['review_token']:
                    raise OfflineError('offline_clear_review_changed')
                current['done'] = True
                return current
            plan = self.store.mutate(complete)
        return {'review_token': plan['review_token'], **plan['reviewed'], 'freed_bytes': plan['reviewed']['bytes']}

    def _remaining(self, data):
        plan = receipt(data)
        if plan is None or plan['done']:
            raise OfflineError('offline_cleanup_not_pending')
        files, remaining = [], []
        for old in plan['files']:
            current = self._file(old['job_id'])
            files.append({**old, **current} if current else old)
            if current:
                remaining.append(current)
        self.store.ensure_supported(row['job_id'] for row in remaining)
        token = hashlib.sha256(canonical_json([plan['review_token'], files])).hexdigest()
        return plan, files, {'review_token': token, 'files': len(remaining), 'bytes': sum(row['bytes'] for row in remaining)}

    def review_remaining(self):
        with file_transaction(self.store.lock):
            return self._remaining(self.store.read())[2]

    def approve_remaining(self, *, review_token):
        with file_transaction(self.store.lock):
            prior = receipt(self.store.read())
            if prior and prior['done'] and review_token == hashlib.sha256(canonical_json([prior['review_token'], prior['files']])).hexdigest():
                return self._finish(prior)
            def approve(data):
                plan, files, preview = self._remaining(data)
                if preview['review_token'] != review_token:
                    raise OfflineError('offline_clear_review_changed')
                plan['files'] = deepcopy(files)
                plan['reviewed']['bytes'] = sum(row['bytes'] for row in files)
                return plan
            return self._finish(self.store.mutate(approve))
