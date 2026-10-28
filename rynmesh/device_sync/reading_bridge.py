"""Reading-source durability before replica receipts; no network authorization.

The authenticated device transfer worker calls this adapter only for its approved
scopes. Constructing it does not opt in, discover devices or send any data.
Local reading writes depend only on ConsumptionStore, never on replica health.
"""
from __future__ import annotations

from ..file_transactions import file_transaction
from .reading import scopes as reading_scopes
from .records import conflict_count


class ReadingBridge:
    def __init__(self, source, replica, *, ensure_enabled=False):
        self.source = source
        self.replica = replica
        self.ensure_enabled = ensure_enabled

    def _snapshot(self, scopes):
        selected = reading_scopes(scopes)
        rows = self.source.sync_snapshot(selected, expected_actor=self.replica.actor, enable=self.ensure_enabled)
        return selected, rows

    def _reconcile(self, scopes):
        selected, rows = self._snapshot(scopes)
        self.replica.reconcile_source(rows, scopes=selected)
        return selected

    def pending(self, device, scopes):
        # One consistent lock order: source -> replica. Never hold over network IO.
        with file_transaction(self.source.lock_path):
            selected, rows = self._snapshot(scopes)
            return self.replica.source_pending(device, rows, scopes=selected)

    def status(self, device, scopes):
        with file_transaction(self.source.lock_path):
            selected, rows = self._snapshot(scopes)
            pending = self.replica.source_pending(device, rows, scopes=selected)
            return {'pending': pending['pending'], 'conflicts': conflict_count(rows)}

    def receive(self, rows, *, scopes):
        selected = reading_scopes(scopes)
        with file_transaction(self.source.lock_path):
            receipts = self.source.sync_receive(rows, scopes=selected, expected_actor=self.replica.actor, enable=self.ensure_enabled)
            # If either commit fails, return no acknowledgement. Source-first
            # commits remain readable and replay safely after restart.
            self._reconcile(selected)
            return receipts

    def acknowledge(self, device, receipts, *, scopes):
        with file_transaction(self.source.lock_path):
            # Include any local edit made since sending before checking the ACK.
            selected, rows = self._snapshot(scopes)
            return self.replica.source_acknowledge(device, rows, receipts, scopes=selected)
