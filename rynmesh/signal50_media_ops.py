"""Local m4-mini media-ops daemon for Signal50 browser automation.

This daemon is intentionally local-only. External callers should submit through
Rynmesh; the provider worker then forwards one queued job into this API.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import uuid
from pathlib import Path
from typing import Any

try:
    from fastapi import Request as FastAPIRequest
except ImportError:  # pragma: no cover
    FastAPIRequest = Any  # type: ignore

from .atomic_io import atomic_write_json
from .signal50_service import (
    DEFAULT_WORK_DIR,
    RELAY_BUNDLE_OPERATION,
    _default_relay_url,
    _exclusive_worker_lock,
    _run_relay_bundle_order,
)
from .store import RynmeshStore

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def create_app(
    *,
    store: RynmeshStore | None = None,
    work_dir: str | Path = DEFAULT_WORK_DIR,
    lock_file: str | Path = "",
    relay_url: str = "",
    signal50_repo: str = "",
    job_timeout_sec: float = 14400.0,
):
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Signal50 media ops server requires `fastapi`") from exc

    active_store = store or RynmeshStore()
    state = MediaOpsState(
        store=active_store,
        work_dir=Path(work_dir).expanduser(),
        lock_file=Path(lock_file).expanduser()
        if str(lock_file or "").strip()
        else Path(work_dir).expanduser() / "media-ops.lock",
        relay_url=relay_url or _default_relay_url(),
        signal50_repo=signal50_repo,
        job_timeout_sec=float(job_timeout_sec),
    )
    app = FastAPI(title="Signal50 Media Ops", version="0.1")

    @app.on_event("startup")
    def _startup() -> None:
        state.start()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "kind": "signal50-media-ops",
            "queue_depth": state.queue_depth(),
            "active_job_id": state.active_job_id,
        }

    @app.post("/api/jobs")
    async def submit_job(request: FastAPIRequest) -> dict[str, Any]:
        _require_local_request(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="job_request_not_object")
        try:
            job = state.submit(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "job": job}

    @app.get("/api/jobs/{job_id}")
    def get_job(request: FastAPIRequest, job_id: str) -> dict[str, Any]:
        _require_local_request(request)
        job = state.load(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_not_found")
        return {"ok": True, "job": job}

    @app.get("/api/jobs")
    def list_jobs(
        request: FastAPIRequest,
        status: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        _require_local_request(request)
        return {
            "ok": True,
            "jobs": state.list_jobs(status=status, limit=limit),
        }

    return app


class MediaOpsState:
    def __init__(
        self,
        *,
        store: RynmeshStore,
        work_dir: Path,
        lock_file: Path,
        relay_url: str,
        signal50_repo: str,
        job_timeout_sec: float,
    ) -> None:
        self.store = store
        self.work_dir = work_dir
        self.jobs_dir = self.work_dir / "jobs"
        self.lock_file = lock_file
        self.relay_url = relay_url
        self.signal50_repo = signal50_repo
        self.job_timeout_sec = job_timeout_sec
        self._queue: queue.Queue[str] = queue.Queue()
        self._started = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._file_lock = threading.RLock()
        self.active_job_id = ""

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
            for job in self.list_jobs(status="", limit=1000):
                if str(job.get("status") or "") in {"queued", "running"}:
                    job["status"] = "queued"
                    self._write_job(job)
                    self._queue.put(str(job["job_id"]))
            self._thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._thread.start()
            self._started = True

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        operation = str(body.get("operation") or "").strip()
        if operation != RELAY_BUNDLE_OPERATION:
            raise ValueError(f"unsupported_operation: {operation}")
        params = body.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("params_not_object")
        job_id = "mops_" + uuid.uuid4().hex
        now = _now_iso()
        job = {
            "job_id": job_id,
            "operation": operation,
            "params": params,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "started_at": "",
            "completed_at": "",
            "result": {},
            "error": "",
        }
        self._write_job(job)
        self._queue.put(job_id)
        return job

    def load(self, job_id: str) -> dict[str, Any]:
        path = self._job_path(job_id)
        with self._file_lock:
            if not path.exists():
                return {}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return payload if isinstance(payload, dict) else {}

    def list_jobs(self, *, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        cleaned_status = str(status or "").strip()
        rows: list[dict[str, Any]] = []
        with self._file_lock:
            for path in sorted(self.jobs_dir.glob("*.json"), reverse=True):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                if cleaned_status and payload.get("status") != cleaned_status:
                    continue
                rows.append(payload)
                if len(rows) >= max(1, int(limit or 50)):
                    break
        return rows

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run_job(job_id)
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: str) -> None:
        job = self.load(job_id)
        if not job or str(job.get("status") or "") in TERMINAL_STATUSES:
            return
        params = dict(job.get("params", {}) or {})
        self.active_job_id = job_id
        job.update({"status": "running", "started_at": _now_iso(), "updated_at": _now_iso()})
        self._write_job(job)
        try:
            with _exclusive_worker_lock(self.lock_file):
                result = _run_relay_bundle_order(
                    self.store,
                    order={
                        "work_order_id": job_id,
                        "params": params,
                    },
                    work_dir=self.work_dir / "work-orders",
                    relay_url=str(params.get("relay_url") or self.relay_url),
                    signal50_repo=str(params.get("signal50_repo") or self.signal50_repo),
                    timeout_sec=self.job_timeout_sec,
                )
            job.update(
                {
                    "status": str(result.get("status") or "completed"),
                    "result": result,
                    "completed_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            )
        except Exception as exc:
            job.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "completed_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            )
        finally:
            self._write_job(job)
            self.active_job_id = ""

    def _job_path(self, job_id: str) -> Path:
        cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in job_id)
        return self.jobs_dir / f"{cleaned}.json"

    def _write_job(self, job: dict[str, Any]) -> None:
        with self._file_lock:
            path = self._job_path(str(job["job_id"]))
            atomic_write_json(
                path, job, indent=2, sort_keys=False, ensure_ascii=True, trailing_newline=True
            )


def _require_local_request(request: Any) -> None:
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "")
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        try:
            from fastapi import HTTPException
        except ImportError:  # pragma: no cover
            raise PermissionError("local_requests_only") from None
        raise HTTPException(status_code=403, detail="local_requests_only")


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local Signal50 media-ops queue server.")
    parser.add_argument("--host", default=os.environ.get("SIGNAL50_MEDIA_OPS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SIGNAL50_MEDIA_OPS_PORT", "5055") or 5055))
    parser.add_argument("--work-dir", default=os.environ.get("SIGNAL50_MEDIA_OPS_WORK_DIR", DEFAULT_WORK_DIR))
    parser.add_argument("--lock-file", default=os.environ.get("SIGNAL50_MEDIA_OPS_LOCK_FILE", ""))
    parser.add_argument("--relay-url", default=os.environ.get("RYNMESH_RELAY_URL", ""))
    parser.add_argument("--signal50-repo", default=os.environ.get("SIGNAL50_REPO", ""))
    parser.add_argument(
        "--job-timeout-sec",
        type=float,
        default=float(os.environ.get("SIGNAL50_MEDIA_OPS_JOB_TIMEOUT_SEC", "14400") or 14400),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Signal50 media ops server requires `uvicorn`.") from exc
    app = create_app(
        work_dir=args.work_dir,
        lock_file=args.lock_file,
        relay_url=args.relay_url,
        signal50_repo=args.signal50_repo,
        job_timeout_sec=args.job_timeout_sec,
    )
    uvicorn.run(app, host=args.host, port=int(args.port), log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
