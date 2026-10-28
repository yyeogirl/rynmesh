"""Owner device controls and bounded, authenticated peer pairing endpoints."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from ..background_workers import BackgroundWorkerSpec, BackoffPolicy, WorkerRunResult
from ..friends.crypto import validate_endpoint
from .pair_crypto import MAX_WIRE_BYTES
from .pairing import PairingService
from .records import SyncError
from .store import ReplicaStore
from .transfer import MAX_WIRE_BYTES as MAX_DATA_WIRE_BYTES
from .transfer import DeviceTransfer


@dataclass
class DeviceSyncState:
    service: PairingService
    local_control: object
    transfer: DeviceTransfer | None = None
    attempts: dict = field(default_factory=dict)
    poll_index: int = 0


SAFE_ERRORS = frozenset({
    'sync_scope_invalid', 'sync_scope_denied', 'sync_pairing_not_found', 'sync_device_identity_changed',
    'sync_cannot_pair_self', 'sync_device_already_paired', 'sync_invite_lifetime_invalid', 'sync_invite_expired',
    'sync_invite_invalid', 'sync_pairing_intent_conflict', 'sync_pairing_review_changed', 'sync_pairing_not_pending',
    'sync_device_not_active', 'sync_revision_conflict', 'sync_policy_invalid', 'sync_policy_revision_conflict',
    'sync_pairing_capacity_exhausted', 'sync_version_unsupported', 'sync_endpoint_unavailable',
    'sync_pairing_invalid', 'sync_pairing_response_invalid', 'sync_pairing_identity_invalid',
    'sync_pairing_store_unavailable',
    'sync_reading_not_found', 'sync_reading_not_conflicted', 'sync_reading_choice_invalid', 'sync_not_enabled',
})


async def body(request, limit=MAX_WIRE_BYTES):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, detail='sync_request_too_large')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, detail='sync_request_invalid') from None


def install_device_sync(app, *, store, home, workers, local_control, messaging_key,
                        post_json=None, endpoint=None, allow_loopback=None, reading=None, conversations=None):
    from ..peer_http import HttpPeerClient, PeerTransportError

    def current():
        return app.state.device_sync.service

    def post(endpoint, path, wire):
        validate_endpoint(endpoint, allow_loopback=current().allow_loopback)
        return HttpPeerClient(endpoint, timeout_s=5).post_json(path, wire,
            max_bytes=MAX_DATA_WIRE_BYTES if path.endswith('/batch') else MAX_WIRE_BYTES)

    if endpoint is None:
        endpoint = os.environ.get('RYNMESH_DEVICE_ENDPOINT', '') or str(store.node_info().get('peer_endpoint', ''))
    if allow_loopback is None:
        allow_loopback = os.environ.get('RYNMESH_DEVICE_ALLOW_LOOPBACK', '0') == '1'
    service = PairingService(getattr(store, 'home', None) or home, identity_private=store.private_key_bytes,
                            messaging_key=messaging_key, name=store.node_name, endpoint=endpoint,
                            post_json=post_json or post, allow_loopback=allow_loopback)
    app.state.device_sync = DeviceSyncState(service, local_control)
    if reading is not None and conversations is not None:
        app.state.device_sync.transfer = DeviceTransfer(pairing=current,
            replica=ReplicaStore(getattr(store, 'home', None) or home, messaging_key=messaging_key),
            reading=reading, conversations=conversations, post_json=post_json or post)

    def control(request):
        app.state.device_sync.local_control(request)

    async def call(method, *args, **kwargs):
        try:
            return await asyncio.to_thread(getattr(current(), method), *args, **kwargs)
        except SyncError as exc:
            code = str(exc)
            raise HTTPException(503 if code in {'sync_pairing_store_unavailable', 'sync_endpoint_unavailable'} else 409,
                                detail=code if code in SAFE_ERRORS else 'sync_operation_unavailable') from None
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, PeerTransportError):
            raise HTTPException(503, detail='sync_operation_unavailable') from None

    def tick():
        node = current()
        rows = [row for row in node.store.snapshot()['pairs'].values()
                if row['status'] in {'active', 'awaiting_ack'} or row.get('revoke_wire')
                or row['status'] == 'awaiting_inviter' and row['expires'] > node.clock()]
        if not rows:
            return WorkerRunResult(activity=False)
        index = app.state.device_sync.poll_index % len(rows)
        app.state.device_sync.poll_index += 1
        row = rows[index]
        try:
            if row.get('revoke_wire'):
                result = node.retry_removal(row['id'])
            elif row['status'] == 'active':
                result = node.exchange_policy(row['id'])
                transfer = app.state.device_sync.transfer
                if transfer is not None:
                    return WorkerRunResult(activity=transfer.run_once(row['id']))
            else:
                result = node.retry(row['id'])
        except Exception:
            # Worker logs contain a fixed code, never an invite, peer response or URL.
            raise RuntimeError('sync_pairing_retry_unavailable') from None
        return WorkerRunResult(activity=result != node.public(row))

    workers.register(BackgroundWorkerSpec(name='device-sync.pairing', run_once=tick, initial_delay_s=3,
        policy=BackoffPolicy(busy_delay_s=3, idle_initial_s=5, idle_multiplier=1.5,
                             idle_max_s=15, error_multiplier=2, error_max_s=30)), replace=True)
    if any(getattr(route, 'name', '') == 'device_sync_list' for route in app.routes):
        return service

    @app.get('/api/local/device-sync', name='device_sync_list')
    async def status(request: Request):
        control(request)
        devices = await call('list')
        transfer = app.state.device_sync.transfer
        if transfer is not None:
            for device in devices:
                device['sync'] = await asyncio.to_thread(transfer.status, device['id'])
        return {**current().readiness(), 'devices': devices, 'invites': await call('list_invites'),
                'data_transfer_available': transfer is not None}

    @app.post('/api/local/device-sync/invites')
    async def invite(request: Request):
        control(request)
        value = await body(request)
        return await call('create_invite', value.get('scopes'), ttl_seconds=value.get('ttl_seconds', 900))

    async def reading_call(method, *args, **kwargs):
        transfer = app.state.device_sync.transfer
        if transfer is None:
            raise HTTPException(404, detail='sync_action_unavailable')
        try:
            return await asyncio.to_thread(getattr(transfer.reading(), method), *args, **kwargs)
        except SyncError as exc:
            code = str(exc)
            raise HTTPException(409, detail=code if code in SAFE_ERRORS else 'sync_operation_unavailable') from None
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, detail='sync_operation_unavailable') from None

    @app.get('/api/local/device-sync/reading/conflicts')
    async def reading_conflicts(request: Request):
        control(request)
        return {'conflicts': await reading_call('sync_issues'), 'local_actor': current().identity['actor']}

    @app.post('/api/local/device-sync/reading/resolve')
    async def resolve_reading(request: Request):
        control(request)
        value = await body(request)
        return await reading_call('sync_resolve', value.get('scope'), value.get('id'),
                                  choice_id=value.get('choice_id'), expected_revision=value.get('expected_revision'))

    @app.post('/api/local/device-sync/invites/inspect')
    async def inspect(request: Request):
        control(request)
        value = await body(request)
        return await call('inspect_invite', value.get('uri'))

    @app.delete('/api/local/device-sync/invites/{invite_id}')
    async def cancel(invite_id: str, request: Request):
        control(request)
        return await call('cancel_invite', invite_id)

    @app.post('/api/local/device-sync/join')
    async def join(request: Request):
        control(request)
        value = await body(request)
        # Persist the owner's decision before the background worker attempts IO.
        return await call('start_join', value.get('uri'), value.get('scopes'))

    @app.post('/api/local/device-sync/devices/{pair_id}/approve')
    async def approve(pair_id: str, request: Request):
        control(request)
        value = await body(request)
        return await call('approve', pair_id, review_token=value.get('review_token'), scopes=value.get('scopes'))

    @app.put('/api/local/device-sync/devices/{pair_id}/policy')
    async def configure(pair_id: str, request: Request):
        control(request)
        value = await body(request)
        return await call('configure', pair_id, expected_revision=value.get('expected_revision'),
                          scopes=value.get('scopes'), paused=value.get('paused'))

    @app.post('/api/local/device-sync/devices/{pair_id}/remove')
    async def remove(pair_id: str, request: Request):
        control(request)
        value = await body(request)
        return await call('revoke', pair_id, expected_revision=value.get('expected_revision'))

    @app.post('/api/local/device-sync/devices/{pair_id}/retry')
    async def retry(pair_id: str, request: Request):
        control(request)
        pair = await call('get', pair_id)
        method = 'retry_removal' if pair['status'] == 'revoked' else 'exchange_policy' if pair['status'] == 'active' else 'retry'
        result = await call(method, pair_id)
        if pair['status'] == 'active' and app.state.device_sync.transfer is not None:
            try:
                await asyncio.to_thread(app.state.device_sync.transfer.run_once, pair_id)
            except Exception:
                raise HTTPException(503, detail='sync_transfer_unconfirmed') from None
        return result

    @app.post('/api/peer/device-sync/{action}')
    async def peer(action: str, request: Request):
        if action not in {'join', 'confirm', 'policy', 'revoke', 'batch'}:
            raise HTTPException(404, detail='sync_action_unavailable')
        if action == 'batch' and app.state.device_sync.transfer is None:
            raise HTTPException(404, detail='sync_action_unavailable')
        attempts, now = app.state.device_sync.attempts, time.monotonic()
        for host in list(attempts):
            attempts[host] = [stamp for stamp in attempts[host] if now - stamp < 60]
            if not attempts[host]:
                attempts.pop(host)
        host = request.client.host if request.client else 'unknown'
        recent = attempts.get(host, [])
        if len(recent) >= 60 or host not in attempts and len(attempts) >= 256:
            raise HTTPException(429, detail='sync_rate_limited', headers={'Retry-After': '60'})
        attempts.setdefault(host, []).append(now)
        wire = await body(request, MAX_DATA_WIRE_BYTES if action == 'batch' else MAX_WIRE_BYTES)
        try:
            if action == 'batch':
                return await asyncio.to_thread(app.state.device_sync.transfer.receive, wire)
            return await asyncio.to_thread(getattr(current(), 'receive_' + action), wire)
        except OSError:
            raise HTTPException(503, detail='sync_operation_unavailable') from None
        except Exception:
            raise HTTPException(403, detail='sync_request_rejected') from None

    return service
