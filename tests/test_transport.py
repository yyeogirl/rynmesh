"""Tests for the pluggable transport seam + active-probe resistance."""

from __future__ import annotations

import hashlib

import pytest

from rynmesh.transport import (
    NETWORK_KEY_HEADER,
    StdlibHttpsTransport,
    TransportError,
    TransportProfile,
    get_transport,
    register_transport,
    reset_transport_cache,
    resolve_profile,
)


def test_default_profile_is_camouflage_with_browser_ua(monkeypatch) -> None:
    monkeypatch.delenv("RYNMESH_TRANSPORT", raising=False)
    profile = resolve_profile()
    assert profile.name == "camouflage"
    assert "Mozilla/" in profile.headers["User-Agent"]
    assert "h2" in profile.alpn  # browser-like ALPN


def test_direct_profile_keeps_legacy_identity(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_TRANSPORT", "direct")
    profile = resolve_profile()
    assert profile.name == "direct"
    assert profile.headers["User-Agent"] == "Rynmesh/0.1"


def test_hardened_profile_requires_tls13(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_TRANSPORT", "hardened")
    profile = resolve_profile()
    assert profile.name == "hardened"
    assert profile.tls_min == "1.3"


def test_proxy_env_routes_through_proxy(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_HTTPS_PROXY", "http://127.0.0.1:9999")
    profile = resolve_profile()
    assert profile.proxies.get("https") == "http://127.0.0.1:9999"


def test_network_key_is_sent_as_salted_hash(monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "swordfish")
    transport = StdlibHttpsTransport(TransportProfile())
    headers = transport._headers(None)
    expected = hashlib.sha256(b"rynmesh-net-key:swordfish").hexdigest()
    assert headers[NETWORK_KEY_HEADER] == expected
    assert "swordfish" not in headers[NETWORK_KEY_HEADER]  # raw key never sent


def test_stdlib_post_is_bounded_and_preserves_required_auth(monkeypatch) -> None:
    import http.server
    import threading

    seen: dict[str, object] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            seen["body"] = self.rfile.read(length)
            seen["content_type"] = self.headers.get("Content-Type")
            seen["auth"] = self.headers.get(NETWORK_KEY_HEADER)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.end_headers()
                return
            body = b"12345" if self.path == "/oversized" else b'{"ok":true}'
            if self.path == "/exact":
                body = b"1234"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("RYNMESH_NETWORK_KEY", "required-key")
        transport = StdlibHttpsTransport(TransportProfile())
        url = f"http://127.0.0.1:{server.server_address[1]}"
        response = transport.post_bytes(
            url + "/ok", b'{"prompt":"marker"}', timeout_s=5, max_bytes=64,
            headers={
                "Content-Type": "application/json",
                NETWORK_KEY_HEADER: "attacker-override",
            },
        )
        assert response == b'{"ok":true}'
        assert seen["body"] == b'{"prompt":"marker"}'
        assert seen["content_type"] == "application/json"
        assert seen["auth"] == hashlib.sha256(
            b"rynmesh-net-key:required-key",
        ).hexdigest()
        assert transport.post_bytes(
            url + "/exact", b"{}", timeout_s=5, max_bytes=4,
        ) == b"1234"
        with pytest.raises(TransportError, match="too large") as error:
            transport.post_bytes(
                url + "/oversized", b"{}", timeout_s=5, max_bytes=4,
            )
        assert error.value.reason == "too_large"
        with pytest.raises(TransportError) as error:
            transport.post_bytes(
                url + "/redirect", b"{}", timeout_s=5, max_bytes=64,
            )
        assert error.value.reason == "http_error"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_transport_plugin_registry(monkeypatch) -> None:
    reset_transport_cache()
    sentinel = object()
    monkeypatch.setenv("RYNMESH_TRANSPORT", "fakeobfs")
    register_transport("fakeobfs", lambda profile: sentinel)  # type: ignore[arg-type,return-value]
    try:
        assert get_transport() is sentinel
    finally:
        reset_transport_cache()


def test_active_probe_resistance_blocks_anonymous_peer_requests(monkeypatch) -> None:
    """With a network key set, an unauthenticated probe gets a generic 404 on
    the peer surface (no Rynmesh banner); a correctly-keyed request gets 200."""
    pytest.importorskip("fastapi")
    pytest.importorskip("cryptography")
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "swordfish")

    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    node = RynmeshStore(home=d / "node", network_dir=d / "mesh", node_name="probe")
    client = TestClient(create_app(node))

    # Anonymous probe: peer surface + health look like an empty server.
    assert client.get("/health").status_code == 404
    assert client.get("/api/v1/node").status_code == 404

    # Correctly-keyed peer request succeeds.
    auth = hashlib.sha256(b"rynmesh-net-key:swordfish").hexdigest()
    assert client.get("/health", headers={"X-Ryn-Auth": auth}).status_code == 200
    assert client.get("/api/v1/node", headers={"X-Ryn-Auth": auth}).status_code == 200


def test_active_probe_resistance_guards_registry_and_relay(monkeypatch, tmp_path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rynmesh.registry import FilePeerRegistry
    from rynmesh.registry_http import create_app
    from rynmesh.relay import FileRelayStore

    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "registry-secret")
    client = TestClient(create_app(
        FilePeerRegistry(tmp_path / "registry"),
        relay_store=FileRelayStore(tmp_path / "relay"),
    ))
    auth = hashlib.sha256(b"rynmesh-net-key:registry-secret").hexdigest()

    # The coordination surface stays hidden from unauthenticated probes...
    assert client.get("/api/v1/jobs/capacity").status_code == 404
    assert client.post("/api/v1/relay/blobs", content=b"probe").status_code == 404
    assert client.get(
        "/api/v1/jobs/capacity", headers={"X-Ryn-Auth": auth}
    ).status_code == 200
    # ...but /health must answer plain probes: load balancers and orchestrator
    # healthchecks can't compute the derived header, and a 404 there marks a
    # healthy registry as down and restart-loops it. Unauthenticated callers
    # get liveness only; the registry identity needs the mesh key.
    plain = client.get("/health")
    assert plain.status_code == 200
    assert "kind" not in plain.json()
    authed = client.get("/health", headers={"X-Ryn-Auth": auth})
    assert authed.status_code == 200
    assert authed.json().get("kind") == "rynmesh-registry"


def test_get_transport_selects_fronted_when_sni_or_connect_set(monkeypatch) -> None:
    from rynmesh.transport import FrontedHttpsTransport, StdlibHttpsTransport

    reset_transport_cache()
    monkeypatch.delenv("RYNMESH_TRANSPORT", raising=False)
    monkeypatch.delenv("RYNMESH_TLS_SNI", raising=False)
    monkeypatch.delenv("RYNMESH_CONNECT_HOST", raising=False)
    assert isinstance(get_transport(), StdlibHttpsTransport)

    reset_transport_cache()
    monkeypatch.setenv("RYNMESH_TLS_SNI", "cdn.example.com")
    assert isinstance(get_transport(), FrontedHttpsTransport)
    reset_transport_cache()


def test_fronted_transport_splits_connect_host_and_host_header() -> None:
    """Over plain HTTP, the fronted transport must dial connect_host while
    sending the original Host header (the connect/Host split mechanism that, on
    TLS, also carries an overridden SNI)."""
    import http.server
    import threading

    from rynmesh.transport import FrontedHttpsTransport, TransportProfile

    seen: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["host"] = self.headers.get("Host", "")
            seen["ua"] = self.headers.get("User-Agent", "")
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            seen["post_body"] = self.rfile.read(length).decode()
            seen["post_host"] = self.headers.get("Host", "")
            seen["post_length"] = self.headers.get("Content-Length", "")
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", "/api/peer/llm/tasks")
                self.end_headers()
                return
            body = b'{"posted": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # connect_host = 127.0.0.1 (where the server really is); the URL names a
        # different backend host that becomes the Host header.
        profile = TransportProfile(
            name="fronted",
            headers={"User-Agent": "Mozilla/5.0 test"},
            connect_host="127.0.0.1",
        )
        transport = FrontedHttpsTransport(profile)
        url = f"http://backend.internal:{port}/api/v1/node"
        data = transport.get_bytes(url, timeout_s=5, max_bytes=1 << 20)
        assert data == b'{"ok": true}'
        assert seen["host"] == f"backend.internal:{port}"  # Host = backend, not connect_host
        assert seen["ua"] == "Mozilla/5.0 test"
        posted = transport.post_bytes(
            f"http://backend.internal:{port}/api/peer/llm/tasks",
            b'{"encrypted":"value"}', timeout_s=5, max_bytes=1 << 20,
            headers={"Content-Type": "application/json"},
        )
        assert posted == b'{"posted": true}'
        assert seen["post_host"] == f"backend.internal:{port}"
        assert seen["post_body"] == '{"encrypted":"value"}'
        assert seen["post_length"] == str(len(b'{"encrypted":"value"}'))
        with pytest.raises(TransportError) as error:
            transport.post_bytes(
                f"http://backend.internal:{port}/redirect", b"{}",
                timeout_s=5, max_bytes=1 << 20,
            )
        assert error.value.reason == "http_error"
    finally:
        server.shutdown()
        t.join(timeout=2)


def test_cdn_ws_profile_is_returned_for_cdn_ws_name(monkeypatch) -> None:
    reset_transport_cache()
    monkeypatch.setenv("RYNMESH_TRANSPORT", "cdn-ws")
    monkeypatch.setenv("RYNMESH_CDN_WS_URL", "wss://cdn.example.com/ryn-ws")
    profile = resolve_profile()
    assert profile.name == "cdn-ws"
    assert "Mozilla/" in profile.headers["User-Agent"]


def test_cdn_ws_transport_tunnels_request_over_websocket(monkeypatch) -> None:
    """End-to-end: CdnWebSocketTransport opens a WebSocket, sends the HTTP
    request as a binary frame, and returns the body from the response frame."""
    import base64
    import hashlib
    import http.server
    import threading

    from rynmesh.transport import CdnWebSocketTransport, TransportProfile

    requests: list[bytes] = []

    class MinimalWsHandler(http.server.BaseHTTPRequestHandler):
        """Implement just enough of RFC 6455 to accept an upgrade, receive one
        binary request frame, and send back a binary response frame."""

        def do_GET(self):  # noqa: N802
            # WebSocket upgrade
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()

            # Receive one masked binary frame (client→server)
            def recv_frame():
                raw = self.rfile
                hdr = raw.read(2)
                masked = bool(hdr[1] & 0x80)
                length = hdr[1] & 0x7F
                if length == 126:
                    length = int.from_bytes(raw.read(2), "big")
                mask_bytes = raw.read(4) if masked else b"\x00\x00\x00\x00"
                payload = bytearray(raw.read(length))
                return bytes(b ^ mask_bytes[i % 4] for i, b in enumerate(payload))

            requests.append(recv_frame())

            # Send one binary frame back (server→client, never masked)
            response_body = b'{"data": "hello from ws"}'
            http_response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(response_body)).encode() + b"\r\n"
                b"\r\n" + response_body
            )
            payload = http_response
            length = len(payload)
            if length < 126:
                frame_header = bytes([0x82, length])
            elif length < 65536:
                frame_header = bytes([0x82, 126]) + length.to_bytes(2, "big")
            else:
                frame_header = bytes([0x82, 127]) + length.to_bytes(8, "big")
            self.wfile.write(frame_header + payload)
            self.wfile.flush()

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), MinimalWsHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setenv("RYNMESH_CDN_WS_URL", f"ws://127.0.0.1:{port}/ryn-ws")
        profile = TransportProfile(name="cdn-ws", headers={"User-Agent": "Mozilla/5.0 test"})
        transport = CdnWebSocketTransport(profile)
        data = transport.get_bytes(
            f"http://backend.internal:{port}/api/v1/node",
            timeout_s=5,
            max_bytes=1 << 20,
        )
        assert b"hello from ws" in data
        posted = transport.post_bytes(
            f"http://backend.internal:{port}/api/peer/llm/tasks",
            b'{"encrypted":"marker"}', timeout_s=5, max_bytes=1 << 20,
            headers={"Content-Type": "application/json"},
        )
        assert b"hello from ws" in posted
        assert requests[0].startswith(b"GET /api/v1/node HTTP/1.1\r\n")
        assert requests[1].startswith(b"POST /api/peer/llm/tasks HTTP/1.1\r\n")
        assert b"Content-Type: application/json\r\n" in requests[1]
        assert requests[1].endswith(b'\r\n\r\n{"encrypted":"marker"}')
    finally:
        server.shutdown()
        t.join(timeout=2)


def test_no_network_key_keeps_peer_surface_open(monkeypatch) -> None:
    """Default (no key): peer surface stays open for plain P2P / dev."""
    pytest.importorskip("fastapi")
    pytest.importorskip("cryptography")
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.delenv("RYNMESH_NETWORK_KEY", raising=False)
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    node = RynmeshStore(home=d / "node", network_dir=d / "mesh", node_name="open")
    client = TestClient(create_app(node))
    assert client.get("/health").status_code == 200


class _PostTransport:
    def __init__(self, response: bytes = b'{"ok":true}') -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def post_bytes(self, url, body, *, timeout_s, max_bytes, headers=None):
        self.calls.append({
            "url": url, "body": body, "timeout_s": timeout_s,
            "max_bytes": max_bytes, "headers": headers,
        })
        return self.response


def test_http_peer_client_posts_compact_utf8_json_through_transport() -> None:
    from rynmesh.peer_http import HttpPeerClient

    transport = _PostTransport('{"reply":"完成"}'.encode())
    client = HttpPeerClient(
        "https://peer.example", timeout_s=17, transport=transport,  # type: ignore[arg-type]
    )
    result = client.post_json(
        "/api/peer/llm/tasks", {"prompt": "中文"}, max_bytes=1234,
    )
    assert result == {"reply": "完成"}
    call = transport.calls[0]
    assert call["url"] == "https://peer.example/api/peer/llm/tasks"
    assert call["timeout_s"] == 17
    assert call["max_bytes"] == 1234
    assert call["headers"] == {"Content-Type": "application/json"}
    assert call["body"] == '{"prompt":"中文"}'.encode()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (b"not-json", "peer_invalid_json"),
        (b"[]", "peer_response_not_object"),
        (b"\xff", "peer_invalid_json"),
    ],
)
def test_http_peer_client_rejects_invalid_post_json(response, message) -> None:
    from rynmesh.peer_http import HttpPeerClient, PeerTransportError

    client = HttpPeerClient(
        "https://peer.example", transport=_PostTransport(response),  # type: ignore[arg-type]
    )
    with pytest.raises(PeerTransportError, match=message):
        client.post_json("/api/peer/llm/tasks", {"private": "do-not-log"})


def test_http_peer_client_post_error_does_not_echo_request_body() -> None:
    from rynmesh.peer_http import HttpPeerClient, PeerTransportError

    class FailingTransport(_PostTransport):
        def post_bytes(self, *args, **kwargs):
            raise TransportError("connection unavailable", reason="http_error")

    client = HttpPeerClient(
        "https://peer.example", transport=FailingTransport(),  # type: ignore[arg-type]
    )
    with pytest.raises(PeerTransportError) as error:
        client.post_json("/api/peer/llm/tasks", {"prompt": "UNIQUE_PRIVATE_MARKER"})
    assert "UNIQUE_PRIVATE_MARKER" not in str(error.value)


def test_http_peer_client_maps_oversized_post_response() -> None:
    from rynmesh.peer_http import HttpPeerClient, PeerTransportError

    class OversizedTransport(_PostTransport):
        def post_bytes(self, *args, **kwargs):
            raise TransportError("response too large", reason="too_large")

    client = HttpPeerClient(
        "https://peer.example", transport=OversizedTransport(),  # type: ignore[arg-type]
    )
    with pytest.raises(PeerTransportError, match="peer_response_too_large"):
        client.post_json("/api/peer/llm/tasks", {})


def test_http_peer_client_rejects_transport_without_post() -> None:
    from rynmesh.peer_http import HttpPeerClient, PeerTransportError

    client = HttpPeerClient(
        "https://peer.example", transport=object(),  # type: ignore[arg-type]
    )
    with pytest.raises(PeerTransportError, match="peer_transport_post_unsupported"):
        client.post_json("/api/peer/llm/tasks", {})


def test_llm_peer_post_helper_uses_active_transport(monkeypatch) -> None:
    import rynmesh.peer_http as peer_http
    from rynmesh.llm_package.routes import _MAX_PEER_RESPONSE_BYTES, _peer_post_json

    transport = _PostTransport(b'{"state":"accepted"}')
    monkeypatch.setattr(peer_http, "get_transport", lambda: transport)
    result = _peer_post_json(
        "https://provider.example", "/api/peer/llm/tasks",
        {"payload": "encrypted-envelope"}, timeout_s=23,
    )
    assert result == {"state": "accepted"}
    assert transport.calls[0]["max_bytes"] == _MAX_PEER_RESPONSE_BYTES


def test_meek_post_uses_versioned_inner_request_envelope(monkeypatch) -> None:
    import base64
    import json

    from rynmesh.transport_plugins import MeekTransport

    monkeypatch.setenv("RYNMESH_MEEK_URL", "https://meek.example/bridge")
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "required-key")
    transport = MeekTransport(TransportProfile(name="meek"))
    captured: dict[str, object] = {}

    def bridge(payload, timeout_s, max_bytes):
        captured.update(payload=payload, timeout_s=timeout_s, max_bytes=max_bytes)
        return b'{"ok":true}'

    monkeypatch.setattr(transport, "_post_to_bridge", bridge)
    result = transport.post_bytes(
        "https://provider.example/api/peer/llm/tasks", b"ciphertext",
        timeout_s=9, max_bytes=99,
        headers={
            "Content-Type": "application/json",
            NETWORK_KEY_HEADER: "attacker-override",
        },
    )
    envelope = json.loads(captured["payload"])
    assert result == b'{"ok":true}'
    assert envelope["kind"] == "rynmesh.transport.request.v1"
    assert envelope["method"] == "POST"
    assert envelope["url"].endswith("/api/peer/llm/tasks")
    assert envelope["headers"]["Content-Type"] == "application/json"
    assert envelope["headers"][NETWORK_KEY_HEADER] == hashlib.sha256(
        b"rynmesh-net-key:required-key",
    ).hexdigest()
    assert base64.b64decode(envelope["body_b64"]) == b"ciphertext"


def test_reality_post_streams_bounded_response_and_preserves_auth(monkeypatch) -> None:
    from rynmesh.transport_plugins import RealityTransport

    class Response:
        def __init__(self, chunks):
            self.chunks = chunks
            self.closed = False
            self.status_code = 200

        def raise_for_status(self):
            return None

        def iter_content(self, *, chunk_size):
            assert chunk_size == 64 * 1024
            yield from self.chunks

        def close(self):
            self.closed = True

    class Session:
        def __init__(self, response):
            self.response = response
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.response

    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "required-key")
    transport = RealityTransport.__new__(RealityTransport)
    transport.profile = TransportProfile(name="reality")
    transport._resolve_url = lambda url, connect_host, sni: (url, {})

    response = Response([b"12", b"34"])
    session = Session(response)
    transport._session = lambda: (session, "", "")
    assert transport.post_bytes(
        "https://peer.example/api/peer/llm/tasks", b"ciphertext",
        timeout_s=8, max_bytes=4,
        headers={NETWORK_KEY_HEADER: "attacker-override"},
    ) == b"1234"
    _, call = session.calls[0]
    assert call["stream"] is True
    assert call["allow_redirects"] is False
    assert call["headers"][NETWORK_KEY_HEADER] == hashlib.sha256(
        b"rynmesh-net-key:required-key",
    ).hexdigest()
    assert response.closed is True

    oversized = Response([b"123", b"45"])
    transport._session = lambda: (Session(oversized), "", "")
    with pytest.raises(TransportError) as error:
        transport.post_bytes(
            "https://peer.example/api/peer/llm/tasks", b"ciphertext",
            timeout_s=8, max_bytes=4,
        )
    assert error.value.reason == "too_large"
    assert oversized.closed is True


def test_ech_fallback_post_uses_fronted_delegate() -> None:
    from rynmesh.transport_plugins import EchTransport

    delegate = _PostTransport(b'{"ok":true}')
    transport = EchTransport.__new__(EchTransport)
    transport._ech_active = False
    transport._delegate = delegate
    assert transport.post_bytes(
        "https://peer.example/api/peer/llm/tasks", b"ciphertext",
        timeout_s=12, max_bytes=345, headers={"Content-Type": "application/json"},
    ) == b'{"ok":true}'
    assert delegate.calls[0]["body"] == b"ciphertext"
    assert delegate.calls[0]["max_bytes"] == 345
