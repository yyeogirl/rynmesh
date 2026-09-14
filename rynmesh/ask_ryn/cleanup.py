"""Durable steps for clearing reviewed conversation copies on this node.

No network calls, automatic consent or remote-erasure claims. Public routes must
require the Owner guard. Source/replica locks are never held during index work.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path

from ..atomic_io import _fsync_dir, atomic_write_json, migration_backup, read_json
from ..crypto import canonical_json
from ..device_sync import records
from ..file_transactions import file_transaction
from ..local_search.index import SearchError
from ..services import peer_box
from .privacy import ConversationPrivacy
from .store import SYNC_VERSION, ConversationError

PREVIOUS_VERSION = 'ryn.conversation-cleanup.v1'
VERSION = 'ryn.conversation-cleanup.v2'
CHANNEL = b'rynmesh-conversation-cleanup-v1'
MAX_FILE = 8 * 1024 * 1024
MAX_HISTORY = 32
STEPS = ('source', 'replica', 'backups', 'search', 'orders')


class ConversationCleanup:
    def __init__(self, home, *, source, replica, pairing_lock, search, erase_order_results):
        self.path = Path(home).resolve() / 'privacy' / 'conversation-cleanup.json'
        self.lock = self.path.parent / '.conversation-cleanup.lock'
        self.source, self.replica, self.pairing_lock = source, replica, pairing_lock
        self.search, self.erase_order_results = search, erase_order_results
        self.privacy = ConversationPrivacy(source)
        self.roots = {'source': source.path.parent.resolve(), 'replica': replica.path.parent.resolve(),
                      'search': search.path.parent.resolve()}
        self.names = {'source': source.path.name, 'replica': replica.path.name, 'search': search.path.name}

    def _read(self):
        if not self.path.exists():
            return {}, {'version': VERSION, 'jobs': {}, 'next_generation': 0}
        envelope = read_json(self.path, max_bytes=MAX_FILE)
        if not isinstance(envelope, dict) or envelope.get('version') not in {PREVIOUS_VERSION, VERSION}:
            raise ConversationError('ask_cleanup_version_unsupported')
        try:
            raw = peer_box.open_sealed(self.source.key, self.source.pub,
                envelope['nonce'], envelope['ciphertext'], info=CHANNEL)
            data = json.loads(raw)
        except Exception:
            raise ConversationError('ask_cleanup_unreadable') from None
        if not isinstance(data, dict) or data.get('version') != envelope['version']:
            raise ConversationError('ask_cleanup_version_unsupported')
        if not isinstance(data.get('jobs'), dict) or len(data['jobs']) > MAX_HISTORY:
            raise ConversationError('ask_cleanup_unreadable')
        generations = set()
        for identifier, job in data['jobs'].items():
            if (not re.fullmatch('[a-f0-9]{64}', identifier) or not isinstance(job, dict)
                    or job.get('id') != identifier or job.get('review_token') != identifier
                    or not isinstance(job.get('done'), list) or job['done'] != list(STEPS[:len(job['done'])])
                    or type(job.get('cancelled', False)) is not bool or job.get('cancelled') and job['done']
                    or not isinstance(job.get('plan'), dict)):
                raise ConversationError('ask_cleanup_unreadable')
            generation = job['plan'].get('review_generation')
            if type(generation) is not int or not 0 <= generation < records.MAX_COUNTER or generation in generations:
                raise ConversationError('ask_cleanup_unreadable')
            generations.add(generation)
            if type(job.get('compacted', False)) is not bool:
                raise ConversationError('ask_cleanup_unreadable')
            if job.get('compacted'):
                if (data['version'] == PREVIOUS_VERSION or not self._terminal(job)
                        or type(job['plan'].get('identity_count')) is not int
                        or not 0 <= job['plan']['identity_count'] <= 10000):
                    raise ConversationError('ask_cleanup_unreadable')
            elif not isinstance(job['plan'].get('identifiers'), list):
                raise ConversationError('ask_cleanup_unreadable')
        if data['version'] == PREVIOUS_VERSION:
            # Read-only inspection does not migrate the file or create backups.
            if 'next_generation' in data:
                raise ConversationError('ask_cleanup_version_unsupported')
            data.update(version=VERSION, next_generation=max(generations, default=-1) + 1)
        counter = data.get('next_generation')
        if type(counter) is not int or not 0 <= counter <= records.MAX_COUNTER or any(value >= counter for value in generations):
            raise ConversationError('ask_cleanup_unreadable')
        return envelope, data

    @staticmethod
    def _terminal(job):
        return bool(job.get('cancelled')) or job['done'] == list(STEPS)

    @staticmethod
    def _compact(job):
        if not ConversationCleanup._terminal(job) or job.get('compacted'):
            return
        plan = job['plan']
        plan['identity_count'] = len(plan['identifiers'])
        for key in ('source', 'additional_identifiers', 'identifiers', 'replica_revision', 'backups', 'task_ids'):
            plan.pop(key, None)
        job['compacted'] = True

    def _save(self, envelope, data):
        for job in data['jobs'].values():
            self._compact(job)
        if envelope.get('version') == PREVIOUS_VERSION:
            if migration_backup(self.path, suffix='.v1.migrated', max_bytes=MAX_FILE) is None:
                raise ConversationError('ask_cleanup_backup_failed')
        nonce, ciphertext = peer_box.seal(self.source.key, self.source.pub, canonical_json(data), info=CHANNEL)
        atomic_write_json(self.path, {**envelope, 'version': VERSION, 'nonce': nonce, 'ciphertext': ciphertext}, max_bytes=MAX_FILE)
        envelope['version'] = VERSION

    def _locks(self):
        stack = ExitStack()
        try:
            # Index builders hold their file lock before reading the source.
            # Follow that order, then the pairing -> source -> replica order.
            if not self.search.writer.acquire(blocking=False):
                raise SearchError('search_index_busy')
            stack.callback(self.search.writer.release)
            for path in (self.search.root / '.index.lock', self.pairing_lock, self.source.lock, self.replica.lock):
                stack.enter_context(file_transaction(path))
            return stack
        except BaseException:
            stack.close()
            raise

    def _artifact(self, scope, name):
        root = self.roots[scope]
        base = self.names[scope]
        allowed = (scope == 'source' and name == base + '.migrated') or bool(
            re.fullmatch(r'\.' + re.escape(base) + r'\.[a-f0-9]{32}\.tmp', name))
        path = root / name
        if not allowed or path.is_symlink() or path.parent.resolve() != root or path.resolve().parent != root:
            raise ConversationError('ask_cleanup_backup_changed')
        return path

    @staticmethod
    def _digest(path):
        with path.open('rb') as handle:
            return hashlib.file_digest(handle, 'sha256').hexdigest()

    def _backups(self):
        result = []
        for scope, root in self.roots.items():
            base = self.names[scope]
            names = [path.name for path in root.glob(f'.{base}.*.tmp')]
            if scope == 'source' and (root / (base + '.migrated')).exists():
                names.append(base + '.migrated')
            if len(names) > 1024:
                raise ConversationError('ask_cleanup_backup_limit')
            for name in sorted(names):
                # Unknown temp names are not files produced by our atomic writer.
                if name.endswith('.tmp') and not re.fullmatch(r'\.' + re.escape(base) + r'\.[a-f0-9]{32}\.tmp', name):
                    continue
                path = self._artifact(scope, name)
                result.append({'scope': scope, 'name': name, 'sha256': self._digest(path), 'bytes': path.stat().st_size})
        return result

    def _plan(self):
        _, replica = self.replica._read()
        extra = sorted(row['id'] for row in replica['records'].values() if row['scope'] == 'conversations')
        preview = self.privacy.preview(additional_identifiers=extra)
        _, source = self.source._read()
        identifiers = set(extra) | set(source['conversations']) | set(source['tombstones']) | set(source['migrations'])
        identifiers.update(run['conversation_id'] for run in self.privacy._runs(source).values())
        if source['version'] == SYNC_VERSION:
            state = self.source._sync_state(source)
            identifiers.update(state.value['entities'])
            identifiers.update(state.value['restores'])
        return {'source': preview, 'additional_identifiers': extra, 'identifiers': sorted(identifiers),
            'replica_revision': records.fingerprint(replica), 'backups': self._backups(),
            'task_ids': sorted(self.privacy._runs(source))}

    def preview(self):
        with file_transaction(self.lock), self._locks():
            _, data = self._read()  # Future journals must not be silently replaced.
            plan = self._plan()
            plan['review_generation'] = data['next_generation']
            return {'review_token': records.fingerprint(plan), **{k: v for k, v in plan['source'].items() if k != 'review_token'},
                'scope': 'reviewed_conversation_copies', 'identities': len(plan['identifiers']),
                'backup_files': len(plan['backups']), 'backup_bytes': sum(row['bytes'] for row in plan['backups']),
                'order_results': len(plan['task_ids']), 'browser_cleanup_required': True,
                'remote_confirmed': False}

    @staticmethod
    def _public(job):
        return {'id': job['id'], 'done': list(job['done']), 'pending': [] if job.get('cancelled') else list(STEPS[len(job['done']):]),
            'cancelled': bool(job.get('cancelled')),
            'local_copies_complete': job['done'] == list(STEPS), 'browser_cleanup_required': True,
            'remote_confirmed': False, 'identities': job['plan']['identity_count'] if job.get('compacted') else len(job['plan']['identifiers']),
            'sequence': job['plan']['review_generation'] + 1}

    def status(self, identifier):
        with file_transaction(self.lock):
            _, data = self._read()
            if identifier not in data['jobs']:
                raise ConversationError('ask_cleanup_not_found')
            return self._public(data['jobs'][identifier])

    def list_status(self):
        with file_transaction(self.lock):
            _, data = self._read()
            return [self._public(job) for job in sorted(data['jobs'].values(),
                key=lambda row: row['plan']['review_generation'], reverse=True)]

    def begin(self, *, review_token):
        if not isinstance(review_token, str) or not re.fullmatch('[a-f0-9]{64}', review_token):
            raise ConversationError('ask_privacy_review_changed')
        with file_transaction(self.lock):
            envelope, data = self._read()
            if review_token not in data['jobs']:
                if any(not job.get('cancelled') and job['done'] != list(STEPS) for job in data['jobs'].values()):
                    raise ConversationError('ask_cleanup_pending')
                if data['next_generation'] >= records.MAX_COUNTER:
                    raise ConversationError('ask_cleanup_limit')
                with self._locks():
                    plan = self._plan()
                    plan['review_generation'] = data['next_generation']
                    if records.fingerprint(plan) != review_token:
                        raise ConversationError('ask_privacy_review_changed')
                    if plan['source']['active_tasks']:
                        raise ConversationError('ask_privacy_tasks_active')
                    # Only terminal summaries leave the bounded activity list.
                    # The monotonic counter is retained even when an old job is
                    # no longer listed, so its old review cannot be reused.
                    if len(data['jobs']) >= MAX_HISTORY:
                        oldest = min(data['jobs'], key=lambda key: data['jobs'][key]['plan']['review_generation'])
                        del data['jobs'][oldest]
                    data['jobs'][review_token] = {'id': review_token, 'review_token': review_token, 'plan': plan, 'done': []}
                    data['next_generation'] += 1
                    self._save(envelope, data)
                    # Hold review locks until the irreversible source decision
                    # commits; no edit can slip between review and this step.
                    self._step_source(data['jobs'][review_token])
                    data['jobs'][review_token]['done'].append('source')
                    self._save(envelope, data)
            return self.resume(review_token)

    def _step_source(self, job):
        plan = job['plan']
        self.privacy.erase_source(review_token=plan['source']['review_token'],
                                  additional_identifiers=plan['additional_identifiers'])

    def cancel_uncommitted(self, identifier):
        """Only abandon a review that has not committed source erasure."""
        with file_transaction(self.lock), self._locks():
            envelope, data = self._read()
            job = data['jobs'].get(identifier)
            if job is None:
                raise ConversationError('ask_cleanup_not_found')
            if job.get('cancelled'):
                return self._public(job)
            _, source = self.source._read()
            receipt = self.privacy._receipt(source)
            if job['done'] or receipt and receipt.get('review_token') == job['plan']['source']['review_token']:
                raise ConversationError('ask_cleanup_already_started')
            job['cancelled'] = True
            self._save(envelope, data)
            return self._public(job)

    def _remaining_backup_review(self, job):
        if job['done'] != ['source', 'replica']:
            raise ConversationError('ask_cleanup_backup_review_unavailable')
        rows = []
        for old in job['plan']['backups']:
            path = self._artifact(old['scope'], old['name'])
            if path.exists():
                rows.append({**old, 'sha256': self._digest(path), 'bytes': path.stat().st_size})
        return rows

    def review_backups(self, identifier):
        with file_transaction(self.lock), self._locks():
            _, data = self._read()
            job = data['jobs'].get(identifier)
            if job is None:
                raise ConversationError('ask_cleanup_not_found')
            rows = self._remaining_backup_review(job)
            return {'review_token': records.fingerprint([identifier, rows]), 'files': len(rows),
                    'bytes': sum(row['bytes'] for row in rows)}

    def approve_backups(self, identifier, *, review_token):
        with file_transaction(self.lock), self._locks():
            envelope, data = self._read()
            job = data['jobs'].get(identifier)
            if job is None:
                raise ConversationError('ask_cleanup_not_found')
            rows = self._remaining_backup_review(job)
            if records.fingerprint([identifier, rows]) != review_token:
                raise ConversationError('ask_cleanup_backup_changed')
            job['plan']['backups'] = rows
            self._save(envelope, data)
            # No mutation of the remaining artifact can slip between approval
            # and unlink through another cooperating source/replica writer.
            self._step_backups(job)
            job['done'].append('backups')
            self._save(envelope, data)
        return self.resume(identifier)

    def _step_replica(self, job):
        def erase(data):
            for identifier in job['plan']['identifiers']:
                key = self.replica._key('conversations', identifier)
                old = data['records'].get(key, {}).get('record', records.empty())
                if not old['erased']:
                    old = records.write('conversations', identifier, old, self.replica.actor, None, erase=True)
                data['records'][key] = {'scope': 'conversations', 'id': identifier, 'record': old}
        self.replica._mutate(erase)

    def _step_backups(self, job):
        for row in job['plan']['backups']:
            path = self._artifact(row['scope'], row['name'])
            if not path.exists():
                continue  # A previous attempt may already have removed it.
            if self._digest(path) != row['sha256']:
                raise ConversationError('ask_cleanup_backup_changed')
            path.unlink()
            _fsync_dir(path.parent)

    def resume(self, identifier):
        with file_transaction(self.lock):
            envelope, data = self._read()
            if identifier not in data['jobs']:
                raise ConversationError('ask_cleanup_not_found')
            job = data['jobs'][identifier]
            if job.get('cancelled'):
                return self._public(job)
            for step in STEPS[len(job['done']):]:
                if step in {'source', 'replica', 'backups'}:
                    with self._locks():
                        getattr(self, '_step_' + step)(job)
                elif step == 'search':
                    self.search.rebuild(force=True)
                else:
                    self.erase_order_results(deepcopy(job['plan']['task_ids']))
                job['done'].append(step)
                self._save(envelope, data)
            return self._public(job)
