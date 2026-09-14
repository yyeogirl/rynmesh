"""Conversation source durability before replication acknowledgement.

The authenticated device transfer worker supplies authorization before these internal
methods are called. They neither grant permission nor discover/send to devices.
"""
from __future__ import annotations

from ..file_transactions import file_transaction
from .records import SyncError, conflict_count

SCOPES = ['conversations']


class ConversationBridge:
    def __init__(self, source, replica):
        self.source, self.replica = source, replica

    def _identity(self):
        if self.source.sync_identity() != self.replica.actor:
            raise SyncError('sync_device_identity_changed')

    def _reconcile(self):
        self.replica.reconcile_source(self._snapshot(), scopes=SCOPES)

    def _snapshot(self):
        return self.source.sync_snapshot(expected_actor=self.replica.actor)

    def pending(self, device):
        with file_transaction(self.source.lock):
            return self.replica.source_pending(device, self._snapshot(), scopes=SCOPES)

    def status(self, device):
        with file_transaction(self.source.lock):
            rows = self._snapshot()
            pending = self.replica.source_pending(device, rows, scopes=SCOPES)
            return {'pending': pending['pending'], 'conflicts': conflict_count(rows)}

    def receive(self, rows):
        with file_transaction(self.source.lock):
            self._identity()
            receipts = self.source.sync_receive(rows)
            self._reconcile()
            return receipts

    def acknowledge(self, device, receipts):
        with file_transaction(self.source.lock):
            return self.replica.source_acknowledge(device, self._snapshot(), receipts, scopes=SCOPES)
