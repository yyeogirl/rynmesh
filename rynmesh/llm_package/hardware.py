"""Cross-platform hardware detection and conservative LLM recommendations."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import catalog


@dataclass
class GPUInfo:
    name: str
    memory_total_mb: int
    memory_free_mb: int
    driver_version: str


@dataclass
class HardwareReport:
    os: str
    architecture: str
    cpu: str
    logical_cpus: int
    ram_total_mb: int
    ram_available_mb: int
    disk_free_mb: int
    nvidia_gpus: list[GPUInfo]
    nvidia_probe: str
    container_runtime: str
    container_available: bool
    native_runtime_available: bool
    native_runtime_present: bool
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_vm_stat(text: str, page_size_default: int = 4096) -> int:
    """Parse macOS `vm_stat` output into available memory in MiB.

    Available memory is the sum of free, inactive, and speculative pages,
    using the page size reported in the `vm_stat` header (falling back to
    `page_size_default` if it cannot be parsed).
    """
    page_size = page_size_default
    header_match = re.search(r"page size of (\d+) bytes", text)
    if header_match:
        page_size = int(header_match.group(1))
    wanted = ("Pages free", "Pages inactive", "Pages speculative")
    pages = 0
    for line in text.splitlines():
        for label in wanted:
            if not line.startswith(label):
                continue
            parts = line.split(":", 1)
            if len(parts) != 2:
                break
            try:
                pages += int(parts[1].strip().rstrip("."))
            except ValueError:
                pass
            break
    return pages * page_size // 2**20


def _darwin_memory() -> tuple[int, int]:
    total_result = subprocess.run(
        ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5, check=True,
    )
    total_mb = int(total_result.stdout.strip()) // 2**20
    vm_result = subprocess.run(
        ["vm_stat"], capture_output=True, text=True, timeout=5, check=True,
    )
    available_mb = _parse_vm_stat(vm_result.stdout)
    return total_mb, available_mb


def _memory() -> tuple[int, int]:
    if platform.system() == "Darwin":
        try:
            return _darwin_memory()
        except (OSError, subprocess.SubprocessError, ValueError, LookupError):
            return 0, 0
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                ("avail_extended", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.total_phys // 2**20, status.avail_phys // 2**20
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        return values["MemTotal"] // 1024, values.get("MemAvailable", values["MemFree"]) // 1024
    except (OSError, KeyError, ValueError):
        return 0, 0


def _nvidia() -> tuple[list[GPUInfo], str]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return [], "nvidia-smi not found; NVIDIA GPU/driver unavailable or not installed"
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=8,
        )
        gpus = []
        for line in result.stdout.splitlines():
            name, total, free, driver = (part.strip() for part in line.split(",", 3))
            gpus.append(GPUInfo(name, int(float(total)), int(float(free)), driver))
        return gpus, "ok" if gpus else "nvidia-smi returned no GPUs"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return [], f"NVIDIA probe failed: {exc}"


def _native_runtime_available(root: str | Path | None = None) -> bool:
    """Whether a native llama-server is resolvable or downloadable here.

    Imported lazily: `runtime_native` reaches back into `lifecycle`, which
    imports this module.
    """
    try:
        from . import runtime_native

        return bool(runtime_native.available(root=root)[0])
    except (ImportError, OSError, RuntimeError, ValueError):
        return False


def _native_runtime_present(root: str | Path | None = None) -> bool:
    """Whether a llama-server resolves *right now* — bundled, managed, or on PATH.

    `native_runtime_available` is true wherever the pinned release could be
    downloaded, so it cannot tell a bundled runtime from a missing one. This
    answers the narrower question the desktop bundle needs. Boolean only: the
    resolved path is node-private and never leaves this function.

    Resolution uses the node's own LLM root; `detect_hardware`'s `path` is a
    disk-probe target (a model directory, for one caller), not that root.
    """
    try:
        from . import runtime_native

        return runtime_native.resolve_server(root=root) is not None
    except (ImportError, OSError, RuntimeError, ValueError):
        return False


def detect_hardware(path: str | Path | None = None, *, runtime_root: str | Path | None = None) -> HardwareReport:
    ram_total, ram_available = _memory()
    disk_target = Path(path or Path.cwd()).expanduser().resolve()
    while not disk_target.exists() and disk_target.parent != disk_target:
        disk_target = disk_target.parent
    disk_free = shutil.disk_usage(disk_target).free // 2**20
    gpus, gpu_status = _nvidia()
    docker = shutil.which("docker")
    container_ok = False
    if docker:
        try:
            subprocess.run([docker, "info", "--format", "{{json .ServerVersion}}"], check=True,
                           capture_output=True, timeout=8)
            container_ok = True
        except (OSError, subprocess.SubprocessError):
            pass
    warnings = []
    if not ram_total:
        warnings.append("Could not determine total RAM; automatic managed installation is disabled.")
    if not gpus:
        warnings.append("No usable NVIDIA GPU was detected; CPU inference or an existing local API remains available.")
    if not container_ok:
        warnings.append(
            "A running Docker engine was not detected; the bundled native runtime or an existing "
            "local API remains available."
        )
    return HardwareReport(
        os=platform.system(), architecture=platform.machine(), cpu=platform.processor() or "unknown",
        logical_cpus=os.cpu_count() or 1, ram_total_mb=ram_total, ram_available_mb=ram_available,
        disk_free_mb=disk_free, nvidia_gpus=gpus, nvidia_probe=gpu_status,
        container_runtime="docker" if docker else "", container_available=container_ok,
        native_runtime_available=_native_runtime_available(runtime_root),
        native_runtime_present=_native_runtime_present(runtime_root), warnings=warnings,
    )


def usable_memory_mb(report: HardwareReport) -> int:
    """Conservative available memory in MiB — the same cap `recommend` checks."""
    usable_ram = int(report.ram_available_mb * 0.75)
    usable_vram = max((gpu.memory_free_mb for gpu in report.nvidia_gpus), default=0)
    return max(usable_ram, int(usable_vram * 0.85))


def recommend(report: HardwareReport) -> list[dict[str, Any]]:
    """Return only catalog profiles that fit conservative available-memory/disk caps.

    Profiles are pinned in `catalog.PROFILES`, smallest first. The largest
    fitting profile is flagged `"recommended": True` so a setup wizard can
    highlight one option; every other fitting profile gets `False`.
    """
    usable_memory = usable_memory_mb(report)
    recommendations: list[dict[str, Any]] = []
    for profile in catalog.PROFILES:
        disk_need = catalog.estimated_disk_mb(profile)
        if profile.estimated_memory_mb <= usable_memory and disk_need <= report.disk_free_mb:
            recommendations.append({
                "profile": profile.profile,
                # Kept as the display string (not the internal alias) so
                # existing /api/local/llm/hardware consumers are unchanged.
                "model_alias": profile.display_name,
                "display_name": profile.display_name,
                "parameter_millions": profile.parameter_millions,
                "quantization": profile.quantization,
                "estimated_memory_mb": profile.estimated_memory_mb,
                "estimated_disk_mb": disk_need,
                "download_bytes": profile.size_bytes,
                "source_url": profile.url,
                "license_id": profile.license_id,
                "license_notice": profile.license_notice,
                "license_url": "https://www.apache.org/licenses/LICENSE-2.0" if profile.license_id == "Apache-2.0" else "",
                "context_window": profile.context_window,
                "max_concurrent": profile.max_concurrent,
                # The managed llama.cpp image is the portable CPU build. GPU
                # data is still reported so an advanced owner can override it,
                # but the safe default must describe what we actually launch.
                "device": "cpu",
                "can_run": True,
                "recommended": False,
            })
    if not recommendations:
        recommendations.append({
            "can_run": False, "reason": "No bundled profile safely fits detected available RAM/disk.",
            "alternatives": ["Connect an existing loopback OpenAI-compatible service", "Free memory/disk and retry"],
        })
    else:
        recommendations[-1]["recommended"] = True
    return recommendations


def report_json(path: str | Path | None = None) -> str:
    report = detect_hardware(path)
    return json.dumps({"hardware": report.to_dict(), "recommendations": recommend(report)}, indent=2)
