"""Tests for cross-platform hardware detection, focused on macOS memory."""

from __future__ import annotations

import subprocess

import rynmesh.llm_package.catalog as llm_catalog
import rynmesh.llm_package.hardware as llm_hardware
import rynmesh.llm_package.runtime_native as llm_runtime_native
import rynmesh.llm_package.runtime_native_install as llm_runtime_install
from rynmesh.llm_package.hardware import GPUInfo, HardwareReport

VM_STAT_SAMPLE_16K_PAGES = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              10000.
Pages active:                            50000.
Pages inactive:                           5000.
Pages speculative:                         500.
Pages throttled:                             0.
Pages wired down:                        20000.
Pages purgeable:                           100.
"Translation faults":                 12345678.
Pages copy-on-write:                    123456.
Pages zero filled:                     1234567.
Pages reactivated:                        1000.
Pages purged:                              500.
File-backed pages:                        3000.
Anonymous pages:                          4000.
Pages stored in compressor:               2000.
Pages occupied by compressor:             1000.
Decompressions:                            300.
Compressions:                              600.
Pageins:                                 70000.
Pageouts:                                    0.
Swapins:                                     0.
Swapouts:                                    0.
"""


def test_parse_vm_stat_uses_reported_page_size():
    # (10000 + 5000 + 500) pages * 16384 bytes / 2**20 MiB-per-byte-divisor
    expected_mb = (10000 + 5000 + 500) * 16384 // 2**20
    assert llm_hardware._parse_vm_stat(VM_STAT_SAMPLE_16K_PAGES) == expected_mb


def test_parse_vm_stat_falls_back_to_default_page_size_when_header_missing():
    text = "Pages free:                              256.\nPages inactive:                            0.\n"
    assert llm_hardware._parse_vm_stat(text, page_size_default=4096) == (256 * 4096) // 2**20


def test_darwin_memory_branch_reads_sysctl_and_vm_stat(monkeypatch):
    monkeypatch.setattr(llm_hardware.platform, "system", lambda: "Darwin")

    def fake_run(command, **_kwargs):
        if command[:2] == ["sysctl", "-n"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(16 * 2**30) + "\n", stderr="")
        if command == ["vm_stat"]:
            return subprocess.CompletedProcess(command, 0, stdout=VM_STAT_SAMPLE_16K_PAGES, stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(llm_hardware.subprocess, "run", fake_run)
    total_mb, available_mb = llm_hardware._memory()
    assert total_mb == 16 * 1024
    assert available_mb == (10000 + 5000 + 500) * 16384 // 2**20


def test_darwin_memory_branch_falls_back_to_zero_on_failure(monkeypatch):
    monkeypatch.setattr(llm_hardware.platform, "system", lambda: "Darwin")

    def fake_run(_command, **_kwargs):
        raise FileNotFoundError("sysctl not found")

    monkeypatch.setattr(llm_hardware.subprocess, "run", fake_run)
    assert llm_hardware._memory() == (0, 0)


def test_parse_vm_stat_tolerates_malformed_lines_without_raising():
    # A label line missing its colon, and a label line with a non-numeric
    # page count, must both be skipped rather than raising IndexError/ValueError.
    text = "Pages free\nPages inactive:   10.\nPages speculative:   not-a-number.\n"
    # Only "Pages inactive: 10" is parseable; default page size is 4096.
    assert llm_hardware._parse_vm_stat(text) == (10 * 4096) // 2**20


def test_darwin_memory_branch_returns_zero_pair_on_garbage_vm_stat_output(monkeypatch):
    monkeypatch.setattr(llm_hardware.platform, "system", lambda: "Darwin")

    def fake_run(command, **_kwargs):
        if command[:2] == ["sysctl", "-n"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(16 * 2**30) + "\n", stderr="")
        if command == ["vm_stat"]:
            return subprocess.CompletedProcess(command, 0, stdout="not vm_stat output at all\n\xff\xfe", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(llm_hardware.subprocess, "run", fake_run)
    total_mb, available_mb = llm_hardware._memory()
    # _parse_vm_stat must not raise on garbage input; with no parseable page
    # lines, available memory comes back as 0 while total is still read.
    assert total_mb == 16 * 1024
    assert available_mb == 0


def test_docker_warning_names_native_runtime_and_local_api(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_hardware.shutil, "which", lambda _name: None)
    report = llm_hardware.detect_hardware(tmp_path)
    assert any(
        "the bundled native runtime or an existing local API remains available" in warning
        for warning in report.warnings
    )


def test_native_runtime_present_needs_a_resolvable_server(monkeypatch, tmp_path):
    """`available` is true wherever the pin exists; `present` needs a real server.

    The desktop bundle check depends on that difference: without it a build with
    an empty resources/llama would still report the runtime as usable.
    """
    monkeypatch.setenv("RYNMESH_LLM_HOME", str(tmp_path / "llm"))
    monkeypatch.delenv("RYNMESH_LLAMA_SERVER", raising=False)
    monkeypatch.delenv("RYNMESH_LLAMA_DIR", raising=False)
    monkeypatch.setattr(llm_runtime_native.shutil, "which", lambda _name: None)

    nothing_installed = llm_hardware.detect_hardware(tmp_path)
    assert nothing_installed.native_runtime_present is False
    assert nothing_installed.native_runtime_available is True  # still downloadable

    bundled = tmp_path / "resources" / "llama"
    bundled.mkdir(parents=True)
    server = bundled / llm_runtime_install.server_filename()
    server.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    server.chmod(0o755)
    monkeypatch.setenv("RYNMESH_LLAMA_DIR", str(bundled))

    bundled_report = llm_hardware.detect_hardware(tmp_path)
    assert bundled_report.native_runtime_present is True
    assert bundled_report.to_dict()["native_runtime_present"] is True


def test_runtime_presence_uses_explicit_node_root_not_disk_probe(monkeypatch, tmp_path):
    monkeypatch.setenv("RYNMESH_LLM_HOME", str(tmp_path / "default-llm"))
    monkeypatch.delenv("RYNMESH_LLAMA_SERVER", raising=False)
    monkeypatch.delenv("RYNMESH_LLAMA_DIR", raising=False)
    monkeypatch.setattr(llm_runtime_native.shutil, "which", lambda _name: None)
    node_root = tmp_path / "separate-node" / "llm"
    managed = llm_runtime_native.managed_root(node_root)
    managed.mkdir(parents=True)
    server = managed / llm_runtime_install.server_filename()
    server.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    server.chmod(0o755)
    assert not llm_hardware.detect_hardware(tmp_path).native_runtime_present
    assert not llm_hardware.detect_hardware(tmp_path, runtime_root=node_root).native_runtime_present
    llm_runtime_install._write_marker(managed, server, "0" * 64)
    assert llm_hardware.detect_hardware(tmp_path, runtime_root=node_root).native_runtime_present
    assert not llm_hardware.detect_hardware(node_root).native_runtime_present


def _report(*, ram_available_mb: int, disk_free_mb: int) -> HardwareReport:
    return HardwareReport(
        os="Linux", architecture="x86_64", cpu="test-cpu", logical_cpus=8,
        ram_total_mb=ram_available_mb * 2, ram_available_mb=ram_available_mb,
        disk_free_mb=disk_free_mb, nvidia_gpus=[], nvidia_probe="nvidia-smi not found",
        container_runtime="", container_available=False, native_runtime_available=True,
        native_runtime_present=False, warnings=[],
    )


EXPECTED_KEYS = {
    "profile", "model_alias", "display_name", "parameter_millions", "quantization",
    "estimated_memory_mb", "estimated_disk_mb", "context_window", "max_concurrent",
    "device", "can_run", "recommended",
}


def test_recommend_marks_exactly_the_largest_fitting_profile_as_recommended():
    # Plenty of RAM/disk: every catalog profile fits.
    report = _report(ram_available_mb=64_000, disk_free_mb=64_000)
    recommendations = llm_hardware.recommend(report)
    assert len(recommendations) == len(llm_catalog.PROFILES)
    for entry in recommendations:
        assert EXPECTED_KEYS <= set(entry)
        assert entry["can_run"] is True
        assert entry["model_alias"] == entry["display_name"]
    recommended_flags = [entry["recommended"] for entry in recommendations]
    assert recommended_flags.count(True) == 1
    assert recommendations[-1]["recommended"] is True
    assert recommendations[-1]["profile"] == llm_catalog.PROFILES[-1].profile


def test_recommend_marks_the_only_fitting_profile_when_just_one_fits():
    light = llm_catalog.profile_by_name("light")
    disk_need = llm_catalog.estimated_disk_mb(light)
    # Enough for "light" (memory floor is ram_available_mb * 0.75) but not "balanced".
    ram_available = int(light.estimated_memory_mb / 0.75) + 10
    report = _report(ram_available_mb=ram_available, disk_free_mb=disk_need + 10)
    recommendations = llm_hardware.recommend(report)
    assert len(recommendations) == 1
    assert recommendations[0]["profile"] == "light"
    assert recommendations[0]["recommended"] is True


def test_recommend_returns_can_run_false_fallback_when_nothing_fits():
    report = _report(ram_available_mb=1, disk_free_mb=1)
    recommendations = llm_hardware.recommend(report)
    assert recommendations == [{
        "can_run": False,
        "reason": "No bundled profile safely fits detected available RAM/disk.",
        "alternatives": [
            "Connect an existing loopback OpenAI-compatible service",
            "Free memory/disk and retry",
        ],
    }]


def test_recommend_uses_gpu_memory_when_it_exceeds_cpu_ram_estimate():
    gpu = GPUInfo(name="Test GPU", memory_total_mb=48_000, memory_free_mb=40_000, driver_version="1")
    report = HardwareReport(
        os="Linux", architecture="x86_64", cpu="test-cpu", logical_cpus=8,
        ram_total_mb=4_000, ram_available_mb=1_000, disk_free_mb=64_000,
        nvidia_gpus=[gpu], nvidia_probe="ok", container_runtime="", container_available=False,
        native_runtime_available=True, native_runtime_present=False, warnings=[],
    )
    assert llm_hardware.usable_memory_mb(report) == int(40_000 * 0.85)
    recommendations = llm_hardware.recommend(report)
    assert len(recommendations) == len(llm_catalog.PROFILES)
