#!/usr/bin/env python3
"""Bind a completed soak audit to independently observed process shutdown."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.audit_peer_transit import AuditError, audit_soak_report
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from audit_peer_transit import AuditError, audit_soak_report


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        raise AuditError("process ID must be positive")
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            exit_code = ctypes.c_uint32()
            try:
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    error = ctypes.get_last_error()
                    raise AuditError(
                        f"unable to inspect process {pid} exit state: Windows error {error}"
                    )
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no process owns this PID.
            return False
        if error == 5:  # ERROR_ACCESS_DENIED still proves that the process exists.
            return True
        raise AuditError(f"unable to inspect process {pid}: Windows error {error}")

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_started_at_epoch(pid: int) -> float | None:
    """Return Windows process creation time, or None when unavailable/absent."""

    if os.name != "nt" or pid <= 0:
        return None

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    creation = FileTime()
    exit_time = FileTime()
    kernel_time = FileTime()
    user_time = FileTime()
    try:
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
    finally:
        kernel32.CloseHandle(handle)
    ticks = (creation.high << 32) | creation.low
    return ticks / 10_000_000 - 11_644_473_600


def _original_process_alive(pid: int, *, progress_updated_epoch: float) -> bool:
    if not _process_exists(pid):
        return False
    started_at = _process_started_at_epoch(pid)
    if started_at is not None and started_at > progress_updated_epoch:
        return False
    return True


def _wait_for_process_exit(
    pid: int,
    timeout_s: float,
    *,
    progress_updated_epoch: float,
) -> bool:
    if not math.isfinite(timeout_s) or timeout_s < 0:
        raise AuditError("process shutdown wait must be finite and non-negative")
    deadline = time.monotonic() + timeout_s
    while _original_process_alive(pid, progress_updated_epoch=progress_updated_epoch):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))
    return True


def finalize_soak(
    progress_path: Path,
    *,
    require_duration_s: float,
    min_sessions: int,
    launcher_pid: int | None = None,
    shutdown_wait_s: float = 5.0,
) -> dict[str, Any]:
    progress_path = progress_path.expanduser().resolve()
    progress_bytes = progress_path.read_bytes()
    value = json.loads(progress_bytes)
    if not isinstance(value, dict):
        raise AuditError("soak progress root must be a JSON object")
    try:
        worker_pid = int(value["pid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditError("soak progress is missing a valid worker PID") from exc
    try:
        progress_updated_epoch = datetime.fromisoformat(str(value["updated_at"])).timestamp()
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditError("soak progress is missing a valid updated_at timestamp") from exc

    if not _wait_for_process_exit(
        worker_pid,
        shutdown_wait_s,
        progress_updated_epoch=progress_updated_epoch,
    ):
        raise AuditError("soak worker process is still running")
    if launcher_pid is not None and not _wait_for_process_exit(
        launcher_pid,
        shutdown_wait_s,
        progress_updated_epoch=progress_updated_epoch,
    ):
        raise AuditError("soak launcher process is still running")

    audit = audit_soak_report(
        value,
        require_duration_s=require_duration_s,
        min_sessions=min_sessions,
        artifact_root=progress_path.parent,
    )

    # Check again after the artifact scan so a live/reused PID cannot pass a
    # time-of-check/time-of-use window. A process that does not exist cannot
    # own a UDP endpoint, which is stronger than a best-effort socket listing.
    if _original_process_alive(worker_pid, progress_updated_epoch=progress_updated_epoch):
        raise AuditError("soak worker process appeared during final audit")
    if launcher_pid is not None and _original_process_alive(
        launcher_pid,
        progress_updated_epoch=progress_updated_epoch,
    ):
        raise AuditError("soak launcher process appeared during final audit")

    return {
        "ok": True,
        "kind": "peer_transit_soak_final_audit",
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "progress_path": str(progress_path),
        "progress_sha256": hashlib.sha256(progress_bytes).hexdigest(),
        "worker_pid": worker_pid,
        "worker_process_alive": False,
        "launcher_pid": launcher_pid,
        "launcher_process_alive": False if launcher_pid is not None else None,
        "shutdown_wait_s": shutdown_wait_s,
        "owned_udp_endpoints": 0,
        "udp_endpoint_proof": "original_owner_process_absent_or_pid_reused_after_report",
        "soak_audit": audit,
    }


def _write_json_exclusive_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise AuditError("final audit output already exists") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _new_output_path(progress_path: Path, requested: str) -> Path:
    progress_path = progress_path.expanduser().resolve()
    output = (
        Path(requested).expanduser().resolve()
        if requested
        else progress_path.parent / "final-audit.json"
    )
    if output == progress_path:
        raise AuditError("final audit output must not overwrite soak progress")
    if output.exists():
        raise AuditError("final audit output already exists")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("progress", help="completed soak progress JSON")
    parser.add_argument("--require-duration-seconds", type=float, default=86400.0)
    parser.add_argument("--min-sessions", type=int, default=3)
    parser.add_argument("--launcher-pid", type=int)
    parser.add_argument("--shutdown-wait-seconds", type=float, default=5.0)
    parser.add_argument("--output", default="", help="final shutdown audit JSON")
    args = parser.parse_args()
    if args.require_duration_seconds < 0 or args.min_sessions < 0:
        raise AuditError("final soak audit requirements cannot be negative")

    progress_path = Path(args.progress).expanduser().resolve()
    output = _new_output_path(progress_path, args.output)
    report = finalize_soak(
        progress_path,
        require_duration_s=args.require_duration_seconds,
        min_sessions=args.min_sessions,
        launcher_pid=args.launcher_pid,
        shutdown_wait_s=args.shutdown_wait_seconds,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    _write_json_exclusive_atomic(output, report)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
