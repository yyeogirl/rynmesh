"""Clear retained consumer results without deleting billing/order identity."""
from __future__ import annotations

import re

from .task_protocol import TERMINAL_STATES, TaskProtocolError


def erase_consumer_results(orders, pending, pending_lock, task_ids):
    if (not isinstance(task_ids, list) or len(task_ids) > 10000
            or any(not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', value) for value in task_ids)):
        raise TaskProtocolError('ask_cleanup_tasks_invalid')
    identifiers = sorted(set(task_ids))
    with pending_lock:
        # A terminal disk checkpoint may precede completion of the background
        # coroutine. Its queued memory entry remains a fence until it exits.
        for identifier in identifiers:
            record, memory = orders.get(identifier), pending.get(identifier)
            if ((record and record.get('state') not in TERMINAL_STATES)
                    or (memory and memory.get('state') not in TERMINAL_STATES)):
                raise TaskProtocolError('ask_cleanup_orders_active')
        removed = 0
        for identifier in identifiers:
            removed += bool(orders.purge_encrypted_response(identifier))
            if identifier in pending:
                # Keep response-loss retries on the existing task even when a
                # rejected submission did not create a persistent order yet.
                row = pending[identifier]
                pending[identifier] = {key: row[key] for key in ('task_id', 'state', 'error_code', '_recorded_at') if key in row}
        return {'results_removed': removed, 'reviewed_tasks': len(identifiers)}
