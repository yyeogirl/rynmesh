"""Pinned three-profile model catalog: light / balanced / quality.

Pure data module: no network access, no subprocess calls, nothing that can
fail at import time. Each profile pins an exact Hugging Face revision,
filename, and SHA-256 digest so a managed install always fetches the same
reviewed bytes; `lifecycle.install_managed` and `hardware.recommend` both
read from `PROFILES` instead of hardcoding model details themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

_APACHE_NOTICE = "Qwen2.5 and llama.cpp are Apache-2.0; review their notices before use."


@dataclass(frozen=True)
class ModelProfile:
    profile: str            # "light" | "balanced" | "quality"
    model_alias: str        # public alias, e.g. "rynmesh-qwen2.5-0.5b-q4"
    display_name: str       # "Qwen2.5-0.5B-Instruct-Q4_K_M"
    parameter_millions: int
    quantization: str       # "Q4_K_M"
    url: str                # pinned HTTPS URL (revision in path)
    sha256: str
    size_bytes: int
    estimated_memory_mb: int
    context_window: int
    max_concurrent: int
    license_id: str = "Apache-2.0"
    license_notice: str = _APACHE_NOTICE


def _pinned_url(repo: str, revision: str, filename: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/{revision}/{filename}"


PROFILES: tuple[ModelProfile, ...] = (
    ModelProfile(
        profile="light",
        model_alias="rynmesh-qwen2.5-0.5b-q4",
        display_name="Qwen2.5-0.5B-Instruct-Q4_K_M",
        parameter_millions=500,
        quantization="Q4_K_M",
        url=_pinned_url(
            "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
            "872f8a96064a1242ac3a3359cad77c3042548405",
            "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        ),
        sha256="74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db",
        size_bytes=491400032,
        estimated_memory_mb=900,
        context_window=4096,
        max_concurrent=2,
    ),
    ModelProfile(
        profile="balanced",
        model_alias="rynmesh-qwen2.5-1.5b-q4",
        display_name="Qwen2.5-1.5B-Instruct-Q4_K_M",
        parameter_millions=1500,
        quantization="Q4_K_M",
        url=_pinned_url(
            "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
            "91cad51170dc346986eccefdc2dd33a9da36ead9",
            "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        ),
        sha256="6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e",
        size_bytes=1117320736,
        estimated_memory_mb=2300,
        context_window=4096,
        max_concurrent=1,
    ),
    ModelProfile(
        profile="quality",
        model_alias="rynmesh-qwen2.5-3b-q4",
        display_name="Qwen2.5-3B-Instruct-Q4_K_M",
        parameter_millions=3000,
        quantization="Q4_K_M",
        url=_pinned_url(
            "Qwen/Qwen2.5-3B-Instruct-GGUF",
            "7dabda4d13d513e3e842b20f0d435c732f172cbe",
            "qwen2.5-3b-instruct-q4_k_m.gguf",
        ),
        sha256="626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d",
        size_bytes=2104932768,
        estimated_memory_mb=4200,
        context_window=4096,
        max_concurrent=1,
    ),
)

_BY_NAME: dict[str, ModelProfile] = {profile.profile: profile for profile in PROFILES}


def profile_by_name(name: str) -> ModelProfile:
    """Look up a catalog profile by name.

    Raises a plain `ValueError` (not `LifecycleError`) so this stays a
    dependency-free data module; every call site already treats `ValueError`
    the same way it treats `LifecycleError`.
    """
    try:
        return _BY_NAME[str(name)]
    except KeyError:
        raise ValueError(
            f"unknown model profile: {name!r}; choose one of {', '.join(_BY_NAME)}"
        ) from None


def estimated_disk_mb(profile: ModelProfile) -> int:
    """Conservative disk headroom for a profile: model weights plus overhead."""
    return int(profile.estimated_memory_mb * 1.5 + 256)
