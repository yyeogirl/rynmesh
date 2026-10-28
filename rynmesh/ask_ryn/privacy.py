"""Reviewed erasure of the encrypted conversation source.

This is one step of personal-data maintenance, not a whole-node or remote wipe.
The caller must separately reconcile replicas and clear backups, derived indexes,
browser copies and order results. No network call belongs inside this transaction.
"""
from __future__ import annotations

import re
from copy import deepcopy

from ..device_sync import records
from ..file_transactions import file_transaction
from .runs import TERMINAL
from .store import SYNC_VERSION, ConversationError, _identity

VERSION = 'ryn.ask-source-erasure.v1'
MAX_IDENTITIES = 10000
_TOKEN = re.compile(r'^[a-f0-9]{64}$')


class ConversationPrivacy:
    def __init__(self, source):
        self.source = source

    @staticmethod
    def _receipt(data):
        receipt = data.get('privacy_erasure')
        if receipt is not None and (not isinstance(receipt, dict) or receipt.get('version') != VERSION):
            raise ConversationError('ask_history_version_unsupported')
        return receipt

    @staticmethod
    def _additional(value):
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > MAX_IDENTITIES:
            raise ConversationError('ask_history_limit')
        return sorted({_identity(identifier) for identifier in value})

    def _review(self, data, additional):
        self._receipt(data)
        # Bind unsent drafts, local recovery and terminal run data too. A source
        # change after review must not silently expand the erasure operation.
        return records.fingerprint({'actor': self.source.actor, 'additional_identities': additional,
            'data': {key: value for key, value in data.items() if key != 'privacy_erasure'}})

    @staticmethod
    def _runs(data):
        return data.get('runs', {}).get('records', {})

    def preview(self, *, additional_identifiers=None):
        additional = self._additional(additional_identifiers)
        with file_transaction(self.source.lock):
            _, data = self.source._read()
            state = self.source._sync_state(data) if data['version'] == SYNC_VERSION else None
            issues = state.issues() if state else []
            return {'review_token': self._review(data, additional),
                'conversations': len(data['conversations']),
                'recovery_items': sum(len(row['branches']) + len(row['recovery']) for row in issues),
                'has_unassigned_draft': bool(data.get('draft', {}).get('text')),
                'active_tasks': sum(run['state'] not in TERMINAL for run in self._runs(data).values()),
                'scope': 'conversation_source'}

    def erase_source(self, *, review_token, additional_identifiers=None):
        """Atomically erase reviewed bodies while retaining replay barriers.

        A repeated, committed review returns its original receipt even if the
        user has since created new work. Active tasks must finish/reconcile first:
        erasing a local request must not imply GPU cancellation or settlement.
        """
        if not isinstance(review_token, str) or not _TOKEN.fullmatch(review_token):
            raise ConversationError('ask_privacy_review_changed')
        additional = self._additional(additional_identifiers)
        with file_transaction(self.source.lock):
            envelope, data = self.source._read()
            previous = self._receipt(data)
            if previous and previous.get('review_token') == review_token:
                return deepcopy(previous['result'])
            if self._review(data, additional) != review_token:
                raise ConversationError('ask_privacy_review_changed')
            runs = self._runs(data)
            if any(run['state'] not in TERMINAL for run in runs.values()):
                raise ConversationError('ask_privacy_tasks_active')

            state = self.source._sync_state(data) if data['version'] == SYNC_VERSION else None
            identifiers = set(data['conversations']) | set(data['tombstones']) | set(data['migrations'])
            identifiers.update(additional)
            identifiers.update(run['conversation_id'] for run in runs.values())
            if state:
                identifiers.update(state.value['entities'])
                identifiers.update(state.value['restores'])
            if len(identifiers) > MAX_IDENTITIES:
                raise ConversationError('ask_history_limit')
            for identifier in identifiers:
                _identity(identifier)

            result = {'scope': 'conversation_source', 'source_erased': True,
                      'identities': len(identifiers), 'remote_confirmed': False}
            if state:
                # Include deleted-only and imported identities: a stale browser
                # migration or old device message must not restore those bodies.
                for identifier in sorted(identifiers):
                    entity = state._entity(identifier)
                    state.erase(data, identifier, records.fingerprint(entity['record']))
                    record = deepcopy(entity['record'])
                    state.value['entities'][identifier] = {
                        'record': record, 'projected': deepcopy(record), 'visible': None}
                state.value['restores'] = {}

            for identifier in identifiers:
                prior = data['conversations'].get(identifier, {})
                tombstone = data['tombstones'].get(identifier, {})
                revision = max(prior.get('revision', 0), tombstone.get('revision', 0))
                data['tombstones'][identifier] = {'revision': max(1, revision), 'privacy_erased': True}
            data['conversations'] = {}
            draft = data.get('draft', {})
            if draft and draft.get('version') != 1:
                raise ConversationError('ask_history_version_unsupported')
            data['draft'] = {'version': 1, 'text': '', 'revision': draft.get('revision', 0) + 1}
            # Task identity/fingerprint prevent response-loss retries from
            # creating another billable request. Payloads and private extensions
            # in these terminal records are part of the reviewed source scope.
            if 'runs' in data:
                data['runs'] = {'version': 1, 'records': {
                    identifier: {key: run[key] for key in (
                        'task_id', 'conversation_id', 'fingerprint', 'state', 'cancel_requested') if key in run}
                    for identifier, run in runs.items()}}
            data['migrations'] = {identifier: {'digest': row['digest']}
                                  for identifier, row in data['migrations'].items()}
            data['privacy_erasure'] = {'version': VERSION, 'review_token': review_token, 'result': result}
            self.source._write(envelope, data, capture_sync=False)
            return deepcopy(result)
