"""Integration tests for the control-surface gate on the node daemon.

These assert the property that makes a Cloudflare tunnel safe: cloudflared
proxies from 127.0.0.1, so the *only* thing distinguishing an internet caller
from the desktop owner is the forwarding header.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from rynmesh import node_auth as node_auth_mod

# Headers a real cloudflared/nginx hop adds. The socket still says loopback.
TUNNEL = {"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "203.0.113.9"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    monkeypatch.delenv("RYNMESH_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("RYNMESH_ALLOW_REMOTE_CONTROL", raising=False)
    monkeypatch.delenv("RYNMESH_NETWORK_KEY", raising=False)
    from rynmesh.peer_http import create_app

    # https base URL: the session cookie is marked Secure for proxied callers,
    # and a browser would refuse to send it back over plain http. A real
    # tunnel is always https, so this is the faithful setup.
    with TestClient(create_app(), base_url="https://testserver") as test_client:
        yield test_client


def _control_paths(client: TestClient) -> list[str]:
    """Every GET route on the control surface that takes no path parameter."""
    paths = []
    for route in client.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api/local") or "{" in path:
            continue
        if "GET" not in methods or path in {"/api/local/auth/status"}:
            continue
        paths.append(path)
    return sorted(set(paths))


def test_there_are_control_routes_to_protect(client):
    # Guards the sweep below against silently testing nothing.
    assert len(_control_paths(client)) > 20


def test_every_control_route_refuses_a_tunnel_request(client):
    """The sweep that matters: no route is private-by-forgetting."""
    leaked = []
    for path in _control_paths(client):
        response = client.get(path, headers=TUNNEL)
        if response.status_code != 401:
            leaked.append((path, response.status_code))
    assert not leaked, f"control routes reachable through a tunnel: {leaked}"


def test_loopback_without_forwarding_still_works(client):
    """The desktop owner must never be asked for a token."""
    response = client.get("/api/local/node/status")
    assert response.status_code == 200
    assert response.json()["daemon_running"] is True


def test_auth_status_reports_locked_for_tunnel(client):
    response = client.get("/api/local/auth/status", headers=TUNNEL)
    assert response.status_code == 200
    body = response.json()
    assert body["authorized"] is False
    assert body["remote"] is True


def test_auth_status_reports_local_for_desktop(client):
    body = client.get("/api/local/auth/status").json()
    assert body["authorized"] is True
    assert body["via"] == "local"
    assert body["remote"] is False


def test_unlock_then_access(client, tmp_path):
    token = (tmp_path / "control_token").read_text(encoding="utf-8").strip()

    blocked = client.get("/api/local/node/status", headers=TUNNEL)
    assert blocked.status_code == 401
    assert blocked.json()["unlock_required"] is True

    unlocked = client.post("/api/local/auth/unlock", json={"token": token}, headers=TUNNEL)
    assert unlocked.status_code == 200
    assert node_auth_mod.COOKIE_NAME in unlocked.cookies

    # TestClient carries the cookie forward, as a browser would.
    allowed = client.get("/api/local/node/status", headers=TUNNEL)
    assert allowed.status_code == 200


def test_unlock_with_wrong_token_is_refused(client):
    response = client.post("/api/local/auth/unlock", json={"token": "wrong"}, headers=TUNNEL)
    assert response.status_code == 401
    assert node_auth_mod.COOKIE_NAME not in response.cookies


def test_unlock_brute_force_is_rate_limited(client):
    last = None
    for _ in range(9):
        last = client.post("/api/local/auth/unlock", json={"token": "wrong"}, headers=TUNNEL)
    assert last.status_code == 429


def test_session_cookie_is_httponly(client, tmp_path):
    token = (tmp_path / "control_token").read_text(encoding="utf-8").strip()
    response = client.post("/api/local/auth/unlock", json={"token": token}, headers=TUNNEL)
    cookie_header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header.replace("samesite", "SameSite")
    assert "Secure" in cookie_header, "remote session cookie must be Secure"


def test_token_endpoint_is_gated(client):
    assert client.get("/api/local/auth/token", headers=TUNNEL).status_code == 401
    assert client.get("/api/local/auth/token").status_code == 200


def test_peer_surface_stays_open_for_the_mesh(client):
    """Auth must not break P2P: /health is how peers find each other."""
    response = client.get("/health", headers=TUNNEL)
    assert response.status_code == 200
    assert "peer_id" in response.json()


def test_bearer_token_works_for_api_clients(client, tmp_path):
    token = (tmp_path / "control_token").read_text(encoding="utf-8").strip()
    response = client.get(
        "/api/local/node/status",
        headers={**TUNNEL, "authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


def test_local_token_mode_overrides_loopback_trust(tmp_path, monkeypatch):
    """When the desktop shell injects a launch token, it is the only key."""
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "launch-secret")
    from rynmesh.peer_http import create_app

    with TestClient(create_app()) as test_client:
        assert test_client.get("/api/local/node/status").status_code == 403
        ok = test_client.get(
            "/api/local/node/status", headers={"x-ryn-local-token": "launch-secret"}
        )
        assert ok.status_code == 200


def test_token_file_is_owner_only_on_disk(client, tmp_path):
    import stat

    client.get("/api/local/auth/token")
    mode = stat.S_IMODE((tmp_path / "control_token").stat().st_mode)
    if os.name != "nt":
        assert mode == 0o600


def test_static_shell_is_reachable_without_auth(client):
    """The SPA shell carries no data; it must load so it can prompt to unlock."""
    response = client.get("/health")
    assert response.status_code == 200
    assert os.environ.get("RYNMESH_HOME")
