"""Tests for the Docker runtime backend and the lifecycle runtime seam."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rynmesh.llm_package.lifecycle as llm_lifecycle
import rynmesh.llm_package.runtime_docker as llm_runtime_docker
import rynmesh.llm_package.runtime_native as llm_runtime_native
from rynmesh.llm_package.lifecycle import LifecycleError
from rynmesh.llm_package.manifest import (
    LLMPackageManifest,
    fingerprint_file,
    save_manifest,
)


def test_available_reports_reason_without_docker_on_path(monkeypatch):
    monkeypatch.setattr(llm_runtime_docker.shutil, "which", lambda _name: None)
    ok, reason = llm_runtime_docker.available()
    assert ok is False
    assert reason
    assert "/" not in reason
    assert "\\" not in reason


def test_backend_raises_for_unknown_runtime():
    manifest = LLMPackageManifest(
        package_id="mystery", mode="managed", public_model_alias="mystery",
        runtime="some_future_runtime",
    )
    with pytest.raises(LifecycleError, match="unsupported runtime"):
        llm_lifecycle._backend(manifest)


def test_backend_resolves_docker_runtime():
    manifest = LLMPackageManifest(
        package_id="docker-one", mode="managed", public_model_alias="docker-one",
        runtime="docker_llama_cpp",
    )
    assert llm_lifecycle._backend(manifest) is llm_runtime_docker


def test_stop_on_external_manifest_never_touches_docker(monkeypatch, tmp_path):
    manifest = LLMPackageManifest(
        package_id="ext", mode="openai_compatible", public_model_alias="ext",
        runtime="external", base_url="http://127.0.0.1:18080",
    )
    manifest_path = tmp_path / "manifest.json"
    save_manifest(manifest, manifest_path)

    def _boom():
        raise AssertionError("Docker must not be touched for an external manifest")

    monkeypatch.setattr(llm_runtime_docker, "_docker", _boom)
    result = llm_lifecycle.stop(manifest_path)
    assert result == {
        "managed": False, "stopped": False, "message": "external service is owner-managed",
    }


def _docker_manifest(tmp_path: Path) -> LLMPackageManifest:
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF" + bytes(96))
    return LLMPackageManifest(
        package_id="docker-hardening", mode="managed", public_model_alias="docker-alias",
        runtime=llm_runtime_docker.RUNTIME_ID, model="docker-alias",
        model_path=str(model), checksum=fingerprint_file(model),
        base_url="http://127.0.0.1:18080", context_window=2048, max_concurrent=1,
    )


def test_the_container_closes_the_same_two_llama_cpp_defaults_as_the_native_runtime(tmp_path,
                                                                                    monkeypatch):
    """Published loopback-only is not enough: a visited page could still drive it."""
    manifest = _docker_manifest(tmp_path)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(llm_runtime_docker, "_docker", lambda: "docker")
    monkeypatch.setattr(llm_runtime_docker.subprocess, "run", fake_run)
    llm_runtime_docker.start(manifest)

    run_command = commands[-1]
    key = manifest.runtime_api_key
    assert len(key) >= 40
    assert run_command[run_command.index("--api-key") + 1] == key
    assert run_command[run_command.index("--cors-origins") + 1] == llm_runtime_native.CORS_ORIGINS
    assert run_command[run_command.index("-lv") + 1] == llm_runtime_native.LOG_VERBOSITY
    # Appended after the server's own capacity flags, not among the docker ones.
    assert run_command.index("--api-key") > run_command.index("-np")
    assert key not in json.dumps(manifest.public_dict())


def test_uninstall_reports_a_process_for_native_and_a_container_for_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_runtime_docker, "remove", lambda _manifest: None)
    monkeypatch.setattr(llm_runtime_native, "remove", lambda _manifest: None)

    docker_path = tmp_path / "docker" / "manifest.json"
    save_manifest(_docker_manifest(tmp_path), docker_path)
    assert llm_lifecycle.uninstall(docker_path)["removed"] == ["runtime_container"]

    native = _docker_manifest(tmp_path)
    native.package_id = "native-hardening"
    native.runtime = llm_runtime_native.RUNTIME_ID
    native_path = tmp_path / "native" / "manifest.json"
    save_manifest(native, native_path)
    assert llm_lifecycle.uninstall(native_path)["removed"] == ["runtime_process"]
