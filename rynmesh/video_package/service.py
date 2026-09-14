"""Authenticated asynchronous HTTP service for private video generation."""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .backend import VideoBackend, VideoBackendError, WanDiffusersBackend


def _bounded_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}_invalid") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name}_out_of_range")
    return parsed


class VideoGenerationService:
    def __init__(self, *, backend: VideoBackend, output_dir: Path) -> None:
        self.backend = backend
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rynmesh-video")
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        prompt = str(body.get("prompt") or "").strip()
        if not prompt or len(prompt) > 4000:
            raise ValueError("prompt_required_or_too_long")
        width = _bounded_int(body.get("width", 480), name="width", minimum=128, maximum=832)
        height = _bounded_int(body.get("height", 272), name="height", minimum=128, maximum=832)
        if width % 16 or height % 16:
            raise ValueError("width_and_height_must_be_multiples_of_16")
        num_frames = _bounded_int(
            body.get("num_frames", 33), name="num_frames", minimum=5, maximum=81
        )
        if (num_frames - 1) % 4:
            raise ValueError("num_frames_must_equal_4k_plus_1")
        steps = _bounded_int(
            body.get("num_inference_steps", 20),
            name="num_inference_steps",
            minimum=1,
            maximum=50,
        )
        fps = _bounded_int(body.get("fps", 8), name="fps", minimum=4, maximum=24)
        seed = _bounded_int(body.get("seed", 42), name="seed", minimum=0, maximum=2**31 - 1)
        job_id = "vid_" + uuid.uuid4().hex
        now = time.time()
        public = {
            "id": job_id,
            "state": "queued",
            "model": self.backend.model_id,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "created_at": now,
            "updated_at": now,
            "width": width,
            "height": height,
            "num_frames": num_frames,
            "num_inference_steps": steps,
            "fps": fps,
            "seed": seed,
        }
        with self._lock:
            self._jobs[job_id] = public
        self._executor.submit(
            self._run,
            job_id,
            prompt,
            width,
            height,
            num_frames,
            steps,
            fps,
            seed,
        )
        return public.copy()

    def _run(
        self,
        job_id: str,
        prompt: str,
        width: int,
        height: int,
        num_frames: int,
        steps: int,
        fps: int,
        seed: int,
    ) -> None:
        started = time.time()
        self._update(job_id, state="running", started_at=started)
        output_path = self.output_dir / f"{job_id}.mp4"
        try:
            result = self.backend.generate(
                prompt=prompt,
                output_path=output_path,
                width=width,
                height=height,
                num_frames=num_frames,
                num_inference_steps=steps,
                fps=fps,
                seed=seed,
            )
            self._update(
                job_id,
                state="succeeded",
                completed_at=time.time(),
                elapsed_seconds=round(time.time() - started, 3),
                output={**result, "content_path": f"/v1/videos/generations/{job_id}/content"},
            )
        except VideoBackendError as exc:
            self._update(
                job_id,
                state="failed",
                completed_at=time.time(),
                elapsed_seconds=round(time.time() - started, 3),
                error=str(exc),
            )
        except Exception as exc:  # fail closed without leaking prompt or paths
            self._update(
                job_id,
                state="failed",
                completed_at=time.time(),
                elapsed_seconds=round(time.time() - started, 3),
                error=f"unexpected_video_backend_failure:{type(exc).__name__}",
            )

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)
            self._jobs[job_id]["updated_at"] = time.time()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._jobs.get(job_id)
            return value.copy() if value else None

    def output_path(self, job_id: str) -> Path | None:
        job = self.get(job_id)
        if not job or job.get("state") != "succeeded":
            return None
        candidate = (self.output_dir / f"{job_id}.mp4").resolve()
        if candidate.parent != self.output_dir or not candidate.is_file():
            return None
        return candidate


def create_app(
    *,
    service: VideoGenerationService | None = None,
    api_token: str | None = None,
):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi import Request as FastAPIRequest
        from fastapi.responses import FileResponse, JSONResponse
    except ImportError as exc:  # pragma: no cover
        raise ImportError("video provider requires fastapi") from exc
    globals()["FastAPIRequest"] = FastAPIRequest

    token = (api_token if api_token is not None else os.environ.get("RYNMESH_VIDEO_API_TOKEN", "")).strip()
    if not token:
        raise RuntimeError("RYNMESH_VIDEO_API_TOKEN is required")
    if service is None:
        model_path = os.environ.get(
            "RYNMESH_VIDEO_MODEL_PATH",
            "/model/ModelScope/Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
        )
        output_dir = Path(os.environ.get("RYNMESH_VIDEO_OUTPUT_DIR", "/opt/rynmesh/data/videos"))
        service = VideoGenerationService(
            backend=WanDiffusersBackend(model_path), output_dir=output_dir
        )

    app = FastAPI(title="Rynmesh Video Provider", version="0.1")

    @app.middleware("http")
    async def authenticate(request: FastAPIRequest, call_next):
        root_path = str(request.scope.get("root_path") or "").rstrip("/")
        if request.url.path != root_path + "/health":
            supplied = request.headers.get("authorization", "")
            expected = "Bearer " + token
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, Any]:
        backend = service.backend.health()
        return {"status": "ok" if backend.get("ok") else "degraded", "backend": backend}

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return {"data": [{"id": service.backend.model_id, "object": "video-model"}]}

    @app.post("/v1/videos/generations", status_code=202)
    async def generate(request: FastAPIRequest) -> dict[str, Any]:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request_body_must_be_an_object")
            return service.submit(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/videos/generations/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        value = service.get(job_id)
        if value is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return value

    @app.get("/v1/videos/generations/{job_id}/content")
    def content(job_id: str):
        path = service.output_path(job_id)
        if path is None:
            raise HTTPException(status_code=404, detail="video_not_ready")
        return FileResponse(path, media_type="video/mp4", filename=f"{job_id}.mp4")

    return app
