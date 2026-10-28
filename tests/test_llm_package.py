from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import rynmesh.llm_package.catalog as llm_catalog
import rynmesh.llm_package.https_only as llm_https_only
import rynmesh.llm_package.lifecycle as llm_lifecycle
import rynmesh.llm_package.model_download as llm_model_download
import rynmesh.llm_package.p2p as llm_p2p
import rynmesh.llm_package.routes as llm_routes
import rynmesh.llm_package.runtime_docker as llm_runtime_docker
import rynmesh.llm_package.runtime_native as llm_runtime_native
from rynmesh.crypto import SignatureError, sign_payload
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter, validate_local_url
from rynmesh.llm_package.lifecycle import LifecycleError, connect_local_api, validate_gguf
from rynmesh.llm_package.manifest import (
    LLMPackageManifest,
    ManifestError,
    Pricing,
    fingerprint_file,
    load_manifest,
)
from rynmesh.llm_package.p2p import (
    IceSignal,
    P2PError,
    apply_remote_signal,
    gather_signal,
    new_connection,
    receive_json,
    selected_pair,
    send_json,
    validate_distinct_public_egress,
)
from rynmesh.llm_package.routes import (
    ProviderService,
    _delivery_error_code,
    _open_provider_response,
    _recover_consumer_orders,
    install_llm_routes,
)
from rynmesh.llm_package.task_balance import TaskBalanceError, TaskBalanceLedger
from rynmesh.llm_package.task_protocol import (
    TaskOrderStore,
    TaskProtocolError,
    open_task,
    seal_task,
)
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


class _OpenAIHandler(BaseHTTPRequestHandler):
    calls = 0
    last_messages = []

    def do_GET(self):
        if self.path == "/v1/models":
            self._send({"object": "list", "data": [{"id": "test-real-api"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        type(self).calls += 1
        type(self).last_messages = body.get("messages", [])
        if body["stream"] is True:
            self._send({"choices": [{"delta": {"content": "stream supported"}}]})
            return
        self._send({
            "choices": [{"message": {"content": "test adapter completion"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        })

    def log_message(self, *_args):
        pass

    def _send(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def openai_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_openai_compatible_health_and_real_request(openai_server):
    adapter = OpenAICompatibleAdapter(base_url=openai_server, model="test-real-api")
    assert adapter.health()["ok"] is True
    result = adapter.infer(prompt="private prompt", max_tokens=8, task_id="t1", timeout_s=5)
    assert result["text"] == "test adapter completion"
    assert result["input_tokens"] == 4 and result["output_tokens"] == 3
    assert adapter.metrics()["requests"] == 1
    assert adapter.capabilities()["streaming"] is False


def test_openai_adapter_blocks_non_loopback_by_default():
    with pytest.raises(AdapterError, match="non-loopback"):
        validate_local_url("http://192.0.2.10:8080")
    assert validate_local_url("http://192.0.2.10:8080", allow_non_loopback=True)


def test_lifecycle_rejects_package_path_traversal_before_writing(tmp_path, openai_server):
    with pytest.raises(LifecycleError, match="lowercase slug"):
        connect_local_api(
            base_url=openai_server, package_id="../../outside", alias="safe-alias",
            root=tmp_path / "llm",
        )
    assert not (tmp_path / "outside").exists()

    with pytest.raises(LifecycleError, match="environment-variable name"):
        connect_local_api(
            base_url=openai_server, package_id="safe-package", alias="safe-alias",
            api_key_env="secret value pasted here", root=tmp_path / "llm",
        )


def test_managed_runtime_and_model_are_immutably_pinned():
    assert "@sha256:" in llm_runtime_docker.DEFAULT_IMAGE
    assert "/resolve/main/" not in llm_lifecycle.DEFAULT_MODEL_URL
    assert llm_lifecycle.DEFAULT_MODEL_REVISION in llm_lifecycle.DEFAULT_MODEL_URL
    assert len(llm_lifecycle.DEFAULT_MODEL_SHA256) == 64
    manifest = LLMPackageManifest(
        package_id="unsafe", mode="managed", public_model_alias="unsafe",
        install_source={"runtime_image": "example.invalid/runtime:latest"},
    )
    with pytest.raises(LifecycleError, match="pinned by SHA-256"):
        llm_runtime_docker._pinned_runtime_image(manifest)
    manifest.install_source["runtime_image"] = "ghcr.io/ggml-org/llama.cpp:server"
    assert llm_runtime_docker._pinned_runtime_image(manifest) == llm_runtime_docker.DEFAULT_IMAGE


# --------------------------------------------------------------------------
# Resumable model download (_download, now implemented in model_download.py
# and re-exported by lifecycle.py under its historical name)
# --------------------------------------------------------------------------

class _ChunkedResponse:
    """A `urlopen` response stand-in that trickles bytes out in small reads.

    A real socket rarely hands back a whole multi-KB body from one
    `.read(n)` call; returning small pieces regardless of the requested size
    exercises the same multi-iteration loop `_download` runs against a real
    connection, and lets a test cancel partway through.
    """

    def __init__(self, payload: bytes, *, status: int, chunk_size: int = 512,
                content_range: str | None = None) -> None:
        self._payload = payload
        self._pos = 0
        self._chunk_size = chunk_size
        self.status = status
        self.headers = {"content-length": str(len(payload))}
        if content_range is not None:
            self.headers["Content-Range"] = content_range

    def read(self, _size: int = -1) -> bytes:
        end = min(len(self._payload), self._pos + self._chunk_size)
        chunk = self._payload[self._pos:end]
        self._pos = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fake_urlopen(body: bytes, *, honor_range: bool):
    """Build a `urlopen` replacement serving `body`, honoring `Range` or not."""

    def urlopen(request, timeout=0):
        range_header = request.get_header("Range")
        if range_header and honor_range:
            offset = int(range_header.split("=", 1)[1].split("-", 1)[0])
            if offset >= len(body):
                raise urllib.error.HTTPError(request.full_url, 416, "Range Not Satisfiable", {}, None)
            remainder = body[offset:]
            content_range = f"bytes {offset}-{len(body) - 1}/{len(body)}"
            return _ChunkedResponse(remainder, status=206, content_range=content_range)
        return _ChunkedResponse(body, status=200)

    return urlopen


DOWNLOAD_BODY = b"rynmesh-model-bytes-" * 160  # 3200 bytes
DOWNLOAD_SHA256 = hashlib.sha256(DOWNLOAD_BODY).hexdigest()
DOWNLOAD_URL = "https://huggingface.co/example/example/resolve/pin/example.gguf"


def test_download_requires_https():
    with pytest.raises(LifecycleError, match="HTTPS"):
        llm_lifecycle._download("http://example.invalid/model.gguf", Path("/tmp/x"), "0" * 64)


def test_download_resume_after_cancel_completes_via_206(tmp_path, monkeypatch):
    destination = tmp_path / "model.gguf"
    part = destination.with_suffix(destination.suffix + ".part")

    # 1. The first attempt is cancelled partway through: `.part` keeps the
    #    bytes written so far and the destination is never created.
    monkeypatch.setattr(llm_model_download, "_urlopen",
                        _fake_urlopen(DOWNLOAD_BODY, honor_range=True))
    calls = {"n": 0}

    def cancel_after_a_couple_chunks():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(LifecycleError, match="setup cancelled"):
        llm_lifecycle._download(DOWNLOAD_URL, destination, DOWNLOAD_SHA256,
                                cancel_check=cancel_after_a_couple_chunks)
    assert part.exists()
    partial_size = part.stat().st_size
    assert 0 < partial_size < len(DOWNLOAD_BODY)
    assert not destination.exists()

    # 2. The second attempt sends Range for the partial size, gets a 206,
    #    completes, and the verified part is renamed onto the destination.
    seen_ranges = []
    replay = _fake_urlopen(DOWNLOAD_BODY, honor_range=True)

    def recording_urlopen(request, timeout=0):
        seen_ranges.append(request.get_header("Range"))
        return replay(request, timeout=timeout)

    monkeypatch.setattr(llm_model_download, "_urlopen", recording_urlopen)
    actual = llm_lifecycle._download(DOWNLOAD_URL, destination, DOWNLOAD_SHA256)
    assert actual == DOWNLOAD_SHA256
    assert seen_ranges == [f"bytes={partial_size}-"]
    assert not part.exists()
    assert destination.read_bytes() == DOWNLOAD_BODY


def test_download_restarts_from_scratch_when_the_server_ignores_range(tmp_path, monkeypatch):
    destination = tmp_path / "model.gguf"
    part = destination.with_suffix(destination.suffix + ".part")
    part.write_bytes(b"stale bytes from an earlier attempt that do not match the real prefix")
    monkeypatch.setattr(llm_model_download, "_urlopen",
                        _fake_urlopen(DOWNLOAD_BODY, honor_range=False))
    actual = llm_lifecycle._download(DOWNLOAD_URL, destination, DOWNLOAD_SHA256)
    assert actual == DOWNLOAD_SHA256
    assert destination.read_bytes() == DOWNLOAD_BODY
    assert not part.exists()


def test_download_restarts_when_a_206_content_range_does_not_match_resume_point(tmp_path, monkeypatch):
    # A server/proxy that mislabels a full restart as 206 (Content-Range
    # starting at 0, not at the requested offset) must be treated as an
    # ordinary restart, not appended onto the existing partial bytes.
    destination = tmp_path / "model.gguf"
    part = destination.with_suffix(destination.suffix + ".part")
    part.write_bytes(b"stale-partial-bytes-not-a-real-prefix-of-the-body")

    def urlopen(request, timeout=0):
        assert request.get_header("Range")  # a resume was actually attempted
        return _ChunkedResponse(DOWNLOAD_BODY, status=206,
                                content_range=f"bytes 0-{len(DOWNLOAD_BODY) - 1}/{len(DOWNLOAD_BODY)}")

    monkeypatch.setattr(llm_model_download, "_urlopen", urlopen)
    actual = llm_lifecycle._download(DOWNLOAD_URL, destination, DOWNLOAD_SHA256)
    assert actual == DOWNLOAD_SHA256
    assert destination.read_bytes() == DOWNLOAD_BODY
    assert not part.exists()


def test_download_treats_a_416_as_an_already_complete_part(tmp_path, monkeypatch):
    destination = tmp_path / "model.gguf"
    part = destination.with_suffix(destination.suffix + ".part")
    part.write_bytes(DOWNLOAD_BODY)  # the whole file is already on disk

    def urlopen(request, timeout=0):
        assert request.get_header("Range") == f"bytes={len(DOWNLOAD_BODY)}-"
        raise urllib.error.HTTPError(request.full_url, 416, "Range Not Satisfiable", {}, None)

    monkeypatch.setattr(llm_model_download, "_urlopen", urlopen)
    actual = llm_lifecycle._download(DOWNLOAD_URL, destination, DOWNLOAD_SHA256)
    assert actual == DOWNLOAD_SHA256
    assert destination.read_bytes() == DOWNLOAD_BODY
    assert not part.exists()


def test_download_closes_the_416_response_before_discarding_it(tmp_path, monkeypatch):
    destination = tmp_path / "model.gguf"
    part = destination.with_suffix(destination.suffix + ".part")
    part.write_bytes(DOWNLOAD_BODY)
    closed = {"value": False}

    class _TrackedHTTPError(urllib.error.HTTPError):
        def close(self):
            closed["value"] = True
            super().close()

    def urlopen(request, timeout=0):
        raise _TrackedHTTPError(request.full_url, 416, "Range Not Satisfiable", {}, None)

    monkeypatch.setattr(llm_model_download, "_urlopen", urlopen)
    llm_lifecycle._download(DOWNLOAD_URL, destination, DOWNLOAD_SHA256)
    assert closed["value"] is True


def test_download_quarantines_a_checksum_mismatch_without_leaking_a_path(tmp_path, monkeypatch):
    destination = tmp_path / "model.gguf"
    part = destination.with_suffix(destination.suffix + ".part")
    corrupt = destination.with_suffix(destination.suffix + ".corrupt")
    monkeypatch.setattr(llm_model_download, "_urlopen",
                        _fake_urlopen(DOWNLOAD_BODY, honor_range=True))
    wrong_sha = "0" * 64
    with pytest.raises(LifecycleError) as failure:
        llm_lifecycle._download(DOWNLOAD_URL, destination, wrong_sha)
    message = str(failure.value)
    assert message == "model checksum mismatch; the download was quarantined and will restart"
    assert str(destination) not in message and str(tmp_path) not in message
    assert corrupt.exists()
    assert corrupt.read_bytes() == DOWNLOAD_BODY
    assert not part.exists()
    assert not destination.exists()


def test_download_size_guard_rejects_a_body_larger_than_pinned(tmp_path, monkeypatch):
    destination = tmp_path / "model.gguf"
    part = destination.with_suffix(destination.suffix + ".part")
    monkeypatch.setattr(llm_model_download, "_urlopen",
                        _fake_urlopen(DOWNLOAD_BODY, honor_range=True))
    with pytest.raises(LifecycleError, match="exceeded the pinned size"):
        llm_lifecycle._download(DOWNLOAD_URL, destination, DOWNLOAD_SHA256, size_bytes=1024)
    assert not part.exists()
    assert not destination.exists()


def test_download_keeps_the_part_on_a_network_error(tmp_path, monkeypatch):
    destination = tmp_path / "model.gguf"
    part = destination.with_suffix(destination.suffix + ".part")

    class _FlakyResponse(_ChunkedResponse):
        def read(self, _size: int = -1) -> bytes:
            if self._pos >= 1024:
                raise OSError("connection reset")
            return super().read(_size)

    monkeypatch.setattr(llm_model_download, "_urlopen",
                        lambda request, timeout=0: _FlakyResponse(DOWNLOAD_BODY, status=200))
    with pytest.raises(LifecycleError, match="download failed"):
        llm_lifecycle._download(DOWNLOAD_URL, destination, DOWNLOAD_SHA256)
    assert part.exists()
    assert 0 < part.stat().st_size < len(DOWNLOAD_BODY)


def test_download_network_error_message_never_includes_a_path(tmp_path, monkeypatch):
    destination = tmp_path / "model.gguf"
    part = destination.with_suffix(destination.suffix + ".part")
    secret_path = str(tmp_path / "secret-owner-file.gguf")

    class _PathLeakingResponse(_ChunkedResponse):
        def read(self, _size: int = -1) -> bytes:
            raise OSError(2, "No such file or directory", secret_path)

    monkeypatch.setattr(llm_model_download, "_urlopen",
                        lambda request, timeout=0: _PathLeakingResponse(DOWNLOAD_BODY, status=200))
    with pytest.raises(LifecycleError) as failure:
        llm_lifecycle._download(DOWNLOAD_URL, destination, DOWNLOAD_SHA256)
    message = str(failure.value)
    # `OSError(2, ...)` is auto-mapped to `FileNotFoundError` by Python, so the
    # exact type name varies — what matters is that only the type name (never
    # `str(exc)`, which would include the filename) reaches the message.
    assert message.startswith("download failed: ")
    assert "/" not in message and "\\" not in message
    assert secret_path not in message
    assert not destination.exists()
    assert part.exists()  # a network error must never discard the resumable part


def test_the_model_download_refuses_a_redirect_off_https(tmp_path, monkeypatch):
    """An HTTPS start is not enough: the whole redirect chain has to stay HTTPS."""
    destination = tmp_path / "model.gguf"
    plaintext = "http://mirror.invalid/model.gguf"
    followed: list[str] = []

    class _RecordingHandler(llm_https_only.HttpsOnlyRedirect):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            followed.append(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    handler = _RecordingHandler()
    with pytest.raises(LifecycleError) as refused:
        handler.redirect_request(
            urllib.request.Request(DOWNLOAD_URL), None, 302, "Found", {}, plaintext,
        )
    assert followed == [plaintext]
    message = str(refused.value)
    assert "non-HTTPS" in message
    assert plaintext not in message and "mirror.invalid" not in message
    assert "/" not in message and "\\" not in message
    assert not destination.exists()


def test_the_model_download_opens_through_the_https_only_opener(monkeypatch):
    """The redirect guard is only real if the download actually installs it."""
    seen: dict[str, object] = {}

    class _Opener:
        def open(self, request, timeout=0):
            return _ChunkedResponse(DOWNLOAD_BODY, status=200)

    def fake_build_opener(*handlers):
        seen["handlers"] = handlers
        return _Opener()

    monkeypatch.setattr(llm_https_only.urllib.request, "build_opener", fake_build_opener)
    llm_model_download._urlopen(urllib.request.Request(DOWNLOAD_URL))
    assert llm_https_only.HttpsOnlyRedirect in seen["handlers"]


# --------------------------------------------------------------------------
# install_managed with an explicit profile
# --------------------------------------------------------------------------

_FAKE_LLAMA_SERVER_BODY = """
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = sys.argv[1:]


def option(name, default=""):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else default


PORT = int(option("--port", "0"))
ALIAS = option("--alias", "fake-alias")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._send({"status": "ok"})
        elif self.path == "/v1/models":
            self._send({"object": "list", "data": [{"id": ALIAS}]})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        self.rfile.read(int(self.headers.get("content-length", "0")))
        self._send({
            "choices": [{"message": {"content": "RYNMESH SELF TEST OK"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 5},
        })

    def log_message(self, *_args):
        pass

    def _send(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
server.serve_forever()
"""


def _write_fake_llama_server(path: Path) -> Path:
    import sys

    path.write_text(f"#!{sys.executable}\n{_FAKE_LLAMA_SERVER_BODY}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _free_tcp_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.skipif(__import__("os").name == "nt", reason="the fake llama-server is a POSIX script")
def test_install_managed_with_an_explicit_profile_uses_the_catalog_entry(tmp_path, monkeypatch):
    import dataclasses

    root = tmp_path / "llm"
    server = _write_fake_llama_server(tmp_path / "llama-server")
    monkeypatch.setattr(llm_runtime_native, "resolve_server", lambda root=None: server)
    payload = b"GGUF" + bytes(96)

    def fake_download(_url, destination, expected_sha256, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return expected_sha256

    monkeypatch.setattr(llm_lifecycle, "_download", fake_download)
    # `_download` is faked to write a tiny stub GGUF rather than the real
    # multi-GB file, so the pinned catalog checksum (of the real bytes) is
    # swapped for the stub's actual digest — everything else about the
    # "balanced" profile (alias, context window, ...) stays the pinned value,
    # and `runtime_native.start`'s own checksum check still runs for real.
    real_balanced = llm_catalog.profile_by_name("balanced")
    stub_balanced = dataclasses.replace(real_balanced, sha256=hashlib.sha256(payload).hexdigest())
    original_profile_by_name = llm_catalog.profile_by_name
    monkeypatch.setattr(
        llm_lifecycle.catalog, "profile_by_name",
        lambda name: stub_balanced if name == "balanced" else original_profile_by_name(name),
    )
    result = llm_lifecycle.install_managed(
        package_id="profile-balanced", root=root, port=_free_tcp_port(),
        accept_risk=True, runtime="native", profile="balanced",
    )
    manifest = load_manifest(result["manifest"])
    try:
        assert manifest.public_model_alias == "rynmesh-qwen2.5-1.5b-q4"
        assert manifest.model == "rynmesh-qwen2.5-1.5b-q4"
        assert Path(manifest.model_path).name == "balanced.gguf"
        assert manifest.install_source["profile"] == "balanced"
        assert manifest.install_source["model_url"] == llm_catalog.profile_by_name("balanced").url
        assert manifest.context_window == llm_catalog.profile_by_name("balanced").context_window
        assert manifest.max_concurrent == llm_catalog.profile_by_name("balanced").max_concurrent
        assert result["self_test"]["ok"] is True
    finally:
        llm_runtime_native.stop(manifest)


@pytest.mark.skipif(__import__("os").name == "nt", reason="the fake llama-server is a POSIX script")
def test_install_managed_reuses_a_verified_legacy_model_gguf_without_downloading(tmp_path,
                                                                                monkeypatch):
    """A pre-profiles `model.gguf` that still matches is reused, not re-fetched."""
    root = tmp_path / "llm"
    server = _write_fake_llama_server(tmp_path / "llama-server")
    monkeypatch.setattr(llm_runtime_native, "resolve_server", lambda root=None: server)
    payload = b"GGUF" + bytes(96)
    digest = hashlib.sha256(payload).hexdigest()

    legacy = root / "models" / "legacy-reuse" / "model.gguf"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(payload)

    def refuse(*_args, **_kwargs):
        raise AssertionError("an already-verified model must never be downloaded again")

    monkeypatch.setattr(llm_lifecycle, "_download", refuse)
    stages: list[tuple[str, int, str]] = []
    result = llm_lifecycle.install_managed(
        package_id="legacy-reuse", root=root, port=_free_tcp_port(),
        model_url="https://huggingface.co/example/example/resolve/main/example.gguf",
        expected_sha256=digest, accept_risk=True, runtime="native",
        progress=lambda *event: stages.append(event),
    )
    manifest = load_manifest(result["manifest"])
    try:
        assert Path(manifest.model_path) == legacy
        assert manifest.checksum == fingerprint_file(legacy)
        assert legacy.read_bytes() == payload  # untouched on disk
        assert not (root / "models" / "legacy-reuse" / "custom.gguf").exists()
        assert ("download_model", 60, "Verified model already present") in stages
    finally:
        llm_runtime_native.stop(manifest)


@pytest.mark.skipif(__import__("os").name == "nt", reason="the fake llama-server is a POSIX script")
def test_install_managed_with_auto_profile_picks_the_recommended_catalog_entry(tmp_path, monkeypatch):
    import dataclasses

    root = tmp_path / "llm"
    server = _write_fake_llama_server(tmp_path / "llama-server")
    monkeypatch.setattr(llm_runtime_native, "resolve_server", lambda root=None: server)
    payload = b"GGUF" + bytes(96)

    def fake_download(_url, destination, expected_sha256, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return expected_sha256

    monkeypatch.setattr(llm_lifecycle, "_download", fake_download)

    # Fixed hardware: enough memory for "light" and "balanced" but not
    # "quality" (900 / 2300 / 4200 MB respectively), so "auto" deterministically
    # recommends "balanced" — the largest profile that still fits.
    fake_report = llm_lifecycle.HardwareReport(
        os="Linux", architecture="x86_64", cpu="test", logical_cpus=8,
        ram_total_mb=8000, ram_available_mb=4000, disk_free_mb=10_000, nvidia_gpus=[],
        nvidia_probe="nvidia-smi not found", container_runtime="", container_available=False,
        native_runtime_available=True, native_runtime_present=True, warnings=[],
    )
    monkeypatch.setattr(llm_lifecycle, "detect_hardware", lambda _base: fake_report)
    sanity_choices = llm_lifecycle.recommend(fake_report)
    assert next(c["profile"] for c in sanity_choices if c.get("recommended")) == "balanced"

    real_balanced = llm_catalog.profile_by_name("balanced")
    stub_balanced = dataclasses.replace(real_balanced, sha256=hashlib.sha256(payload).hexdigest())
    original_profile_by_name = llm_catalog.profile_by_name
    monkeypatch.setattr(
        llm_lifecycle.catalog, "profile_by_name",
        lambda name: stub_balanced if name == "balanced" else original_profile_by_name(name),
    )
    result = llm_lifecycle.install_managed(
        package_id="profile-auto", root=root, port=_free_tcp_port(),
        runtime="native", profile="auto",
    )
    manifest = load_manifest(result["manifest"])
    try:
        # The manifest records which concrete profile "auto" resolved to, not
        # the literal string "auto".
        assert manifest.install_source["profile"] == "balanced"
        assert manifest.public_model_alias == "rynmesh-qwen2.5-1.5b-q4"
        assert Path(manifest.model_path).name == "balanced.gguf"
        assert result["self_test"]["ok"] is True
    finally:
        llm_runtime_native.stop(manifest)


def test_resolve_install_profile_raises_a_lifecycle_error_when_recommend_marks_nothing(monkeypatch):
    # Defensive path: if `recommend()` ever returned a fitting list with no
    # entry flagged `recommended`, the old `next(...)` without a default
    # raised a bare StopIteration (a 500 through the HTTP route) instead of
    # a clean LifecycleError.
    choices = [{"can_run": True, "profile": "light"}]  # no "recommended" key set
    with pytest.raises(LifecycleError, match="no recommended profile was available"):
        llm_lifecycle._resolve_install_profile(
            profile="auto", model_url="", expected_sha256="", expected_size_bytes=None,
            accept_risk=False, report=object(), choices=choices,
        )


def test_resolve_install_profile_custom_override_derives_metadata_from_choices(monkeypatch):
    fitting_choice = {
        "can_run": True, "context_window": 4096, "max_concurrent": 2, "estimated_memory_mb": 900,
    }
    selected = llm_lifecycle._resolve_install_profile(
        profile="auto",
        model_url="https://huggingface.co/example/example/resolve/pin/example.gguf",
        expected_sha256="a" * 64, expected_size_bytes=123456,
        accept_risk=False, report=object(), choices=[fitting_choice],
    )
    assert selected["name"] == "custom"
    assert selected["size_bytes"] == 123456
    assert selected["context_window"] == 4096
    assert selected["max_concurrent"] == 2
    assert selected["estimated_memory_mb"] == 900
    assert selected["license_id"] == "unknown"


def test_resolve_install_profile_custom_override_respects_the_hardware_gate(monkeypatch):
    no_fit_choices = [{"can_run": False, "reason": "No bundled profile safely fits detected available RAM/disk."}]
    with pytest.raises(LifecycleError, match="No bundled profile safely fits"):
        llm_lifecycle._resolve_install_profile(
            profile="auto",
            model_url="https://huggingface.co/example/example/resolve/pin/example.gguf",
            expected_sha256="a" * 64, expected_size_bytes=None,
            accept_risk=False, report=object(), choices=no_fit_choices,
        )
    # With accept_risk it proceeds, falling back to the old conservative floor.
    selected = llm_lifecycle._resolve_install_profile(
        profile="auto",
        model_url="https://huggingface.co/example/example/resolve/pin/example.gguf",
        expected_sha256="a" * 64, expected_size_bytes=None,
        accept_risk=True, report=object(), choices=no_fit_choices,
    )
    assert selected["name"] == "custom"
    assert selected["context_window"] == 2048
    assert selected["max_concurrent"] == 1
    assert selected["estimated_memory_mb"] == 1024


def test_install_managed_custom_override_on_a_no_fit_hardware_report_raises_without_downloading(tmp_path, monkeypatch):
    def refuse_download(*_args, **_kwargs):
        raise AssertionError("a custom override on unfit hardware must never download without accept_risk")

    monkeypatch.setattr(llm_lifecycle, "_download", refuse_download)
    fake_report = llm_lifecycle.HardwareReport(
        os="Linux", architecture="x86_64", cpu="test", logical_cpus=1,
        ram_total_mb=1, ram_available_mb=1, disk_free_mb=1, nvidia_gpus=[],
        nvidia_probe="nvidia-smi not found", container_runtime="", container_available=False,
        native_runtime_available=True, native_runtime_present=True, warnings=[],
    )
    monkeypatch.setattr(llm_lifecycle, "detect_hardware", lambda _base: fake_report)
    with pytest.raises(LifecycleError, match="No bundled profile safely fits"):
        llm_lifecycle.install_managed(
            package_id="custom-no-fit", root=tmp_path / "llm",
            model_url="https://huggingface.co/example/example/resolve/pin/example.gguf",
            expected_sha256="a" * 64,
        )


@pytest.mark.skipif(__import__("os").name == "nt", reason="the fake llama-server is a POSIX script")
def test_install_managed_custom_override_with_accept_risk_proceeds_on_no_fit_hardware(tmp_path, monkeypatch):
    root = tmp_path / "llm"
    server = _write_fake_llama_server(tmp_path / "llama-server")
    monkeypatch.setattr(llm_runtime_native, "resolve_server", lambda root=None: server)
    payload = b"GGUF" + bytes(96)
    digest = hashlib.sha256(payload).hexdigest()

    def fake_download(_url, destination, expected_sha256, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return expected_sha256

    monkeypatch.setattr(llm_lifecycle, "_download", fake_download)
    fake_report = llm_lifecycle.HardwareReport(
        os="Linux", architecture="x86_64", cpu="test", logical_cpus=1,
        ram_total_mb=1, ram_available_mb=1, disk_free_mb=1, nvidia_gpus=[],
        nvidia_probe="nvidia-smi not found", container_runtime="", container_available=False,
        native_runtime_available=True, native_runtime_present=True, warnings=[],
    )
    monkeypatch.setattr(llm_lifecycle, "detect_hardware", lambda _base: fake_report)
    result = llm_lifecycle.install_managed(
        package_id="custom-accept-risk", root=root, port=_free_tcp_port(),
        model_url="https://huggingface.co/example/example/resolve/pin/example.gguf",
        expected_sha256=digest, accept_risk=True, runtime="native",
    )
    manifest = load_manifest(result["manifest"])
    try:
        assert manifest.install_source["profile"] == "custom"
        # Falls back to the old single-model conservative floor, not a
        # hardcoded value disconnected from `choices[0]`.
        assert manifest.context_window == 2048
        assert manifest.max_concurrent == 1
        assert manifest.hardware_requirements["estimated_memory_mb"] == 1024
        assert result["self_test"]["ok"] is True
    finally:
        llm_runtime_native.stop(manifest)


def test_install_managed_with_an_unfitting_explicit_profile_raises_without_downloading(tmp_path, monkeypatch):
    def refuse_download(*_args, **_kwargs):
        raise AssertionError("a profile that does not fit must never be downloaded")

    monkeypatch.setattr(llm_lifecycle, "_download", refuse_download)
    # An impossibly small disk/RAM report guarantees no catalog profile fits.
    fake_report = llm_lifecycle.HardwareReport(
        os="Linux", architecture="x86_64", cpu="test", logical_cpus=1,
        ram_total_mb=1, ram_available_mb=1, disk_free_mb=1, nvidia_gpus=[],
        nvidia_probe="nvidia-smi not found", container_runtime="", container_available=False,
        native_runtime_available=True, native_runtime_present=True, warnings=[],
    )
    monkeypatch.setattr(llm_lifecycle, "detect_hardware", lambda _base: fake_report)
    with pytest.raises(LifecycleError, match="profile quality needs about"):
        llm_lifecycle.install_managed(
            package_id="profile-too-big", root=tmp_path / "llm",
            runtime="native", profile="quality",
        )


def test_install_managed_requires_both_model_url_and_expected_sha256_together(tmp_path):
    with pytest.raises(LifecycleError, match="requires both model_url and expected_sha256"):
        llm_lifecycle.install_managed(
            package_id="partial-override", root=tmp_path / "llm",
            model_url="https://huggingface.co/example/example/resolve/pin/example.gguf",
        )


def test_profile_by_name_unknown_name_propagates_as_value_error(tmp_path):
    with pytest.raises(ValueError, match="unknown model profile"):
        llm_lifecycle.install_managed(
            package_id="bad-profile", root=tmp_path / "llm",
            runtime="native", profile="does-not-exist", accept_risk=True,
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.001])
def test_manifest_rejects_unsafe_public_pricing(value):
    manifest = LLMPackageManifest(
        package_id="priced", mode="openai_compatible", public_model_alias="priced",
        base_url="http://127.0.0.1:8080", pricing=Pricing(input_per_1k=value),
    )
    with pytest.raises(ManifestError, match="finite and non-negative"):
        manifest.validate()


def test_managed_container_drops_privileges(monkeypatch, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF" + b"0" * 64)
    manifest = LLMPackageManifest(
        package_id="safe", mode="managed", public_model_alias="safe",
        runtime="docker_llama_cpp", model_path=str(model),
        checksum=fingerprint_file(model), base_url="http://127.0.0.1:18080",
        install_source={"runtime_image": llm_runtime_docker.DEFAULT_IMAGE},
    )
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(llm_runtime_docker, "_docker", lambda: "docker")
    monkeypatch.setattr(llm_runtime_docker.subprocess, "run", fake_run)
    llm_runtime_docker.start(manifest)
    run_command = commands[-1]
    assert run_command[0:2] == ["docker", "run"]
    assert run_command[run_command.index("--cap-drop") + 1] == "ALL"
    assert run_command[run_command.index("--security-opt") + 1] == "no-new-privileges"
    assert llm_runtime_docker.DEFAULT_IMAGE in run_command


def test_local_setup_publish_pause_flow_is_explicit_and_persistent(tmp_path, openai_server):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )

    with TestClient(app) as client:
        setup = client.post("/api/local/llm/setup", json={
            "mode": "openai-compatible", "package_id": "configured-api",
            "alias": "public-safe-alias", "base_url": openai_server,
            "model": "test-real-api",
        })
        assert setup.status_code == 200
        setup_body = setup.json()
        assert setup_body["configured"] is True
        assert setup_body["publication_enabled"] is False
        assert "output_preview" not in setup_body["setup"]["self_test"]

        offline = client.get("/api/local/llm/service/status").json()
        assert offline["configured"] is True
        assert offline["online"] is False
        assert offline["accepting_orders"] is False

        published = client.post("/api/local/llm/services/publish", json={
            "network_id": "provider-test", "benchmark": False,
        })
        assert published.status_code == 200
        assert published.json()["record"]["metadata"]["llm_service"]["online"] is True

        online = client.get("/api/local/llm/service/status").json()
        assert online["online"] is True
        assert online["publication_enabled"] is True
        assert online["network_id"] == "provider-test"

        paused = client.post("/api/local/llm/services/pause").json()
        assert paused["online"] is False
        assert paused["accepting_orders"] is False
        assert paused["publication_enabled"] is False

        started = time.monotonic()
        accepted = client.post("/api/local/llm/orders/async", json={
            "provider_peer_id": "missing-provider", "service_id": "missing-service",
            "prompt": "private prompt that must not be stored", "max_tokens": 8,
        })
        assert accepted.status_code == 200
        assert time.monotonic() - started < 2
        task_id = accepted.json()["task_id"]
        deadline = time.monotonic() + 3
        status = {}
        while time.monotonic() < deadline:
            status = client.get(f"/api/local/llm/orders/{task_id}").json()
            if status.get("state") in {"failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert status["state"] == "failed"
        assert "private prompt" not in json.dumps(status)

        invalid_tokens = client.post("/api/local/llm/orders/async", json={
            "provider_peer_id": "missing-provider", "service_id": "missing-service",
            "prompt": "private prompt", "max_tokens": "not-a-number",
        })
        assert invalid_tokens.status_code == 400
        assert invalid_tokens.json()["detail"] == "max_tokens must be a positive integer"

        privacy = client.put("/api/local/llm/privacy", json={
            "result_retention_seconds": 0,
        })
        assert privacy.status_code == 200
        assert privacy.json()["result_retention_seconds"] == 0
        assert privacy.json()["plaintext_persisted"] is False

        history_store = TaskOrderStore(home / "llm" / "consumer-orders")
        history_store.claim(task_id="history_cleanup", bindings={"request": "test"})
        history_store.transition(task_id="history_cleanup", state="accepted")
        history_store.transition(task_id="history_cleanup", state="running")
        history_store.transition(
            task_id="history_cleanup", state="succeeded",
            encrypted_response={"ciphertext": "encrypted-only"},
        )
        privacy = client.put("/api/local/llm/privacy", json={
            "result_retention_seconds": 0,
        })
        assert privacy.status_code == 200
        assert "encrypted_response" not in history_store.get("history_cleanup")
        cleared = client.delete("/api/local/llm/orders").json()
        assert cleared["removed"] >= 1
        assert history_store.get("history_cleanup") is None

    settings = json.loads((home / "llm" / "provider-settings.json").read_text(encoding="utf-8"))
    assert Path(settings["manifest"]).parts[-2:] == ("configured-api", "manifest.json")
    assert settings["publication_enabled"] is False
    assert settings["network_id"] == "provider-test"


def test_async_setup_reports_progress_and_exposes_safe_lifecycle_actions(tmp_path, openai_server):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )

    with TestClient(app) as client:
        queued = client.post("/api/local/llm/setup/async", json={
            "mode": "openai-compatible",
            "package_id": "async-local-api",
            "alias": "async-safe-alias",
            "base_url": openai_server,
            "model": "test-real-api",
        })
        assert queued.status_code == 200
        assert queued.json()["state"] == "queued"
        deadline = time.monotonic() + 5
        setup_status = {}
        while time.monotonic() < deadline:
            setup_status = client.get("/api/local/llm/setup/status").json()
            if setup_status.get("state") in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert setup_status["state"] == "succeeded"
        assert setup_status["progress"] == 100
        assert "output_preview" not in json.dumps(setup_status)

        provider = client.get("/api/local/llm/service/status").json()
        assert provider["configured"] is True
        assert provider["lifecycle"]["runtime"]["managed"] is False

        tested = client.post("/api/local/llm/service/actions/self-test", json={})
        assert tested.status_code == 200
        assert tested.json()["result"]["self_test"]["ok"] is True
        assert "output_preview" not in tested.text

        stopped = client.post("/api/local/llm/service/actions/stop", json={})
        assert stopped.status_code == 200
        assert stopped.json()["publication_enabled"] is False


def test_async_setup_can_be_cancelled_without_replacing_existing_configuration(
    tmp_path, monkeypatch,
):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    started = threading.Event()

    def slow_connect(**kwargs):
        progress = kwargs["progress"]
        cancel_check = kwargs["cancel_check"]
        progress("connect", 20, "Checking the local model API")
        started.set()
        while not cancel_check():
            time.sleep(0.01)
        raise LifecycleError("setup cancelled")

    monkeypatch.setattr(llm_routes, "connect_local_api", slow_connect)
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )

    with TestClient(app) as client:
        queued = client.post("/api/local/llm/setup/async", json={
            "mode": "openai-compatible", "package_id": "cancelled-api",
            "alias": "cancelled-alias", "base_url": "http://127.0.0.1:8080",
        }).json()
        assert started.wait(timeout=2)
        cancelling = client.post(f"/api/local/llm/setup/{queued['job_id']}/cancel").json()
        assert cancelling["state"] == "cancelling"
        deadline = time.monotonic() + 3
        final = {}
        while time.monotonic() < deadline:
            final = client.get("/api/local/llm/setup/status").json()
            if final.get("state") == "cancelled":
                break
            time.sleep(0.02)
        assert final["state"] == "cancelled"
        assert final["retryable"] is True
        assert not (home / "llm" / "provider-settings.json").exists()


def test_interrupted_setup_status_is_recovered_as_retryable_failure(tmp_path):
    home = tmp_path / "node"
    job_path = home / "llm" / "setup-job.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "job_id": "setup_interrupted", "state": "running",
        "stage": "download_model", "progress": 42,
    }), encoding="utf-8")
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )

    with TestClient(app) as client:
        status = client.get("/api/local/llm/setup/status").json()
    assert status["state"] == "failed"
    assert status["error_code"] == "setup_interrupted"
    assert status["retryable"] is True

    # `write_setup_job` (the recovery write above) must still produce the same
    # on-disk bytes now that it goes through `atomic_write_json`.
    raw = job_path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, indent=2, sort_keys=True)


def test_provider_and_consumer_settings_format_unchanged(tmp_path):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )

    with TestClient(app) as client:
        # No manifest is configured, so this just persists publication_enabled=False.
        paused = client.post("/api/local/llm/services/pause")
        assert paused.status_code == 200

        privacy = client.put(
            "/api/local/llm/privacy", json={"result_retention_seconds": 86400}
        )
        assert privacy.status_code == 200

    provider_raw = (home / "llm" / "provider-settings.json").read_text(encoding="utf-8")
    provider_parsed = json.loads(provider_raw)
    assert provider_raw == json.dumps(provider_parsed, indent=2, sort_keys=True)

    consumer_raw = (home / "llm" / "consumer-settings.json").read_text(encoding="utf-8")
    consumer_parsed = json.loads(consumer_raw)
    assert consumer_raw == json.dumps(consumer_parsed, indent=2, sort_keys=True)
    assert consumer_parsed["result_retention_seconds"] == 86400


def test_node_shutdown_stops_every_owned_inference_server(tmp_path, monkeypatch):
    """Quitting the node must not orphan a `llama-server` child."""
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    messaging_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app, store=store, home=home, messaging_key=messaging_key,
        resolve_endpoint=lambda _peer_id: "", resolve_pubkey=lambda _peer_id: "",
    )
    stopped: list[bool] = []
    monkeypatch.setattr(llm_runtime_native, "stop_owned_children",
                        lambda: stopped.append(True))

    with TestClient(app) as client:
        client.get("/api/local/llm/service/status")
        assert stopped == []
    assert stopped == [True]

    # The node's own app replaces Starlette's shutdown handling with a custom
    # lifespan, so it reaches the same hook through `app.state`.
    assert app.state.llm_shutdown is not None
    app.state.llm_shutdown()
    assert stopped == [True, True]


def test_manifest_public_view_has_no_paths_urls_or_key_names(tmp_path):
    manifest = LLMPackageManifest(
        package_id="private-model", mode="import_gguf", public_model_alias="private-alias",
        base_url="http://127.0.0.1:8080", api_key_env="VERY_SECRET_KEY",
        model_path=str(tmp_path / "commercial-secret.gguf"), runtime_command=["secret-bin"],
        runtime_api_key="a-loopback-bearer-token",
        model_fingerprint="sha256:" + "a" * 64,
    )
    public = json.dumps(manifest.public_dict())
    assert "commercial-secret" not in public
    assert "VERY_SECRET_KEY" not in public
    assert "127.0.0.1" not in public
    assert "secret-bin" not in public
    assert "runtime_api_key" not in public
    assert "a-loopback-bearer-token" not in public
    assert "private-alias" in public


def test_gguf_import_is_read_only_and_fingerprinted(tmp_path):
    model = tmp_path / "owned.gguf"
    original = b"GGUF" + b"\x00" * 64
    model.write_bytes(original)
    details = validate_gguf(model, allow_risk=True)
    assert details["format"] == "GGUF"
    assert details["fingerprint"] == fingerprint_file(model)
    assert model.read_bytes() == original
    with pytest.raises(LifecycleError, match="expected GGUF"):
        bad = tmp_path / "bad.bin"
        bad.write_bytes(b"nope" * 10)
        validate_gguf(bad, allow_risk=True)


def test_task_balance_hold_settle_release_and_dedupe(tmp_path):
    ledger = TaskBalanceLedger(tmp_path / "task-balance.json", initial_dev_balance=10)
    first = ledger.hold(task_id="one", amount=2, service_id="svc", provider_peer_id="p")
    assert ledger.hold(task_id="one", amount=2, service_id="svc", provider_peer_id="p") == first
    settled = ledger.settle(task_id="one", amount=1.25, input_tokens=3, output_tokens=4,
                            duration_ms=5, service_id="svc", provider_peer_id="p")
    assert settled["state"] == "settled"
    assert ledger.settle(task_id="one", amount=1.25, input_tokens=3, output_tokens=4,
                         duration_ms=5, service_id="svc", provider_peer_id="p")["settled_amount"] == 1.25
    ledger.hold(task_id="two", amount=3, service_id="svc", provider_peer_id="p")
    released = ledger.release(task_id="two", reason="cancelled")
    assert released["state"] == "released"
    reheld = ledger.hold(task_id="two", amount=3, service_id="svc", provider_peer_id="p")
    assert reheld["state"] == "held"
    ledger.release(task_id="two", reason="retry_failed")
    with pytest.raises(TaskBalanceError):
        ledger.settle(task_id="two", amount=1, input_tokens=1, output_tokens=1,
                      duration_ms=1, service_id="svc", provider_peer_id="p")
    summary = ledger.summary()
    assert summary["development_only"] is True
    assert summary["available"] == 8.75 and summary["held"] == 0
    assert all("prompt" not in json.dumps(event) for event in ledger.events())


def test_task_balance_rejects_task_id_reuse_for_different_request(tmp_path):
    ledger = TaskBalanceLedger(tmp_path / "task-balance.json", initial_dev_balance=10)
    ledger.hold(
        task_id="same", amount=2, service_id="svc", provider_peer_id="provider-a",
        idempotency_key="key", request_fingerprint="fingerprint-a",
    )
    with pytest.raises(TaskBalanceError, match="idempotency conflict"):
        ledger.hold(
            task_id="same", amount=2, service_id="svc", provider_peer_id="provider-b",
            idempotency_key="key", request_fingerprint="fingerprint-b",
        )


def test_task_store_rejects_state_rollback_and_binding_changes(tmp_path):
    orders = TaskOrderStore(tmp_path / "orders")
    bindings = {
        "consumer_peer_id": "consumer", "service_id": "svc",
        "idempotency_key": "key", "request_fingerprint": "fingerprint",
    }
    _, claimed = orders.claim(task_id="bound", bindings=bindings)
    assert claimed is True
    _, claimed_again = orders.claim(task_id="bound", bindings=bindings)
    assert claimed_again is False
    orders.transition(task_id="bound", state="accepted")
    orders.transition(task_id="bound", state="running")
    with pytest.raises(Exception, match="invalid task transition"):
        orders.transition(task_id="bound", state="accepted")
    with pytest.raises(Exception, match="idempotency conflict"):
        orders.claim(task_id="bound", bindings={**bindings, "service_id": "other"})


def test_task_envelope_is_ciphertext_and_authenticated(tmp_path):
    sender = RynmeshStore(home=tmp_path / "s", network_dir=tmp_path / "net")
    recipient = RynmeshStore(home=tmp_path / "r", network_dir=tmp_path / "net")
    recipient_msg = peer_box.load_or_create_messaging_key(tmp_path / "r-msg")
    signed = seal_task(
        body={"task_id": "task_one", "prompt": "NEVER VISIBLE IN RELAY"},
        task_id="task_one", kind="llm_request", sender_peer_id=sender.peer_id,
        recipient_peer_id=recipient.peer_id, sender_signing_key=sender.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(recipient_msg), expires_at=_expires(),
    )
    wire = json.dumps(signed.to_dict())
    assert "NEVER VISIBLE IN RELAY" not in wire
    _, body = open_task(signed, recipient_peer_id=recipient.peer_id,
                        recipient_messaging_key=recipient_msg, expected_kind="llm_request")
    assert body["prompt"] == "NEVER VISIBLE IN RELAY"
    tampered = signed.to_dict()
    tampered["payload"]["ciphertext"] += "A"
    with pytest.raises(SignatureError):
        open_task(tampered, recipient_peer_id=recipient.peer_id,
                  recipient_messaging_key=recipient_msg, expected_kind="llm_request")


def test_ice_udp_direct_transport_exchanges_chunked_json_without_relay(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.delenv("RYNMESH_P2P_REQUIRE_PUBLIC", raising=False)

    async def scenario():
        consumer = new_connection(controlling=True)
        provider = new_connection(controlling=False)
        try:
            consumer_signal, provider_signal = await asyncio.gather(
                gather_signal(consumer), gather_signal(provider),
            )
            await asyncio.gather(
                apply_remote_signal(consumer, provider_signal),
                apply_remote_signal(provider, consumer_signal),
            )
            await asyncio.gather(consumer.connect(), provider.connect())
            consumer_evidence = selected_pair(consumer)
            provider_evidence = selected_pair(provider)
            assert consumer_evidence["transport"] == "ice_udp_direct"
            assert provider_evidence["relay_used"] is False
            assert consumer_evidence["remote"]["type"] != "relay"

            request = {"ciphertext": "x" * 5000}
            response = {"ciphertext": "y" * 7000}

            async def provider_side():
                received, request_bytes = await receive_json(provider, timeout_s=5)
                assert received == request and request_bytes > 5000
                await send_json(provider, response, timeout_s=5)

            provider_task = asyncio.create_task(provider_side())
            await send_json(consumer, request, timeout_s=5)
            received, response_bytes = await receive_json(consumer, timeout_s=5)
            await provider_task
            assert received == response and response_bytes > 7000
        finally:
            await consumer.close()
            await provider.close()

    asyncio.run(scenario())


def test_public_nat_mode_refuses_to_fall_back_to_host_candidate(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_PUBLIC", "1")

    async def scenario():
        connection = new_connection(controlling=True)
        try:
            with pytest.raises(P2PError, match="server-reflexive STUN candidate"):
                await gather_signal(connection)
        finally:
            await connection.close()

    asyncio.run(scenario())


def test_public_nat_mode_accepts_peer_reflexive_nominated_candidate(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_PUBLIC", "1")
    local = SimpleNamespace(type="srflx", transport="udp", host="117.50.189.73", port=3000)
    remote = SimpleNamespace(type="prflx", transport="udp", host="14.154.222.55", port=52066)
    pair = SimpleNamespace(local_candidate=local, remote_candidate=remote)
    connection = SimpleNamespace(_nominated={1: pair})

    evidence = selected_pair(connection)

    assert evidence["transport"] == "ice_udp_direct"
    assert evidence["relay_used"] is False
    assert evidence["peer_public_mapping_nominated"] is True
    assert evidence["path_kind"] == "peer_reflexive"


def test_strict_p2p_connection_never_passes_turn_configuration(monkeypatch):
    monkeypatch.delenv("RYNMESH_P2P_BIND_PORT", raising=False)
    monkeypatch.setenv("RYNMESH_P2P_STUN", "stun.example.test:3478")
    monkeypatch.setenv("RYNMESH_P2P_TURN", "turn.example.test:3478")
    monkeypatch.setenv("RYNMESH_P2P_TURN_USERNAME", "must-be-ignored")
    monkeypatch.setenv("RYNMESH_P2P_TURN_PASSWORD", "must-be-ignored")
    captured = {}
    sentinel = object()

    def fake_connection(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(llm_p2p.aioice, "Connection", fake_connection)

    assert llm_p2p.new_connection(controlling=True) is sentinel
    assert captured == {
        "ice_controlling": True,
        "components": 1,
        "stun_server": ("stun.example.test", 3478),
        "use_ipv4": True,
        "use_ipv6": True,
    }
    assert all("turn" not in key.lower() for key in captured)


def test_provider_can_bind_ice_to_one_firewall_port(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    monkeypatch.setenv("RYNMESH_P2P_BIND_PORT", "3000")
    connection = new_connection(controlling=False)
    assert connection._rynmesh_bind_port == 3000


def test_invalid_fixed_ice_port_fails_closed(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_BIND_PORT", "70000")
    with pytest.raises(P2PError, match="between 1 and 65535"):
        new_connection(controlling=False)


def test_ice_signal_rejects_turn_relay_candidate_before_connecting():
    with pytest.raises(P2PError, match="TURN/relay"):
        IceSignal.from_dict({
            "username": "remote",
            "password": "remote-password",
            "candidates": [
                "relay 1 udp 1677734910 203.0.113.10 50000 typ relay "
                "raddr 0.0.0.0 rport 0",
            ],
        })

    with pytest.raises(P2PError, match="non-UDP"):
        IceSignal.from_dict({
            "username": "remote",
            "password": "remote-password",
            "candidates": [
                "tcp 1 tcp 1518280447 192.0.2.1 9 typ host tcptype active",
            ],
        })

    async def scenario():
        connection = new_connection(controlling=True)
        signal = IceSignal(
            username="remote",
            password="remote-password",
            candidates=(
                "relay 1 udp 1677734910 203.0.113.10 50000 typ relay "
                "raddr 0.0.0.0 rport 0",
            ),
        )
        try:
            with pytest.raises(P2PError, match="TURN/relay"):
                await apply_remote_signal(connection, signal)
        finally:
            await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("candidate", "error"),
    [
        (
            "host-name 1 udp 2130706431 example.invalid 50000 typ host",
            "host must be an IP literal",
        ),
        (
            "related-name 1 udp 1694498815 203.0.113.10 50000 typ srflx "
            "raddr internal.invalid rport 50000",
            "related host must be an IP literal",
        ),
        (
            "component 2 udp 2130706431 192.0.2.10 50000 typ host",
            "component must be 1",
        ),
        (
            "zero-port 1 udp 2130706431 192.0.2.10 0 typ host",
            "port is invalid",
        ),
        (
            "unspecified 1 udp 2130706431 0.0.0.0 50000 typ host",
            "not a usable unicast address",
        ),
        (
            "multicast 1 udp 2130706431 239.1.2.3 50000 typ host",
            "not a usable unicast address",
        ),
        (
            "broadcast 1 udp 2130706431 255.255.255.255 50000 typ host",
            "not a usable unicast address",
        ),
    ],
)
def test_ice_signal_rejects_non_literal_or_non_unicast_destination(candidate, error):
    with pytest.raises(P2PError, match=error):
        IceSignal.from_dict({
            "username": "strict",
            "password": "strict-password",
            "candidates": [candidate],
        })


@pytest.mark.parametrize(
    "candidate",
    [
        "ipv4 1 udp 2130706431 192.0.2.10 50000 typ host",
        "ipv6 1 udp 2130706431 2001:db8::10 50000 typ host",
        "srflx 1 udp 1694498815 203.0.113.10 50000 typ srflx "
        "raddr 10.0.0.10 rport 50000",
    ],
)
def test_ice_signal_accepts_bounded_ip_literal_candidates(candidate):
    signal = IceSignal.from_dict({
        "username": "strict",
        "password": "strict-password",
        "candidates": [candidate],
    })
    assert signal.candidates == (candidate,)


def test_distinct_public_egress_acceptance_fails_fast_for_shared_mapping(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC", "1")
    local = IceSignal(
        username="local",
        password="local-password",
        candidates=(
            "local 1 udp 1694498815 98.158.108.218 50001 typ srflx raddr 10.0.0.2 rport 50001",
        ),
    )
    same_egress = IceSignal(
        username="remote",
        password="remote-password",
        candidates=(
            "remote 1 udp 1694498815 98.158.108.218 50002 typ srflx raddr 192.168.1.2 rport 50002",
        ),
    )
    other_egress = IceSignal(
        username="remote",
        password="remote-password",
        candidates=(
            "remote 1 udp 1694498815 203.0.113.8 50002 typ srflx raddr 192.168.1.2 rport 50002",
        ),
    )

    with pytest.raises(P2PError, match="distinct public egress"):
        validate_distinct_public_egress(local, same_egress)
    validate_distinct_public_egress(local, other_egress)
    assert _delivery_error_code(
        P2PError("strict P2P acceptance requires distinct public egress addresses"),
        transport="p2p",
    ) == "p2p_distinct_public_egress_required"


def test_restart_recovery_fails_interrupted_order_and_releases_hold(tmp_path):
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "balance.json")
    orders.transition(task_id="task_interrupted", state="created")
    orders.transition(task_id="task_interrupted", state="accepted")
    orders.transition(task_id="task_interrupted", state="running")
    balance.hold(
        task_id="task_interrupted",
        amount=0.25,
        service_id="private-service",
        provider_peer_id="provider-peer",
    )

    _recover_consumer_orders(orders, balance)
    _recover_consumer_orders(orders, balance)

    assert orders.get("task_interrupted")["state"] == "failed"
    assert balance.summary()["held"] == 0.0
    assert balance.summary()["available"] == 100.0
    releases = [event for event in balance.events() if event["kind"] == "release"]
    assert len(releases) == 1
    assert releases[0]["reason"] == "consumer_restart_recovery"


def test_restart_recovery_completes_received_result_settlement(tmp_path):
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "balance.json")
    orders.claim(task_id="task_received", bindings={
        "provider_peer_id": "provider-peer", "service_id": "private-service",
        "idempotency_key": "task_received", "request_fingerprint": "fingerprint",
    })
    orders.transition(task_id="task_received", state="accepted")
    orders.transition(task_id="task_received", state="running")
    orders.checkpoint(task_id="task_received", metadata={
        "settlement_pending": True, "provider_peer_id": "provider-peer",
        "service_id": "private-service", "network_id": "rynmesh-main",
        "amount": 0.1, "input_tokens": 4, "output_tokens": 2, "duration_ms": 7,
    })
    balance.hold(
        task_id="task_received", amount=0.25, service_id="private-service",
        provider_peer_id="provider-peer", idempotency_key="task_received",
        request_fingerprint="fingerprint",
    )

    _recover_consumer_orders(orders, balance)
    _recover_consumer_orders(orders, balance)

    assert orders.get("task_received")["state"] == "succeeded"
    assert balance.summary()["held"] == 0.0
    assert balance.summary()["available"] == 99.9
    settlements = [event for event in balance.events() if event["kind"] == "settle"]
    assert len(settlements) == 1


class _FakeAdapter:
    def __init__(self):
        self.calls = 0
        self.cancelled = []

    def health(self): return {"ok": True, "model": "fake-test-only"}
    def models(self): return [{"id": "fake-test-only"}]
    def capabilities(self): return {"chat_completions": True}
    def metrics(self): return {"requests": self.calls}
    def shutdown(self): pass
    def cancel(self, task_id): self.cancelled.append(task_id); return True
    def infer(self, *, prompt, max_tokens, task_id, timeout_s):
        self.calls += 1
        return {"text": "provider output", "model": "fake-test-only", "input_tokens": 5,
                "output_tokens": 2, "duration_ms": 7}


class _BlockingAdapter(_FakeAdapter):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def infer(self, *, prompt, max_tokens, task_id, timeout_s):
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=5)
        return {"text": "provider output", "model": "fake-test-only", "input_tokens": 5,
                "output_tokens": 2, "duration_ms": 7}


def test_local_model_works_unpublished_without_registry_or_peer_transport(tmp_path, monkeypatch, openai_server):
    from rynmesh.ask_ryn.context import AskContextService
    from rynmesh.ask_ryn.runs import AskRunService
    from rynmesh.ask_ryn.store import ConversationStore

    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    app = FastAPI()
    def no_peer(*args, **kwargs):
        raise AssertionError("local inference must not contact peer transport")
    monkeypatch.setattr(llm_routes, "_peer_post_json", no_peer)
    # A server's P2P policy must not send same-device computation out to ICE.
    monkeypatch.setenv("RYNMESH_LLM_TRANSPORT", "p2p")
    commands = install_llm_routes(app, store=store, home=store.home, messaging_key=key, resolve_endpoint=no_peer, resolve_pubkey=no_peer)
    client = TestClient(app)
    setup = client.post("/api/local/llm/setup", json={"mode": "openai-compatible", "package_id": "own-model", "alias": "Only my device", "base_url": openai_server, "model": "test-real-api"})
    assert setup.status_code == 200, setup.text
    assert setup.json()["status"]["ready"] and not setup.json()["publication_enabled"]
    assert store.list_job_capacities(network_id="network", capability=llm_routes.CAPABILITY)["capacities"] == []
    def offline_registry(**kwargs):
        raise OSError("registry unavailable")
    monkeypatch.setattr(store, "list_job_capacities", offline_registry)
    services = client.get("/api/local/llm/services?network_id=network").json()["services"]
    assert len(services) == 1 and services[0]["peer_id"] == store.peer_id
    assert services[0]["online"] and services[0]["service"]["pricing"]["minimum"] == 0
    history = ConversationStore(store.home / "ask-ryn", key)
    row = history.save({"id": "own-conversation", "title": "Own model", "serviceKey": store.peer_id + "::own-model", "serviceName": "Only my device", "providerPeerId": store.peer_id, "networkId": "network", "createdAt": "2026-09-11T00:00:00Z", "updatedAt": "2026-09-11T00:00:00Z", "messages": []}, expected_revision=0)
    context = AskContextService(lambda: None, commands.discover)
    preview = context.preview(row, "A local question")
    runs = AskRunService(history, lambda: context, lambda: commands)
    task_id = "task_" + "d" * 32
    request = {"task_id": task_id, "conversation_id": row["id"], "expected_revision": 1, "question": "A local question", "prompt_sha256": preview["prompt_sha256"]}
    calls_before = _OpenAIHandler.calls
    runs.begin(request)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and runs.get(task_id)["state"] not in {"succeeded", "failed", "interrupted"}:
        runs.run_once()
        time.sleep(0.02)
    assert runs.get(task_id)["state"] == "succeeded", runs.get(task_id)
    assert history.get(row["id"])["messages"][-1]["content"] == "test adapter completion"
    assert history.get(row["id"])["messages"][-1]["cost"] == 0
    assert commands.status(task_id)["transport"] == "local_runtime"
    assert preview["prompt_format"] == "chat_messages_v1"
    assert _OpenAIHandler.last_messages == json.loads(preview["prompt"])
    assert _OpenAIHandler.last_messages[-1] == {"role": "user", "content": "A local question"}
    changed_format = client.post("/api/local/llm/orders", json={
        "task_id": task_id, "idempotency_key": task_id, "provider_peer_id": store.peer_id,
        "service_id": "own-model", "network_id": "network", "prompt": preview["prompt"],
        "max_tokens": preview["max_output_tokens"], "transport": "auto", "prompt_format": "text",
    })
    assert changed_format.status_code == 409
    listed = next(order for order in client.get("/api/local/llm/orders").json()["orders"] if order["task_id"] == task_id)
    assert listed["transport"] == "local_runtime"
    assert listed["state"] == "succeeded"
    assert _OpenAIHandler.calls == calls_before + 1
    assert client.get("/api/local/task-balance").json()["available"] == 100
    assert not client.get("/api/local/llm/service/status").json()["publication_enabled"]
    assert runs.begin(request)["state"] == "succeeded"
    assert _OpenAIHandler.calls == calls_before + 1
    # A healthy own model does not admit another signed peer while unpublished.
    other = RynmeshStore(home=tmp_path / "other", network_dir=tmp_path / "network")
    foreign = seal_task(body={"task_id": "foreign", "service_id": "own-model", "prompt": "Foreign question", "max_amount": 1, "reply_messaging_pub": peer_box.public_key_b64(key)}, task_id="foreign", kind="llm_request", sender_peer_id=other.peer_id, recipient_peer_id=store.peer_id, sender_signing_key=other.private_key_bytes, recipient_messaging_pub=peer_box.public_key_b64(key), expires_at=_expires()).to_dict()
    denied = client.post("/api/peer/llm/tasks", json=foreign).json()
    _, rejection = open_task(denied, recipient_peer_id=other.peer_id, recipient_messaging_key=key, expected_kind="llm_response")
    assert rejection["error_code"] == "ai_permission_denied"
    assert _OpenAIHandler.calls == calls_before + 1
    monkeypatch.setattr(OpenAICompatibleAdapter, "health", lambda _: {"ok": False, "error": "model missing"})
    assert client.get("/api/local/llm/services?network_id=network").json()["services"][0]["online"] is False


def test_local_cancellation_uses_the_same_provider_without_peer_delivery(tmp_path, monkeypatch):
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    adapter = _BlockingAdapter()
    manifest = LLMPackageManifest(package_id="own-model", mode="openai_compatible", public_model_alias="Own model", base_url="http://127.0.0.1")
    monkeypatch.delenv("RYNMESH_LLM_SERVICE_MANIFEST", raising=False)
    llm_routes.atomic_write_json(store.home / "llm" / "provider-settings.json", {"manifest": "synthetic-manifest", "publication_enabled": False})
    monkeypatch.setattr(llm_routes, "load_manifest", lambda _: manifest)
    monkeypatch.setattr(llm_routes, "adapter_from_manifest", lambda _: adapter)
    def no_remote(*args, **kwargs):
        raise AssertionError("local cancellation must not use peer delivery")
    monkeypatch.setattr(llm_routes, "_peer_post_json", no_remote)
    app = FastAPI()
    commands = install_llm_routes(app, store=store, home=store.home, messaging_key=key, resolve_endpoint=no_remote, resolve_pubkey=no_remote)
    request = {"task_id": "local_cancel", "provider_peer_id": store.peer_id, "service_id": "own-model", "prompt": "Local cancellable question", "max_tokens": 8}
    commands.submit(request)
    assert adapter.started.wait(timeout=3)
    try:
        commands.cancel(request["task_id"])
        assert adapter.cancelled == [request["task_id"]]
        assert commands.status(request["task_id"])["result_pending"] is True
    finally:
        adapter.release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = commands.status(request["task_id"])
        if not result.get("result_pending"):
            break
        time.sleep(0.01)
    assert result["state"] == "cancelled"
    assert adapter.calls == 1
    assert TestClient(app).get("/api/local/task-balance").json()["held"] == 0


@pytest.mark.parametrize('old_state', ['created', 'accepted', 'running'])
def test_provider_restart_fails_unfinished_orders_without_replaying_or_touching_completed(tmp_path, old_state):
    provider = RynmeshStore(home=tmp_path / 'provider', network_dir=tmp_path / 'network')
    key = peer_box.load_or_create_messaging_key(provider.home / 'messaging.x25519')
    orders = TaskOrderStore(provider.home / 'llm/provider-orders')
    body = {'task_id': 'interrupted', 'service_id': 'svc', 'prompt': 'Synthetic recovery request',
            'max_tokens': 8, 'max_amount': 0, 'reply_messaging_pub': peer_box.public_key_b64(key)}
    orders.claim(task_id='interrupted', bindings={'consumer_peer_id': provider.peer_id, 'service_id': 'svc',
        'idempotency_key': 'interrupted', 'request_fingerprint': llm_routes._request_fingerprint(body, provider.private_key_bytes)})
    if old_state in {'accepted', 'running'}:
        orders.transition(task_id='interrupted', state='accepted')
    if old_state == 'running':
        orders.transition(task_id='interrupted', state='running')
    orders.claim(task_id='finished', bindings={'service_id': 'svc'})
    for state in ['accepted', 'running', 'succeeded']:
        orders.transition(task_id='finished', state=state)
    completed = orders.get('finished')
    for _ in range(2):
        install_llm_routes(FastAPI(), store=provider, home=provider.home, messaging_key=key,
            resolve_endpoint=lambda _: '', resolve_pubkey=lambda _: '')
    recovered = orders.get('interrupted')
    assert recovered['state'] == 'failed'
    assert sum(e['state'] == 'failed' for e in recovered['history']) == 1
    assert recovered['history'][-1]['error_code'] == 'provider_restarted_before_completion'
    assert orders.get('finished') == completed
    adapter = _FakeAdapter()
    service = ProviderService(manifest=LLMPackageManifest(package_id='svc', mode='openai_compatible',
        public_model_alias='Synthetic recovery', base_url='http://127.0.0.1:1'), adapter=adapter,
        store=provider, task_store=orders, balance=TaskBalanceLedger(tmp_path / 'balance.json'), messaging_key=key)
    request = seal_task(body=body, task_id='interrupted', kind='llm_request', sender_peer_id=provider.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=provider.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(key), expires_at=_expires()).to_dict()
    for _ in range(2):
        _, reply = open_task(service.handle(request), recipient_peer_id=provider.peer_id,
            recipient_messaging_key=key, expected_kind='llm_response')
        assert reply['error_code'] == 'provider_restarted_before_completion'
        assert reply['state'] == 'failed'
    assert adapter.calls == 0
    assert orders.get('interrupted') == recovered


def test_provider_executes_and_settles_once_without_persisting_bodies(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _FakeAdapter()
    manifest = LLMPackageManifest(
        package_id="svc", mode="openai_compatible", public_model_alias="alias",
        base_url="http://127.0.0.1:1",
    )
    orders = TaskOrderStore(tmp_path / "orders")
    balance = TaskBalanceLedger(tmp_path / "provider-balance.json")
    service = ProviderService(manifest=manifest, adapter=adapter, store=provider,
                              task_store=orders, balance=balance, messaging_key=provider_msg, access_check=lambda *_: {})
    request = seal_task(
        body={"task_id": "task_same", "service_id": "svc", "prompt": "TOP SECRET PROMPT",
              "max_tokens": 8, "max_amount": 1, "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_same", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    first = service.handle(request)
    second = service.handle(request)
    assert first == second and adapter.calls == 1
    changed = seal_task(
        body={"task_id": "task_same", "service_id": "svc", "prompt": "CHANGED PROMPT",
              "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_same", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    with pytest.raises(TaskProtocolError, match="idempotency conflict"):
        service.handle(changed)
    _, result = open_task(first, recipient_peer_id=consumer.peer_id,
                          recipient_messaging_key=consumer_msg, expected_kind="llm_response")
    assert result["output"] == "provider output" and result["state"] == "succeeded"
    disk = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    assert "TOP SECRET PROMPT" not in disk
    assert "provider output" not in disk
    settlement = sign_payload({
        "kind": "llm_settlement", "task_id": "task_same", "from_peer_id": consumer.peer_id,
        "to_peer_id": provider.peer_id, "amount": result["amount"], "service_id": "svc",
        "settlement_id": "settle:task_same",
    }, private_key_bytes=consumer.private_key_bytes).to_dict()
    attacker = RynmeshStore(home=tmp_path / "attacker", network_dir=net)
    forged = sign_payload({
        "kind": "llm_settlement", "task_id": "task_same", "from_peer_id": attacker.peer_id,
        "to_peer_id": provider.peer_id, "amount": result["amount"], "service_id": "svc",
        "settlement_id": "settle:task_same",
    }, private_key_bytes=attacker.private_key_bytes).to_dict()
    with pytest.raises(Exception, match="not the task consumer"):
        service.settle_earning(forged)
    one = service.settle_earning(settlement)
    two = service.settle_earning(settlement)
    assert one["event_id"] == two["event_id"] == "earning:task_same"
    assert balance.summary()["earned"] == result["amount"]


def test_provider_bounds_retained_records_and_skips_paused_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("RYNMESH_LLM_MAX_PROVIDER_RECORDS_PER_PEER", "1")
    monkeypatch.setenv("RYNMESH_LLM_MAX_PROVIDER_RECORDS", "2")
    monkeypatch.setenv("RYNMESH_LLM_REQUESTS_PER_MINUTE", "10")
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    orders = TaskOrderStore(tmp_path / "orders")
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc", mode="openai_compatible", public_model_alias="alias",
            base_url="http://127.0.0.1:1",
        ),
        adapter=_FakeAdapter(), store=provider, task_store=orders,
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg, access_check=lambda *_: {},  # Admission is isolated from these protocol tests.
    )

    def request(task_id: str, prompt: str, reply_key: str | None = None) -> dict:
        return seal_task(
            body={"task_id": task_id, "service_id": "svc", "prompt": prompt,
                  "max_tokens": 8, "max_amount": 1,
                  "reply_messaging_pub": reply_key or peer_box.public_key_b64(consumer_msg)},
            task_id=task_id, kind="llm_request", sender_peer_id=consumer.peer_id,
            recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
            recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
        ).to_dict()

    service.handle(request("first", "one"))
    with pytest.raises(TaskProtocolError, match="peer_task_record_limit"):
        service.handle(request("second", "two"))
    assert len(list((tmp_path / "orders").glob("*.json"))) == 1

    service.accepting_orders = False
    rejected = service.handle(request("paused", "three"))
    _, result = open_task(
        rejected, recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_msg, expected_kind="llm_response",
    )
    assert result["error_code"] == "service_paused"
    assert len(list((tmp_path / "orders").glob("*.json"))) == 1

    with pytest.raises(TaskProtocolError, match="messaging key is invalid"):
        service.handle(request("invalid-key", "four", "not-base64"))
    assert len(list((tmp_path / "orders").glob("*.json"))) == 1


def test_task_store_prunes_expired_terminal_records(tmp_path):
    store = TaskOrderStore(tmp_path / "orders")
    store.transition(task_id="old", state="created")
    store.transition(task_id="old", state="failed")
    path = tmp_path / "orders" / "old.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    path.write_text(json.dumps(record), encoding="utf-8")
    store.transition(task_id="new", state="created")
    store.transition(task_id="new", state="failed")
    removed = store.prune_terminal(
        older_than=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert removed == 1
    assert store.get("old") is None
    assert store.get("new") is not None


class _PacketConnection:
    def __init__(self, packets):
        self.packets = iter(packets)

    async def recv(self):
        return next(self.packets)


def _p2p_packet(message_id: bytes, *, sequence: int, total: int, body: bytes = b"x") -> bytes:
    return llm_p2p._HEADER.pack(
        llm_p2p._MAGIC, llm_p2p._DATA, message_id, sequence, total, b"d" * 32,
    ) + body


def test_p2p_receiver_rejects_oversized_chunk_declarations():
    packet = _p2p_packet(
        b"a" * 16, sequence=0, total=llm_p2p._MAX_CHUNKS + 1,
    )
    with pytest.raises(P2PError, match="declaration exceeds safe limits"):
        asyncio.run(receive_json(_PacketConnection([packet]), timeout_s=1))


def test_p2p_receiver_bounds_simultaneous_messages():
    packets = [
        _p2p_packet(index.to_bytes(16), sequence=0, total=2)
        for index in range(llm_p2p._MAX_IN_FLIGHT_MESSAGES + 1)
    ]
    with pytest.raises(P2PError, match="too many simultaneous"):
        asyncio.run(receive_json(_PacketConnection(packets), timeout_s=1))


def test_reliable_p2p_send_bounds_each_udp_burst_to_its_window():
    class Connection:
        def __init__(self) -> None:
            self.acks: list[bytes] = []
            self.sent_since_recv = 0
            self.peak_burst = 0

        async def send(self, packet: bytes) -> None:
            kind, message_id, sequence, total, digest, _body = llm_p2p._decode_header(
                packet
            )
            assert kind == llm_p2p._DATA
            self.sent_since_recv += 1
            self.peak_burst = max(self.peak_burst, self.sent_since_recv)
            self.acks.append(
                llm_p2p._HEADER.pack(
                    llm_p2p._MAGIC,
                    llm_p2p._ACK,
                    message_id,
                    sequence,
                    total,
                    digest,
                )
            )

        async def recv(self) -> bytes:
            self.sent_since_recv = 0
            return self.acks.pop(0)

    connection = Connection()
    payload = b"x" * (llm_p2p._CHUNK_BYTES * (llm_p2p._SEND_WINDOW + 2))
    sent = asyncio.run(llm_p2p.send_bytes(connection, payload, timeout_s=2))
    assert sent == len(payload)
    assert connection.peak_burst == llm_p2p._SEND_WINDOW == 8


def test_provider_concurrent_duplicate_executes_once(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _BlockingAdapter()
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc", mode="openai_compatible", public_model_alias="alias",
            base_url="http://127.0.0.1:1", timeout_seconds=2,
        ),
        adapter=adapter, store=provider, task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg, access_check=lambda *_: {},  # Admission is isolated from these protocol tests.
    )
    request = seal_task(
        body={"task_id": "task_concurrent", "idempotency_key": "same-request",
              "service_id": "svc", "prompt": "private", "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_concurrent", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    responses = []
    threads = [threading.Thread(target=lambda: responses.append(service.handle(request))) for _ in range(2)]
    threads[0].start()
    assert adapter.started.wait(timeout=2)
    threads[1].start()
    adapter.release.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert adapter.calls == 1
    assert len(responses) == 2 and responses[0] == responses[1]
    assert service.task_store.get("task_concurrent")["state"] == "succeeded"


def test_signed_cancel_reaches_running_provider_and_rejects_other_identity(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    attacker = RynmeshStore(home=tmp_path / "attacker", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _BlockingAdapter()
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc", mode="openai_compatible", public_model_alias="alias",
            base_url="http://127.0.0.1:1", timeout_seconds=2,
        ),
        adapter=adapter, store=provider, task_store=TaskOrderStore(tmp_path / "orders"),
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg, access_check=lambda *_: {},  # Admission is isolated from these protocol tests.
    )
    request = seal_task(
        body={"task_id": "task_cancel_running", "idempotency_key": "cancel-running",
              "service_id": "svc", "prompt": "private", "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="task_cancel_running", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    responses = []
    worker = threading.Thread(target=lambda: responses.append(service.handle(request)))
    worker.start()
    assert adapter.started.wait(timeout=2)

    def cancellation(sender: RynmeshStore) -> dict:
        return sign_payload({
            "kind": "llm_cancel", "task_id": "task_cancel_running",
            "from_peer_id": sender.peer_id, "to_peer_id": provider.peer_id,
            "service_id": "svc", "cancel_id": "cancel:task_cancel_running",
        }, private_key_bytes=sender.private_key_bytes).to_dict()

    with pytest.raises(Exception, match="not the task consumer"):
        service.cancel_signed(cancellation(attacker))
    assert service.cancel_signed(cancellation(consumer)) is True
    assert service.task_store.get("task_cancel_running")["state"] == "cancelled"
    adapter.release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    _, result = open_task(
        responses[0], recipient_peer_id=consumer.peer_id,
        recipient_messaging_key=consumer_msg, expected_kind="llm_response",
    )
    assert result["state"] == "cancelled"
    assert result["error_code"] == "consumer_cancelled"
    assert adapter.cancelled == ["task_cancel_running"]


def test_consumer_rejects_response_signed_by_another_provider(tmp_path):
    net = tmp_path / "net"
    expected = RynmeshStore(home=tmp_path / "expected", network_dir=net)
    rogue = RynmeshStore(home=tmp_path / "rogue", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    response = seal_task(
        body={"task_id": "task_response", "service_id": "svc", "state": "succeeded",
              "input_tokens": 1, "output_tokens": 1, "duration_ms": 1, "amount": 0.001,
              "output": "rogue"},
        task_id="task_response", kind="llm_response", sender_peer_id=rogue.peer_id,
        recipient_peer_id=consumer.peer_id, sender_signing_key=rogue.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(consumer_msg), expires_at=_expires(),
    ).to_dict()
    with pytest.raises(Exception, match="not the selected provider"):
        _open_provider_response(
            response, recipient_peer_id=consumer.peer_id, messaging_key=consumer_msg,
            task_id="task_response", provider_peer_id=expected.peer_id, service_id="svc",
        )
    with pytest.raises(TaskProtocolError, match="response task mismatch"):
        _open_provider_response(
            response, recipient_peer_id=consumer.peer_id, messaging_key=consumer_msg,
            task_id="different_original_task", provider_peer_id=rogue.peer_id, service_id="svc",
        )


def test_provider_explicitly_rejects_capacity_and_cancel_is_terminal(tmp_path):
    net = tmp_path / "net"
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    provider_msg = peer_box.load_or_create_messaging_key(tmp_path / "provider-msg")
    consumer_msg = peer_box.load_or_create_messaging_key(tmp_path / "consumer-msg")
    adapter = _FakeAdapter()
    manifest = LLMPackageManifest(
        package_id="svc", mode="openai_compatible", public_model_alias="alias",
        base_url="http://127.0.0.1:1", max_concurrent=1,
    )
    orders = TaskOrderStore(tmp_path / "orders")
    service = ProviderService(
        manifest=manifest, adapter=adapter, store=provider, task_store=orders,
        balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=provider_msg, access_check=lambda *_: {},  # Admission is isolated from these protocol tests.
    )
    request = seal_task(
        body={"task_id": "busy_task", "service_id": "svc", "prompt": "body",
              "max_tokens": 8, "max_amount": 1,
              "reply_messaging_pub": peer_box.public_key_b64(consumer_msg)},
        task_id="busy_task", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(provider_msg), expires_at=_expires(),
    ).to_dict()
    assert service._slots.acquire(blocking=False)
    try:
        encrypted = service.handle(request)
    finally:
        service._slots.release()
    _, result = open_task(encrypted, recipient_peer_id=consumer.peer_id,
                          recipient_messaging_key=consumer_msg, expected_kind="llm_response")
    assert result["state"] == "rejected" and result["error_code"] == "capacity_exhausted"
    assert adapter.calls == 0
    orders.transition(task_id="cancel_me", state="created", metadata={"service_id": "svc"})
    assert service.cancel("cancel_me") is True
    assert orders.get("cancel_me")["state"] == "cancelled"
    assert adapter.cancelled == ["cancel_me"]


def test_fixed_port_overlap_is_rejected_and_port_reusable_after_close(monkeypatch):
    import socket

    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("RYNMESH_P2P_BIND_PORT", str(port))

    async def scenario():
        first = new_connection(controlling=False)
        second = new_connection(controlling=False)
        try:
            assert len(await first.get_component_candidates(1, ["127.0.0.1"])) == 1
            # Use another thread/event loop, as provider offer handlers do.
            def overlap():
                with pytest.raises(llm_p2p.P2PCapacityError) as caught:
                    asyncio.run(second.get_component_candidates(1, ["127.0.0.1"]))
                assert _delivery_error_code(caught.value, transport="p2p") == "p2p_capacity_exhausted"
            await asyncio.to_thread(overlap)
        finally:
            await second.close()
            await first.close()
        third = new_connection(controlling=False)
        try:
            assert len(await third.get_component_candidates(1, ["127.0.0.1"])) == 1
        finally:
            await third.close()

    asyncio.run(scenario())


def test_fixed_port_failed_bind_releases_reservation(monkeypatch):
    import socket

    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        monkeypatch.setenv("RYNMESH_P2P_BIND_PORT", str(port))

        async def fail():
            connection = new_connection(controlling=False)
            try:
                with pytest.raises(P2PError, match="fixed_port_unavailable"):
                    await connection.get_component_candidates(1, ["127.0.0.1"])
            finally:
                await connection.close()
        asyncio.run(fail())

    async def retry():
        connection = new_connection(controlling=False)
        try:
            assert await connection.get_component_candidates(1, ["127.0.0.1"])
        finally:
            await connection.close()
    asyncio.run(retry())
