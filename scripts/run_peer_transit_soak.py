#!/usr/bin/env python3
"""Run a persistent three-node peer-transit soak and emit live JSON progress."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rynmesh.peer_transit_service import PeerTransitWorker, send_file_via_peer
from rynmesh.registry import FilePeerRegistry
from rynmesh.store import RynmeshStore

try:
    from scripts.audit_peer_transit import audit_peer_transit
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from audit_peer_transit import audit_peer_transit


MARKER = b"RYNMESH-SOAK-PLAINTEXT-MARKER-2026"
MAX_MEMORY_GROWTH_BYTES = 32 * 1024 * 1024


class _FrameAudit:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.frames = 0
        self.bytes = 0
        self.plaintext_found = False

    def __call__(self, frame: bytes) -> None:
        with self._lock:
            self.frames += 1
            self.bytes += len(frame)
            if MARKER in frame:
                self.plaintext_found = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "frames": self.frames,
                "bytes": self.bytes,
                "plaintext_found": self.plaintext_found,
            }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_payload(path: Path, size_bytes: int) -> None:
    block = (MARKER + b"-peer-transit-soak-") * 2048
    remaining = size_bytes
    with path.open("wb") as handle:
        while remaining:
            chunk = block[: min(remaining, len(block))]
            handle.write(chunk)
            remaining -= len(chunk)


def run_soak(
    *,
    duration_s: float,
    interval_s: float,
    payload_bytes: int,
    timeout_s: float,
    capacity_refresh_s: float = 15 * 60.0,
    work_root: Path,
    progress_path: Path,
) -> dict[str, Any]:
    os.environ.setdefault("RYNMESH_P2P_STUN", "off")
    work_root.mkdir(parents=True, exist_ok=True)
    registry = FilePeerRegistry(work_root / "registry")
    source = RynmeshStore(home=work_root / "source", network_dir=work_root / "source-net")
    relay = RynmeshStore(home=work_root / "relay", network_dir=work_root / "relay-net")
    target = RynmeshStore(home=work_root / "target", network_dir=work_root / "target-net")
    for store in (source, relay, target):
        store.registry = registry

    network_id = "peer-transit-soak"
    frame_audit = _FrameAudit()
    stop = threading.Event()
    relay_worker = PeerTransitWorker(
        relay,
        role="transit",
        network_id=network_id,
        timeout_s=timeout_s,
        audit_frame=frame_audit,
        capacity_refresh_s=capacity_refresh_s,
    )
    target_worker = PeerTransitWorker(
        target,
        role="target",
        network_id=network_id,
        inbox=work_root / "target-inbox",
        timeout_s=timeout_s,
        capacity_refresh_s=capacity_refresh_s,
    )
    relay_worker.register()
    target_worker.register()
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

    payload = work_root / "soak-payload.bin"
    _write_payload(payload, payload_bytes)
    started_wall = time.time()
    started_monotonic = time.monotonic()
    deadline_wall = started_wall + duration_s
    deadline_monotonic = started_monotonic + duration_s
    sessions = 0
    failures: list[dict[str, str]] = []
    baseline_memory: int | None = None
    last_evidence: dict[str, Any] | None = None
    baseline_threads = threading.active_count()
    tracemalloc.start()

    def snapshot(status: str) -> dict[str, Any]:
        current_memory, peak_memory = tracemalloc.get_traced_memory()
        memory_growth = 0 if baseline_memory is None else max(0, current_memory - baseline_memory)
        frame_snapshot = frame_audit.snapshot()
        return {
            "result": status,
            "pid": os.getpid(),
            "started_at": datetime.fromtimestamp(started_wall, timezone.utc).isoformat(),
            "updated_at": _utc_now(),
            "deadline_at": datetime.fromtimestamp(deadline_wall, timezone.utc).isoformat(),
            "clock_source": "time.monotonic",
            "duration_target_s": duration_s,
            "elapsed_s": max(0.0, time.monotonic() - started_monotonic),
            "wall_elapsed_s": max(0.0, time.time() - started_wall),
            "sessions_completed": sessions,
            "failures": failures,
            "current_python_memory_bytes": current_memory,
            "peak_python_memory_bytes": peak_memory,
            "baseline_python_memory_bytes": baseline_memory,
            "memory_growth_bytes": memory_growth,
            "memory_growth_limit_bytes": MAX_MEMORY_GROWTH_BYTES,
            "active_threads": threading.active_count(),
            "baseline_threads": baseline_threads,
            "transit_frames": frame_snapshot["frames"],
            "transit_bytes": frame_snapshot["bytes"],
            "plaintext_found_on_transit": frame_snapshot["plaintext_found"],
            "last_session_id": None if last_evidence is None else last_evidence.get("session_id"),
            "worker_control_errors": {
                "relay": relay_worker.control_error_snapshot(),
                "target": target_worker.control_error_snapshot(),
            },
        }

    _write_json_atomic(progress_path, snapshot("running"))
    try:
        while time.monotonic() < deadline_monotonic:
            iteration_started = time.monotonic()
            try:
                evidence = send_file_via_peer(
                    source,
                    payload,
                    relay_peer_id=relay.peer_id,
                    target_peer_id=target.peer_id,
                    network_id=network_id,
                    timeout_s=timeout_s,
                )
                evidence["plaintext_found_on_transit"] = frame_audit.snapshot()["plaintext_found"]
                audit_peer_transit(evidence)
                last_evidence = evidence
                sessions += 1
                Path(str(evidence["receipt"]["stored_path"])).unlink(missing_ok=True)
                if sessions == 3:
                    baseline_memory = tracemalloc.get_traced_memory()[0]
            except Exception as exc:  # noqa: BLE001
                failures.append({"at": _utc_now(), "error": f"{type(exc).__name__}: {exc}"})
                _write_json_atomic(progress_path, snapshot("fail"))
                break
            _write_json_atomic(progress_path, snapshot("running"))
            remaining_interval = interval_s - (time.monotonic() - iteration_started)
            if remaining_interval > 0:
                stop.wait(
                    min(
                        remaining_interval,
                        max(0.0, deadline_monotonic - time.monotonic()),
                    )
                )
    finally:
        stop.set()
        relay_thread.join(timeout=5)
        target_thread.join(timeout=5)

    current_memory, _peak_memory = tracemalloc.get_traced_memory()
    memory_growth = 0 if baseline_memory is None else max(0, current_memory - baseline_memory)
    partial_files = len(list((work_root / "target-inbox" / ".tmp").glob("*.part")))
    completed_duration = time.monotonic() >= deadline_monotonic
    passed = (
        completed_duration
        and sessions >= 3
        and not failures
        and not frame_audit.snapshot()["plaintext_found"]
        and memory_growth <= MAX_MEMORY_GROWTH_BYTES
        and partial_files == 0
        and not relay_thread.is_alive()
        and not target_thread.is_alive()
        and all(
            int(item["count"]) == 0
            for item in (
                relay_worker.control_error_snapshot(),
                target_worker.control_error_snapshot(),
            )
        )
    )
    report = snapshot("pass" if passed else "fail")
    report.update({
        "completed_duration": completed_duration,
        "memory_growth_bytes": memory_growth,
        "partial_files": partial_files,
        "worker_threads_stopped": not relay_thread.is_alive() and not target_thread.is_alive(),
        "last_evidence": last_evidence,
    })
    tracemalloc.stop()
    _write_json_atomic(progress_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-hours", type=float, default=24.0)
    parser.add_argument("--duration-seconds", type=float, default=None)
    parser.add_argument("--interval-seconds", type=float, default=10.0)
    parser.add_argument("--payload-kib", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--capacity-refresh-seconds", type=float, default=15 * 60.0)
    parser.add_argument("--work-root", default="")
    parser.add_argument("--progress", default="")
    args = parser.parse_args()
    duration_s = args.duration_seconds if args.duration_seconds is not None else args.duration_hours * 3600.0
    if (
        duration_s <= 0
        or args.interval_seconds < 0
        or args.payload_kib < 1
        or args.capacity_refresh_seconds <= 0
    ):
        raise SystemExit(
            "duration, payload, and capacity refresh must be positive; interval must be non-negative"
        )
    work_root = Path(args.work_root).expanduser() if args.work_root else Path(tempfile.mkdtemp(prefix="rynmesh-peer-transit-soak-"))
    progress_path = Path(args.progress).expanduser() if args.progress else work_root / "progress.json"
    report = run_soak(
        duration_s=duration_s,
        interval_s=args.interval_seconds,
        payload_bytes=args.payload_kib * 1024,
        timeout_s=args.timeout,
        capacity_refresh_s=args.capacity_refresh_seconds,
        work_root=work_root,
        progress_path=progress_path,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
