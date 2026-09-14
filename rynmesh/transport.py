"""Pluggable, camouflaged transport for Rynmesh peer traffic.

Rationale (see docs/RYNMESH_TRANSPORT_CENSORSHIP.md): a censor like the GFW does
not block "HTTPS"; it blocks *identifiable* things — known SNIs, IPs, distinctive
handshakes, and unknown fully-encrypted protocols. The winning strategy is NOT a
bespoke protocol (that is *easier* to fingerprint) but to look like ordinary
HTTPS the censor cannot afford to block, and to keep the wire format swappable.

This module is that seam. Rynmesh's real "private protocol" stays at the
application layer (Ed25519-signed, content-addressed objects); the transport
below is deliberately boring and camouflaged.

Design:
- `Transport` — minimal interface the peer client needs (GET bytes; stream to
  file), so obfuscating transports can be added without touching call sites.
- `StdlibHttpsTransport` — the default, **zero new dependencies** (urllib + ssl).
  Supports camouflage headers, a TLS profile (min version + ALPN), an outbound
  proxy (HTTP CONNECT / SOCKS-via-env for a pluggable-transport or Tor bridge),
  a shared network-key auth header, and redirect suppression (SSRF-safe).
- `register_transport()` — plugin point for heavier transports later
  (CDN-WebSocket, XTLS-REALITY mimicry, QUIC masquerade, obfs4/Snowflake PT).

Profiles are selected with `RYNMESH_TRANSPORT` (default: `camouflage`).
"""

from __future__ import annotations

import hashlib
import os
import ssl
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

# A realistic, current desktop-Chrome User-Agent. The UA travels *inside* TLS,
# so this is about not self-identifying as "Rynmesh" in logs / plaintext HTTP
# and blending with ordinary browser traffic, not about defeating passive DPI
# (which only sees the TLS ClientHello).
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

NETWORK_KEY_HEADER = "X-Ryn-Auth"


class TransportError(RuntimeError):
    """Transport-layer failure. ``reason`` lets callers map to domain errors."""

    def __init__(self, message: str, *, reason: str = "http_error") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class TransportProfile:
    """How outbound peer traffic should look on the wire."""

    name: str = "camouflage"
    headers: dict[str, str] = field(default_factory=dict)
    tls_min: str = "1.2"  # "1.2" | "1.3"
    alpn: tuple[str, ...] = ("h2", "http/1.1")
    verify_tls: bool = True
    proxies: dict[str, str] = field(default_factory=dict)  # {"https": "...", "http": "..."}
    # Advanced (CDN front / ECH): connect to one host but present another SNI.
    # The stdlib transport documents these as a plugin-only capability.
    sni: str = ""
    connect_host: str = ""


def network_key() -> str:
    """Shared peer-network key (enables active-probe resistance when set)."""
    return os.environ.get("RYNMESH_NETWORK_KEY", "").strip()


def network_key_header() -> dict[str, str]:
    """Outbound auth header for peers running probe resistance.

    Returns ``{X-Ryn-Auth: <salted hash>}`` when a network key is configured,
    else ``{}``. Single source of truth for the header name + value derivation
    so every peer→peer caller authenticates identically.
    """
    key = network_key()
    if not key:
        return {}
    # Sent as a salted hash so the raw key is not echoed into peer logs.
    return {
        NETWORK_KEY_HEADER: hashlib.sha256(
            ("rynmesh-net-key:" + key).encode("utf-8")
        ).hexdigest()
    }


def _profile_headers(profile: TransportProfile) -> dict[str, str]:
    headers = dict(profile.headers)
    headers.update(network_key_header())
    return headers


def resolve_profile() -> TransportProfile:
    """Build the active profile from RYNMESH_TRANSPORT + env overrides."""
    name = os.environ.get("RYNMESH_TRANSPORT", "camouflage").strip().lower() or "camouflage"
    proxies: dict[str, str] = {}
    https_proxy = os.environ.get("RYNMESH_HTTPS_PROXY", "").strip()
    http_proxy = os.environ.get("RYNMESH_HTTP_PROXY", "").strip()
    if https_proxy:
        proxies["https"] = https_proxy
    if http_proxy:
        proxies["http"] = http_proxy

    if name == "direct":
        # Today's plain behavior: identify as Rynmesh, default TLS.
        return TransportProfile(
            name="direct",
            headers={"User-Agent": "Rynmesh/0.1"},
            tls_min="1.2",
            alpn=(),
            proxies=proxies,
        )
    if name == "hardened":
        # Strongest blend: browser headers, TLS 1.3 only (pairs with ECH).
        return TransportProfile(
            name="hardened",
            headers=dict(_BROWSER_HEADERS),
            tls_min="1.3",
            alpn=("h2", "http/1.1"),
            proxies=proxies,
            sni=os.environ.get("RYNMESH_TLS_SNI", "").strip(),
            connect_host=os.environ.get("RYNMESH_CONNECT_HOST", "").strip(),
        )
    if name == "cdn-ws":
        # CDN-WebSocket: browser headers + TLS 1.2+ (broad CDN compatibility).
        return TransportProfile(
            name="cdn-ws",
            headers=dict(_BROWSER_HEADERS),
            tls_min="1.2",
            alpn=("h2", "http/1.1"),
            proxies=proxies,
            sni=os.environ.get("RYNMESH_TLS_SNI", "").strip(),
            connect_host=os.environ.get("RYNMESH_CONNECT_HOST", "").strip(),
        )
    if name == "reality":
        # REALITY: curl_cffi Chrome fingerprint + SNI/connect-host split.
        return TransportProfile(
            name="reality",
            headers=dict(_BROWSER_HEADERS),
            tls_min="1.3",
            alpn=("h2", "http/1.1"),
            proxies=proxies,
            sni=os.environ.get("RYNMESH_TLS_SNI", "").strip(),
            connect_host=os.environ.get("RYNMESH_CONNECT_HOST", "").strip(),
        )
    if name in ("meek", "ech"):
        # Meek / ECH: browser headers; TLS 1.2+ for broad compatibility.
        return TransportProfile(
            name=name,
            headers=dict(_BROWSER_HEADERS),
            tls_min="1.2",
            alpn=("h2", "http/1.1"),
            proxies=proxies,
            sni=os.environ.get("RYNMESH_TLS_SNI", "").strip(),
            connect_host=os.environ.get("RYNMESH_CONNECT_HOST", "").strip(),
        )
    # Default: camouflage — browser-like, broad TLS compatibility. A name that
    # matches a registered plugin transport is preserved so get_transport can
    # dispatch to it (with camouflage defaults as its base profile).
    return TransportProfile(
        name=name if name in _REGISTRY else "camouflage",
        headers=dict(_BROWSER_HEADERS),
        tls_min="1.2",
        alpn=("h2", "http/1.1"),
        proxies=proxies,
        sni=os.environ.get("RYNMESH_TLS_SNI", "").strip(),
        connect_host=os.environ.get("RYNMESH_CONNECT_HOST", "").strip(),
    )


class Transport(Protocol):
    """The minimal bounded-I/O surface the peer client needs."""

    def get_bytes(
        self, url: str, *, timeout_s: float, max_bytes: int, headers: dict[str, str] | None = None
    ) -> bytes: ...

    def post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> bytes: ...

    def download(
        self, url: str, dest: Path, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> Path: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Suppress redirects: a peer must not bounce us to an unvalidated host
    (SSRF) or to a censor-controlled endpoint."""

    def redirect_request(self, *args: Any, **kwargs: Any):  # noqa: D401
        return None


def _ssl_context(profile: TransportProfile) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        ctx.minimum_version = (
            ssl.TLSVersion.TLSv1_3 if profile.tls_min == "1.3" else ssl.TLSVersion.TLSv1_2
        )
    except (ValueError, AttributeError):  # pragma: no cover
        pass
    if profile.alpn:
        try:
            ctx.set_alpn_protocols(list(profile.alpn))
        except NotImplementedError:  # pragma: no cover
            pass
    if not profile.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class StdlibHttpsTransport:
    """Default transport: urllib + ssl, no third-party dependencies.

    Covers the high-value, low-risk camouflage levers (browser headers, TLS
    min/ALPN, outbound proxy, redirect suppression, network-key auth). When
    SNI/connect-host splitting is requested, `get_transport` selects
    `FrontedHttpsTransport` instead.
    """

    def __init__(self, profile: TransportProfile) -> None:
        self.profile = profile
        self._opener = self._build_opener(profile)

    def _build_opener(self, profile: TransportProfile) -> urllib.request.OpenerDirector:
        ctx = _ssl_context(profile)
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPSHandler(context=ctx),
            _NoRedirect(),
            # Ignore ambient OS proxy env unless explicitly configured.
            urllib.request.ProxyHandler(profile.proxies or {}),
        ]
        return urllib.request.build_opener(*handlers)

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        merged = _profile_headers(self.profile)
        if extra:
            merged.update(extra)
        # Caller-supplied headers must not suppress mesh authentication.
        merged.update(network_key_header())
        return merged

    def get_bytes(
        self, url: str, *, timeout_s: float, max_bytes: int, headers: dict[str, str] | None = None
    ) -> bytes:
        req = urllib.request.Request(url, method="GET", headers=self._headers(headers))
        try:
            with self._opener.open(req, timeout=timeout_s) as resp:
                data = resp.read(max_bytes + 1)
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            raise TransportError(f"http error: {exc}", reason="http_error") from exc
        if len(data) > max_bytes:
            raise TransportError("response too large", reason="too_large")
        return data

    def post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        req = urllib.request.Request(
            url, data=body, method="POST", headers=self._headers(headers),
        )
        try:
            with self._opener.open(req, timeout=timeout_s) as resp:
                data = resp.read(max_bytes + 1)
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            raise TransportError(f"http error: {exc}", reason="http_error") from exc
        if len(data) > max_bytes:
            raise TransportError("response too large", reason="too_large")
        return data

    def download(
        self, url: str, dest: Path, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> Path:
        dest = Path(dest).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, method="GET", headers=self._headers(headers))
        try:
            with self._opener.open(req, timeout=timeout_s) as resp, dest.open("wb") as handle:
                total = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise TransportError("media too large", reason="too_large")
                    handle.write(chunk)
        except TransportError:
            dest.unlink(missing_ok=True)
            raise
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            dest.unlink(missing_ok=True)
            raise TransportError(f"http error: {exc}", reason="http_error") from exc
        return dest


class FrontedHttpsTransport:
    """Transport with a real SNI / connect-host / Host split (stdlib only).

    Defeats the GFW's dominant TLS method — SNI filtering — by decoupling three
    things urllib conflates:

    - **connect_host**: the IP/host the TCP socket actually opens to (e.g. a CDN
      edge or an unblocked IP).
    - **sni** (`RYNMESH_TLS_SNI`): the server name presented in the *cleartext*
      TLS ClientHello — set this to a benign/allowed name the censor will not
      block. Certificate validation (when enabled) is done against this name.
    - **Host header**: the real backend the URL names, so a CDN/origin routes
      correctly.

    Pure `socket` + `ssl` + `http.client`; no third-party dependency. This is the
    practical substitute for Encrypted Client Hello (ECH), which Python's `ssl`
    cannot yet do (it needs OpenSSL 3.5+ ECH APIs not exposed by CPython).
    """

    def __init__(self, profile: TransportProfile) -> None:
        self.profile = profile
        self._ctx = _ssl_context(profile)

    def _headers(self, host_header: str, extra: dict[str, str] | None) -> dict[str, str]:
        merged = _profile_headers(self.profile)
        merged["Host"] = host_header  # real backend; routed by CDN/origin
        merged["Connection"] = "close"  # one socket per request
        if extra:
            merged.update(extra)
        merged.update(network_key_header())
        return merged

    def _open(
        self, url: str, timeout_s: float, extra_headers: dict[str, str] | None,
        *, method: str = "GET", body: bytes | None = None,
    ):
        import http.client
        import socket

        parts = urlparse(url)
        scheme = parts.scheme
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        connect_host = self.profile.connect_host or host
        sni = self.profile.sni or host
        is_default_port = port == (443 if scheme == "https" else 80)
        host_header = host if is_default_port else f"{host}:{port}"

        raw = socket.create_connection((connect_host, port), timeout=timeout_s)
        try:
            if scheme == "https":
                raw = self._ctx.wrap_socket(raw, server_hostname=sni)
            conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
            conn.sock = raw  # use our pre-dialed (and TLS-wrapped) socket
            request_headers = self._headers(host_header, extra_headers)
            if body is not None:
                request_headers["Content-Length"] = str(len(body))
            conn.request(method, path, body=body, headers=request_headers)
            resp = conn.getresponse()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            try:
                raw.close()
            except OSError:
                pass
            raise TransportError(f"http error: {exc}", reason="http_error") from exc
        if not 200 <= resp.status < 300:
            conn.close()
            raise TransportError(f"http status {resp.status}", reason="http_error")
        return conn, resp

    def get_bytes(
        self, url: str, *, timeout_s: float, max_bytes: int, headers: dict[str, str] | None = None
    ) -> bytes:
        conn, resp = self._open(url, timeout_s, headers)
        try:
            data = resp.read(max_bytes + 1)
        except (OSError, ssl.SSLError) as exc:
            raise TransportError(f"http error: {exc}", reason="http_error") from exc
        finally:
            conn.close()
        if len(data) > max_bytes:
            raise TransportError("response too large", reason="too_large")
        return data

    def post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        conn, resp = self._open(url, timeout_s, headers, method="POST", body=body)
        try:
            data = resp.read(max_bytes + 1)
        except (OSError, ssl.SSLError) as exc:
            raise TransportError(f"http error: {exc}", reason="http_error") from exc
        finally:
            conn.close()
        if len(data) > max_bytes:
            raise TransportError("response too large", reason="too_large")
        return data

    def download(
        self, url: str, dest: Path, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> Path:
        dest = Path(dest).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        conn, resp = self._open(url, timeout_s, headers)
        try:
            with dest.open("wb") as handle:
                total = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise TransportError("media too large", reason="too_large")
                    handle.write(chunk)
        except TransportError:
            dest.unlink(missing_ok=True)
            raise
        except (OSError, ssl.SSLError) as exc:
            dest.unlink(missing_ok=True)
            raise TransportError(f"http error: {exc}", reason="http_error") from exc
        finally:
            conn.close()
        return dest


class CdnWebSocketTransport:
    """Tunnel peer traffic over WebSocket to a CDN/relay endpoint.

    What the censor sees: a browser WebSocket upgrade to a major CDN edge
    (Cloudflare, Fastly, …). That edge is shared by millions of sites — blocking
    it causes enormous collateral damage, so it is one of the hardest edges to
    block. This is the "collateral damage" strategy in practice.

    What actually happens: each peer request is sent as a single
    binary WebSocket frame (the raw HTTP/1.1 request bytes); the server
    (rynmesh node behind the CDN) reads that frame and returns the response
    as a binary frame. The CDN just proxies WebSocket traffic — it never sees
    Rynmesh-specific content because all objects are already encrypted at the
    application layer.

    Server-side requirement: a Rynmesh node must be fronted by a CDN/reverse-proxy
    configured to pass WebSocket upgrades through (Cloudflare Flexible/Full, nginx
    `proxy_read_timeout`, etc.) — see docs/RYNMESH_TRANSPORT_CENSORSHIP.md §8.

    Zero third-party dependencies: the WebSocket upgrade and frame codec are
    implemented directly over `ssl.wrap_socket`. Only binary text frames are
    used (opcode 0x82) with a fresh random 4-byte mask per frame (required by
    the WebSocket spec for client→server frames).

    Config:
    - `RYNMESH_TRANSPORT=cdn-ws`
    - `RYNMESH_CDN_WS_URL`    base wss:// URL of the WebSocket relay endpoint
                               e.g. wss://ryn.cdn.example.com/ryn-ws
    - `RYNMESH_TLS_SNI`        override ClientHello SNI (optional; defaults to
                               the CDN hostname)
    - `RYNMESH_CONNECT_HOST`   override TCP connect host (optional; defaults to
                               the CDN hostname — use this to dial a specific
                               CDN edge IP)
    """

    _WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, profile: TransportProfile) -> None:
        self.profile = profile
        self._ctx = _ssl_context(profile)
        ws_url = os.environ.get("RYNMESH_CDN_WS_URL", "").strip()
        if not ws_url:
            raise TransportError(
                "RYNMESH_CDN_WS_URL is required for cdn-ws transport", reason="config_error"
            )
        p = urlparse(ws_url)
        if p.scheme not in ("ws", "wss"):
            raise TransportError(
                "RYNMESH_CDN_WS_URL must start with ws:// or wss://", reason="config_error"
            )
        self._ws_scheme = p.scheme
        self._ws_host = p.hostname or ""
        self._ws_port = p.port or (443 if p.scheme == "wss" else 80)
        self._ws_path = (p.path or "/ryn-ws") + (("?" + p.query) if p.query else "")

    def _ws_connect(self, timeout_s: float):
        """Open a fresh WebSocket connection, return the ssl-wrapped socket."""
        import socket

        connect_host = self.profile.connect_host or self._ws_host
        sni = self.profile.sni or self._ws_host
        raw = socket.create_connection((connect_host, self._ws_port), timeout=timeout_s)
        if self._ws_scheme == "wss":
            raw = self._ctx.wrap_socket(raw, server_hostname=sni)

        # WebSocket opening handshake (RFC 6455 §4.1).
        import base64 as _b64
        import os as _os

        nonce = _b64.b64encode(_os.urandom(16)).decode("ascii")
        host_header = (
            self._ws_host
            if self._ws_port in (80, 443)
            else f"{self._ws_host}:{self._ws_port}"
        )
        headers = {
            **_profile_headers(self.profile),
            "Host": host_header,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": nonce,
            "Sec-WebSocket-Version": "13",
        }
        req_lines = [f"GET {self._ws_path} HTTP/1.1"]
        req_lines.extend(f"{k}: {v}" for k, v in headers.items())
        req_lines += ["", ""]
        raw.sendall("\r\n".join(req_lines).encode("utf-8"))

        # Read the 101 Switching Protocols response.
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = raw.recv(4096)
            if not chunk:
                raw.close()
                raise TransportError("ws handshake: connection closed", reason="http_error")
            buf.extend(chunk)
        if b"101" not in buf[:20]:
            raw.close()
            raise TransportError(f"ws handshake failed: {buf[:80]!r}", reason="http_error")
        return raw

    @staticmethod
    def _ws_send_frame(sock: Any, payload: bytes) -> None:
        """Send a masked binary WebSocket frame (client→server MUST be masked)."""
        import os as _os

        mask = _os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        if length < 126:
            header = bytes([0x82, 0x80 | length]) + mask
        elif length < 65536:
            header = bytes([0x82, 0xFE]) + length.to_bytes(2, "big") + mask
        else:
            header = bytes([0x82, 0xFF]) + length.to_bytes(8, "big") + mask
        sock.sendall(header + masked)

    @staticmethod
    def _ws_recv_frame(sock: Any, max_bytes: int) -> bytes:
        """Receive one WebSocket frame (server→client frames are never masked)."""

        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise TransportError("ws: connection closed mid-frame", reason="http_error")
                buf.extend(chunk)
            return bytes(buf)

        header = recv_exact(2)
        # opcode: text=0x81, binary=0x82, close=0x88, ping=0x89
        opcode = header[0] & 0x0F
        if opcode == 0x08:
            raise TransportError("ws: server sent close frame", reason="http_error")
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(recv_exact(8), "big")
        if length > max_bytes:
            raise TransportError("ws: frame too large", reason="too_large")
        return recv_exact(length)

    def _do_request(
        self, url: str, timeout_s: float, max_bytes: int,
        extra_headers: dict[str, str] | None, *, method: str = "GET",
        body: bytes | None = None,
    ) -> bytes:
        """Tunnel one bounded HTTP request through a WebSocket connection."""
        parts = urlparse(url)
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        default_port = port in (80, 443)
        host_header = host if default_port else f"{host}:{port}"

        req_headers = dict(_profile_headers(self.profile))
        if extra_headers:
            req_headers.update(extra_headers)
        req_headers.update(network_key_header())
        req_headers["Host"] = host_header
        if body is not None:
            req_headers["Content-Length"] = str(len(body))
        req_lines = [f"{method} {path} HTTP/1.1"]
        req_lines.extend(f"{k}: {v}" for k, v in req_headers.items())
        req_lines += ["", ""]
        raw_request = "\r\n".join(req_lines).encode("utf-8") + (body or b"")

        sock = self._ws_connect(timeout_s)
        try:
            self._ws_send_frame(sock, raw_request)
            # Bound tunneled HTTP headers separately from the response body.
            raw_response = self._ws_recv_frame(sock, max_bytes + 64 * 1024)
        finally:
            try:
                sock.close()
            except OSError:
                pass

        # Parse the HTTP response out of the frame.
        sep = raw_response.find(b"\r\n\r\n")
        if sep == -1:
            raise TransportError("ws: malformed HTTP response frame", reason="http_error")
        status_line = raw_response[:sep].split(b"\r\n", 1)[0].split()
        try:
            status = int(status_line[1])
        except (IndexError, ValueError) as exc:
            raise TransportError("ws: malformed HTTP status", reason="http_error") from exc
        if not 200 <= status < 300:
            raise TransportError(f"http status {status}", reason="http_error")
        response_body = raw_response[sep + 4:]
        if len(response_body) > max_bytes:
            raise TransportError("response too large", reason="too_large")
        return response_body

    def get_bytes(
        self, url: str, *, timeout_s: float, max_bytes: int, headers: dict[str, str] | None = None
    ) -> bytes:
        try:
            return self._do_request(url, timeout_s, max_bytes, headers)
        except TransportError:
            raise
        except (OSError, ssl.SSLError) as exc:
            raise TransportError(f"cdn-ws error: {exc}", reason="http_error") from exc

    def post_bytes(
        self, url: str, body: bytes, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        try:
            return self._do_request(
                url, timeout_s, max_bytes, headers, method="POST", body=body,
            )
        except TransportError:
            raise
        except (OSError, ssl.SSLError) as exc:
            raise TransportError(f"cdn-ws error: {exc}", reason="http_error") from exc

    def download(
        self, url: str, dest: Path, *, timeout_s: float, max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> Path:
        data = self.get_bytes(url, timeout_s=timeout_s, max_bytes=max_bytes, headers=headers)
        dest = Path(dest).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest


# --- transport registry (plugin point) -----------------------------------

TransportFactory = Callable[[TransportProfile], Transport]
_REGISTRY: dict[str, TransportFactory] = {}
_CACHE: dict[str, Transport] = {}


def register_transport(name: str, factory: TransportFactory) -> None:
    """Register an obfuscating transport (e.g. 'reality', 'cdn-ws', 'pt')."""
    _REGISTRY[name.lower()] = factory


# Built-in plugin registrations -------------------------------------------
register_transport("cdn-ws", lambda profile: CdnWebSocketTransport(profile))


def get_transport(profile: TransportProfile | None = None) -> Transport:
    """Return the active transport for the resolved/given profile (cached)."""
    active = profile or resolve_profile()
    cache_key = "|".join(
        [
            active.name,
            active.tls_min,
            ",".join(active.alpn),
            str(active.verify_tls),
            ";".join(f"{k}={v}" for k, v in sorted(active.proxies.items())),
            active.sni,
            active.connect_host,
        ]
    )
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    factory = _REGISTRY.get(active.name)
    if factory:
        transport: Transport = factory(active)
    elif active.sni or active.connect_host:
        # Real SNI/connect/Host split (defeats SNI filtering, substitutes for ECH).
        transport = FrontedHttpsTransport(active)
    else:
        transport = StdlibHttpsTransport(active)
    _CACHE[cache_key] = transport
    return transport


def reset_transport_cache() -> None:
    """Drop cached transports (tests after env changes)."""
    _CACHE.clear()
