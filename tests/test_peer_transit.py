from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh import peer_transit_service
from rynmesh import registry as registry_module
from rynmesh.crypto import SignedPayload
from rynmesh.jobs import WorkOrder, WorkResult, sign_work_order, sign_work_result
from rynmesh.llm_package.p2p import (
    apply_remote_signal,
    gather_signal,
    new_connection,
)
from rynmesh.peer_transit import (
    PathMetrics,
    PeerTransitError,
    RouteManager,
    RoutePolicy,
    RouteState,
    TransitCipher,
    TransitSessionOpen,
    messaging_public_key,
    new_session_id,
    receive_encrypted_stream,
    relay_bidirectional_once,
    send_encrypted_stream,
    sign_session_open,
    transit_evidence,
    verify_session_open,
)
from rynmesh.peer_transit_service import (
    CAPACITY_MAX_AGE_HOURS,
    DEFAULT_CAPACITY_REFRESH_S,
    PeerTransitWorker,
    send_file_adaptive,
    send_file_direct,
)
from rynmesh.registry import FilePeerRegistry, RegistryError
from rynmesh.store import RynmeshStore
from scripts import audit_peer_transit as audit_module
from scripts import finalize_peer_transit_soak as finalize_soak_module
from scripts import run_peer_transit_acceptance as acceptance_module
from scripts import run_peer_transit_soak as soak_module
from scripts.audit_peer_transit import (
    AuditError,
    audit_direct_file,
    audit_peer_transit,
    audit_soak_report,
)
from scripts.run_peer_transit_acceptance import (
    _control_plane_blackout_acceptance,
    _prepare_work_root,
    _relay_unavailable_acceptance,
    _route_acceptance,
    _wait_for_worker_trace,
)


def _future(seconds: float = 300) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_acceptance_cli_persists_runtime_failure_without_polluting_stale_root(
    tmp_path,
    monkeypatch,
) -> None:
    def fail_after_preparation(**kwargs):
        work_root = kwargs["work_root"]
        _prepare_work_root(work_root)
        (work_root / "target-inbox").mkdir()
        (work_root / "target-inbox" / "failed.part").write_bytes(b"partial")
        raise ConnectionError("diagnostic connection loss")

    monkeypatch.setattr(acceptance_module, "run_acceptance", fail_after_preparation)
    fresh = tmp_path / "fresh"
    report_path = fresh / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_peer_transit_acceptance.py",
            "--size-mib",
            "1",
            "--concurrent",
            "1",
            "--work-root",
            str(fresh),
            "--output",
            str(report_path),
        ],
    )
    assert acceptance_module.main() == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["result"] == "error"
    assert report["error_type"] == "ConnectionError"
    assert report["partial_files"] == 1
    assert "diagnostic connection loss" in report["traceback"]

    stale = tmp_path / "stale"
    stale.mkdir()
    sentinel = stale / "existing.json"
    sentinel.write_text("preserve", encoding="utf-8")
    stale_report = stale / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_peer_transit_acceptance.py",
            "--work-root",
            str(stale),
            "--output",
            str(stale_report),
        ],
    )
    assert acceptance_module.main() == 1
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not stale_report.exists()


def test_result_polling_binds_provider_and_requester_identity() -> None:
    calls: list[dict[str, object]] = []
    legitimate = {
        "work_order_id": "order-1",
        "network_id": "network-1",
        "provider_peer_id": "expected-provider",
        "requester_peer_id": "expected-requester",
        "status": "accepted",
    }
    forged_provider = {
        **legitimate,
        "provider_peer_id": "attacker",
        "status": "failed",
        "message": "forged failure",
    }
    forged_requester = {
        **legitimate,
        "requester_peer_id": "other-requester",
        "status": "failed",
        "message": "wrong requester failure",
    }
    forged_order = {
        **legitimate,
        "work_order_id": "other-order",
        "status": "failed",
        "message": "wrong order failure",
    }
    forged_network = {
        **legitimate,
        "network_id": "other-network",
        "status": "failed",
        "message": "wrong network failure",
    }

    class StubStore:
        def list_work_results(self, **kwargs):
            calls.append(kwargs)
            # Deliberately ignore the supplied filters to prove the caller also
            # checks the signed identities before trusting a status/result body.
            return {
                "work_results": [
                    legitimate,
                    forged_provider,
                    forged_requester,
                    forged_order,
                    forged_network,
                ]
            }

    result = peer_transit_service._poll_result(
        StubStore(),
        work_order_id="order-1",
        network_id="network-1",
        expected_provider_peer_id="expected-provider",
        expected_requester_peer_id="expected-requester",
        wanted_status="accepted",
        timeout_s=1,
    )

    assert result is legitimate
    assert calls == [
        {
            "work_order_id": "order-1",
            "network_id": "network-1",
            "provider_peer_id": "expected-provider",
            "requester_peer_id": "expected-requester",
        }
    ]


def test_result_polling_ignores_signed_result_from_wrong_registry_peer(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "source-net")
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "provider-net")
    attacker = RynmeshStore(home=tmp_path / "attacker", network_dir=tmp_path / "attacker-net")
    for store in (source, provider, attacker):
        store.registry = registry

    submitted = source.submit_work_order(
        provider_peer_id=provider.peer_id,
        capability="identity-binding.test",
        operation="serve",
        network_id="identity-binding",
    )
    order_id = submitted["work_order_id"]
    with pytest.raises(RegistryError, match="work_result_order_identity_mismatch"):
        attacker.publish_work_result(
            work_order_id=order_id,
            requester_peer_id=source.peer_id,
            status="failed",
            message="forged failure",
            network_id="identity-binding",
        )
    provider.publish_work_result(
        work_order_id=order_id,
        requester_peer_id=source.peer_id,
        status="accepted",
        message="legitimate result",
        network_id="identity-binding",
    )

    result = peer_transit_service._poll_result(
        source,
        work_order_id=order_id,
        network_id="identity-binding",
        expected_provider_peer_id=provider.peer_id,
        expected_requester_peer_id=source.peer_id,
        wanted_status="accepted",
        timeout_s=1,
    )

    assert result["provider_peer_id"] == provider.peer_id
    assert result["message"] == "legitimate result"


def test_wrong_provider_result_cannot_hide_open_order_from_expected_provider(
    tmp_path, monkeypatch
) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    requester = RynmeshStore(home=tmp_path / "requester", network_dir=tmp_path / "requester-net")
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "provider-net")
    attacker = RynmeshStore(home=tmp_path / "attacker", network_dir=tmp_path / "attacker-net")
    for store in (requester, provider, attacker):
        store.registry = registry

    submitted = requester.submit_work_order(
        provider_peer_id=provider.peer_id,
        capability="identity-binding.test",
        operation="serve",
        network_id="identity-binding",
    )
    order_id = submitted["work_order_id"]
    forged = sign_work_result(
        WorkResult(
            work_order_id=order_id,
            provider_peer_id=attacker.peer_id,
            requester_peer_id=requester.peer_id,
            status="failed",
            message="forged close",
            network_id="identity-binding",
        ),
        private_key_bytes=attacker.private_key_bytes,
    )
    real_list_results = registry.list_work_results

    def noisy_list_results(**kwargs):
        return [forged, *real_list_results(**kwargs)]

    monkeypatch.setattr(registry, "list_work_results", noisy_list_results)

    still_open = provider.poll_work_orders(network_id="identity-binding")["work_orders"]
    assert [item["work_order_id"] for item in still_open] == [order_id]

    provider.publish_work_result(
        work_order_id=order_id,
        requester_peer_id=requester.peer_id,
        status="accepted",
        network_id="identity-binding",
    )
    assert provider.poll_work_orders(network_id="identity-binding")["work_orders"] == []


def test_registry_rejects_work_order_id_overwrite_and_orphan_result(tmp_path) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    requester = RynmeshStore(home=tmp_path / "requester", network_dir=tmp_path / "requester-net")
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "provider-net")
    attacker = RynmeshStore(home=tmp_path / "attacker", network_dir=tmp_path / "attacker-net")
    for store in (requester, provider, attacker):
        store.registry = registry

    submitted = requester.submit_work_order(
        provider_peer_id=provider.peer_id,
        capability="identity-binding.test",
        operation="serve",
        network_id="identity-binding",
    )
    original = WorkOrder.from_dict(submitted["order"])
    repeated = registry.submit_work_order(
        sign_work_order(original, private_key_bytes=requester.private_key_bytes)
    )
    assert repeated["work_order_id"] == submitted["work_order_id"]

    collision = WorkOrder(
        work_order_id=submitted["work_order_id"],
        requester_peer_id=attacker.peer_id,
        provider_peer_id=provider.peer_id,
        capability="identity-binding.test",
        operation="serve",
        network_id="identity-binding",
    )
    with pytest.raises(RegistryError, match="work_order_id_conflict"):
        registry.submit_work_order(
            sign_work_order(collision, private_key_bytes=attacker.private_key_bytes)
        )

    visible = provider.poll_work_orders(network_id="identity-binding")["work_orders"]
    assert [item["requester_peer_id"] for item in visible] == [requester.peer_id]

    with pytest.raises(RegistryError, match="work_result_order_not_found"):
        provider.publish_work_result(
            work_order_id="missing-order",
            requester_peer_id=requester.peer_id,
            status="failed",
            network_id="identity-binding",
        )


def test_open_work_order_index_does_not_rescan_closed_history(tmp_path, monkeypatch) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    requester = RynmeshStore(home=tmp_path / "requester", network_dir=tmp_path / "requester-net")
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "provider-net")
    requester.registry = registry
    provider.registry = registry

    for index in range(20):
        submitted = requester.submit_work_order(
            provider_peer_id=provider.peer_id,
            capability="index-scaling.test",
            operation=f"closed-{index}",
            network_id="index-scaling",
        )
        provider.publish_work_result(
            work_order_id=submitted["work_order_id"],
            requester_peer_id=requester.peer_id,
            status="accepted",
            network_id="index-scaling",
        )
    active = requester.submit_work_order(
        provider_peer_id=provider.peer_id,
        capability="index-scaling.test",
        operation="active",
        network_id="index-scaling",
    )

    calls = 0
    real_verify = registry_module.verify_work_order

    def counted_verify(signed):
        nonlocal calls
        calls += 1
        return real_verify(signed)

    monkeypatch.setattr(registry_module, "verify_work_order", counted_verify)
    visible = provider.poll_work_orders(
        network_id="index-scaling",
        capability="index-scaling.test",
    )["work_orders"]

    assert [item["work_order_id"] for item in visible] == [active["work_order_id"]]
    assert calls == 1

    provider.publish_work_result(
        work_order_id=active["work_order_id"],
        requester_peer_id=requester.peer_id,
        status="accepted",
        network_id="index-scaling",
    )
    calls = 0
    assert provider.poll_work_orders(network_id="index-scaling")["work_orders"] == []
    assert calls == 0


def test_open_work_order_index_rebuilds_legacy_registry(tmp_path) -> None:
    root = tmp_path / "registry"
    registry = FilePeerRegistry(root)
    requester = RynmeshStore(home=tmp_path / "requester", network_dir=tmp_path / "requester-net")
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "provider-net")
    requester.registry = registry
    provider.registry = registry

    closed = requester.submit_work_order(
        provider_peer_id=provider.peer_id,
        capability="index-rebuild.test",
        operation="closed",
        network_id="index-rebuild",
    )
    provider.publish_work_result(
        work_order_id=closed["work_order_id"],
        requester_peer_id=requester.peer_id,
        status="completed",
        network_id="index-rebuild",
    )
    active = requester.submit_work_order(
        provider_peer_id=provider.peer_id,
        capability="index-rebuild.test",
        operation="active",
        network_id="index-rebuild",
    )

    for path in sorted(registry.open_work_orders_dir.rglob("*.json"), reverse=True):
        path.unlink()
    rebuilt = FilePeerRegistry(root)
    provider.registry = rebuilt

    visible = provider.poll_work_orders(
        network_id="index-rebuild",
        capability="index-rebuild.test",
    )["work_orders"]
    assert [item["work_order_id"] for item in visible] == [active["work_order_id"]]
    assert rebuilt._open_index_ready_path.is_file()


def test_open_work_order_index_retries_windows_marker_delete(tmp_path, monkeypatch) -> None:
    registry = FilePeerRegistry(tmp_path / "registry")
    requester = RynmeshStore(home=tmp_path / "requester", network_dir=tmp_path / "requester-net")
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "provider-net")
    requester.registry = registry
    provider.registry = registry
    submitted = requester.submit_work_order(
        provider_peer_id=provider.peer_id,
        capability="index-delete-race.test",
        operation="close",
        network_id="index-delete-race",
    )
    provider.publish_work_result(
        work_order_id=submitted["work_order_id"],
        requester_peer_id=requester.peer_id,
        status="accepted",
        network_id="index-delete-race",
    )
    order_path = registry.work_orders_dir / f"{registry_module._peer_slug(submitted['work_order_id'])}.json"
    marker = registry._open_order_marker_path(
        provider_peer_id=provider.peer_id,
        order_path=order_path,
    )
    registry._write_open_order_marker(
        provider_peer_id=provider.peer_id,
        order_path=order_path,
    )
    real_unlink = Path.unlink
    denied_once = False

    def flaky_unlink(path, *args, **kwargs):
        nonlocal denied_once
        if path == marker and not denied_once:
            denied_once = True
            raise PermissionError("simulated Windows sharing violation")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    assert provider.poll_work_orders(network_id="index-delete-race")["work_orders"] == []
    assert marker.is_file()
    assert provider.poll_work_orders(network_id="index-delete-race")["work_orders"] == []
    assert not marker.exists()


@pytest.mark.parametrize("provider,requester", [("", "requester"), ("provider", "")])
def test_result_polling_rejects_incomplete_identity_binding(provider, requester) -> None:
    with pytest.raises(PeerTransitError, match="identity binding is incomplete"):
        peer_transit_service._poll_result(
            object(),
            work_order_id="order-1",
            network_id="network-1",
            expected_provider_peer_id=provider,
            expected_requester_peer_id=requester,
            wanted_status="accepted",
            timeout_s=1,
        )


def test_acceptance_work_root_must_not_contain_stale_evidence(tmp_path) -> None:
    missing = tmp_path / "missing"
    _prepare_work_root(missing)
    assert missing.is_dir()

    empty = tmp_path / "empty"
    empty.mkdir()
    _prepare_work_root(empty)

    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "old-report.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PeerTransitError, match="must be empty"):
        _prepare_work_root(stale)

    not_a_directory = tmp_path / "report.json"
    not_a_directory.write_text("{}", encoding="utf-8")
    with pytest.raises(PeerTransitError, match="not a directory"):
        _prepare_work_root(not_a_directory)


def test_worker_refreshes_capacity_before_discovery_record_expires(tmp_path, monkeypatch) -> None:
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    worker = PeerTransitWorker(store, role="transit", network_id="refresh-test")
    assert DEFAULT_CAPACITY_REFRESH_S < CAPACITY_MAX_AGE_HOURS * 3600

    registrations: list[int] = []
    polls = 0
    stop = threading.Event()

    def register() -> dict[str, str]:
        registrations.append(len(registrations) + 1)
        return {"status": "registered"}

    def pending_orders() -> list[dict[str, object]]:
        nonlocal polls
        polls += 1
        if polls == 2:
            stop.set()
        return []

    clock = iter((0.0, 1.0, DEFAULT_CAPACITY_REFRESH_S + 1.0))
    monkeypatch.setattr(worker, "register", register)
    monkeypatch.setattr(worker, "_pending_orders", pending_orders)
    monkeypatch.setattr(peer_transit_service.time, "monotonic", lambda: next(clock))

    worker.serve_forever(poll_interval_s=0, stop_event=stop)

    assert registrations == [1, 2]


def test_worker_honors_max_concurrent_and_deduplicates_in_flight_orders(
    tmp_path, monkeypatch
) -> None:
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    worker = PeerTransitWorker(
        store,
        role="transit",
        network_id="bounded-concurrency",
        max_concurrent=3,
    )
    orders = [
        {
            "work_order_id": f"order-{index}",
            "operation": peer_transit_service.OPEN_OPERATION,
            "params": {"signed_session_open": {"payload": {"session_id": f"session-{index}"}}},
        }
        for index in range(3)
    ]
    stop = threading.Event()
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    active = 0
    peak = 0
    called: list[str] = []

    def run_order(order, _handler) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            called.append(str(order["work_order_id"]))
        barrier.wait(timeout=2)
        stop.set()
        with lock:
            active -= 1

    monkeypatch.setattr(worker, "register", lambda: {"status": "registered"})
    monkeypatch.setattr(worker, "_pending_orders", lambda: list(orders))
    monkeypatch.setattr(worker, "_run_order", run_order)

    worker.serve_forever(poll_interval_s=0.001, stop_event=stop)

    assert peak == 3
    assert active == 0
    assert sorted(called) == ["order-0", "order-1", "order-2"]


def test_worker_records_control_loop_errors(tmp_path, monkeypatch) -> None:
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    worker = PeerTransitWorker(store, role="transit", network_id="control-errors")
    stop = threading.Event()
    calls = 0

    def pending_orders():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient registry failure")
        stop.set()
        return []

    monkeypatch.setattr(worker, "register", lambda: {"status": "registered"})
    monkeypatch.setattr(worker, "_pending_orders", pending_orders)

    worker.serve_forever(poll_interval_s=0, stop_event=stop)

    assert worker.control_error_snapshot() == {
        "count": 1,
        "first": "OSError: transient registry failure",
        "last": "OSError: transient registry failure",
    }


def test_capacity_discovery_retries_atomic_refresh_read_window(tmp_path, monkeypatch) -> None:
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    peer_id = "transit-peer"
    calls = 0

    def list_job_capacities(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"capacities": []}
        return {
            "capacities": [
                {
                    "peer_id": peer_id,
                    "metadata": {
                        "protocol_version": peer_transit_service.PROTOCOL_VERSION,
                        "roles": ["transit"],
                    },
                }
            ]
        }

    delays: list[float] = []
    monkeypatch.setattr(store, "list_job_capacities", list_job_capacities)
    monkeypatch.setattr(peer_transit_service.time, "sleep", delays.append)

    capacity = peer_transit_service._find_capacity(
        store,
        peer_id=peer_id,
        role="transit",
        network_id="retry-test",
    )

    assert capacity["peer_id"] == peer_id
    assert calls == 2
    assert delays == [peer_transit_service.CAPACITY_LOOKUP_RETRY_S]


def test_capacity_publish_retries_windows_atomic_replace_sharing_violation(
    tmp_path,
    monkeypatch,
) -> None:
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    worker = PeerTransitWorker(store, role="transit", network_id="replace-retry-test")
    original_replace = Path.replace
    attempts = 0

    def transient_replace(path: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("simulated Windows sharing violation")
        return original_replace(path, target)

    delays: list[float] = []
    monkeypatch.setattr(Path, "replace", transient_replace)
    monkeypatch.setattr(registry_module.time, "sleep", delays.append)

    worker.register()

    capacities = store.list_job_capacities(
        network_id="replace-retry-test",
        capability=peer_transit_service.TRANSIT_CAPABILITY,
    )["capacities"]
    assert attempts == 2
    assert delays == [registry_module.ATOMIC_REPLACE_RETRY_S]
    assert [capacity["peer_id"] for capacity in capacities] == [store.peer_id]


def test_established_data_plane_survives_control_plane_blackout(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")

    result = _control_plane_blackout_acceptance(tmp_path, timeout_s=30)

    assert result["ok"] is True
    assert result["registry_probe_blocked"] is True
    assert result["registry_blocked_calls"] >= 1
    assert result["request_completed_during_blackout"] is True
    assert result["blackout_elapsed_s"] > 0
    assert result["source_sha256"] == result["target_sha256"]
    assert result["ice_relay_candidate_used"] is False
    assert result["partial_target_files"] == 0
    assert result["worker_threads_stopped"] is True
    assert audit_peer_transit(result["evidence"])["source_size_bytes"] == 4 * 1024 * 1024


def test_soak_auditor_fails_closed_on_resource_or_lifecycle_gaps(monkeypatch) -> None:
    monkeypatch.setattr(
        audit_module,
        "audit_peer_transit",
        lambda value: {
            "protocol_version": "rynmesh.peer-transit.v1",
            "source_size_bytes": 32,
            "request_frames": 2,
            "response_frames": 1,
        },
    )
    report = {
        "result": "pass",
        "completed_duration": True,
        "clock_source": "time.monotonic",
        "duration_target_s": 86400,
        "elapsed_s": 86401,
        "sessions_completed": 100,
        "failures": [],
        "plaintext_found_on_transit": False,
        "memory_growth_bytes": 1024,
        "memory_growth_limit_bytes": 2048,
        "partial_files": 0,
        "worker_threads_stopped": True,
        "worker_control_errors": {
            "relay": {"count": 0, "first": "", "last": ""},
            "target": {"count": 0, "first": "", "last": ""},
        },
        "transit_frames": 300,
        "transit_bytes": 4096,
        "last_evidence": {"session_id": "abc"},
    }
    assert (
        audit_soak_report(
            report,
            require_duration_s=86400,
            min_sessions=100,
        )["minimum_transit_bytes"]
        == 3200
    )

    for key, bad_value, message in (
        ("result", "running", "complete"),
        ("clock_source", "time.time", "monotonic"),
        ("duration_target_s", float("nan"), "finite"),
        ("elapsed_s", float("inf"), "finite"),
        ("elapsed_s", 86399, "duration"),
        ("sessions_completed", 99, "few sessions"),
        ("failures", [{"error": "boom"}], "failed"),
        ("plaintext_found_on_transit", True, "plaintext"),
        ("memory_growth_bytes", 4096, "memory"),
        ("partial_files", 1, "partial"),
        ("worker_threads_stopped", False, "threads"),
        ("transit_frames", 299, "frame count"),
        ("transit_bytes", 3199, "byte count"),
    ):
        tampered = {**report, key: bad_value}
        with pytest.raises(AuditError, match=message):
            audit_soak_report(
                tampered,
                require_duration_s=86400,
                min_sessions=100,
            )

    with pytest.raises(AuditError, match="finite"):
        audit_soak_report(report, require_duration_s=float("nan"), min_sessions=100)

    control_error = copy.deepcopy(report)
    control_error["worker_control_errors"]["relay"] = {
        "count": 1,
        "first": "PermissionError: busy",
        "last": "PermissionError: busy",
    }
    with pytest.raises(AuditError, match="control loop"):
        audit_soak_report(control_error, require_duration_s=86400, min_sessions=100)


def test_soak_duration_survives_forward_wall_clock_jump(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")

    class JumpingWallClock:
        def __init__(self) -> None:
            self.first_wall_read = True

        def time(self) -> float:
            current = time.time()
            if self.first_wall_read:
                self.first_wall_read = False
                return current
            return current + 3600.0

        @staticmethod
        def monotonic() -> float:
            return time.monotonic()

    monkeypatch.setattr(soak_module, "time", JumpingWallClock())
    report = soak_module.run_soak(
        duration_s=2.0,
        interval_s=0.0,
        payload_bytes=4096,
        timeout_s=20.0,
        capacity_refresh_s=0.1,
        work_root=tmp_path / "jump-soak",
        progress_path=tmp_path / "jump-soak" / "progress.json",
    )

    assert report["result"] == "pass"
    assert report["clock_source"] == "time.monotonic"
    assert 2.0 <= report["elapsed_s"] < 10.0
    assert report["wall_elapsed_s"] - report["elapsed_s"] > 3500.0
    assert (
        audit_soak_report(
            report,
            require_duration_s=2.0,
            min_sessions=3,
        )["ok"]
        is True
    )


def test_soak_artifact_auditor_scans_transit_storage_logs_and_parts(tmp_path) -> None:
    for name in ("relay", "relay-net", "registry"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "clean.bin").write_bytes(b"encrypted-control-data")
    (tmp_path / "stdout.log").write_text("worker started\n", encoding="utf-8")
    (tmp_path / "stderr.log").write_bytes(b"")
    (tmp_path / "target-inbox" / ".tmp").mkdir(parents=True)

    audit = audit_module._audit_soak_artifacts(tmp_path)
    assert audit["artifact_files_scanned"] == 5
    assert audit["artifact_partial_files"] == 0
    assert audit["artifact_resume_checkpoints"] == 0
    assert audit["artifact_open_work_order_markers"] == 0
    assert audit["stderr_bytes"] == 0

    (tmp_path / "relay" / "leak.bin").write_bytes(
        b"prefix-" + audit_module.SOAK_PLAINTEXT_MARKER + b"-suffix"
    )
    with pytest.raises(AuditError, match="plaintext marker"):
        audit_module._audit_soak_artifacts(tmp_path)
    (tmp_path / "relay" / "leak.bin").unlink()

    (tmp_path / "target-inbox" / ".tmp" / "orphan.part").write_bytes(b"partial")
    with pytest.raises(AuditError, match="partial"):
        audit_module._audit_soak_artifacts(tmp_path)
    (tmp_path / "target-inbox" / ".tmp" / "orphan.part").unlink()

    (tmp_path / "target-inbox" / ".tmp" / "orphan.resume.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    with pytest.raises(AuditError, match="resume checkpoint"):
        audit_module._audit_soak_artifacts(tmp_path)
    (tmp_path / "target-inbox" / ".tmp" / "orphan.resume.json").unlink()

    open_markers = tmp_path / "registry" / "open-work-orders" / "provider"
    open_markers.mkdir(parents=True)
    (open_markers / "orphan.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(AuditError, match="open work-order marker"):
        audit_module._audit_soak_artifacts(tmp_path)


def test_soak_artifact_auditor_requires_target_directory_and_logs(tmp_path) -> None:
    missing_target = tmp_path / "missing-target"
    for name in ("relay", "relay-net", "registry"):
        (missing_target / name).mkdir(parents=True)
    (missing_target / "stdout.log").write_bytes(b"")
    (missing_target / "stderr.log").write_bytes(b"")
    with pytest.raises(AuditError, match="directories are incomplete"):
        audit_module._audit_soak_artifacts(missing_target)

    missing_logs = tmp_path / "missing-logs"
    for name in ("relay", "relay-net", "registry", "target-inbox"):
        (missing_logs / name).mkdir(parents=True)
    with pytest.raises(AuditError, match="logs are incomplete"):
        audit_module._audit_soak_artifacts(missing_logs)


def test_final_soak_audit_binds_progress_hash_and_absent_processes(
    tmp_path,
    monkeypatch,
) -> None:
    progress = tmp_path / "progress.json"
    progress.write_text(
        '{"pid": 12345, "result": "pass", "updated_at": "2026-09-02T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    expected_hash = hashlib.sha256(progress.read_bytes()).hexdigest()
    waited_pids: list[tuple[int, float]] = []
    rechecked_pids: list[tuple[int, float]] = []

    def wait_for_process_exit(
        pid: int,
        timeout_s: float,
        *,
        progress_updated_epoch: float,
    ) -> bool:
        waited_pids.append((pid, timeout_s))
        return True

    def original_process_alive(pid: int, *, progress_updated_epoch: float) -> bool:
        rechecked_pids.append((pid, progress_updated_epoch))
        return False

    monkeypatch.setattr(finalize_soak_module, "_wait_for_process_exit", wait_for_process_exit)
    monkeypatch.setattr(finalize_soak_module, "_original_process_alive", original_process_alive)
    monkeypatch.setattr(
        finalize_soak_module,
        "audit_soak_report",
        lambda value, **kwargs: {
            "ok": value["result"] == "pass",
            "artifact_root": str(kwargs["artifact_root"]),
        },
    )

    report = finalize_soak_module.finalize_soak(
        progress,
        require_duration_s=86400,
        min_sessions=100,
        launcher_pid=54321,
    )

    assert waited_pids == [(12345, 5.0), (54321, 5.0)]
    assert [pid for pid, _updated in rechecked_pids] == [12345, 54321]
    assert report["progress_sha256"] == expected_hash
    assert report["worker_process_alive"] is False
    assert report["launcher_process_alive"] is False
    assert report["owned_udp_endpoints"] == 0
    assert report["udp_endpoint_proof"] == (
        "original_owner_process_absent_or_pid_reused_after_report"
    )
    assert report["soak_audit"]["ok"] is True


@pytest.mark.parametrize(
    ("live_pid", "message"),
    ((12345, "worker process"), (54321, "launcher process")),
)
def test_final_soak_audit_rejects_live_process(
    tmp_path,
    monkeypatch,
    live_pid,
    message,
) -> None:
    progress = tmp_path / "progress.json"
    progress.write_text(
        '{"pid": 12345, "result": "pass", "updated_at": "2026-09-02T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        finalize_soak_module,
        "_wait_for_process_exit",
        lambda pid, timeout_s, **kwargs: pid != live_pid,
    )

    with pytest.raises(AuditError, match=message):
        finalize_soak_module.finalize_soak(
            progress,
            require_duration_s=86400,
            min_sessions=100,
            launcher_pid=54321,
        )


def test_final_soak_audit_distinguishes_reused_pid(monkeypatch) -> None:
    monkeypatch.setattr(finalize_soak_module, "_process_exists", lambda pid: True)
    monkeypatch.setattr(
        finalize_soak_module,
        "_process_started_at_epoch",
        lambda pid: 201.0,
    )

    assert (
        finalize_soak_module._original_process_alive(
            12345,
            progress_updated_epoch=200.0,
        )
        is False
    )

    monkeypatch.setattr(
        finalize_soak_module,
        "_process_started_at_epoch",
        lambda pid: 199.0,
    )
    assert (
        finalize_soak_module._original_process_alive(
            12345,
            progress_updated_epoch=200.0,
        )
        is True
    )


def test_final_soak_audit_requires_new_output_path(tmp_path) -> None:
    progress = tmp_path / "progress.json"
    progress.write_text("{}\n", encoding="utf-8")

    with pytest.raises(AuditError, match="must not overwrite"):
        finalize_soak_module._new_output_path(progress, str(progress))

    existing = tmp_path / "final-audit.json"
    existing.write_text("immutable\n", encoding="utf-8")
    with pytest.raises(AuditError, match="already exists"):
        finalize_soak_module._new_output_path(progress, str(existing))
    assert existing.read_text(encoding="utf-8") == "immutable\n"

    fresh = tmp_path / "new-final-audit.json"
    assert finalize_soak_module._new_output_path(progress, str(fresh)) == fresh.resolve()


def test_final_soak_audit_publish_is_exclusive_and_atomic(tmp_path) -> None:
    output = tmp_path / "final-audit.json"
    first = {"ok": True, "revision": 1}
    finalize_soak_module._write_json_exclusive_atomic(output, first)
    first_bytes = output.read_bytes()
    assert json.loads(first_bytes) == first

    with pytest.raises(AuditError, match="already exists"):
        finalize_soak_module._write_json_exclusive_atomic(
            output,
            {"ok": True, "revision": 2},
        )

    assert output.read_bytes() == first_bytes
    assert list(tmp_path.glob(".final-audit.json.*.tmp")) == []


def test_route_acceptance_auditor_enforces_timing_metrics_and_no_flap() -> None:
    route = _route_acceptance()
    audit_module._audit_route_report(route)

    cases = [
        ("thirty", lambda item: item["events"][1].update(at=31.1)),
        ("finite", lambda item: item["events"][1].update(at=float("nan"))),
        ("flap", lambda item: item["events"].append(copy.deepcopy(item["events"][-1]))),
        ("probes", lambda item: item.update(recovery_probe_times=[92, 150, 180, 212])),
        ("metrics", lambda item: item["degraded_direct_metrics"].update(loss_ratio=0.14)),
        ("metrics", lambda item: item["degraded_direct_metrics"].update(jitter_ms=49)),
    ]
    for message, mutate in cases:
        tampered = copy.deepcopy(route)
        mutate(tampered)
        with pytest.raises(AuditError, match=message):
            audit_module._audit_route_report(tampered)


def test_datagram_impairment_is_deterministic_and_within_contract() -> None:
    profile = acceptance_module._DatagramImpairment()
    for _ in range(200):
        profile.plan()
    snapshot = profile.snapshot()

    assert snapshot["transport"] == "real_local_ice_udp_application_datagrams"
    assert snapshot["attempted_datagrams"] == 200
    assert snapshot["dropped_datagrams"] == 36
    assert snapshot["delivered_datagrams"] == 164
    assert snapshot["observed_loss_ratio"] == pytest.approx(0.18)
    assert 250 <= snapshot["scheduled_rtt_min_ms"]
    assert snapshot["scheduled_rtt_max_ms"] <= 350
    assert snapshot["scheduled_rtt_max_ms"] - snapshot["scheduled_rtt_min_ms"] >= 50


def test_degraded_network_auditor_recomputes_loss_and_binds_real_paths(monkeypatch) -> None:
    identities = {
        "source_peer_id": "source",
        "transit_peer_id": "transit",
        "target_peer_id": "target",
        "source_sha256": "sha256:abc",
    }
    monkeypatch.setattr(
        audit_module,
        "audit_direct_file",
        lambda _item: {
            "source_peer_id": "source",
            "target_peer_id": "target",
            "source_sha256": "sha256:abc",
        },
    )
    monkeypatch.setattr(audit_module, "audit_peer_transit", lambda _item: identities)
    evidence = {
        "ok": True,
        "transfer_timeout_s": 120.0,
        "route_degraded_path": "peer_transit",
        "impairment": {
            "transport": "real_local_ice_udp_application_datagrams",
            "configured_rtt_min_ms": 250.0,
            "configured_rtt_max_ms": 350.0,
            "configured_jitter_ms": 75.0,
            "configured_loss_ratio": 0.18,
            "attempted_datagrams": 200,
            "dropped_datagrams": 36,
            "delivered_datagrams": 164,
            "observed_loss_ratio": 0.18,
            "scheduled_rtt_min_ms": 250.0,
            "scheduled_rtt_max_ms": 350.0,
        },
        "direct_under_impairment": {
            "ok": True,
            "elapsed_s": 8.0,
            "transit_bytes_before": 100,
            "transit_bytes_after": 100,
            "committed_target_files": 1,
            "partial_target_files": 0,
            "source_sha256": "sha256:abc",
            "target_sha256": "sha256:abc",
            "evidence": {},
        },
        "adaptive_after_degrade": {
            "ok": True,
            "elapsed_s": 1.0,
            "transit_bytes_before": 100,
            "transit_bytes_after": 200,
            "committed_target_files": 1,
            "partial_target_files": 0,
            "source_sha256": "sha256:abc",
            "target_sha256": "sha256:abc",
            "route_events": [
                {"reason": "direct_degraded"},
                {"reason": "transit_better"},
            ],
            "evidence": {
                "path_mode": "peer_transit",
                "selected_path": "peer_transit",
                "direct_fallback_error": "",
            },
        },
    }
    result = audit_module._audit_degraded_network_gate(
        evidence,
        transit_audit=identities,
    )
    assert result["observed_loss_ratio"] == pytest.approx(0.18)

    mutations = [
        ("impairment", lambda item: item["impairment"].update(dropped_datagrams=0)),
        ("intact", lambda item: item["direct_under_impairment"].update(committed_target_files=2)),
        ("transit", lambda item: item["adaptive_after_degrade"].update(transit_bytes_after=100)),
    ]
    for message, mutate in mutations:
        tampered = copy.deepcopy(evidence)
        mutate(tampered)
        with pytest.raises(AuditError, match=message):
            audit_module._audit_degraded_network_gate(
                tampered,
                transit_audit=identities,
            )


def test_post_recovery_direct_file_auditor_binds_route_and_counter_stop(monkeypatch) -> None:
    identities = {
        "source_peer_id": "source",
        "target_peer_id": "target",
        "source_sha256": "sha256:abc",
    }
    monkeypatch.setattr(audit_module, "audit_direct_file", lambda _item: identities)
    hop = {
        "transport": "ice_udp_direct",
        "relay_used": False,
        "local": {"transport": "udp", "type": "host"},
        "remote": {"transport": "udp", "type": "host"},
    }
    evidence = {
        "ok": True,
        "route_recovered_path": "direct",
        "source_sha256": "sha256:abc",
        "target_sha256": "sha256:abc",
        "transit_bytes_before": 1234,
        "transit_bytes_after": 1234,
        "source_hop": hop,
        "evidence": {},
    }
    assert (
        audit_module._audit_post_recovery_direct_file(
            evidence,
            transit_audit=identities,
        )
        == identities
    )

    with pytest.raises(AuditError, match="post-recovery"):
        audit_module._audit_post_recovery_direct_file(
            {**evidence, "route_recovered_path": "peer_transit"},
            transit_audit=identities,
        )
    with pytest.raises(AuditError, match="carried bytes"):
        audit_module._audit_post_recovery_direct_file(
            {**evidence, "transit_bytes_after": 1235},
            transit_audit=identities,
        )


def test_acceptance_memory_auditor_recomputes_numeric_gate() -> None:
    limit = 128 * 1024 * 1024
    performance = {
        "memory_bounded": True,
        "peak_python_memory_bytes": 5_500_000,
        "peak_python_memory_limit_bytes": limit,
    }
    assert audit_module._audit_memory_gate(performance) == (5_500_000, limit)

    forged = {**performance, "peak_python_memory_bytes": limit + 1}
    with pytest.raises(AuditError, match="memory"):
        audit_module._audit_memory_gate(forged)


def test_concurrent_auditor_requires_unique_signed_session_evidence(monkeypatch) -> None:
    identities = {
        "source_peer_id": "source",
        "transit_peer_id": "transit",
        "target_peer_id": "target",
        "source_size_bytes": 1024 * 1024,
    }
    monkeypatch.setattr(audit_module, "audit_peer_transit", lambda _item: identities)
    performance = {
        "concurrency_ok": True,
        "concurrent_sessions": 3,
        "concurrent_payload_bytes": 1024 * 1024,
        "concurrent_completed": 3,
    }
    evidence = [{"session_id": value} for value in ("one", "two", "three")]
    assert audit_module._audit_concurrent_sessions(
        performance,
        evidence,
        transit_audit=identities,
        min_concurrent=3,
    ) == {"one", "two", "three"}

    duplicate = [evidence[0], evidence[0], evidence[2]]
    with pytest.raises(AuditError, match="duplicated"):
        audit_module._audit_concurrent_sessions(
            performance,
            duplicate,
            transit_audit=identities,
            min_concurrent=3,
        )

    with pytest.raises(AuditError, match="gate"):
        audit_module._audit_concurrent_sessions(
            {**performance, "concurrent_payload_bytes": 64 * 1024},
            evidence,
            transit_audit=identities,
            min_concurrent=3,
        )


def test_concurrent_timeline_auditor_requires_actual_overlap() -> None:
    performance = {
        "concurrent_elapsed_s": 1.1,
        "peak_concurrent_observed": 3,
    }
    timeline = [
        {"session_id": "one", "started_s": 0.0, "ended_s": 1.0},
        {"session_id": "two", "started_s": 0.1, "ended_s": 0.9},
        {"session_id": "three", "started_s": 0.2, "ended_s": 0.8},
    ]
    assert (
        audit_module._audit_concurrent_timeline(
            performance,
            timeline,
            expected_session_ids={"one", "two", "three"},
            min_concurrent=3,
        )
        == 3
    )

    sequential = [
        {"session_id": "one", "started_s": 0.0, "ended_s": 0.2},
        {"session_id": "two", "started_s": 0.3, "ended_s": 0.5},
        {"session_id": "three", "started_s": 0.6, "ended_s": 0.8},
    ]
    with pytest.raises(AuditError, match="overlap"):
        audit_module._audit_concurrent_timeline(
            performance,
            sequential,
            expected_session_ids={"one", "two", "three"},
            min_concurrent=3,
        )


def test_worker_trace_waits_for_returned_handler_callbacks() -> None:
    trace_lock = threading.Lock()
    trace = {
        "relay": {"session": {"started": 1.0}},
        "target": {"session": {"started": 1.0, "finished": 2.0}},
    }

    def finish_relay() -> None:
        time.sleep(0.02)
        with trace_lock:
            trace["relay"]["session"]["finished"] = 2.0

    thread = threading.Thread(target=finish_relay)
    thread.start()
    assert _wait_for_worker_trace(trace, trace_lock, {"session"}, timeout_s=1.0) is True
    thread.join(timeout=1)

    with trace_lock:
        del trace["relay"]["session"]["finished"]
    assert _wait_for_worker_trace(trace, trace_lock, {"session"}, timeout_s=0.01) is False


def test_acceptance_overhead_auditor_recomputes_ratio_from_byte_counts() -> None:
    performance = {
        "plaintext_request_bytes": 1_000_000,
        "encrypted_request_bytes": 1_010_000,
        "protocol_overhead_ratio": 0.01,
        "protocol_overhead_within_15_percent": True,
    }
    assert audit_module._audit_overhead_gate(performance) == pytest.approx(0.01)

    forged = {
        **performance,
        "encrypted_request_bytes": 1_200_000,
        "protocol_overhead_ratio": 0.01,
    }
    with pytest.raises(AuditError, match="overhead"):
        audit_module._audit_overhead_gate(forged)

    with pytest.raises(AuditError, match="finite"):
        audit_module._audit_overhead_gate({**performance, "protocol_overhead_ratio": float("nan")})


def test_unavailable_auditor_requires_bounded_atomic_failure() -> None:
    unavailable = {
        "ok": True,
        "elapsed_s": 0.55,
        "operation_timeout_s": 0.5,
        "maximum_elapsed_s": 2.0,
        "error": "timed out waiting for transit result",
        "relay_worker_started": True,
        "relay_worker_stopped_before_request": True,
        "advertised_capacity_remained": True,
        "committed_target_files": 0,
        "partial_target_files": 0,
    }
    audit_module._audit_unavailable_gate(unavailable)

    for key, value, message in (
        ("elapsed_s", 2.01, "bounded"),
        ("elapsed_s", float("nan"), "finite"),
        ("maximum_elapsed_s", float("inf"), "finite"),
        ("committed_target_files", 1, "bounded"),
        ("partial_target_files", 1, "bounded"),
    ):
        tampered = {**unavailable, key: value}
        with pytest.raises(AuditError, match=message):
            audit_module._audit_unavailable_gate(tampered)


def test_unavailable_acceptance_terminates_advertised_relay(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    unavailable = _relay_unavailable_acceptance(tmp_path)

    assert unavailable["ok"] is True
    assert unavailable["relay_worker_started"] is True
    assert unavailable["relay_worker_stopped_before_request"] is True
    assert unavailable["advertised_capacity_remained"] is True
    assert unavailable["committed_target_files"] == 0
    assert unavailable["partial_target_files"] == 0
    audit_module._audit_unavailable_gate(unavailable)


def test_registry_control_plane_auditor_recomputes_record_sizes() -> None:
    control = {
        "record_count": 3,
        "record_sizes_bytes": [1024, 2048, 4096],
        "max_record_bytes": 4096,
        "total_record_bytes": 7168,
        "maximum_record_bytes": 64 * 1024,
        "application_payload_bytes": 0,
        "plaintext_marker_found": False,
    }
    audit_module._audit_registry_control_plane(control)

    for key, value in (
        ("record_count", 2),
        ("max_record_bytes", 2048),
        ("total_record_bytes", 7167),
        ("maximum_record_bytes", 128 * 1024),
        ("application_payload_bytes", 1),
        ("plaintext_marker_found", True),
    ):
        with pytest.raises(AuditError, match="control-plane"):
            audit_module._audit_registry_control_plane({**control, key: value})


def test_signed_session_open_binds_source_target_expiry_and_one_hop(tmp_path) -> None:
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "network")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "network")
    ephemeral = X25519PrivateKey.generate()
    session = TransitSessionOpen(
        session_id=new_session_id(),
        source_peer_id=source.peer_id,
        target_peer_id=target.peer_id,
        source_ephemeral_pub=messaging_public_key(ephemeral),
        expires_at=_future(),
    )
    signed = sign_session_open(session, source_signing_key=source.private_key_bytes)
    assert verify_session_open(signed, expected_target_peer_id=target.peer_id) == session

    forged = signed.to_dict()
    forged["payload"]["target_peer_id"] = source.peer_id
    with pytest.raises(PeerTransitError, match="invalid signed"):
        verify_session_open(forged, expected_target_peer_id=target.peer_id)

    expired = TransitSessionOpen(**{**session.to_dict(), "expires_at": _future(-1)})
    with pytest.raises(PeerTransitError, match="expired"):
        verify_session_open(sign_session_open(expired, source_signing_key=source.private_key_bytes))

    recursive = TransitSessionOpen(**{**session.to_dict(), "hop_limit": 2})
    with pytest.raises(PeerTransitError, match="hop_limit=1"):
        verify_session_open(
            sign_session_open(recursive, source_signing_key=source.private_key_bytes)
        )


def test_transit_cipher_is_end_to_end_authenticated_and_replay_safe(tmp_path) -> None:
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "network")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "network")
    target_key = X25519PrivateKey.generate()
    session_id = new_session_id()
    source_cipher, ephemeral = TransitCipher.for_source(
        session_id=session_id,
        source_peer_id=source.peer_id,
        target_peer_id=target.peer_id,
        target_messaging_pub=messaging_public_key(target_key),
    )
    session = TransitSessionOpen(
        session_id=session_id,
        source_peer_id=source.peer_id,
        target_peer_id=target.peer_id,
        source_ephemeral_pub=messaging_public_key(ephemeral),
        expires_at=_future(),
    )
    target_cipher = TransitCipher.for_target(
        session=session,
        target_messaging_key=target_key,
    )

    marker = b"RYNMESH-TRANSIT-PLAINTEXT-CHECK-2026"
    frame = source_cipher.seal("request", marker, final=True)
    assert marker not in frame

    tampered = bytearray(frame)
    tampered[-1] ^= 1
    with pytest.raises(PeerTransitError, match="authentication failed"):
        target_cipher.open("request", bytes(tampered))

    plaintext, header = target_cipher.open("request", frame)
    assert plaintext == marker
    assert header.final is True
    with pytest.raises(PeerTransitError, match="replayed"):
        target_cipher.open("request", frame)


def test_route_manager_uses_hysteresis_for_degrade_transit_and_recovery() -> None:
    manager = RouteManager(
        RoutePolicy(
            degraded_hold_s=30,
            transit_min_hold_s=60,
            recovery_hold_s=120,
            recovery_probe_count=5,
        )
    )
    healthy = PathMetrics(reachable=True, rtt_p95_ms=40, loss_ratio=0)
    poor = PathMetrics(reachable=True, rtt_p95_ms=320, loss_ratio=0.18)
    transit = PathMetrics(reachable=True, rtt_p95_ms=80, loss_ratio=0.01)

    assert manager.update(direct=healthy, transit=transit, now_monotonic=0) == "direct"
    assert manager.update(direct=poor, transit=transit, now_monotonic=1) == "direct"
    assert manager.state == RouteState.DEGRADED
    assert manager.update(direct=poor, transit=transit, now_monotonic=31) == "peer_transit"
    assert manager.state == RouteState.PEER_TRANSIT

    # Minimum hold prevents an immediate return and recovery itself has a
    # second stability window.
    assert manager.update(direct=healthy, transit=transit, now_monotonic=60) == "peer_transit"
    assert manager.update(direct=healthy, transit=transit, now_monotonic=92) == "peer_transit"
    assert manager.state == RouteState.RECOVERING
    manager.update(direct=healthy, transit=transit, now_monotonic=150)
    manager.update(direct=healthy, transit=transit, now_monotonic=180)
    manager.update(direct=healthy, transit=transit, now_monotonic=211)
    assert manager.update(direct=healthy, transit=transit, now_monotonic=212) == "direct"
    assert manager.state == RouteState.DIRECT


def test_route_manager_hard_failure_switches_without_quality_hold() -> None:
    manager = RouteManager()
    failed = PathMetrics(
        reachable=False,
        rtt_p95_ms=0,
        loss_ratio=1,
        consecutive_failures=3,
    )
    transit = PathMetrics(reachable=True, rtt_p95_ms=90, loss_ratio=0.01)
    assert manager.update(direct=failed, transit=transit, now_monotonic=10) == "peer_transit"
    assert manager.events[-1]["reason"] == "hard_failure"


def test_route_policy_loads_validated_operator_thresholds(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_TRANSIT_LATENCY_THRESHOLD_MS", "180")
    monkeypatch.setenv("RYNMESH_TRANSIT_LOSS_THRESHOLD", "0.12")
    monkeypatch.setenv("RYNMESH_TRANSIT_DEGRADED_HOLD_S", "15")
    manager = RouteManager()
    assert manager.policy.latency_threshold_ms == 180
    assert manager.policy.loss_threshold == 0.12
    assert manager.policy.degraded_hold_s == 15

    monkeypatch.setenv("RYNMESH_TRANSIT_LOSS_THRESHOLD", "1.5")
    with pytest.raises(PeerTransitError, match="ratios"):
        RouteManager()


def test_adaptive_sender_retries_via_peer_after_hard_direct_failure(monkeypatch) -> None:
    from rynmesh import peer_transit_service as service

    calls: list[str] = []
    direct_timeouts: list[float] = []

    def fail_direct(*args, **kwargs):
        calls.append("direct")
        direct_timeouts.append(kwargs["timeout_s"])
        raise PeerTransitError("direct ICE failed")

    def pass_transit(*args, **kwargs):
        calls.append("peer_transit")
        return {"path_mode": "peer_transit", "result": "pass"}

    monkeypatch.setattr(service, "send_file_direct", fail_direct)
    monkeypatch.setattr(service, "send_file_via_peer", pass_transit)
    result = service.send_file_adaptive(
        object(),
        "unused.bin",
        target_peer_id="target",
        relay_peer_id="relay",
    )
    assert calls == ["direct", "peer_transit"]
    assert direct_timeouts == [8.0]
    assert result["direct_attempt_timeout_s"] == 8.0
    assert result["selected_path"] == "peer_transit"
    assert "direct ICE failed" in result["direct_fallback_error"]
    assert result["route_events"][-1]["reason"] == "hard_failure"

    with pytest.raises(PeerTransitError, match="within"):
        service.send_file_adaptive(
            object(),
            "unused.bin",
            target_peer_id="target",
            relay_peer_id="relay",
            direct_attempt_timeout_s=11,
        )


def test_two_direct_ice_legs_forward_only_ciphertext_without_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_PUBLIC", raising=False)

    source_store = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "network")
    transit_store = RynmeshStore(home=tmp_path / "transit", network_dir=tmp_path / "network")
    target_store = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "network")
    target_key = X25519PrivateKey.generate()
    marker = b"RYNMESH-TRANSIT-PLAINTEXT-CHECK-2026"
    body = marker + (b"-opaque-payload" * 32768)

    async def scenario() -> dict:
        source_connection = new_connection(controlling=True)
        transit_left = new_connection(controlling=False)
        transit_right = new_connection(controlling=True)
        target_connection = new_connection(controlling=False)
        connections = [source_connection, transit_left, transit_right, target_connection]
        try:
            signals = await asyncio.gather(*(gather_signal(item) for item in connections))
            await asyncio.gather(
                apply_remote_signal(source_connection, signals[1]),
                apply_remote_signal(transit_left, signals[0]),
                apply_remote_signal(transit_right, signals[3]),
                apply_remote_signal(target_connection, signals[2]),
            )
            await asyncio.gather(*(item.connect() for item in connections))

            session_id = new_session_id()
            source_cipher, ephemeral = TransitCipher.for_source(
                session_id=session_id,
                source_peer_id=source_store.peer_id,
                target_peer_id=target_store.peer_id,
                target_messaging_pub=messaging_public_key(target_key),
            )
            session = TransitSessionOpen(
                session_id=session_id,
                source_peer_id=source_store.peer_id,
                target_peer_id=target_store.peer_id,
                source_ephemeral_pub=messaging_public_key(ephemeral),
                expires_at=_future(),
            )
            signed = sign_session_open(
                session,
                source_signing_key=source_store.private_key_bytes,
            )
            verified = verify_session_open(
                SignedPayload.from_dict(signed.to_dict()),
                expected_target_peer_id=target_store.peer_id,
            )
            target_cipher = TransitCipher.for_target(
                session=verified,
                target_messaging_key=target_key,
            )

            captured_frames: list[bytes] = []
            target_received = bytearray()
            source_response = bytearray()

            async def target_side() -> dict:
                received = await receive_encrypted_stream(
                    target_connection,
                    target_cipher,
                    direction="request",
                    sink=target_received.extend,
                    timeout_s=10,
                )
                response = json_bytes({"received_sha256": received["sha256"]})
                await send_encrypted_stream(
                    target_connection,
                    target_cipher,
                    direction="response",
                    chunks=[response],
                    timeout_s=10,
                )
                return received

            relay_task = asyncio.create_task(
                relay_bidirectional_once(
                    transit_left,
                    transit_right,
                    session_id=session_id,
                    timeout_s=10,
                    audit_frame=captured_frames.append,
                )
            )
            target_task = asyncio.create_task(target_side())
            source_sent = await send_encrypted_stream(
                source_connection,
                source_cipher,
                direction="request",
                chunks=(
                    body[index : index + 128 * 1024] for index in range(0, len(body), 128 * 1024)
                ),
                timeout_s=10,
            )
            await receive_encrypted_stream(
                source_connection,
                source_cipher,
                direction="response",
                sink=source_response.extend,
                timeout_s=10,
            )
            counters, target_received_evidence = await asyncio.gather(relay_task, target_task)

            assert bytes(target_received) == body
            assert marker not in b"".join(captured_frames)
            response_payload = __import__("json").loads(source_response)
            assert response_payload["received_sha256"] == source_sent["sha256"]
            assert target_received_evidence["sha256"] == source_sent["sha256"]

            return transit_evidence(
                source_peer_id=source_store.peer_id,
                transit_peer_id=transit_store.peer_id,
                target_peer_id=target_store.peer_id,
                source_connection=source_connection,
                target_connection=transit_right,
                counters=counters,
                source_sha256=source_sent["sha256"],
                target_sha256=target_received_evidence["sha256"],
                plaintext_found_on_transit=False,
            )
        finally:
            await asyncio.gather(*(item.close() for item in connections))

    evidence = asyncio.run(scenario())
    assert evidence["result"] == "pass"
    assert evidence["path_mode"] == "peer_transit"
    assert evidence["ice_relay_candidate_used"] is False
    assert evidence["transit_rx_bytes"] > len(body)
    assert evidence["transit_tx_bytes"] > len(body)
    assert evidence["source_sha256"] == "sha256:" + hashlib.sha256(body).hexdigest()


def json_bytes(value: dict) -> bytes:
    import json

    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def test_registry_signaling_carries_no_file_body_and_workers_stream_via_peer(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_PUBLIC", raising=False)
    network_id = "three-node-peer-transit"
    registry = FilePeerRegistry(tmp_path / "registry")
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "source-net")
    relay = RynmeshStore(home=tmp_path / "relay", network_dir=tmp_path / "relay-net")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "target-net")
    source.registry = registry
    relay.registry = registry
    target.registry = registry

    marker = b"RYNMESH-TRANSIT-PLAINTEXT-CHECK-2026"
    source_file = tmp_path / "large-secret.bin"
    source_file.write_bytes(marker + (b"0123456789abcdef" * 131072))
    captured: list[bytes] = []
    relay_worker = PeerTransitWorker(
        relay,
        role="transit",
        network_id=network_id,
        timeout_s=20,
        audit_frame=captured.append,
    )
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=tmp_path / "target-inbox",
        timeout_s=20,
    )
    relay_worker.register()
    target_worker.register()
    adaptive_manager = RouteManager(RoutePolicy(degraded_hold_s=30))
    poor = PathMetrics(True, 340, 0.18)
    good_transit = PathMetrics(True, 70, 0.01)
    adaptive_manager.update(direct=poor, transit=good_transit, now_monotonic=0)
    adaptive_manager.update(direct=poor, transit=good_transit, now_monotonic=31)

    def wait_for_order(store: RynmeshStore, operation: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            orders = store.poll_work_orders(
                network_id=network_id,
                capability="rynmesh.peer-transit.v1",
            )["work_orders"]
            if any(item["operation"] == operation for item in orders):
                return
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {operation}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        source_future = pool.submit(
            send_file_adaptive,
            source,
            source_file,
            relay_peer_id=relay.peer_id,
            target_peer_id=target.peer_id,
            direct_metrics=poor,
            transit_metrics=good_transit,
            route_manager=adaptive_manager,
            network_id=network_id,
            timeout_s=20,
        )
        wait_for_order(relay, "open_peer_transit")
        relay_future = pool.submit(relay_worker.serve_once)
        wait_for_order(target, "accept_peer_transit")
        target_future = pool.submit(target_worker.serve_once)
        evidence = source_future.result(timeout=30)
        assert relay_future.result(timeout=10) == 1
        assert target_future.result(timeout=10) == 1

    assert evidence["result"] == "pass"
    assert evidence["ice_relay_candidate_used"] is False
    assert evidence["source_sha256"] == evidence["target_sha256"]
    assert evidence["transit_rx_bytes"] > source_file.stat().st_size
    assert evidence["transit_tx_bytes"] > source_file.stat().st_size
    assert evidence["selected_path"] == "peer_transit"
    assert any(event["reason"] == "transit_better" for event in evidence["route_events"])
    assert marker not in b"".join(captured)
    assert audit_peer_transit(evidence)["ok"] is True

    tampered = __import__("copy").deepcopy(evidence)
    tampered["ice_relay_candidate_used"] = True
    with pytest.raises(AuditError, match="TURN"):
        audit_peer_transit(tampered)

    tampered = __import__("copy").deepcopy(evidence)
    tampered["relay_evidence"]["transit_rx_bytes"] += 1
    with pytest.raises(AuditError, match="signed relay"):
        audit_peer_transit(tampered)

    tampered = __import__("copy").deepcopy(evidence)
    tampered["request_frames"] += 1
    with pytest.raises(AuditError, match="frame counters"):
        audit_peer_transit(tampered)

    tampered = __import__("copy").deepcopy(evidence)
    tampered["target_result"]["result_refs"]["sha256"] = "sha256:" + "0" * 64
    with pytest.raises(AuditError, match="signature"):
        audit_peer_transit(tampered)

    delivered = list((tmp_path / "target-inbox").glob("*-large-secret.bin"))
    assert len(delivered) == 1
    assert delivered[0].read_bytes() == source_file.read_bytes()

    # Registry stores signed offers/answers and session metadata only.  The
    # unique body marker must not appear anywhere in its mailbox files.
    for path in (tmp_path / "registry").rglob("*.json"):
        assert marker not in path.read_bytes()


def test_direct_file_path_uses_one_non_turn_ice_pair(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    network_id = "direct-file"
    registry = FilePeerRegistry(tmp_path / "registry")
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "source-net")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "target-net")
    source.registry = registry
    target.registry = registry
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=tmp_path / "inbox",
        timeout_s=10,
    )
    target_worker.register()
    source_file = tmp_path / "direct.bin"
    source_file.write_bytes(b"direct-peer-file" * 8192)

    with ThreadPoolExecutor(max_workers=2) as pool:
        source_future = pool.submit(
            send_file_direct,
            source,
            source_file,
            target_peer_id=target.peer_id,
            network_id=network_id,
            timeout_s=10,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if target.poll_work_orders(
                network_id=network_id,
                capability="rynmesh.peer-transit.v1",
            )["work_orders"]:
                break
            time.sleep(0.01)
        target_future = pool.submit(target_worker.serve_once)
        evidence = source_future.result(timeout=15)
        assert target_future.result(timeout=5) == 1

    assert evidence["path_mode"] == "direct"
    assert evidence["ice_relay_candidate_used"] is False
    assert evidence["source_hop"]["remote"]["type"] != "relay"
    assert evidence["source_sha256"] == evidence["target_sha256"]
    assert audit_direct_file(evidence)["source_size_bytes"] == source_file.stat().st_size


def test_transit_resumes_from_verified_boundary_after_ice_disconnect(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    network_id = "peer-transit-resume"
    registry = FilePeerRegistry(tmp_path / "registry")
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "source-net")
    relay = RynmeshStore(home=tmp_path / "relay", network_dir=tmp_path / "relay-net")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "target-net")
    for store in (source, relay, target):
        store.registry = registry
    inbox = tmp_path / "target-inbox"
    relay_worker = PeerTransitWorker(
        relay,
        role="transit",
        network_id=network_id,
        timeout_s=10,
    )
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=inbox,
        timeout_s=10,
    )
    relay_worker.register()
    target_worker.register()
    source_file = tmp_path / "resume.bin"
    source_file.write_bytes(bytes(range(251)) * 800)

    original_send = peer_transit_service.send_encrypted_stream
    request_calls = 0

    async def disconnect_second_segment(connection, cipher, **kwargs):
        nonlocal request_calls
        if kwargs.get("direction") == "request":
            request_calls += 1
            if request_calls == 2:
                await connection.close()
                raise ConnectionError("injected ICE disconnect after verified boundary")
        return await original_send(connection, cipher, **kwargs)

    monkeypatch.setattr(
        peer_transit_service,
        "send_encrypted_stream",
        disconnect_second_segment,
    )
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=3) as pool:
        relay_future = pool.submit(
            relay_worker.serve_forever,
            poll_interval_s=0.01,
            stop_event=stop,
        )
        target_future = pool.submit(
            target_worker.serve_forever,
            poll_interval_s=0.01,
            stop_event=stop,
        )
        try:
            evidence = peer_transit_service.send_file_via_peer(
                source,
                source_file,
                relay_peer_id=relay.peer_id,
                target_peer_id=target.peer_id,
                network_id=network_id,
                timeout_s=10,
                resume_segment_bytes=64 * 1024,
                max_resume_attempts=2,
            )
        finally:
            stop.set()
            relay_future.result(timeout=10)
            target_future.result(timeout=10)

    assert evidence["result"] == "pass"
    assert evidence["resume_attempts"] == 1
    assert len(evidence["failed_attempts"]) == 1
    failure = evidence["failed_attempts"][0]
    assert failure["segment_index"] == 1
    assert failure["attempt"] == 0
    assert failure["offset_bytes"] == 64 * 1024
    assert failure["error_type"] == "ConnectionError"
    assert failure["error"] == "injected ICE disconnect after verified boundary"
    assert failure["retryable"] is True
    assert failure["session_id"]
    assert evidence["verified_boundaries"] == [
        64 * 1024,
        128 * 1024,
        192 * 1024,
        source_file.stat().st_size,
    ]
    assert len(evidence["session_ids"]) == 4
    assert len(set(evidence["session_ids"])) == 4
    assert evidence["segment_evidence"][1]["attempt"] == 1
    delivered = list(inbox.glob("*-resume.bin"))
    assert len(delivered) == 1
    assert delivered[0].read_bytes() == source_file.read_bytes()
    assert not list(inbox.rglob("*.part"))
    assert not list(inbox.rglob("*.resume.json"))
    assert target_worker._active_resume_transfers == set()
    audited = audit_peer_transit(evidence)
    assert audited["verified_segments"] == 4
    assert audited["resume_attempts"] == 1

    tampered = copy.deepcopy(evidence)
    tampered["segment_evidence"][1]["offset_bytes"] += 1
    with pytest.raises(AuditError, match="boundary"):
        audit_peer_transit(tampered)


def test_direct_resumes_from_verified_boundary_after_ice_disconnect(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    network_id = "direct-resume"
    registry = FilePeerRegistry(tmp_path / "registry")
    source = RynmeshStore(home=tmp_path / "source", network_dir=tmp_path / "source-net")
    target = RynmeshStore(home=tmp_path / "target", network_dir=tmp_path / "target-net")
    source.registry = registry
    target.registry = registry
    inbox = tmp_path / "inbox"
    worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=inbox,
        timeout_s=10,
    )
    worker.register()
    source_file = tmp_path / "direct-resume.bin"
    source_file.write_bytes(bytes(range(239)) * 600)
    original_send = peer_transit_service.send_encrypted_stream
    request_calls = 0

    async def disconnect_second_segment(connection, cipher, **kwargs):
        nonlocal request_calls
        if kwargs.get("direction") == "request":
            request_calls += 1
            if request_calls == 2:
                await connection.close()
                raise ConnectionError("injected direct ICE disconnect")
        return await original_send(connection, cipher, **kwargs)

    monkeypatch.setattr(
        peer_transit_service,
        "send_encrypted_stream",
        disconnect_second_segment,
    )
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker_future = pool.submit(
            worker.serve_forever,
            poll_interval_s=0.01,
            stop_event=stop,
        )
        try:
            evidence = send_file_direct(
                source,
                source_file,
                target_peer_id=target.peer_id,
                network_id=network_id,
                timeout_s=10,
                resume_segment_bytes=64 * 1024,
                max_resume_attempts=2,
            )
        finally:
            stop.set()
            worker_future.result(timeout=10)

    assert evidence["resume_attempts"] == 1
    assert evidence["verified_boundaries"] == [64 * 1024, 128 * 1024, source_file.stat().st_size]
    assert evidence["segment_evidence"][1]["attempt"] == 1
    assert audit_direct_file(evidence)["verified_segments"] == 3
    delivered = list(inbox.glob("*-direct-resume.bin"))
    assert len(delivered) == 1
    assert delivered[0].read_bytes() == source_file.read_bytes()
    assert not list(inbox.rglob("*.part"))
    assert not list(inbox.rglob("*.resume.json"))
    assert worker._active_resume_transfers == set()


def test_target_final_paths_are_namespaced_by_authenticated_source(tmp_path) -> None:
    inbox = tmp_path / "target-inbox"
    transfer_id = "0123456789abcdef0123456789abcdef"
    stored: list[tuple[bytes, dict[str, object]]] = []

    for index, body in enumerate((b"a" * 4096, b"b" * 4096), start=1):
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        sink = peer_transit_service._TargetFileSink(
            inbox,
            session_id=f"session-{index}",
            source_peer_id=f"authenticated-source-{index}",
            max_file_bytes=1024 * 1024,
        )
        manifest = {
            "kind": "file",
            "filename": "same-name.bin",
            "transfer_id": transfer_id,
            "size_bytes": len(body),
            "sha256": digest,
            "offset_bytes": 0,
            "segment_size_bytes": len(body),
            "segment_sha256": digest,
            "prefix_sha256": digest,
            "final": True,
        }
        sink.write(b"M" + json.dumps(manifest).encode("utf-8"))
        sink.write(b"D" + body)
        receipt = sink.finish()
        sink.acknowledge_receipt()
        stored.append((body, receipt))

    paths = [Path(str(receipt["stored_path"])) for _body, receipt in stored]
    assert paths[0] != paths[1]
    assert all(path.parent == inbox for path in paths)
    assert [path.read_bytes() for path in paths] == [body for body, _receipt in stored]
    assert not list(inbox.rglob("*.part"))
    assert not list(inbox.rglob("*.resume.json"))


def test_rejected_overlapping_resume_cannot_delete_active_partial_file(tmp_path):
    body = b"active-transfer" * 1024
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    active = set()

    def claim(key):
        if key in active:
            raise PeerTransitError("transit resume transfer is already active")
        active.add(key)

    def sink():
        return peer_transit_service._TargetFileSink(
            tmp_path / "inbox", session_id=new_session_id(), source_peer_id="same-source",
            max_file_bytes=1024 * 1024, claim_transfer=claim, release_transfer=active.remove,
        )

    manifest = {
        "kind": "file", "filename": "test.bin", "transfer_id": new_session_id(),
        "size_bytes": len(body), "sha256": digest, "offset_bytes": 0,
        "segment_size_bytes": len(body), "segment_sha256": digest,
        "prefix_sha256": digest, "final": True,
    }
    first, second = sink(), sink()
    try:
        first.write(b"M" + json.dumps(manifest).encode())
        first.write(b"D" + body)
        first._handle.flush()
        with pytest.raises(PeerTransitError, match="already active"):
            second.write(b"M" + json.dumps(manifest).encode())
        second.abort()
        assert first._path.read_bytes() == body
        receipt = first.finish()
        first.acknowledge_receipt()
        assert Path(receipt["stored_path"]).read_bytes() == body
        assert active == set()
    finally:
        first.abort()
