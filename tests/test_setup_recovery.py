from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package import routes, setup_recovery
from rynmesh.llm_package.errors import LifecycleError
from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


@pytest.fixture
def previous_api():
    state = {"healthy": True, "checks": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state["checks"] += 1
            body = json.dumps({"data": [{"id": "previous-model"}]}).encode()
            self.send_response(200 if state["healthy"] else 503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def make_app(home, network):
    store = RynmeshStore(home=home, network_dir=network)
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    routes.install_llm_routes(
        app, store=store, home=home, messaging_key=key,
        resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "",
    )
    return app


def terminal(client):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get("/api/local/llm/setup/status").json()
        if job["state"] in {"failed", "cancelled", "succeeded"}:
            return job
        time.sleep(0.01)
    pytest.fail("setup did not reach a terminal state")


@pytest.mark.parametrize("outcome,resume", [
    ("restored", "none"), ("unavailable", "none"), ("failed", "restart"), ("failed", "retry"),
])
def test_cancel_reports_actual_recovery_and_keeps_failed_snapshot(tmp_path, monkeypatch, previous_api, outcome, resume):
    home = tmp_path / "node"
    api, url = previous_api
    manifest = home / "llm" / "previous.json"
    save_manifest(LLMPackageManifest(
        package_id="previous", mode="openai_compatible", public_model_alias="previous",
        base_url=url, model="previous-model", install_source={"extension": "PRIVATE-RECOVERY-MARKER"},
    ), manifest)
    original = manifest.read_bytes()
    settings = home / "llm" / "provider-settings.json"
    settings.write_text(json.dumps({"manifest": str(manifest), "publication_enabled": False,
                                    "extension": {"preserve": True}}), encoding="utf-8")
    original_settings = settings.read_bytes()
    started, reported, release = threading.Event(), threading.Event(), threading.Event()
    checking_health, finish_health = threading.Event(), threading.Event()
    start_runtime = routes.start_runtime

    def delayed_health(path):
        checking_health.set()
        assert finish_health.wait(timeout=5)
        return start_runtime(path)

    monkeypatch.setattr(routes, "start_runtime", delayed_health)

    def cancelled_install(**kwargs):
        manifest.write_bytes(b"incomplete replacement")
        started.set()
        while not kwargs["cancel_check"]():
            time.sleep(0.01)
        kwargs["progress"]("download_model", 55, "Late download progress")
        reported.set()
        assert release.wait(timeout=5)
        raise LifecycleError("setup cancelled")

    monkeypatch.setattr(routes, "connect_local_api", cancelled_install)
    writer = setup_recovery.atomic_write_bytes

    def fail_restore(path, data, **kwargs):
        if path == manifest and data == original and outcome == "failed":
            raise PermissionError("PRIVATE-RECOVERY-MARKER")
        return writer(path, data, **kwargs)

    monkeypatch.setattr(setup_recovery, "atomic_write_bytes", fail_restore)
    app = make_app(home, tmp_path / "network")
    with TestClient(app) as client:
        assert client.get("/api/local/llm/service/status").json()["configured"] is True
        job = client.post("/api/local/llm/setup/async", json={"mode": "openai-compatible"}).json()
        try:
            assert started.wait(timeout=3)
            assert client.post("/api/local/llm/setup", json={"mode": "openai-compatible"}).status_code == 409
            assert client.get("/api/local/llm/service/status").json()["configured"] is False
            client.post(f"/api/local/llm/setup/{job['job_id']}/cancel")
            assert reported.wait(timeout=3)
            assert client.get("/api/local/llm/setup/status").json()["state"] == "cancelling"
            api["healthy"] = outcome != "unavailable"
        finally:
            release.set()
        try:
            if outcome != "failed":
                assert checking_health.wait(timeout=3)
                # The snapshot has already been restored on disk, but its
                # cached adapter must stay hidden until recovery finishes.
                assert client.get("/api/local/llm/service/status").json()["configured"] is False
        finally:
            finish_health.set()
        result = terminal(client)
    assert result["recovery_state"] == outcome
    assert result["state"] == ("cancelled" if outcome == "restored" else "failed")
    assert result["retryable"] is True
    assert "PRIVATE-RECOVERY-MARKER" not in json.dumps(result)
    assert str(home) not in json.dumps(result)
    if outcome == "failed":
        assert (home / "llm" / "setup-recovery.json").exists()
        assert "could not be restored" in result["message"]
        monkeypatch.setattr(setup_recovery, "atomic_write_bytes", writer)
        finish_health.set()
        # Recreate the routes as a restarted node; its durable snapshot must
        # restore the original bytes, not capture the incomplete replacement.
        if resume == "restart":
            with TestClient(make_app(home, tmp_path / "network")) as restarted:
                status = restarted.get("/api/local/llm/setup/status").json()
                assert status["recovery_state"] == "restored_unchecked"
        else:
            def retry_install(**_kwargs):
                assert manifest.read_bytes() == original
                raise LifecycleError("The new source is unavailable")

            monkeypatch.setattr(routes, "connect_local_api", retry_install)
            with TestClient(app) as retrying:
                assert retrying.post("/api/local/llm/setup/async", json={"mode": "openai-compatible"}).status_code == 200
                assert terminal(retrying)["recovery_state"] == "restored"
    else:
        assert api["checks"] > 0
    assert manifest.read_bytes() == original
    assert settings.read_bytes() == original_settings


def test_completed_recovery_receipt_cannot_restore_old_bytes(tmp_path, monkeypatch):
    recovery = setup_recovery.SetupRecovery(tmp_path)
    manifest = tmp_path / "previous.json"
    original = b'{"future-extension": {"keep": true}}\n'
    manifest.write_bytes(original)
    recovery.capture(str(manifest))
    manifest.write_bytes(b"replacement")
    assert recovery.restore() == "restored"
    assert manifest.read_bytes() == original
    manifest.write_bytes(b"later successful configuration")
    assert setup_recovery.SetupRecovery(tmp_path).restore() == "absent"
    assert manifest.read_bytes() == b"later successful configuration"


@pytest.mark.parametrize("damage", ["version", "digest", "unknown"])
def test_bad_recovery_record_preserves_files_and_fails_closed(tmp_path, damage):
    recovery = setup_recovery.SetupRecovery(tmp_path)
    manifest = tmp_path / "previous.json"
    manifest.write_bytes(b'{"model": "original"}')
    recovery.capture(str(manifest))
    record = json.loads(recovery.path.read_bytes())
    if damage == "version":
        record["version"] = 2
    elif damage == "digest":
        record["manifest"]["sha256"] = "0" * 64
    else:
        record["future_extension"] = True
    raw = json.dumps(record).encode()
    recovery.path.write_bytes(raw)
    manifest.write_bytes(b"newer bytes")
    with pytest.raises(setup_recovery.SetupRecoveryError):
        setup_recovery.SetupRecovery(tmp_path).restore()
    assert manifest.read_bytes() == b"newer bytes"
    assert recovery.path.read_bytes() == raw


def test_recovery_capacity_and_failed_cleanup_keep_safe_receipt(tmp_path, monkeypatch):
    recovery = setup_recovery.SetupRecovery(tmp_path)
    manifest = tmp_path / "previous.json"
    original = b" " * setup_recovery.MAX_CONFIG_BYTES
    manifest.write_bytes(original)
    recovery.settings.parent.mkdir(parents=True)
    recovery.settings.write_bytes(original)
    recovery.capture(str(manifest))
    assert recovery.path.stat().st_size < setup_recovery.MAX_JOURNAL_BYTES
    assert recovery.restore() == "restored"
    assert manifest.read_bytes() == original
    assert recovery.settings.read_bytes() == original
    manifest.write_bytes(original + b"x")
    with pytest.raises(LifecycleError, match="setup has not started"):
        recovery.capture(str(manifest))
    assert not recovery.path.exists()
    manifest.write_bytes(b"original")
    recovery.capture(str(manifest))
    unlink = type(recovery.path).unlink

    def held_receipt(path, **kwargs):
        if path == recovery.path:
            raise PermissionError("receipt held")
        return unlink(path, **kwargs)

    monkeypatch.setattr(type(recovery.path), "unlink", held_receipt)
    assert recovery.restore() == "restored"
    assert json.loads(recovery.path.read_bytes()) == {"version": 1, "state": "committed"}
    manifest.write_bytes(b"new configuration")
    assert setup_recovery.SetupRecovery(tmp_path).restore() == "absent"
    assert manifest.read_bytes() == b"new configuration"


def test_managed_retry_choices_survive_failure_and_restart_without_private_inputs(tmp_path, monkeypatch):
    home = tmp_path / "node"

    def interrupted_install(**kwargs):
        kwargs["progress"]("download_model", 20, "Downloading model data; verification pending")
        raise LifecycleError("download incomplete; retry to resume")

    monkeypatch.setattr(routes, "install_managed", interrupted_install)
    choices = {"mode": "managed", "profile": "light", "package_id": "resume-model", "port": 18925}
    with TestClient(make_app(home, tmp_path / "network")) as client:
        queued = client.post("/api/local/llm/setup/async", json={
            **choices, "accept_risk": True, "base_url": "PRIVATE-URL-MARKER",
            "model_path": "PRIVATE-PATH-MARKER", "api_key_env": "PRIVATE-KEY-MARKER",
        }).json()
        assert queued["resume_configuration"] == choices
        completed = terminal(client)
        assert completed["resume_configuration"] == choices
    with TestClient(make_app(home, tmp_path / "network")) as restarted:
        assert restarted.get("/api/local/llm/setup/status").json()["resume_configuration"] == choices
    raw = (home / "llm/setup-job.json").read_text(encoding="utf-8")
    assert "PRIVATE-" not in raw and "accept_risk" not in raw


@pytest.mark.parametrize("changes", [
    {"profile": "auto"}, {"profile": {}}, {"port": True}, {"port": 70000},
    {"mode": "openai-compatible"}, {"package_id": "../private"}, {"runtime": "docker"},
])
def test_unreviewable_or_non_ui_configuration_is_not_used_as_managed_retry(changes):
    body = {"mode": "managed", "profile": "light", "package_id": "local-small", "port": 18080, **changes}
    assert setup_recovery.managed_resume_configuration(body) is None
