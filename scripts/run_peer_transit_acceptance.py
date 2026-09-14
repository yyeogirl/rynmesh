#!/usr/bin/env python3
"""Run deterministic three-node peer-transit acceptance on real local ICE pairs.

This is the hermetic gate.  It proves protocol behavior, identity continuity,
two separate non-TURN ICE legs, ciphertext-only forwarding, streaming hashes,
route hysteresis, bounded relay unavailability and concurrent callers.  Public
NAT acceptance still requires running the same workers on three physical
networks with STUN enabled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import threading
import time
import traceback
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import rynmesh.peer_transit_service as peer_transit_service
from rynmesh.llm_package.p2p import (
    _close_connection,
    apply_remote_signal,
    gather_signal,
    new_connection,
    selected_pair,
)
from rynmesh.peer_transit import PathMetrics, RouteManager, RoutePolicy, validate_ice_hop
from rynmesh.peer_transit_service import (
    PeerTransitError,
    PeerTransitWorker,
    advertise_transit_capacity,
    send_file_adaptive,
    send_file_direct,
    send_file_via_peer,
)
from rynmesh.registry import FilePeerRegistry
from rynmesh.store import RynmeshStore

try:
    from scripts.audit_peer_transit import AuditError, audit_acceptance_report, audit_peer_transit
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from audit_peer_transit import AuditError, audit_acceptance_report, audit_peer_transit

MARKER = b"RYNMESH-TRANSIT-PLAINTEXT-CHECK-2026"
MAX_ACCEPTANCE_PEAK_MEMORY_BYTES = 128 * 1024 * 1024
MAX_REGISTRY_CONTROL_RECORD_BYTES = 64 * 1024
CONCURRENT_PROBE_BYTES = 1024 * 1024


def _prepare_work_root(work_root: Path) -> None:
    """Create an empty evidence root or reject stale mixed-run artifacts."""

    if work_root.exists():
        if not work_root.is_dir():
            raise PeerTransitError("acceptance work root is not a directory")
        if next(work_root.iterdir(), None) is not None:
            raise PeerTransitError("acceptance work root must be empty")
        return
    work_root.mkdir(parents=True)


class _FrameAudit:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.frames = 0
        self.bytes = 0
        self.max_frame_bytes = 0
        self.plaintext_found = False

    def __call__(self, frame: bytes) -> None:
        with self._lock:
            self.frames += 1
            self.bytes += len(frame)
            self.max_frame_bytes = max(self.max_frame_bytes, len(frame))
            if MARKER in frame:
                self.plaintext_found = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "frames": self.frames,
                "bytes": self.bytes,
                "max_frame_bytes": self.max_frame_bytes,
                "plaintext_found": self.plaintext_found,
            }


class _RegistryBlackout:
    """Fail registry calls while preserving the same underlying registry."""

    def __init__(self, registry: FilePeerRegistry) -> None:
        self._registry = registry
        self.blackout = threading.Event()
        self.two_hops_ready = threading.Event()
        self.blocked_calls = 0

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._registry, name)
        if not callable(attribute):
            return attribute

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if self.blackout.is_set():
                self.blocked_calls += 1
                raise OSError("acceptance control-plane blackout")
            result = attribute(*args, **kwargs)
            signed_payload = dict(getattr(args[0], "payload", {}) or {}) if args else {}
            result_refs = dict(signed_payload.get("result_refs") or {})
            if (
                name == "publish_work_result"
                and signed_payload.get("status") == "running"
                and result_refs.get("path_mode") == "peer_transit"
            ):
                self.two_hops_ready.set()
            return result

        return guarded


class _DatagramImpairment:
    """Deterministic application-datagram shaper for the real direct ICE pair.

    The production transport remains untouched.  Acceptance temporarily wraps
    each direct connection's ``send`` method so the same aioice UDP socket is
    used after a bounded one-way delay, while a deterministic subset of calls
    is omitted to model loss.  The two one-way bounds correspond to the
    contract's 250-350 ms round-trip delay range.
    """

    rtt_min_ms = 250.0
    rtt_max_ms = 350.0
    jitter_ms = 75.0
    target_loss_ratio = 0.18

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempted = 0
        self._dropped = 0
        self._scheduled_rtt_ms: list[float] = []

    def plan(self) -> tuple[bool, float]:
        with self._lock:
            self._attempted += 1
            sequence = self._attempted
            # Multiplication by 37 distributes the eighteen dropped positions
            # through every hundred calls instead of creating one long burst.
            dropped = (sequence * 37) % 100 < 18
            # A second coprime sequence covers the inclusive 250-350 ms RTT
            # interval deterministically.  Each endpoint delays one direction
            # by half of the selected round-trip value.
            rtt_ms = self.rtt_min_ms + float((sequence * 53) % 101)
            self._scheduled_rtt_ms.append(rtt_ms)
            if dropped:
                self._dropped += 1
            return dropped, rtt_ms / 2000.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            attempted = self._attempted
            dropped = self._dropped
            samples = list(self._scheduled_rtt_ms)
        return {
            "transport": "real_local_ice_udp_application_datagrams",
            "configured_rtt_min_ms": self.rtt_min_ms,
            "configured_rtt_max_ms": self.rtt_max_ms,
            "configured_jitter_ms": self.jitter_ms,
            "configured_loss_ratio": self.target_loss_ratio,
            "attempted_datagrams": attempted,
            "dropped_datagrams": dropped,
            "delivered_datagrams": attempted - dropped,
            "observed_loss_ratio": 0.0 if attempted == 0 else dropped / attempted,
            "scheduled_rtt_min_ms": min(samples, default=0.0),
            "scheduled_rtt_max_ms": max(samples, default=0.0),
        }


def _impaired_connection_factory(original_factory: Any, profile: _DatagramImpairment):
    """Return an aioice factory whose application sends are delayed/dropped."""

    def create(*, controlling: bool):
        connection = original_factory(controlling=controlling)
        original_send = connection.send
        original_close = connection.close
        pending: set[asyncio.Task[Any]] = set()

        async def delayed_send(payload: bytes) -> None:
            dropped, delay_s = profile.plan()
            if dropped:
                return

            async def deliver() -> None:
                await asyncio.sleep(delay_s)
                await original_send(payload)

            pending.add(asyncio.create_task(deliver()))

        async def close() -> None:
            if pending:
                await asyncio.gather(*tuple(pending))
            await original_close()

        connection.send = delayed_send
        connection.close = close
        return connection

    return create


def _write_payload(path: Path, size_bytes: int, *, marker: bytes = MARKER) -> None:
    block = (b"rynmesh-peer-transit-acceptance-" * 2048)[:64 * 1024]
    remaining = max(0, size_bytes - len(marker))
    with path.open("wb") as handle:
        handle.write(marker[:size_bytes])
        while remaining > 0:
            chunk = block[:remaining]
            handle.write(chunk)
            remaining -= len(chunk)


def _wait_for_worker_trace(
    worker_trace: dict[str, dict[str, dict[str, float]]],
    trace_lock: threading.Lock,
    session_ids: set[str],
    *,
    timeout_s: float,
) -> bool:
    """Wait only for completion callbacks from handlers that already returned results."""

    if not session_ids:
        return True
    deadline = time.monotonic() + max(0.0, min(5.0, timeout_s))
    while True:
        with trace_lock:
            complete = all(
                {"started", "finished"} <= set(worker_trace[role].get(session_id, {}))
                for role in ("relay", "target")
                for session_id in session_ids
            )
        if complete:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


async def _direct_probe() -> dict[str, Any]:
    source = new_connection(controlling=True)
    target = new_connection(controlling=False)
    started = time.monotonic()
    try:
        source_signal, target_signal = await asyncio.gather(
            gather_signal(source),
            gather_signal(target),
        )
        await asyncio.gather(
            apply_remote_signal(source, target_signal),
            apply_remote_signal(target, source_signal),
        )
        await asyncio.gather(source.connect(), target.connect())
        source_evidence = selected_pair(source)
        target_evidence = selected_pair(target)
        validate_ice_hop(source_evidence)
        validate_ice_hop(target_evidence)
        return {
            "ok": True,
            "elapsed_s": time.monotonic() - started,
            "source": source_evidence,
            "target": target_evidence,
            "ice_relay_candidate_used": False,
        }
    finally:
        await asyncio.gather(_close_connection(source), _close_connection(target))


def _route_acceptance() -> dict[str, Any]:
    policy = RoutePolicy(
        degraded_hold_s=30,
        transit_min_hold_s=60,
        recovery_hold_s=120,
        recovery_probe_count=5,
    )
    manager = RouteManager(policy)
    healthy = PathMetrics(True, 40, 0)
    degraded = PathMetrics(True, 330, 0.18)
    hard_failed = PathMetrics(False, 0, 1, consecutive_failures=3)
    transit = PathMetrics(True, 80, 0.01)
    manager.update(direct=healthy, transit=transit, now_monotonic=0)
    manager.update(direct=degraded, transit=transit, now_monotonic=1)
    manager.update(direct=degraded, transit=transit, now_monotonic=31)
    degraded_path = manager.path_mode
    recovery_probe_times = [92.0, 150.0, 180.0, 211.0, 212.0]
    for probe_at in recovery_probe_times:
        manager.update(direct=healthy, transit=transit, now_monotonic=probe_at)
    recovered_path = manager.path_mode

    hard_manager = RouteManager()
    started = time.monotonic()
    hard_path = hard_manager.update(
        direct=hard_failed,
        transit=transit,
        now_monotonic=started,
    )
    hard_elapsed = time.monotonic() - started
    ok = (
        degraded_path == "peer_transit"
        and recovered_path == "direct"
        and hard_path == "peer_transit"
        and hard_elapsed < 10
    )
    return {
        "ok": ok,
        "degraded_path": degraded_path,
        "recovered_path": recovered_path,
        "hard_failure_path": hard_path,
        "hard_failure_switch_s": hard_elapsed,
        "events": manager.events,
        "hard_failure_events": hard_manager.events,
        "policy": {
            "hard_failure_count": policy.hard_failure_count,
            "loss_threshold": policy.loss_threshold,
            "latency_threshold_ms": policy.latency_threshold_ms,
            "transit_improvement_ratio": policy.transit_improvement_ratio,
            "degraded_hold_s": policy.degraded_hold_s,
            "transit_min_hold_s": policy.transit_min_hold_s,
            "recovery_hold_s": policy.recovery_hold_s,
            "recovery_probe_count": policy.recovery_probe_count,
        },
        "healthy_direct_metrics": {
            "reachable": healthy.reachable,
            "rtt_p95_ms": healthy.rtt_p95_ms,
            "loss_ratio": healthy.loss_ratio,
            "consecutive_failures": healthy.consecutive_failures,
        },
        "degraded_direct_metrics": {
            "reachable": degraded.reachable,
            "rtt_p95_ms": degraded.rtt_p95_ms,
            "jitter_ms": 75.0,
            "loss_ratio": degraded.loss_ratio,
            "consecutive_failures": degraded.consecutive_failures,
        },
        "transit_metrics": {
            "reachable": transit.reachable,
            "rtt_p95_ms": transit.rtt_p95_ms,
            "loss_ratio": transit.loss_ratio,
            "consecutive_failures": transit.consecutive_failures,
        },
        "recovery_probe_times": recovery_probe_times,
    }


def _degraded_network_acceptance(
    *,
    work_root: Path,
    source: RynmeshStore,
    relay: RynmeshStore,
    target: RynmeshStore,
    network_id: str,
    timeout_s: float,
    frame_audit: _FrameAudit,
    route: dict[str, Any],
) -> dict[str, Any]:
    """Exercise real direct UDP loss/retry, then actual adaptive peer transit."""

    direct_file = work_root / "degraded-direct.bin"
    _write_payload(direct_file, 64 * 1024)
    profile = _DatagramImpairment()
    original_factory = peer_transit_service.new_connection
    transit_before_direct = int(frame_audit.snapshot()["bytes"])
    direct_started = time.monotonic()
    try:
        peer_transit_service.new_connection = _impaired_connection_factory(
            original_factory,
            profile,
        )
        direct_evidence = send_file_direct(
            source,
            direct_file,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
        )
    finally:
        peer_transit_service.new_connection = original_factory
    direct_elapsed = time.monotonic() - direct_started
    transit_after_direct = int(frame_audit.snapshot()["bytes"])
    impairment = profile.snapshot()
    direct_delivered = list(
        (work_root / "target-inbox").glob("*-degraded-direct.bin")
    )
    direct_partials = list((work_root / "target-inbox").rglob("*.part"))
    observed_loss = float(impairment["observed_loss_ratio"])
    impairment_ok = (
        int(impairment["attempted_datagrams"]) >= 100
        and int(impairment["dropped_datagrams"]) > 0
        and 0.15 <= observed_loss <= 0.20
        and 250 <= float(impairment["scheduled_rtt_min_ms"])
        and float(impairment["scheduled_rtt_max_ms"]) <= 350
        and (
            float(impairment["scheduled_rtt_max_ms"])
            - float(impairment["scheduled_rtt_min_ms"])
        )
        >= 50
    )
    direct_ok = (
        direct_evidence.get("path_mode") == "direct"
        and direct_evidence.get("ice_relay_candidate_used") is False
        and direct_evidence.get("source_sha256") == direct_evidence.get("target_sha256")
        and transit_before_direct == transit_after_direct
        and len(direct_delivered) == 1
        and not direct_partials
    )

    degraded = PathMetrics(True, 330, 0.18)
    transit = PathMetrics(True, 80, 0.01)
    manager = RouteManager(
        RoutePolicy(
            degraded_hold_s=30,
            transit_min_hold_s=60,
            recovery_hold_s=120,
            recovery_probe_count=5,
        )
    )
    manager.update(direct=PathMetrics(True, 40, 0), transit=transit, now_monotonic=0)
    manager.update(direct=degraded, transit=transit, now_monotonic=1)
    manager.update(direct=degraded, transit=transit, now_monotonic=31)

    adaptive_file = work_root / "degraded-adaptive.bin"
    _write_payload(adaptive_file, 64 * 1024)
    transit_before_adaptive = int(frame_audit.snapshot()["bytes"])
    adaptive_started = time.monotonic()
    adaptive_evidence = send_file_adaptive(
        source,
        adaptive_file,
        relay_peer_id=relay.peer_id,
        target_peer_id=target.peer_id,
        direct_metrics=degraded,
        transit_metrics=transit,
        route_manager=manager,
        network_id=network_id,
        timeout_s=timeout_s,
        direct_attempt_timeout_s=min(8.0, timeout_s),
    )
    adaptive_elapsed = time.monotonic() - adaptive_started
    transit_after_adaptive = int(frame_audit.snapshot()["bytes"])
    adaptive_delivered = list(
        (work_root / "target-inbox").glob("*-degraded-adaptive.bin")
    )
    adaptive_partials = list((work_root / "target-inbox").rglob("*.part"))
    adaptive_reasons = [
        str(item.get("reason") or "")
        for item in adaptive_evidence.get("route_events", [])
    ]
    adaptive_ok = (
        route.get("degraded_path") == "peer_transit"
        and adaptive_evidence.get("path_mode") == "peer_transit"
        and adaptive_evidence.get("selected_path") == "peer_transit"
        and not str(adaptive_evidence.get("direct_fallback_error") or "")
        and adaptive_evidence.get("source_sha256")
        == adaptive_evidence.get("target_sha256")
        and transit_after_adaptive > transit_before_adaptive
        and adaptive_reasons == ["direct_degraded", "transit_better"]
        and len(adaptive_delivered) == 1
        and not adaptive_partials
    )
    return {
        "ok": impairment_ok and direct_ok and adaptive_ok,
        "transfer_timeout_s": timeout_s,
        "impairment": impairment,
        "route_degraded_path": route.get("degraded_path"),
        "direct_under_impairment": {
            "ok": direct_ok,
            "elapsed_s": direct_elapsed,
            "transit_bytes_before": transit_before_direct,
            "transit_bytes_after": transit_after_direct,
            "committed_target_files": len(direct_delivered),
            "partial_target_files": len(direct_partials),
            "source_sha256": direct_evidence.get("source_sha256"),
            "target_sha256": direct_evidence.get("target_sha256"),
            "evidence": direct_evidence,
        },
        "adaptive_after_degrade": {
            "ok": adaptive_ok,
            "elapsed_s": adaptive_elapsed,
            "transit_bytes_before": transit_before_adaptive,
            "transit_bytes_after": transit_after_adaptive,
            "committed_target_files": len(adaptive_delivered),
            "partial_target_files": len(adaptive_partials),
            "route_events": adaptive_evidence.get("route_events"),
            "source_sha256": adaptive_evidence.get("source_sha256"),
            "target_sha256": adaptive_evidence.get("target_sha256"),
            "evidence": adaptive_evidence,
        },
    }


def _relay_unavailable_acceptance(root: Path) -> dict[str, Any]:
    registry = FilePeerRegistry(root / "failure-registry")
    source = RynmeshStore(home=root / "failure-source", network_dir=root / "failure-source-net")
    relay = RynmeshStore(home=root / "failure-relay", network_dir=root / "failure-relay-net")
    target = RynmeshStore(home=root / "failure-target", network_dir=root / "failure-target-net")
    for store in (source, relay, target):
        store.registry = registry
    network_id = "peer-transit-unavailable"
    advertise_transit_capacity(target, network_id=network_id, roles=("target",))
    operation_timeout_s = 0.5
    relay_worker = PeerTransitWorker(
        relay,
        role="transit",
        network_id=network_id,
        timeout_s=operation_timeout_s,
    )
    relay_stop = threading.Event()
    relay_thread = threading.Thread(
        target=relay_worker.serve_forever,
        kwargs={"poll_interval_s": 0.01, "stop_event": relay_stop},
        daemon=True,
    )
    relay_thread.start()
    capacity_deadline = time.monotonic() + 2.0
    relay_capacity_visible = False
    while time.monotonic() < capacity_deadline:
        capacities = relay.list_job_capacities(
            network_id=network_id,
            capability=peer_transit_service.TRANSIT_CAPABILITY,
        ).get("capacities", [])
        if any(item.get("peer_id") == relay.peer_id for item in capacities):
            relay_capacity_visible = True
            break
        time.sleep(0.01)
    relay_worker_started = relay_thread.is_alive() and relay_capacity_visible
    relay_stop.set()
    relay_thread.join(timeout=2.0)
    relay_worker_stopped_before_request = not relay_thread.is_alive()
    advertised_capacity_remained = any(
        item.get("peer_id") == relay.peer_id
        for item in relay.list_job_capacities(
            network_id=network_id,
            capability=peer_transit_service.TRANSIT_CAPABILITY,
        ).get("capacities", [])
    )
    payload = root / "failure-payload.bin"
    _write_payload(payload, 4096)
    maximum_elapsed_s = 2.0
    started = time.monotonic()
    error = ""
    try:
        send_file_via_peer(
            source,
            payload,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=operation_timeout_s,
        )
    except PeerTransitError as exc:
        error = str(exc)
    elapsed = time.monotonic() - started
    failure_inbox = target.home / "transit-inbox"
    committed_target_files = len([path for path in failure_inbox.glob("*") if path.is_file()])
    partial_target_files = len(list(failure_inbox.rglob("*.part")))
    return {
        "ok": (
            relay_worker_started
            and relay_worker_stopped_before_request
            and advertised_capacity_remained
            and "timed out" in error.lower()
            and elapsed <= maximum_elapsed_s
            and committed_target_files == 0
            and partial_target_files == 0
        ),
        "elapsed_s": elapsed,
        "operation_timeout_s": operation_timeout_s,
        "maximum_elapsed_s": maximum_elapsed_s,
        "error": error,
        "relay_worker_started": relay_worker_started,
        "relay_worker_stopped_before_request": relay_worker_stopped_before_request,
        "advertised_capacity_remained": advertised_capacity_remained,
        "committed_target_files": committed_target_files,
        "partial_target_files": partial_target_files,
    }


def _control_plane_blackout_acceptance(root: Path, *, timeout_s: float) -> dict[str, Any]:
    """Prove an established two-hop data plane survives registry/STUN isolation."""

    registry = _RegistryBlackout(FilePeerRegistry(root / "blackout-registry"))
    source = RynmeshStore(home=root / "blackout-source", network_dir=root / "blackout-source-net")
    relay = RynmeshStore(home=root / "blackout-relay", network_dir=root / "blackout-relay-net")
    target = RynmeshStore(home=root / "blackout-target", network_dir=root / "blackout-target-net")
    for store in (source, relay, target):
        store.registry = registry
    network_id = "peer-transit-control-plane-blackout"
    inbox = root / "blackout-target-inbox"
    relay_worker = PeerTransitWorker(
        relay,
        role="transit",
        network_id=network_id,
        timeout_s=timeout_s,
    )
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=inbox,
        timeout_s=timeout_s,
    )
    relay_worker.register()
    target_worker.register()
    stop = threading.Event()
    workers = [
        threading.Thread(
            target=relay_worker.serve_forever,
            kwargs={"poll_interval_s": 0.02, "stop_event": stop},
            daemon=True,
        ),
        threading.Thread(
            target=target_worker.serve_forever,
            kwargs={"poll_interval_s": 0.02, "stop_event": stop},
            daemon=True,
        ),
    ]
    payload = root / "control-plane-blackout.bin"
    _write_payload(payload, 4 * 1024 * 1024)
    original_send = peer_transit_service.send_encrypted_stream
    original_receive = peer_transit_service.receive_encrypted_stream
    state: dict[str, Any] = {
        "registry_probe_blocked": False,
        "request_completed_during_blackout": False,
        "blackout_started": 0.0,
        "blackout_ended": 0.0,
    }

    async def send_with_blackout(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("direction") == "request" and not registry.blackout.is_set():
            if not registry.two_hops_ready.wait(timeout=timeout_s):
                raise PeerTransitError("two ICE hops were not ready before control-plane blackout")
            registry.blackout.set()
            state["blackout_started"] = time.monotonic()
            try:
                registry.list_job_capacities(
                    network_id=network_id,
                    capability=peer_transit_service.TRANSIT_CAPABILITY,
                    max_age_hours=1,
                )
            except OSError:
                state["registry_probe_blocked"] = True
            else:
                raise PeerTransitError("registry remained reachable during blackout")
        return await original_send(*args, **kwargs)

    async def receive_with_blackout(*args: Any, **kwargs: Any) -> Any:
        result = await original_receive(*args, **kwargs)
        if kwargs.get("direction") == "request":
            state["request_completed_during_blackout"] = registry.blackout.is_set()
            state["blackout_ended"] = time.monotonic()
            registry.blackout.clear()
        return result

    peer_transit_service.send_encrypted_stream = send_with_blackout
    peer_transit_service.receive_encrypted_stream = receive_with_blackout
    for worker in workers:
        worker.start()
    evidence: dict[str, Any] = {}
    error = ""
    try:
        evidence = send_file_via_peer(
            source,
            payload,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - acceptance records the exact failure
        error = f"{type(exc).__name__}: {exc}"
    finally:
        registry.blackout.clear()
        peer_transit_service.send_encrypted_stream = original_send
        peer_transit_service.receive_encrypted_stream = original_receive
        stop.set()
        for worker in workers:
            worker.join(timeout=5)

    blackout_elapsed = max(
        0.0,
        float(state["blackout_ended"]) - float(state["blackout_started"]),
    )
    partial_files = len(list(inbox.glob("*.part")))
    delivered = list(inbox.glob("*-control-plane-blackout.bin"))
    workers_stopped = all(not worker.is_alive() for worker in workers)
    ok = (
        not error
        and state["registry_probe_blocked"] is True
        and state["request_completed_during_blackout"] is True
        and blackout_elapsed > 0
        and registry.blocked_calls >= 1
        and evidence.get("source_sha256") == evidence.get("target_sha256")
        and evidence.get("ice_relay_candidate_used") is False
        and len(delivered) == 1
        and partial_files == 0
        and workers_stopped
    )
    return {
        "ok": ok,
        "error": error,
        "registry_probe_blocked": state["registry_probe_blocked"],
        "registry_blocked_calls": registry.blocked_calls,
        "request_completed_during_blackout": state["request_completed_during_blackout"],
        "blackout_elapsed_s": blackout_elapsed,
        "transfer_timeout_s": timeout_s,
        "stun_disabled": os.environ.get("RYNMESH_P2P_STUN", "").lower() == "off",
        "source_sha256": evidence.get("source_sha256"),
        "target_sha256": evidence.get("target_sha256"),
        "ice_relay_candidate_used": evidence.get("ice_relay_candidate_used"),
        "payload_size_bytes": payload.stat().st_size,
        "target_files": len(delivered),
        "partial_target_files": partial_files,
        "worker_threads_stopped": workers_stopped,
        "evidence": evidence,
    }


def _resume_after_disconnect_acceptance(
    *,
    work_root: Path,
    source: RynmeshStore,
    relay: RynmeshStore,
    target: RynmeshStore,
    network_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    payload = work_root / "resume-after-disconnect.bin"
    segment_bytes = 64 * 1024
    _write_payload(payload, 3 * segment_bytes + 4096, marker=MARKER + b"-resume")
    original_send = peer_transit_service.send_encrypted_stream
    request_calls = 0

    async def disconnect_second_segment(connection: Any, cipher: Any, **kwargs: Any) -> Any:
        nonlocal request_calls
        if kwargs.get("direction") == "request":
            request_calls += 1
            if request_calls == 2:
                await connection.close()
                raise ConnectionError("acceptance injected ICE disconnect after verified boundary")
        return await original_send(connection, cipher, **kwargs)

    peer_transit_service.send_encrypted_stream = disconnect_second_segment
    started = time.monotonic()
    try:
        evidence = send_file_via_peer(
            source,
            payload,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
            resume_segment_bytes=segment_bytes,
            max_resume_attempts=2,
        )
    finally:
        peer_transit_service.send_encrypted_stream = original_send
    elapsed = time.monotonic() - started
    audit = audit_peer_transit(evidence)
    delivered = list((work_root / "target-inbox").glob("*-resume-after-disconnect.bin"))
    partials = list((work_root / "target-inbox").rglob("*.part"))
    checkpoints = list((work_root / "target-inbox").rglob("*.resume.json"))
    expected_boundaries = [
        segment_bytes,
        2 * segment_bytes,
        3 * segment_bytes,
        payload.stat().st_size,
    ]
    failures = list(evidence.get("failed_attempts") or [])
    failed_session_ids = {str(item.get("session_id") or "") for item in failures}
    successful_session_ids = {str(item) for item in evidence.get("session_ids") or []}
    ok = (
        audit.get("ok") is True
        and int(evidence.get("resume_attempts", -1)) == 1
        and evidence.get("verified_boundaries") == expected_boundaries
        and len(failures) == 1
        and int(failures[0].get("offset_bytes", -1)) == segment_bytes
        and failures[0].get("retryable") is True
        and bool(failed_session_ids)
        and "" not in failed_session_ids
        and failed_session_ids.isdisjoint(successful_session_ids)
        and len(delivered) == 1
        and delivered[0].read_bytes() == payload.read_bytes()
        and not partials
        and not checkpoints
    )
    return {
        "ok": ok,
        "elapsed_s": elapsed,
        "injected_disconnect_after_offset_bytes": segment_bytes,
        "resume_attempts": evidence.get("resume_attempts"),
        "verified_boundaries": evidence.get("verified_boundaries"),
        "failed_session_ids": sorted(failed_session_ids),
        "successful_session_ids": sorted(successful_session_ids),
        "target_files": len(delivered),
        "partial_target_files": len(partials),
        "checkpoint_files": len(checkpoints),
        "source_sha256": evidence.get("source_sha256"),
        "target_sha256": evidence.get("target_sha256"),
        "evidence": evidence,
        "audit": audit,
    }


def run_acceptance(
    *,
    size_bytes: int,
    concurrent_sessions: int,
    timeout_s: float,
    work_root: Path,
) -> dict[str, Any]:
    # Hermetic acceptance uses real host ICE candidates and never contacts an
    # external STUN service.  A physical three-network run overrides this with
    # its chosen STUN endpoint to prove server-reflexive mappings.
    os.environ.setdefault("RYNMESH_P2P_STUN", "off")
    _prepare_work_root(work_root)
    registry_root = work_root / "registry"
    registry = FilePeerRegistry(registry_root)
    source = RynmeshStore(home=work_root / "source", network_dir=work_root / "source-net")
    relay = RynmeshStore(home=work_root / "relay", network_dir=work_root / "relay-net")
    target = RynmeshStore(home=work_root / "target", network_dir=work_root / "target-net")
    for store in (source, relay, target):
        store.registry = registry
    network_id = "peer-transit-acceptance"
    frame_audit = _FrameAudit()
    worker_trace_lock = threading.Lock()
    worker_trace: dict[str, dict[str, dict[str, float]]] = {
        "relay": {},
        "target": {},
    }

    def session_auditor(worker_name: str):
        def record(event: dict[str, Any]) -> None:
            session_id = str(event.get("session_id") or "")
            phase = str(event.get("phase") or "")
            if not session_id or phase not in {"started", "finished"}:
                return
            with worker_trace_lock:
                worker_trace[worker_name].setdefault(session_id, {})[phase] = float(
                    event["at_monotonic"]
                )

        return record

    relay_worker = PeerTransitWorker(
        relay,
        role="transit",
        network_id=network_id,
        timeout_s=timeout_s,
        max_concurrent=max(8, concurrent_sessions),
        audit_frame=frame_audit,
        audit_session=session_auditor("relay"),
    )
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=work_root / "target-inbox",
        timeout_s=timeout_s,
        max_concurrent=max(8, concurrent_sessions),
        allow_direct=True,
        audit_session=session_auditor("target"),
    )
    relay_worker.register()
    target_worker.register()
    stop = threading.Event()
    relay_thread = threading.Thread(
        target=relay_worker.serve_forever,
        kwargs={"poll_interval_s": 0.02, "stop_event": stop},
        daemon=True,
    )
    target_thread = threading.Thread(
        target=target_worker.serve_forever,
        kwargs={"poll_interval_s": 0.02, "stop_event": stop},
        daemon=True,
    )
    relay_thread.start()
    target_thread.start()

    payload = work_root / "acceptance-payload.bin"
    _write_payload(payload, size_bytes)
    tracemalloc.start()
    started = time.monotonic()
    try:
        evidence = send_file_via_peer(
            source,
            payload,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
        )
        main_elapsed = time.monotonic() - started
        _current, peak_memory = tracemalloc.get_traced_memory()
        frame_snapshot = frame_audit.snapshot()
        evidence["plaintext_found_on_transit"] = frame_snapshot["plaintext_found"]
        evidence["transit_frame_audit"] = {
            "frames": frame_snapshot["frames"],
            "bytes": frame_snapshot["bytes"],
            "max_frame_bytes": frame_snapshot["max_frame_bytes"],
        }
        evidence["elapsed_s"] = main_elapsed
        evidence["peak_python_memory_bytes"] = peak_memory
        main_audit = audit_peer_transit(evidence)

        concurrency_files: list[Path] = []
        for index in range(concurrent_sessions):
            item = work_root / f"concurrent-{index:02d}.bin"
            _write_payload(
                item,
                CONCURRENT_PROBE_BYTES,
                marker=MARKER + str(index).encode(),
            )
            concurrency_files.append(item)
        concurrency_started = time.monotonic()
        concurrent_results: list[dict[str, Any]] = []
        caller_concurrent_timeline: list[dict[str, Any]] = []
        if concurrency_files:
            def run_concurrent(index: int, item: Path) -> tuple[dict[str, Any], dict[str, Any]]:
                started_at = time.monotonic() - concurrency_started
                result = send_file_via_peer(
                    source,
                    item,
                    relay_peer_id=relay.peer_id,
                    target_peer_id=target.peer_id,
                    network_id=network_id,
                    timeout_s=timeout_s,
                )
                ended_at = time.monotonic() - concurrency_started
                return result, {
                    "index": index,
                    "session_id": str(result.get("session_id") or ""),
                    "started_s": started_at,
                    "ended_s": ended_at,
                }

            with ThreadPoolExecutor(max_workers=concurrent_sessions) as pool:
                futures = [
                    pool.submit(
                        run_concurrent,
                        index,
                        item,
                    )
                    for index, item in enumerate(concurrency_files)
                ]
                concurrent_pairs = [
                    future.result(timeout=timeout_s + 10) for future in futures
                ]
                concurrent_results = [item[0] for item in concurrent_pairs]
                caller_concurrent_timeline = [item[1] for item in concurrent_pairs]
        concurrency_elapsed = time.monotonic() - concurrency_started
        concurrent_session_ids = {
            str(result.get("session_id") or "") for result in concurrent_results
        }
        worker_trace_complete = _wait_for_worker_trace(
            worker_trace,
            worker_trace_lock,
            concurrent_session_ids,
            timeout_s=timeout_s,
        )

        def worker_timeline(worker_name: str) -> list[dict[str, Any]]:
            with worker_trace_lock:
                trace = {
                    session_id: dict(times)
                    for session_id, times in worker_trace[worker_name].items()
                    if session_id in concurrent_session_ids
                }
            return [
                {
                    "session_id": session_id,
                    "started_s": times["started"] - concurrency_started,
                    "ended_s": times["finished"] - concurrency_started,
                }
                for session_id, times in sorted(trace.items())
                if "started" in times and "finished" in times
            ]

        def timeline_peak(timeline: list[dict[str, Any]]) -> int:
            events = sorted(
                [(float(item["started_s"]), 1) for item in timeline]
                + [(float(item["ended_s"]), -1) for item in timeline],
                key=lambda item: (item[0], -item[1]),
            )
            active = 0
            peak = 0
            for _at, delta in events:
                active += delta
                peak = max(peak, active)
            return peak

        concurrent_timeline = worker_timeline("relay")
        target_concurrent_timeline = worker_timeline("target")
        peak_concurrent = timeline_peak(concurrent_timeline)
        peak_target_concurrent = timeline_peak(target_concurrent_timeline)
        concurrency_ok = worker_trace_complete and all(
            result.get("source_sha256") == result.get("target_sha256")
            and result.get("ice_relay_candidate_used") is False
            for result in concurrent_results
        ) and (
            len(concurrent_timeline) == concurrent_sessions
            and len(target_concurrent_timeline) == concurrent_sessions
            and peak_concurrent == concurrent_sessions
            and peak_target_concurrent == concurrent_sessions
        )

        route = _route_acceptance()
        degraded_network = _degraded_network_acceptance(
            work_root=work_root,
            source=source,
            relay=relay,
            target=target,
            network_id=network_id,
            timeout_s=timeout_s,
            frame_audit=frame_audit,
            route=route,
        )
        direct_file = work_root / "healthy-direct.bin"
        _write_payload(direct_file, 64 * 1024)
        transit_bytes_before_direct = int(frame_audit.snapshot()["bytes"])
        direct_file_started = time.monotonic()
        direct_file_evidence = send_file_direct(
            source,
            direct_file,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
        )
        direct_file_elapsed = time.monotonic() - direct_file_started
        transit_bytes_after_direct = int(frame_audit.snapshot()["bytes"])
        healthy_direct_file = {
            "ok": (
                direct_file_evidence.get("path_mode") == "direct"
                and direct_file_evidence.get("ice_relay_candidate_used") is False
                and direct_file_evidence.get("source_sha256")
                == direct_file_evidence.get("target_sha256")
                and transit_bytes_after_direct == transit_bytes_before_direct
            ),
            "elapsed_s": direct_file_elapsed,
            "source_sha256": direct_file_evidence.get("source_sha256"),
            "target_sha256": direct_file_evidence.get("target_sha256"),
            "transit_bytes_before": transit_bytes_before_direct,
            "transit_bytes_after": transit_bytes_after_direct,
            "route_recovered_path": route.get("recovered_path"),
            "source_hop": direct_file_evidence.get("source_hop"),
            "evidence": direct_file_evidence,
        }

        hard_failure_file = work_root / "hard-failure-fallback.bin"
        _write_payload(hard_failure_file, 64 * 1024)
        target_worker.allow_direct = False
        hard_failure_started = time.monotonic()
        hard_failure_evidence = send_file_adaptive(
            source,
            hard_failure_file,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=timeout_s,
            direct_attempt_timeout_s=min(8.0, timeout_s),
        )
        hard_failure_elapsed = time.monotonic() - hard_failure_started
        audit_peer_transit(hard_failure_evidence)
        actual_hard_failure = {
            "ok": (
                hard_failure_evidence.get("path_mode") == "peer_transit"
                and bool(hard_failure_evidence.get("direct_fallback_error"))
                and hard_failure_elapsed <= 10
            ),
            "elapsed_s": hard_failure_elapsed,
            "selected_path": hard_failure_evidence.get("path_mode"),
            "direct_fallback_error": hard_failure_evidence.get("direct_fallback_error"),
            "route_events": hard_failure_evidence.get("route_events"),
            "evidence": hard_failure_evidence,
        }
        resume_after_disconnect = _resume_after_disconnect_acceptance(
            work_root=work_root,
            source=source,
            relay=relay,
            target=target,
            network_id=network_id,
            timeout_s=timeout_s,
        )
    finally:
        tracemalloc.stop()
        stop.set()
        relay_thread.join(timeout=5)
        target_thread.join(timeout=5)

    worker_control_errors = {
        "relay": relay_worker.control_error_snapshot(),
        "target": target_worker.control_error_snapshot(),
    }
    worker_control_loop_clean = all(
        int(item["count"]) == 0 for item in worker_control_errors.values()
    )

    registry_records = list(registry_root.rglob("*.json"))
    registry_record_sizes = [path.stat().st_size for path in registry_records]
    registry_plaintext_found = any(MARKER in path.read_bytes() for path in registry_records)
    registry_control_plane = {
        "record_count": len(registry_record_sizes),
        "record_sizes_bytes": registry_record_sizes,
        "max_record_bytes": max(registry_record_sizes, default=0),
        "total_record_bytes": sum(registry_record_sizes),
        "maximum_record_bytes": MAX_REGISTRY_CONTROL_RECORD_BYTES,
        "application_payload_bytes": int(evidence.get("registry_payload_bytes", -1)),
        "plaintext_marker_found": registry_plaintext_found,
    }
    direct = asyncio.run(_direct_probe())
    unavailable = _relay_unavailable_acceptance(work_root)
    control_plane_blackout = _control_plane_blackout_acceptance(
        work_root,
        timeout_s=timeout_s,
    )
    delivered = list((work_root / "target-inbox").glob("*-acceptance-payload.bin"))
    one_gib_required = size_bytes >= 1024 ** 3
    one_gib_ok = not one_gib_required or (
        evidence["source_size_bytes"] == 1024 ** 3
        and evidence["source_sha256"] == evidence["target_sha256"]
    )
    memory_bounded = evidence["peak_python_memory_bytes"] <= MAX_ACCEPTANCE_PEAK_MEMORY_BYTES
    encrypted_request_bytes = int(evidence["sent"]["wire_bytes"])
    plaintext_request_bytes = int(evidence["sent"]["plaintext_bytes"])
    protocol_overhead_ratio = (
        (encrypted_request_bytes - plaintext_request_bytes) / plaintext_request_bytes
    )
    session_established_s = float(evidence["session_established_s"])
    performance = {
        "ok": (
            main_elapsed <= timeout_s
            and session_established_s <= 5
            and memory_bounded
            and one_gib_ok
            and concurrency_ok
            and protocol_overhead_ratio <= 0.15
            and actual_hard_failure["ok"]
        ),
        "main_elapsed_s": main_elapsed,
        "transfer_within_timeout": main_elapsed <= timeout_s,
        "session_established_s": session_established_s,
        "session_established_within_5s": session_established_s <= 5,
        "encrypted_request_bytes": encrypted_request_bytes,
        "plaintext_request_bytes": plaintext_request_bytes,
        "protocol_overhead_ratio": protocol_overhead_ratio,
        "protocol_overhead_within_15_percent": protocol_overhead_ratio <= 0.15,
        "hard_failure_fallback_within_10s": actual_hard_failure["ok"],
        "peak_python_memory_bytes": evidence["peak_python_memory_bytes"],
        "peak_python_memory_limit_bytes": MAX_ACCEPTANCE_PEAK_MEMORY_BYTES,
        "memory_bounded": memory_bounded,
        "one_gib_required": one_gib_required,
        "one_gib_ok": one_gib_ok,
        "concurrent_sessions": concurrent_sessions,
        "concurrent_payload_bytes": CONCURRENT_PROBE_BYTES,
        "concurrent_completed": len(concurrent_results),
        "concurrent_elapsed_s": concurrency_elapsed,
        "peak_concurrent_observed": peak_concurrent,
        "peak_target_concurrent_observed": peak_target_concurrent,
        "concurrency_observation": "relay_and_target_worker_handlers",
        "worker_trace_complete": worker_trace_complete,
        "concurrency_ok": concurrency_ok,
    }
    final_frame_snapshot = frame_audit.snapshot()
    checks = {
        "healthy_direct": direct["ok"] and healthy_direct_file["ok"],
        "two_hop_peer_transit": main_audit["ok"],
        "automatic_degrade_and_recovery": route["ok"],
        "real_degraded_network": degraded_network["ok"],
        "actual_hard_failure_fallback": actual_hard_failure["ok"],
        "verified_chunk_resume": resume_after_disconnect["ok"],
        "bounded_transit_unavailability": unavailable["ok"],
        "established_data_plane_survives_control_plane_blackout": control_plane_blackout["ok"],
        "no_turn": evidence["ice_relay_candidate_used"] is False,
        "registry_has_no_payload_marker": not registry_plaintext_found,
        "registry_control_sized": (
            bool(registry_record_sizes)
            and registry_control_plane["max_record_bytes"]
            <= MAX_REGISTRY_CONTROL_RECORD_BYTES
            and registry_control_plane["application_payload_bytes"] == 0
        ),
        "transit_has_no_plaintext_marker": not final_frame_snapshot["plaintext_found"],
        "target_hash_matches": evidence["source_sha256"] == evidence["target_sha256"],
        "target_file_committed": len(delivered) == 1,
        "worker_control_loop_clean": worker_control_loop_clean,
        "performance": performance["ok"],
    }
    report = {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "main_evidence": evidence,
        "main_audit": main_audit,
        "direct": direct,
        "healthy_direct_file": healthy_direct_file,
        "route": route,
        "degraded_network": degraded_network,
        "actual_hard_failure": actual_hard_failure,
        "resume_after_disconnect": resume_after_disconnect,
        "unavailable": unavailable,
        "control_plane_blackout": control_plane_blackout,
        "concurrent_evidence": concurrent_results,
        "concurrent_timeline": concurrent_timeline,
        "target_concurrent_timeline": target_concurrent_timeline,
        "caller_concurrent_timeline": caller_concurrent_timeline,
        "performance": performance,
        "registry_plaintext_found": registry_plaintext_found,
        "registry_control_plane": registry_control_plane,
        "worker_control_errors": worker_control_errors,
        "work_root": str(work_root),
    }
    try:
        report["report_audit"] = audit_acceptance_report(
            report,
            require_one_gib=one_gib_required,
            min_concurrent=concurrent_sessions,
        )
    except AuditError as exc:
        report["result"] = "fail"
        report["report_audit"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=8)
    parser.add_argument("--concurrent", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--work-root", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--evidence", default="")
    args = parser.parse_args()
    if args.size_mib < 1 or args.concurrent < 0:
        raise SystemExit("size-mib must be >= 1 and concurrent must be >= 0")
    root = (
        Path(args.work_root).expanduser()
        if args.work_root
        else Path(tempfile.mkdtemp(prefix="rynmesh-peer-transit-acceptance-"))
    )
    root_was_available = not root.exists() or (
        root.is_dir() and next(root.iterdir(), None) is None
    )
    try:
        report = run_acceptance(
            size_bytes=args.size_mib * 1024 * 1024,
            concurrent_sessions=args.concurrent,
            timeout_s=args.timeout,
            work_root=root,
        )
    except Exception as exc:  # noqa: BLE001 - persist exact acceptance failure
        report = {
            "result": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "work_root": str(root),
            "partial_files": (
                len(list(root.rglob("*.part"))) if root.is_dir() else 0
            ),
            "failed_at_unix_s": time.time(),
        }
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output and root_was_available:
            Path(args.output).write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 1
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    if args.evidence:
        Path(args.evidence).write_text(
            json.dumps(report["main_evidence"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(rendered, end="")
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
