"""Causal conversation outbox inside the source's encrypted transaction.

Projected records retain the context of the local branch. Receiving another
branch cannot silently change the context of an in-flight local request.
"""
from __future__ import annotations

from copy import deepcopy

from ..ask_ryn.store import _identity, clean_conversation
from . import records
from .records import SyncError

VERSION = 'ryn.conversation-sync.v1'
SCOPE = 'conversations'
MAX_ENTITIES = 10000
ACTIVE = {'queued', 'dispatching', 'running'}


def active(data, identifier):
    return any(row['conversation_id'] == identifier and row['state'] in ACTIVE
               for row in data.get('runs', {}).get('records', {}).values())


def cancel_runs(data, identifier):
    for run in data.get('runs', {}).get('records', {}).values():
        if run['conversation_id'] == identifier:
            run.pop('body', None)
            if run['state'] in ACTIVE:
                run['cancel_requested'] = True


class ConversationState:
    def __init__(self, value):
        if not isinstance(value, dict) or value.get('version') != VERSION:
            raise SyncError('sync_version_unsupported')
        records.actor_id(value.get('actor'))
        if not isinstance(value.get('entities'), dict) or len(value['entities']) > MAX_ENTITIES:
            raise SyncError('sync_capacity_exhausted')
        if not isinstance(value.get('restores'), dict) or len(value['restores']) > MAX_ENTITIES:
            raise SyncError('sync_record_invalid')
        self.value = value
        for identifier, entity in value['entities'].items():
            _identity(identifier)
            if not isinstance(entity, dict) or not {'record', 'projected', 'visible'} <= entity.keys():
                raise SyncError('sync_record_invalid')
            records.validate(SCOPE, identifier, entity['record'])
            records.validate(SCOPE, identifier, entity['projected'])
            if records.clean_value(SCOPE, identifier, entity['visible']) != entity['visible']:
                raise SyncError('sync_value_invalid')
            if 'local_draft' in entity:
                draft = clean_conversation(entity['local_draft'])
                if draft['id'] != identifier or not draft.get('draft'):
                    raise SyncError('sync_record_invalid')

    @classmethod
    def create(cls, actor):
        return cls({'version': VERSION, 'actor': actor, 'entities': {}, 'restores': {}})

    def _entity(self, identifier):
        if identifier not in self.value['entities']:
            if len(self.value['entities']) >= MAX_ENTITIES:
                raise SyncError('sync_capacity_exhausted')
            self.value['entities'][identifier] = {'record': records.empty(), 'projected': records.empty(), 'visible': None}
        return self.value['entities'][identifier]

    def _write(self, identifier, value, *, erase=False):
        entity = self._entity(identifier)
        current = entity['record']
        base = deepcopy(current if erase else entity['projected'])
        actor = self.value['actor']
        if current['clock'].get(actor, 0) > base['clock'].get(actor, 0):
            base['clock'][actor] = current['clock'][actor]
            base['heads'] = [head for head in base['heads'] if head['actor'] != actor]
        updated = records.write(SCOPE, identifier, base, actor, value, erase=erase)
        entity.update(record=records.merge(SCOPE, identifier, current, updated), projected=updated, visible=deepcopy(value))
        if erase:
            entity.pop('local_draft', None)

    def capture(self, data, *, seed=False):
        """Called for all source writes, including background run completion."""
        for identifier, row in list(data['conversations'].items()):
            # Changes to queued requests, their title and prompt stay local.
            if active(data, identifier) and not seed:
                continue
            value = records.clean_value(SCOPE, identifier, row)
            entity = self.value['entities'].get(identifier)
            if entity is None or records.fingerprint(value) != records.fingerprint(entity['visible']):
                self._write(identifier, value)
            entity = self.value['entities'][identifier]
            if not active(data, identifier):
                entity.pop('deferred', None)
            if records.view(SCOPE, identifier, entity['record'])['deleted'] and not active(data, identifier):
                data['conversations'].pop(identifier, None)
                entity['visible'] = None
        for identifier in data['tombstones']:
            if identifier in data['conversations']:
                continue  # Hidden while an original run is reconciling.
            entity = self.value['entities'].get(identifier)
            if data['tombstones'][identifier].get('privacy_erased'):
                if entity is None or not entity['record']['erased']:
                    self._write(identifier, None, erase=True)
                continue
            if entity is None or entity['visible'] is not None:
                self._write(identifier, None)

    def summary(self, identifier):
        entity = self.value['entities'].get(identifier)
        if entity is None:
            return None
        current = records.view(SCOPE, identifier, entity['record'])
        return {key: current[key] for key in ('revision', 'conflict', 'deleted', 'erased')} | {
            'branch_count': len(current['branches']), 'recovery_count': len(current['recovery']),
            'deferred': bool(entity.get('deferred'))}

    def issues(self):
        result = []
        for identifier, entity in self.value['entities'].items():
            current = records.view(SCOPE, identifier, entity['record'])
            if current['conflict'] or current['recovery'] or entity.get('deferred') or entity.get('local_draft'):
                result.append({**current, 'deferred': bool(entity.get('deferred')), 'discard_token': self.recovery_review(identifier),
                               **({'local_draft': clean_conversation(entity['local_draft'])} if entity.get('local_draft') else {})})
        return result

    def recovery_review(self, identifier):
        entity = self.value['entities'][identifier]
        return records.fingerprint({'id': identifier, 'revision': records.fingerprint(entity['record']),
                                    'local_draft': entity.get('local_draft')})

    def export(self):
        return [{'scope': SCOPE, 'id': identifier, 'record': deepcopy(entity['record'])}
                for identifier, entity in self.value['entities'].items()]

    def merge(self, data, rows):
        receipts, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'scope', 'id', 'record'} or row['scope'] != SCOPE:
                raise SyncError('sync_scope_denied')
            identifier = _identity(row['id'])
            if identifier in seen:
                raise SyncError('sync_batch_invalid')
            seen.add(identifier)
            entity = self._entity(identifier)
            previous = records.fingerprint(entity['record'])
            entity['record'] = records.merge(SCOPE, identifier, entity['record'], row['record'])
            if records.fingerprint(entity['record']) != previous:
                self.project(data, identifier)
            receipts.append({'scope': SCOPE, 'id': identifier, 'revision': records.fingerprint(row['record'])})
        return receipts

    def project(self, data, identifier):
        entity = self.value['entities'][identifier]
        current = records.view(SCOPE, identifier, entity['record'])
        prior = data['conversations'].get(identifier)
        if current['deleted']:
            if current['erased']:
                entity.pop('local_draft', None)
            elif prior and prior.get('draft'):
                # Remote deletion must not lose an unsent local draft. Keep its
                # original binding locally; this field is never in export().
                entity['local_draft'] = {**records.clean_value(SCOPE, identifier, prior), 'draft': prior['draft']}
            data['tombstones'].setdefault(identifier, {'sync': True, 'revision': (prior or {}).get('revision', 0) + 1})
            cancel_runs(data, identifier)
            if active(data, identifier) and prior and not current['erased']:
                # Hidden from owner reads/new runs, retained for original-task
                # reconciliation. Final messages become a recovery branch.
                entity['deferred'] = True
                prior['revision'] += 1
                return
            data['conversations'].pop(identifier, None)
            entity.update(visible=None, projected=deepcopy(entity['record']))
            entity.pop('deferred', None)
            return
        if current['conflict'] or active(data, identifier):
            if prior:
                prior['revision'] += 1
            entity['deferred'] = active(data, identifier)
            return  # A third device shows the branches, never an arbitrary winner.
        if not current['branches']:
            return
        incoming = deepcopy(current['branches'][0]['value'])
        if identifier in data['tombstones']:
            raise SyncError('sync_conversation_deleted')
        if prior and any(prior[key] != incoming[key] for key in ('serviceKey', 'providerPeerId', 'networkId', 'createdAt')):
            raise SyncError('sync_service_binding_mismatch')
        old_messages = {row['id']: row for row in (prior or {}).get('messages', [])}
        incoming['messages'] = [{**old_messages.get(row['id'], {}), **row} for row in incoming['messages']]
        data['conversations'][identifier] = {**(prior or {}), **incoming, 'revision': (prior or {}).get('revision', 0) + 1}
        entity.update(projected=deepcopy(entity['record']), visible=records.clean_value(SCOPE, identifier, incoming))
        entity.pop('deferred', None)

    def erase(self, data, identifier, expected_revision):
        entity = self.value['entities'].get(identifier)
        if entity is None or records.fingerprint(entity['record']) != expected_revision:
            raise SyncError('sync_revision_conflict')
        if not entity['record']['erased']:
            self._write(identifier, None, erase=True)
            self.project(data, identifier)
