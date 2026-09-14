"""Lazy video-generation backends.

The optional ML stack is imported only when the first generation starts. This
keeps the ordinary Rynmesh node lightweight and lets operators use a dedicated
GPU environment for the provider gateway.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Protocol


class VideoBackendError(RuntimeError):
    """A safe, user-facing video backend failure."""


class VideoBackend(Protocol):
    model_id: str

    def health(self) -> dict[str, Any]: ...

    def generate(
        self,
        *,
        prompt: str,
        output_path: Path,
        width: int,
        height: int,
        num_frames: int,
        num_inference_steps: int,
        fps: int,
        seed: int,
    ) -> dict[str, Any]: ...


class WanDiffusersBackend:
    """Wan 2.1 text-to-video through the official Diffusers pipeline."""

    def __init__(self, model_path: str, *, model_id: str = "Wan2.1-T2V-1.3B") -> None:
        self.model_path = str(Path(model_path).expanduser().resolve())
        self.model_id = model_id
        self._pipe: Any | None = None

    def health(self) -> dict[str, Any]:
        path = Path(self.model_path)
        result: dict[str, Any] = {
            "ok": path.is_dir() and (path / "model_index.json").is_file(),
            "backend": "wan_diffusers",
            "model": self.model_id,
            "model_present": path.is_dir(),
            "pipeline_loaded": self._pipe is not None,
        }
        try:
            import torch

            result.update(
                {
                    "torch": torch.__version__,
                    "cuda_available": torch.cuda.is_available(),
                    "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "",
                    "compute_capability": (
                        ".".join(str(v) for v in torch.cuda.get_device_capability(0))
                        if torch.cuda.is_available()
                        else ""
                    ),
                }
            )
            result["ok"] = bool(result["ok"] and torch.cuda.is_available())
        except ImportError:
            result.update({"ok": False, "error": "torch_not_installed"})
        try:
            import diffusers

            result["diffusers"] = diffusers.__version__
        except ImportError:
            result.update({"ok": False, "error": "diffusers_not_installed"})
        return result

    def _load(self) -> Any:
        if self._pipe is not None:
            return self._pipe
        try:
            import torch
            from diffusers import WanPipeline
        except ImportError as exc:
            raise VideoBackendError("video_runtime_dependencies_missing") from exc
        if not torch.cuda.is_available():
            raise VideoBackendError("cuda_unavailable")
        major, minor = torch.cuda.get_device_capability(0)
        if (major, minor) < (7, 0):
            raise VideoBackendError("gpu_compute_capability_too_old")
        try:
            pipe = WanPipeline.from_pretrained(
                self.model_path,
                torch_dtype=torch.float16,
                local_files_only=True,
            )
            pipe.enable_model_cpu_offload()
            pipe.vae.enable_tiling()
        except Exception as exc:
            raise VideoBackendError(f"video_pipeline_load_failed:{type(exc).__name__}") from exc
        self._pipe = pipe
        return pipe

    def generate(
        self,
        *,
        prompt: str,
        output_path: Path,
        width: int,
        height: int,
        num_frames: int,
        num_inference_steps: int,
        fps: int,
        seed: int,
    ) -> dict[str, Any]:
        try:
            import torch
            from diffusers.utils import export_to_video
        except ImportError as exc:
            raise VideoBackendError("video_runtime_dependencies_missing") from exc
        pipe = self._load()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        generator = torch.Generator(device="cuda").manual_seed(seed)
        try:
            result = pipe(
                prompt=prompt,
                negative_prompt=(
                    "static, blurry, low quality, watermark, subtitles, distorted anatomy"
                ),
                width=width,
                height=height,
                num_frames=num_frames,
                num_inference_steps=num_inference_steps,
                guidance_scale=5.0,
                generator=generator,
            )
            export_to_video(result.frames[0], str(output_path), fps=fps)
        except Exception as exc:
            raise VideoBackendError(f"video_generation_failed:{type(exc).__name__}") from exc
        if not output_path.is_file() or output_path.stat().st_size < 1024:
            raise VideoBackendError("video_output_missing_or_empty")
        digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return {
            "model": self.model_id,
            "sha256": digest,
            "bytes": output_path.stat().st_size,
            "duration_seconds": float(probe.stdout.strip() or 0),
        }
