"""Reviewed erasure of sharing metadata; retain only replay barriers and receipts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..atomic_io import atomic_write_bytes
from ..crypto import canonical_json

KEY = 'card_erasure'
LIMIT = 10000
FILES = ('content-cards.jsonl', 'state.json.migrated')


def controls(data):
    value = data.setdefault(KEY, {'version': 1, 'deleted': [], 'receipts': [], 'pending': None})
    if (not isinstance(value, dict) or value.get('version') != 1
            or not isinstance(value.get('deleted'), list) or len(value['deleted']) > LIMIT
            or any(not isinstance(key, str) or not 0 < len(key) <= 256 for key in value['deleted'])
            or not isinstance(value.get('receipts'), list) or len(value['receipts']) > 32
            or value.get('pending') is not None and not isinstance(value['pending'], dict)):
        raise ValueError('friend_card_cleanup_version_unsupported')
    pending = value['pending']
    if pending is not None:
        if (not isinstance(pending.get('token'), str) or len(pending['token']) != 64
                or not isinstance(pending.get('ids'), list) or len(pending['ids']) > LIMIT
                or any(not isinstance(key, str) or not 0 < len(key) <= 256 for key in pending['ids'])
                or not isinstance(pending.get('files'), list) or len(pending['files']) > len(FILES)
                or any(not isinstance(row, dict) or row.get('name') not in FILES
                       or any(not isinstance(row.get(key), str) or len(row[key]) != 64 for key in ('before', 'after'))
                       for row in pending['files'])):
            raise ValueError('friend_card_cleanup_version_unsupported')
    for receipt in value['receipts']:
        if (not isinstance(receipt, dict) or not isinstance(receipt.get('token'), str)
                or not isinstance(receipt.get('result'), dict)
                or receipt['result'].get('complete') is not True
                or type(receipt['result'].get('cards')) is not int):
            raise ValueError('friend_card_cleanup_version_unsupported')
    return value


def erased(data, identifier):
    return identifier in controls(data)['deleted']


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def rewrite(path: Path, identifiers: set[str]):
    if path.is_symlink():
        raise ValueError('friend_card_cleanup_file_unavailable')
    with path.open('rb') as handle:
        raw = handle.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('friend_card_cleanup_file_unavailable')
    if path.name == 'content-cards.jsonl':
        kept = []
        ids = set()
        for line in raw.splitlines(keepends=True):
            if not line.strip():
                kept.append(line)
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get('card_id'), str):
                raise ValueError('friend_card_cleanup_file_unavailable')
            ids.add(row['card_id'])
            if row['card_id'] not in identifiers:
                kept.append(line)
        return raw, b''.join(kept), ids
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('version') not in (None, 'ryn.friends.v2') or not isinstance(data.get('cards', {}), dict):
        raise ValueError('friend_card_cleanup_file_unavailable')
    ids = set(data.get('cards', {}))
    if ids & identifiers:
        data['cards'] = {key: row for key, row in data['cards'].items() if key not in identifiers}
        return raw, json.dumps(data, ensure_ascii=False, indent=2).encode(), ids
    return raw, raw, ids


class CardCleanup:
    def __init__(self, store):
        self.store = store

    def _plan(self, data):
        control = controls(data)
        pending = control['pending']
        ids = set(pending['ids'] if pending else data['cards'])
        if not pending:
            for name in FILES:
                path = self.store.root / name
                if path.exists():
                    ids.update(rewrite(path, set())[2])
        if any(not isinstance(key, str) or not 0 < len(key) <= 256 for key in ids):
            raise ValueError('friend_card_cleanup_file_unavailable')
        if len(set(control['deleted']) | ids) > LIMIT:
            raise ValueError('friend_card_cleanup_limit')
        files = []
        for name in FILES:
            path = self.store.root / name
            if path.exists():
                before, after, _ = rewrite(path, ids)
                if before != after:
                    files.append({'name': name, 'before': digest(before), 'after': digest(after)})
        return {'ids': sorted(ids), 'files': files,
                'source_hash': digest(canonical_json(data['cards'])) if not pending else '',
                'previous': pending['token'] if pending else ''}

    @staticmethod
    def _result(job, complete):
        return {'cards': len(job['ids']), 'complete': complete,
                'remote_confirmed': False, 'retained_deletion_ids': True}

    def preview(self):
        with self.store._guard():
            data = self.store._read(self.store.state_path, self.store._empty())
            plan = self._plan(data)
            return {'review_token': digest(canonical_json(plan)), 'cards': len(plan['ids']),
                    'legacy_files': len(plan['files']), 'resuming': bool(controls(data)['pending'])}

    def begin(self, token):
        if not isinstance(token, str) or len(token) != 64:
            raise ValueError('friend_card_cleanup_review_changed')
        with self.store._guard():
            data = self.store._read(self.store.state_path, self.store._empty())
            control = controls(data)
            for receipt in control['receipts']:
                if receipt['token'] == token:
                    return receipt['result']
            pending = control['pending']
            if pending is None or token != pending['token']:
                plan = self._plan(data)
                if token != digest(canonical_json(plan)):
                    raise ValueError('friend_card_cleanup_review_changed')
                ids = set(plan['ids'])
                # A new review of unfinished legacy files never targets new cards.
                if pending is None:
                    data['cards'] = {key: row for key, row in data['cards'].items() if key not in ids}
                    control['deleted'] = sorted(set(control['deleted']) | ids)
                pending = {'token': token, 'ids': plan['ids'], 'files': plan['files']}
                control['pending'] = pending
                self.store._write(self.store.state_path, data)
            for file in pending['files']:
                path = self.store.root / file['name']
                if file['name'] not in FILES:
                    raise ValueError('friend_card_cleanup_file_unavailable')
                if not path.exists():
                    continue
                before, after, _ = rewrite(path, set(pending['ids']))
                if digest(before) == file['after']:
                    continue  # File commit succeeded but the response/state write was lost.
                if digest(before) != file['before'] or digest(after) != file['after']:
                    raise ValueError('friend_card_cleanup_review_changed')
                atomic_write_bytes(path, after)
            result = self._result(pending, True)
            control['receipts'] = [*control['receipts'], {'token': token, 'result': result}][-32:]
            control['pending'] = None
            self.store._write(self.store.state_path, data)
            return result
