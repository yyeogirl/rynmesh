"""Download recovery against real HTTP responses; TLS policy is tested separately."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rynmesh.llm_package import model_download
from rynmesh.llm_package.errors import LifecycleError

BODY = b"synthetic-model-recovery-" * 256
DIGEST = hashlib.sha256(BODY).hexdigest()
URL = "https://example.invalid/pinned/model.gguf"


@pytest.fixture
def download_server(monkeypatch):
    state = {"mode": "normal", "ranges": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requested = self.headers.get("Range")
            state["ranges"].append(requested)
            offset = int(requested[6:-1]) if requested else 0
            mode = state["mode"]
            payload = BODY[offset:]
            if mode == "early_416" or offset >= len(BODY):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206 if requested else 200)
            if requested and mode != "missing_range":
                start = 32 if mode == "wrong_offset" else offset
                total = len(BODY) + 1 if mode == "wrong_total" else len(BODY)
                end = len(BODY) - 2 if mode == "wrong_length" else len(BODY) - 1
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Content-Length", "invalid" if mode == "bad_length" else str(len(payload)))
            if mode == "encoded":
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(payload[:1024] if mode == "truncated" else payload)
            self.wfile.flush()
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["transport_url"] = f"http://127.0.0.1:{server.server_port}/model"

    def open_local(request, timeout=300):
        # Keep the downloader's HTTPS input contract, substituting only the
        # transport for the fixture. Real urllib parses the wire response.
        local = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/model", headers=dict(request.header_items()),
        )
        return urllib.request.urlopen(local, timeout=timeout)

    monkeypatch.setattr(model_download, "_urlopen", open_local)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_truncated_body_keeps_received_prefix_for_the_next_range(tmp_path, download_server):
    destination = tmp_path / "model.gguf"
    part = tmp_path / "model.gguf.part"
    download_server["mode"] = "truncated"
    with pytest.raises(LifecycleError, match="incomplete"):
        model_download.download(URL, destination, DIGEST, size_bytes=len(BODY))
    assert part.read_bytes() == BODY[:1024]
    assert not destination.exists()
    assert not (tmp_path / "model.gguf.corrupt").exists()
    download_server["mode"] = "normal"
    # Retry from a fresh process: only the saved bytes carry across the restart.
    retry = subprocess.run([
        sys.executable, "-c", """
import sys, urllib.request
from pathlib import Path
from rynmesh.llm_package import model_download
def open_local(request, timeout=300):
    local = urllib.request.Request(sys.argv[2], headers=dict(request.header_items()))
    return urllib.request.urlopen(local, timeout=timeout)
model_download._urlopen = open_local
result = model_download.download(sys.argv[3], Path(sys.argv[1]), sys.argv[4], size_bytes=int(sys.argv[5]))
print(result)
""", str(destination), download_server["transport_url"], URL, DIGEST, str(len(BODY)),
    ], capture_output=True, text=True, timeout=30)
    assert retry.returncode == 0
    assert retry.stdout.strip() == DIGEST
    assert download_server["ranges"] == [None, "bytes=1024-"]
    assert destination.read_bytes() == BODY


@pytest.mark.parametrize("mode", [
    "missing_range", "wrong_offset", "encoded", "early_416", "wrong_total", "wrong_length", "bad_length",
])
def test_invalid_resume_response_preserves_original_prefix(tmp_path, download_server, mode):
    destination = tmp_path / "model.gguf"
    part = tmp_path / "model.gguf.part"
    prefix = BODY[:256]
    part.write_bytes(prefix)
    download_server["mode"] = mode
    with pytest.raises(LifecycleError) as failure:
        model_download.download(URL, destination, DIGEST, size_bytes=len(BODY))
    assert part.read_bytes() == prefix
    assert not destination.exists()
    assert not (tmp_path / "model.gguf.corrupt").exists()
    assert "example.invalid" not in str(failure.value)
    assert str(tmp_path) not in str(failure.value)
    download_server["mode"] = "normal"
    assert model_download.download(URL, destination, DIGEST, size_bytes=len(BODY)) == DIGEST
    assert download_server["ranges"] == ["bytes=256-", "bytes=256-"]
    assert destination.read_bytes() == BODY


def test_cancel_before_open_does_not_contact_the_source(tmp_path, download_server):
    with pytest.raises(LifecycleError, match="setup cancelled"):
        model_download.download(URL, tmp_path / "model.gguf", DIGEST, cancel_check=lambda: True)
    assert download_server["ranges"] == []
    assert list(tmp_path.iterdir()) == []


def test_cancel_during_verification_keeps_complete_part_until_retry(tmp_path, download_server):
    destination = tmp_path / "model.gguf"
    stages = []
    cancelled = False

    def progress(stage, percent, message):
        nonlocal cancelled
        stages.append((stage, percent, message))
        if stage == "checksum":
            cancelled = True

    with pytest.raises(LifecycleError, match="setup cancelled"):
        model_download.download(
            URL, destination, DIGEST, size_bytes=len(BODY),
            progress=progress, cancel_check=lambda: cancelled,
        )
    assert not destination.exists()
    assert (tmp_path / "model.gguf.part").read_bytes() == BODY
    assert all("verification pending" in message for stage, _, message in stages if stage == "download_model")
    assert model_download.download(URL, destination, DIGEST, size_bytes=len(BODY)) == DIGEST
    assert destination.read_bytes() == BODY
