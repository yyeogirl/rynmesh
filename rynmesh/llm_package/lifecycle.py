"""Installation, import, self-test, and split runtime/model lifecycle."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

from . import catalog, runtime_docker, runtime_native
from .adapters import AdapterError, adapter_from_manifest
from .errors import LifecycleError
from .hardware import HardwareReport, detect_hardware, recommend, usable_memory_mb
from .manifest import (
    LLMPackageManifest,
    fingerprint_file,
    load_manifest,
    save_manifest,
)
from .model_download import CancelCheck, ProgressCallback
from .model_download import download as _download

# The "light" catalog profile, kept available under its historical names:
# existing tests/docs/CLI callers reference these three module constants.
_LIGHT_PROFILE = catalog.profile_by_name("light")
DEFAULT_MODEL_URL = _LIGHT_PROFILE.url
DEFAULT_MODEL_SHA256 = _LIGHT_PROFILE.sha256
DEFAULT_MODEL_REVISION = DEFAULT_MODEL_URL.split("/resolve/", 1)[1].split("/", 1)[0]
GGUF_MAGIC = b"GGUF"

RUNTIME_DOCKER = runtime_docker.RUNTIME_ID
RUNTIME_NATIVE = runtime_native.RUNTIME_ID
RUNTIME_EXTERNAL = "external"


def _backend(manifest: LLMPackageManifest):
    """Return the runtime backend module for `manifest.runtime`."""
    if manifest.runtime == RUNTIME_DOCKER:
        return runtime_docker
    if manifest.runtime == RUNTIME_NATIVE:
        return runtime_native
    raise LifecycleError(f"unsupported runtime: {manifest.runtime}")


def select_runtime(preference: str = "auto", *, root: str | Path | None = None):
    """Pick a runtime backend module for a package that does not exist yet.

    "auto" prefers the native child-process runtime (no Docker on consumer
    desktops) and falls back to Docker for server nodes that have the engine.
    `root` is the install root the caller will use, so a runtime already
    downloaded under a custom root counts as available.
    """
    choice = str(preference or "auto").strip().lower()
    if choice in {"native", RUNTIME_NATIVE}:
        ok, reason = runtime_native.available(root)
        if not ok:
            raise LifecycleError(reason)
        return runtime_native
    if choice in {"docker", RUNTIME_DOCKER}:
        ok, reason = runtime_docker.available()
        if not ok:
            raise LifecycleError(reason)
        return runtime_docker
    if choice != "auto":
        raise LifecycleError("runtime preference must be auto, native, or docker")
    native_ok, native_reason = runtime_native.available(root)
    if native_ok:
        return runtime_native
    docker_ok, docker_reason = runtime_docker.available()
    if docker_ok:
        return runtime_docker
    raise LifecycleError(
        f"no local inference runtime is available: {native_reason}; {docker_reason}"
    )


def _progress(
    callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
    stage: str,
    percent: int,
    message: str,
) -> None:
    if cancel_check and cancel_check():
        raise LifecycleError("setup cancelled")
    if callback:
        callback(stage, max(0, min(100, percent)), message)


def _stop_after_failed_start(manifest: LLMPackageManifest) -> None:
    """Best-effort stop of a server we started but never finished verifying.

    An install that raises between `start()` and `save_manifest` (a health
    timeout on a big model, a cancel, a failing self-test) would otherwise
    leave the child running while its minted loopback key exists only in
    memory. Nothing the stop itself can raise is worth replacing the real
    failure with, so every error here is swallowed.
    """
    try:
        _backend(manifest).stop(manifest)
    except Exception:  # noqa: BLE001 - the original failure must reach the caller
        pass


def _validate_package_id(package_id: str) -> str:
    if not package_id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for ch in package_id):
        raise LifecycleError("package_id must be a non-empty lowercase slug")
    return package_id


def default_root() -> Path:
    return Path(os.environ.get("RYNMESH_LLM_HOME", Path.home() / ".rynmesh" / "llm")).expanduser()


def manifest_path(package_id: str, root: str | Path | None = None) -> Path:
    return Path(root or default_root()).expanduser() / "packages" / package_id / "manifest.json"


def validate_gguf(path: str | Path, *, report: HardwareReport | None = None,
                  allow_risk: bool = False) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise LifecycleError("GGUF path is not a readable file")
    try:
        with source.open("rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        raise LifecycleError(f"GGUF file is not readable: {exc}") from exc
    if magic != GGUF_MAGIC:
        raise LifecycleError("unsupported model format: expected GGUF magic")
    size = source.stat().st_size
    if size < 32:
        raise LifecycleError("GGUF file is truncated")
    hardware = report or detect_hardware(source.parent)
    estimated_memory = int(size / 2**20 * 1.35 + 384)
    safe_memory = max(int(hardware.ram_available_mb * 0.75), max(
        (int(gpu.memory_free_mb * 0.85) for gpu in hardware.nvidia_gpus), default=0
    ))
    fits = bool(safe_memory and estimated_memory <= safe_memory)
    if not fits and not allow_risk:
        raise LifecycleError(
            f"model needs about {estimated_memory} MiB but the conservative available limit is "
            f"{safe_memory} MiB; pass --accept-risk to override"
        )
    return {"path": str(source), "format": "GGUF", "size_bytes": size,
            "estimated_memory_mb": estimated_memory, "fits": fits,
            "fingerprint": fingerprint_file(source)}


def wait_healthy(manifest: LLMPackageManifest, *, timeout_s: float = 120) -> dict[str, Any]:
    adapter = adapter_from_manifest(manifest)
    deadline = time.monotonic() + timeout_s
    latest: dict[str, Any] = {"ok": False, "error": "not started"}
    while time.monotonic() < deadline:
        latest = adapter.health()
        if latest.get("ok"):
            return latest
        time.sleep(1)
    raise LifecycleError(f"runtime did not become healthy: {latest.get('error', 'unknown error')}")


def self_test(manifest: LLMPackageManifest) -> dict[str, Any]:
    adapter = adapter_from_manifest(manifest)
    health = adapter.health()
    if not health.get("ok"):
        raise LifecycleError(f"health check failed: {health.get('error', 'unknown error')}")
    result = adapter.infer(
        prompt="Reply with exactly: RYNMESH SELF TEST OK", max_tokens=64,
        task_id="selftest", timeout_s=manifest.timeout_seconds,
    )
    text = str(result.pop("text", ""))
    if not text.strip():
        raise LifecycleError("real inference self-test returned empty output")
    return {"ok": True, "output_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "output_preview": text[:120], **result}


_CUSTOM_MODEL_FALLBACK = {
    # Used only when accept_risk lets an install through despite nothing in
    # the catalog fitting — the same conservative floor the old single-model
    # code fell back to.
    "context_window": 2048, "max_concurrent": 1, "estimated_memory_mb": 1024,
}


def _resolve_install_profile(
    *, profile: str, model_url: str, expected_sha256: str, expected_size_bytes: int | None,
    accept_risk: bool, report: HardwareReport, choices: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pick the model to install: a pinned catalog profile or a custom override.

    Returns a plain dict (not `ModelProfile`, since a custom override has no
    catalog entry) with the keys `install_managed` needs downstream: `name`,
    `url`, `sha256`, `size_bytes`, `alias`, `context_window`,
    `max_concurrent`, `estimated_memory_mb`, `license_id`, `license_notice`.
    """
    has_url, has_sha = bool(model_url), bool(expected_sha256)
    if has_url != has_sha:
        raise LifecycleError("a custom model install requires both model_url and expected_sha256")
    if has_url and has_sha:
        digest = expected_sha256.lower()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise LifecycleError("managed model install requires a pinned SHA-256 digest")
        # A custom override still has to pass the same hardware gate the old
        # single-model code ran — there being no catalog entry to size it
        # against is not a reason to skip the check, only a reason to fall
        # back to the same conservative floor if `accept_risk` lets it through.
        if not choices[0].get("can_run") and not accept_risk:
            raise LifecycleError(str(choices[0].get("reason")))
        metadata = choices[0] if choices[0].get("can_run") else _CUSTOM_MODEL_FALLBACK
        return {
            "name": "custom", "url": model_url, "sha256": digest, "size_bytes": expected_size_bytes,
            "alias": "rynmesh-custom-model",
            "context_window": metadata["context_window"],
            "max_concurrent": metadata["max_concurrent"],
            "estimated_memory_mb": metadata["estimated_memory_mb"],
            "license_id": "unknown",
            "license_notice": "Operator must review and comply with the model and runtime licenses.",
        }

    requested = str(profile or "auto").strip().lower()
    if requested == "auto":
        if not choices[0].get("can_run"):
            if not accept_risk:
                raise LifecycleError(str(choices[0].get("reason")))
            resolved = catalog.profile_by_name("light")
        else:
            recommended_name = next((c["profile"] for c in choices if c.get("recommended")), None)
            if recommended_name is None:
                raise LifecycleError("no recommended profile was available for automatic selection")
            resolved = catalog.profile_by_name(recommended_name)
    else:
        resolved = catalog.profile_by_name(requested)  # raises ValueError on an unknown name
        fits = any(c.get("profile") == resolved.profile and c.get("can_run") for c in choices)
        if not fits and not accept_risk:
            limit = usable_memory_mb(report)
            raise LifecycleError(
                f"profile {resolved.profile} needs about {resolved.estimated_memory_mb} MiB but the "
                f"conservative available limit is {limit} MiB; choose a smaller profile or accept the risk"
            )
    return {
        "name": resolved.profile, "url": resolved.url, "sha256": resolved.sha256,
        "size_bytes": resolved.size_bytes, "alias": resolved.model_alias,
        "context_window": resolved.context_window, "max_concurrent": resolved.max_concurrent,
        "estimated_memory_mb": resolved.estimated_memory_mb, "license_id": resolved.license_id,
        "license_notice": resolved.license_notice,
    }


def install_managed(*, package_id: str = "local-small", root: str | Path | None = None,
                    port: int = 18080, model_url: str = "", expected_sha256: str = "",
                    expected_size_bytes: int | None = None,
                    accept_risk: bool = False, runtime: str = "auto", profile: str = "auto",
                    progress: ProgressCallback | None = None,
                    cancel_check: CancelCheck | None = None) -> dict[str, Any]:
    package_id = _validate_package_id(package_id)
    base = Path(root or default_root()).expanduser()
    _progress(progress, cancel_check, "hardware", 5, "Checking available hardware")
    report = detect_hardware(base)
    choices = recommend(report)
    selected = _resolve_install_profile(
        profile=profile, model_url=model_url, expected_sha256=expected_sha256,
        expected_size_bytes=expected_size_bytes, accept_risk=accept_risk,
        report=report, choices=choices,
    )
    _progress(progress, cancel_check, "runtime_check", 10, "Checking the local inference runtime")
    backend = select_runtime(runtime, root=base)
    _progress(progress, cancel_check, "checksum", 12, "Verifying model source metadata")
    digest = selected["sha256"]
    checksum = "sha256:" + digest
    model_dir = base / "models" / package_id
    # Profile-scoped filename so switching profiles never overwrites another
    # already-verified model; a pre-existing bare `model.gguf` (from before
    # profiles existed) is still honored when its checksum matches.
    model_path = model_dir / f"{selected['name']}.gguf"
    legacy_path = model_dir / "model.gguf"
    if model_path.exists() and fingerprint_file(model_path) == checksum:
        _progress(progress, cancel_check, "download_model", 60, "Verified model already present")
    elif legacy_path.exists() and fingerprint_file(legacy_path) == checksum:
        model_path = legacy_path
        _progress(progress, cancel_check, "download_model", 60, "Verified model already present")
    else:
        _download(
            selected["url"], model_path, digest, size_bytes=selected["size_bytes"],
            progress=progress, cancel_check=cancel_check,
        )
    _progress(progress, cancel_check, "pull_runtime", 65, "Preparing the local inference runtime")
    backend.prepare(progress=progress, cancel_check=cancel_check, root=base)
    manifest = LLMPackageManifest(
        package_id=package_id, mode="managed", public_model_alias=selected["alias"],
        adapter="openai_compatible", runtime=backend.RUNTIME_ID, model=selected["alias"],
        model_path=str(model_path), model_owned=True, base_url=f"http://127.0.0.1:{port}",
        runtime_dir=str(base) if backend.RUNTIME_ID == RUNTIME_NATIVE else "",
        checksum=checksum, model_fingerprint=checksum,
        context_window=int(selected["context_window"]), max_output_tokens=256,
        max_concurrent=int(selected["max_concurrent"]),
        hardware_requirements={"estimated_memory_mb": int(selected["estimated_memory_mb"])},
        install_source={"model_url": selected["url"], "profile": selected["name"],
                        **backend.install_source(None)},
        license_notice=selected["license_notice"], license_id=selected["license_id"],
        lifecycle={"start": "rynmesh-llm start", "stop": "rynmesh-llm stop",
                   "restart": "rynmesh-llm restart", "status": "rynmesh-llm status"},
    )
    _progress(progress, cancel_check, "start_runtime", 82, "Starting the local model runtime")
    _backend(manifest).start(manifest)
    try:
        _progress(progress, cancel_check, "health_check", 90, "Waiting for the model health check")
        wait_healthy(manifest)
        _progress(progress, cancel_check, "self_test", 96,
                  "Running a real private inference self-test")
        result = self_test(manifest)
        save_manifest(manifest, manifest_path(package_id, base))
    except (LifecycleError, AdapterError, OSError):
        # Cancellation arrives through `_progress` as a LifecycleError too.
        _stop_after_failed_start(manifest)
        raise
    _progress(progress, cancel_check, "completed", 100, "Local model is ready")
    return {"manifest": str(manifest_path(package_id, base)), "hardware": report.to_dict(),
            "recommendation": selected, "self_test": result}


def import_gguf(*, source: str | Path, package_id: str, alias: str,
                root: str | Path | None = None, port: int = 18080,
                accept_risk: bool = False, runtime: str = "auto",
                progress: ProgressCallback | None = None,
                cancel_check: CancelCheck | None = None) -> dict[str, Any]:
    package_id = _validate_package_id(package_id)
    base = Path(root or default_root()).expanduser()
    _progress(progress, cancel_check, "validate_model", 10, "Validating the GGUF file in place")
    details = validate_gguf(source, allow_risk=accept_risk)
    _progress(progress, cancel_check, "runtime_check", 25, "Checking the local inference runtime")
    backend = select_runtime(runtime, root=base)
    _progress(progress, cancel_check, "pull_runtime", 40, "Preparing the local inference runtime")
    backend.prepare(progress=progress, cancel_check=cancel_check, root=base)
    manifest = LLMPackageManifest(
        package_id=package_id, mode="import_gguf", public_model_alias=alias,
        adapter="openai_compatible", runtime=backend.RUNTIME_ID, model=alias,
        model_path=details["path"], model_owned=False, base_url=f"http://127.0.0.1:{port}",
        runtime_dir=str(base) if backend.RUNTIME_ID == RUNTIME_NATIVE else "",
        checksum=details["fingerprint"], model_fingerprint=details["fingerprint"],
        hardware_requirements={"estimated_memory_mb": details["estimated_memory_mb"]},
        install_source={**backend.install_source(None), "kind": "user_owned_read_only_gguf"},
    )
    path = manifest_path(package_id, base)
    _progress(progress, cancel_check, "start_runtime", 80, "Starting the read-only GGUF runtime")
    _backend(manifest).start(manifest)
    try:
        _progress(progress, cancel_check, "health_check", 90, "Waiting for the model health check")
        wait_healthy(manifest)
        _progress(progress, cancel_check, "self_test", 96,
                  "Running a real private inference self-test")
        result = self_test(manifest)
        save_manifest(manifest, path)
    except (LifecycleError, AdapterError, OSError):
        # Cancellation arrives through `_progress` as a LifecycleError too.
        _stop_after_failed_start(manifest)
        raise
    _progress(progress, cancel_check, "completed", 100, "Imported model is ready")
    return {"manifest": str(path), "import": {k: v for k, v in details.items() if k != "path"},
            "self_test": result}


def connect_local_api(*, base_url: str, package_id: str, alias: str, model: str = "",
                      api_key_env: str = "", adapter: str = "openai_compatible",
                      root: str | Path | None = None, allow_non_loopback: bool = False,
                      progress: ProgressCallback | None = None,
                      cancel_check: CancelCheck | None = None) -> dict[str, Any]:
    package_id = _validate_package_id(package_id)
    if api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise LifecycleError(
            "API key setting must be an environment-variable name, never the secret value"
        )
    _progress(progress, cancel_check, "connect", 20, "Checking the local model API")
    manifest = LLMPackageManifest(
        package_id=package_id, mode="ollama" if adapter == "ollama" else "openai_compatible",
        public_model_alias=alias, adapter=adapter, runtime=RUNTIME_EXTERNAL, base_url=base_url,
        api_key_env=api_key_env, model=model, allow_non_loopback=allow_non_loopback,
        install_source={"kind": "existing_local_service"},
    )
    active = adapter_from_manifest(manifest)
    health = active.health()
    if not health.get("ok"):
        raise LifecycleError(f"local API health check failed: {health.get('error', 'unknown error')}")
    if not manifest.model:
        manifest.model = str(health.get("model") or "")
    capabilities = active.capabilities()
    if capabilities.get("streaming") and "streaming" not in manifest.capabilities:
        manifest.capabilities.append("streaming")
    _progress(progress, cancel_check, "self_test", 75, "Running a real private inference self-test")
    result = self_test(manifest)
    path = manifest_path(package_id, root)
    save_manifest(manifest, path)
    _progress(progress, cancel_check, "completed", 100, "Local API connection is ready")
    return {"manifest": str(path), "health": health, "capabilities": capabilities,
            "self_test": result}


def start(path: str | Path) -> dict[str, Any]:
    manifest = load_manifest(path)
    if manifest.runtime == RUNTIME_EXTERNAL:
        return {"managed": False, "health": adapter_from_manifest(manifest).health()}
    before = manifest.runtime_api_key
    _backend(manifest).start(manifest)
    if manifest.runtime_api_key != before:
        # A loopback key minted by this start has to reach the private
        # manifest before anything talks to the server, or the next process
        # would call an authenticated runtime without the bearer token.
        save_manifest(manifest, path)
    return {"managed": True, "health": wait_healthy(manifest)}


def stop(path: str | Path) -> dict[str, Any]:
    manifest = load_manifest(path)
    if manifest.runtime == RUNTIME_EXTERNAL:
        return {"managed": False, "stopped": False, "message": "external service is owner-managed"}
    stopped = _backend(manifest).stop(manifest)
    return {"managed": True, "stopped": stopped}


def restart(path: str | Path) -> dict[str, Any]:
    stop(path)
    return start(path)


def update(path: str | Path) -> dict[str, Any]:
    """Update the managed runtime without silently replacing/deleting models."""
    manifest = load_manifest(path)
    if manifest.runtime == RUNTIME_EXTERNAL:
        return {"managed": False, "updated": False, "message": "external runtime is owner-managed",
                "self_test": self_test(manifest)}
    _backend(manifest).update(manifest)
    restarted = restart(path)
    return {"managed": True, "updated": True, "runtime": restarted,
            "model_preserved": True, "self_test": self_test(manifest)}


def status(path: str | Path) -> dict[str, Any]:
    manifest = load_manifest(path)
    runtime_state = {"managed": False} if manifest.runtime == RUNTIME_EXTERNAL else _backend(manifest).state(manifest)
    return {"package_id": manifest.package_id, "mode": manifest.mode,
            "runtime": runtime_state,
            "health": adapter_from_manifest(manifest).health(), "public": manifest.public_dict(),
            "storage": model_storage(manifest)}


def model_storage(manifest: LLMPackageManifest) -> dict[str, Any]:
    """Inspect the selected model without exposing paths or guessing on I/O failure."""
    result = {"model_owned": manifest.model_owned, "model_present": None, "model_bytes": None}
    if not manifest.model_path:
        return result
    try:
        info = Path(manifest.model_path).expanduser().stat()
    except FileNotFoundError:
        return {**result, "model_present": False, "model_bytes": 0}
    except OSError:
        return result
    present = stat.S_ISREG(info.st_mode)
    return {**result, "model_present": present, "model_bytes": info.st_size if present else 0}


def uninstall(path: str | Path, *, delete_environment: bool = True,
              delete_model: bool = False, confirm_model_delete: bool = False) -> dict[str, Any]:
    manifest_file = Path(path).expanduser()
    manifest = load_manifest(manifest_file)
    removed = []
    if delete_environment and manifest.runtime != RUNTIME_EXTERNAL:
        _backend(manifest).remove(manifest)
        # The native backend owns a child process, not a container; reporting
        # "runtime_container" there described something that never existed.
        removed.append("runtime_process" if manifest.runtime == RUNTIME_NATIVE
                       else "runtime_container")
    if delete_model:
        if not confirm_model_delete:
            raise LifecycleError("model deletion needs separate explicit confirmation")
        if not manifest.model_owned:
            raise LifecycleError("Rynmesh will not delete an imported/user-owned model")
        model = Path(manifest.model_path).resolve()
        owned_root = manifest_file.resolve().parents[2] / "models"
        try:
            model.relative_to(owned_root)
        except ValueError as exc:
            raise LifecycleError("managed model path escaped the owned model directory") from exc
        model.unlink(missing_ok=True)
        removed.append("managed_model")
    return {"removed": removed, "model_preserved": "managed_model" not in removed,
            "user_owned_model": not manifest.model_owned,
            "private_manifest_preserved": manifest_file.exists()}
