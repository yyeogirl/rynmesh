"""Docker-backed llama.cpp runtime backend.

Moved out of `lifecycle.py` verbatim (no behavior change) so `lifecycle.py`
can dispatch on `manifest.runtime` and Task 2 can add a native backend beside
this one. Callers reach this module only through `lifecycle._backend`, except
where a manifest does not exist yet (`install_managed`/`import_gguf` call
`available()`/`prepare()` directly, matching the pre-extraction order).
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import LifecycleError
from .manifest import LLMPackageManifest, fingerprint_file

# The llama.cpp server flags are the same binary's either way, so the hardening
# constants are defined once, next to the flag verification, in the native
# backend, and imported here rather than copied.
from .runtime_native import API_KEY_BYTES, CORS_ORIGINS, LOG_VERBOSITY

RUNTIME_ID = "docker_llama_cpp"

DEFAULT_IMAGE = (
    "ghcr.io/ggml-org/llama.cpp:server@"
    "sha256:db8e923e6edc9241ad788979af79543a1e1ba55dbb7d41e62490ef0d0ad3c8e7"
)


def _docker_pull(
    image: str,
    *,
    progress: Any = None,
    cancel_check: Any = None,
    timeout_s: float = 600,
) -> None:
    docker = _docker()
    process = subprocess.Popen(
        [docker, "pull", image],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout_s
    try:
        while process.poll() is None:
            if cancel_check and cancel_check():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise LifecycleError("setup cancelled")
            if time.monotonic() >= deadline:
                process.terminate()
                raise LifecycleError("downloading the llama.cpp runtime timed out")
            if progress:
                progress("pull_runtime", 72, "Preparing the local inference runtime")
            time.sleep(0.25)
    finally:
        if process.poll() is None:
            process.kill()
    if process.returncode:
        raise LifecycleError("unable to download the llama.cpp runtime image")


def _docker() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise LifecycleError("Docker is not installed; connect an existing local API or install Docker first")
    try:
        subprocess.run([executable, "info", "--format", "{{.ServerVersion}}"], check=True,
                       capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError("Docker is installed but its engine is not running") from exc
    return executable


def _container_name(package_id: str) -> str:
    return "rynmesh-llm-" + re.sub(r"[^a-z0-9_.-]", "-", package_id.lower())


def _pinned_runtime_image(manifest: LLMPackageManifest | None = None) -> str:
    image = str((manifest.install_source if manifest else {}).get("runtime_image") or DEFAULT_IMAGE)
    if image == "ghcr.io/ggml-org/llama.cpp:server":
        # Migrate manifests created by the pre-pinning preview to the reviewed
        # digest without ever pulling or running the mutable legacy tag.
        return DEFAULT_IMAGE
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image):
        raise LifecycleError("managed runtime image must be pinned by SHA-256 digest")
    return image


def _docker_state(package_id: str) -> dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        return {"installed": False, "running": False, "status": "docker unavailable"}
    result = subprocess.run(
        [docker, "inspect", "--format", "{{json .State}}", _container_name(package_id)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        return {"installed": False, "running": False, "status": "not created"}
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError:
        state = {}
    return {"installed": True, "running": bool(state.get("Running")),
            "status": str(state.get("Status") or "unknown"), "exit_code": state.get("ExitCode")}


def _run_container(manifest: LLMPackageManifest) -> None:
    docker = _docker()
    model = Path(manifest.model_path).resolve()
    if not model.is_file():
        raise LifecycleError("configured model file is missing")
    if manifest.checksum and fingerprint_file(model) != manifest.checksum:
        raise LifecycleError("configured model checksum no longer matches; refusing to start")
    name = _container_name(manifest.package_id)
    subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=30)
    port = int(urlparse(manifest.base_url).port or 8080)
    runtime_image = _pinned_runtime_image(manifest)
    if not manifest.runtime_api_key:
        manifest.runtime_api_key = secrets.token_urlsafe(API_KEY_BYTES)
    command = [
        docker, "run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp:size=128m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
        "-p", f"127.0.0.1:{port}:8080", "-v", f"{model.parent}:/models:ro", runtime_image,
        "-m", f"/models/{model.name}", "--host", "0.0.0.0", "--port", "8080",
        "--alias", manifest.public_model_alias, "-c", str(manifest.context_window),
        "-np", str(manifest.max_concurrent),
        # Same dangerous llama.cpp defaults as the native backend: the
        # published port is loopback-only, but without these any page the
        # owner visits could still drive the model through it — and the
        # server's own log stays at error level either way.
        "--api-key", manifest.runtime_api_key, "--cors-origins", CORS_ORIGINS,
        "-lv", LOG_VERBOSITY,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise LifecycleError("llama.cpp container failed to start (inspect Docker logs for details)")


def available() -> tuple[bool, str]:
    """(True, "") when the Docker engine answers; else (False, safe reason)."""
    try:
        _docker()
    except LifecycleError as exc:
        return False, str(exc)
    return True, ""


def prepare(*, progress: Any = None, cancel_check: Any = None, root: Any = None) -> None:
    """Pull the pinned image (was `_docker_pull(DEFAULT_IMAGE, ...)`).

    `root` exists only so both backends share one `prepare` signature; the
    Docker image lives in the engine's own store, not under the Rynmesh root.
    """
    _docker_pull(DEFAULT_IMAGE, progress=progress, cancel_check=cancel_check)


def start(manifest: LLMPackageManifest) -> None:
    _run_container(manifest)


def stop(manifest: LLMPackageManifest) -> bool:
    result = subprocess.run([_docker(), "stop", _container_name(manifest.package_id)],
                            capture_output=True, text=True, timeout=60)
    return result.returncode == 0


def remove(manifest: LLMPackageManifest) -> None:
    subprocess.run([_docker(), "rm", "-f", _container_name(manifest.package_id)],
                   capture_output=True, timeout=60)


def update(manifest: LLMPackageManifest) -> None:
    subprocess.run([_docker(), "pull", _pinned_runtime_image(manifest)], check=True, timeout=600)


def state(manifest: LLMPackageManifest) -> dict[str, Any]:
    return _docker_state(manifest.package_id)


def install_source(manifest_or_none: LLMPackageManifest | None) -> dict[str, str]:
    return {"runtime_image": _pinned_runtime_image(manifest_or_none)}
