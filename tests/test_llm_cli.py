"""CLI flag plumbing for `rynmesh-llm setup` (issue #34 native runtime)."""

from __future__ import annotations

import json

import rynmesh.llm_package.cli as llm_cli


def test_setup_managed_passes_profile_and_runtime_to_install_managed(monkeypatch, capsys):
    captured: dict[str, object] = {}

    def fake_install_managed(**kwargs):
        captured.update(kwargs)
        return {"manifest": "irrelevant"}

    monkeypatch.setattr(llm_cli, "install_managed", fake_install_managed)

    rc = llm_cli.main([
        "setup", "--mode", "managed", "--profile", "balanced", "--runtime", "native", "--yes",
    ])

    assert rc == 0
    assert captured["profile"] == "balanced"
    assert captured["runtime"] == "native"
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"manifest": "irrelevant"}


def test_setup_managed_defaults_profile_and_runtime_to_auto(monkeypatch, capsys):
    captured: dict[str, object] = {}

    def fake_install_managed(**kwargs):
        captured.update(kwargs)
        return {"manifest": "irrelevant"}

    monkeypatch.setattr(llm_cli, "install_managed", fake_install_managed)

    rc = llm_cli.main(["setup", "--mode", "managed", "--yes"])

    assert rc == 0
    assert captured["profile"] == "auto"
    assert captured["runtime"] == "auto"


def test_setup_import_gguf_passes_runtime_to_import_gguf(monkeypatch, tmp_path, capsys):
    captured: dict[str, object] = {}

    def fake_import_gguf(**kwargs):
        captured.update(kwargs)
        return {"manifest": "irrelevant"}

    monkeypatch.setattr(llm_cli, "import_gguf", fake_import_gguf)
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"not a real gguf")

    rc = llm_cli.main([
        "setup", "--mode", "import-gguf", "--model-path", str(model_path), "--runtime", "docker", "--yes",
    ])

    assert rc == 0
    assert captured["runtime"] == "docker"
