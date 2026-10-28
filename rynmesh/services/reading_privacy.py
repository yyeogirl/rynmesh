"""Reviewed reading-source cleanup, including known replica causal history.

This is a source transaction for a future coordinated cleanup job. It does not
delete replica files, backups or search indexes, enable synchronization, or claim
that unobserved edits on other devices have been erased.
"""
from __future__ import annotations

import re
from copy import deepcopy

from ..atomic_io import migration_backup
from ..device_sync import records
from ..device_sync.reading import MAX_ENTITIES, SCOPES, ReadingState, scopes
from ..file_transactions import file_transaction
from .consumption import MAX_SYNC_HISTORY_BYTES, PRIVACY_VERSION, ConsumptionError

VERSION = 'ryn.reading-source-erasure.v1'
TOKEN = re.compile(r'[a-f0-9]{64}')


def validate_receipt(value):
    if not isinstance(value, dict) or value.get('version') != VERSION:
        raise ConsumptionError('reading_privacy_version_unsupported')
    records.actor_id(value.get('actor'))
    if (not isinstance(value.get('review_token'), str) or not TOKEN.fullmatch(value['review_token'])
            or not isinstance(value.get('additional_revision'), str) or not TOKEN.fullmatch(value['additional_revision'])
            or not isinstance(value.get('barriers'), dict) or len(value['barriers']) > MAX_ENTITIES
            or not isinstance(value.get('result'), dict)):
        raise ConsumptionError('reading_privacy_receipt_invalid')
    for key, row in value['barriers'].items():
        if (not isinstance(row, dict) or not isinstance(row.get('scope'), str) or row['scope'] not in SCOPES
                or key != ReadingState.key(row['scope'], row.get('id'))):
            raise ConsumptionError('reading_privacy_receipt_invalid')
        record = records.validate(row['scope'], row['id'], row.get('record'))
        if (len(record['heads']) != 1 or record['heads'][0]['value'] is not None
                or record['heads'][0]['actor'] != value['actor']):
            raise ConsumptionError('reading_privacy_receipt_invalid')
    result = value['result']
    if (result.get('scope') != 'reading_source' or result.get('source_cleared') is not True
            or result.get('remote_confirmed') is not False or type(result.get('identities')) is not int
            or not 0 <= result['identities'] <= MAX_ENTITIES):
        raise ConsumptionError('reading_privacy_receipt_invalid')
    return value


def seed_barriers(receipt, state, actor, selected, local):
    """Apply old deletion clocks only as the owner opts in to each scope."""
    if actor != receipt['actor']:
        raise records.SyncError('sync_device_identity_changed')
    selected = scopes(selected)
    state = state or ReadingState.create(actor)
    added = selected - set(state.value['scopes'])
    for key, row in receipt['barriers'].items():
        if row['scope'] in added:
            state.value['entities'][key] = {**deepcopy(row), 'projected': deepcopy(row['record'])}
    # enable captures new local bookmarks/positions after the old clocks. This
    # permits deliberate re-saving while stale reviewed messages remain older.
    state.enable(actor, selected, local)
    if len(state.value['entities']) > MAX_ENTITIES:
        raise records.SyncError('sync_capacity_exhausted')
    return state


class ReadingPrivacy:
    def __init__(self, source, *, actor):
        records.actor_id(actor)
        self.source, self.actor = source, actor

    @staticmethod
    def _additional(rows):
        if rows is None:
            return {}
        if not isinstance(rows, list) or len(rows) > MAX_ENTITIES:
            raise ConsumptionError('reading_privacy_limit')
        result = {}
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'scope', 'id', 'record'}
                    or not isinstance(row.get('scope'), str) or row['scope'] not in SCOPES):
                raise ConsumptionError('reading_privacy_replica_invalid')
            key = ReadingState.key(row['scope'], row['id'])
            if key in result:
                raise ConsumptionError('reading_privacy_replica_invalid')
            records.validate(row['scope'], row['id'], row['record'])
            result[key] = deepcopy(row)
        return result

    def _review(self, document, sync, additional):
        if sync is not None and sync.value['actor'] != self.actor:
            raise records.SyncError('sync_device_identity_changed')
        if document.get('version') == PRIVACY_VERSION and document['privacy_erasure']['actor'] != self.actor:
            raise records.SyncError('sync_device_identity_changed')
        return records.fingerprint({'actor': self.actor, 'document': document, 'additional': additional})

    def preview(self, *, additional_records=None):
        additional = self._additional(additional_records)
        with file_transaction(self.source.lock_path):
            document, local, sync = self.source._document()
            return {'review_token': self._review(document, sync, additional),
                    'scope': 'reading_source', 'local_items': len(local),
                    'source_entities': len(sync.value['entities']) if sync else 0,
                    'replica_entities': len(additional)}

    def erase_source(self, *, review_token, additional_records=None):
        if not isinstance(review_token, str) or not TOKEN.fullmatch(review_token):
            raise ConsumptionError('reading_privacy_review_changed')
        additional = self._additional(additional_records)
        with file_transaction(self.source.lock_path):
            document, local, sync = self.source._document()
            previous = document.get('privacy_erasure') if document.get('version') == PRIVACY_VERSION else None
            if previous and previous['review_token'] == review_token:
                if previous['actor'] != self.actor or previous['additional_revision'] != records.fingerprint(additional):
                    raise ConsumptionError('reading_privacy_review_changed')
                return deepcopy(previous['result'])
            if self._review(document, sync, additional) != review_token:
                raise ConsumptionError('reading_privacy_review_changed')
            known = deepcopy(previous['barriers']) if previous else {}
            for row in [*(sync.value['entities'].values() if sync else []), *additional.values()]:
                scope, identifier = row['scope'], row['id']
                key = ReadingState.key(scope, identifier)
                base = known.get(key, {}).get('record', records.empty())
                base = records.merge(scope, identifier, base, row['record'])
                if 'projected' in row:
                    base = records.merge(scope, identifier, base, row['projected'])
                known[key] = {'scope': scope, 'id': identifier, 'record': base}
            if len(known) > MAX_ENTITIES:
                raise ConsumptionError('reading_privacy_limit')
            for row in known.values():
                row['record'] = records.write(row['scope'], row['id'], row['record'], self.actor, None)
            identities = set(local) | {row['id'] for row in known.values()}
            if len(identities) > MAX_ENTITIES:
                raise ConsumptionError('reading_privacy_limit')
            result = {'scope': 'reading_source', 'source_cleared': True,
                      'identities': len(identities), 'remote_confirmed': False}
            receipt = {**(previous or {}), 'version': VERSION, 'actor': self.actor, 'review_token': review_token,
                       'additional_revision': records.fingerprint(additional), 'barriers': known, 'result': result}
            validate_receipt(receipt)
            if sync:
                # Remove known private entity extensions and old projections;
                # retain document and sync-header extensions outside this scope.
                sync.value['entities'] = {key: {**deepcopy(row), 'projected': deepcopy(row['record'])}
                                          for key, row in known.items() if row['scope'] in sync.value['scopes']}
            version = document.get('version')
            if not isinstance(version, str):
                version = None
            if version != PRIVACY_VERSION:
                suffix = {'ryn.consumption.v2': '.v2.migrated', 'ryn.consumption.v3': '.v3.migrated'}.get(version, '.migrated')
                if self.source.path.exists() and migration_backup(self.source.path, suffix=suffix, max_bytes=MAX_SYNC_HISTORY_BYTES) is None:
                    raise ConsumptionError('consumption_backup_failed')
                if version is None:
                    document = {'legacy_metadata': {key: value for key, value in document.items() if not isinstance(value, dict)}}
            document.update(version=PRIVACY_VERSION, privacy_erasure=receipt)
            self.source._save_document(document, {}, sync)
            return deepcopy(result)
