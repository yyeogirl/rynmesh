"""Unit tests for the shared node auth module.

The security property under test is narrow and load-bearing: a loopback socket
address only means "local" when nothing proxied the request.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import time

import pytest

from rynmesh.node_auth import COOKIE_NAME, NodeAuth, is_forwarded, is_loopback_addr


@pytest.fixture()
def auth(tmp_path):
    return NodeAuth(home=tmp_path)


# ---- the tunnel rule ----------------------------------------------------


def test_loopback_without_forwarding_is_trusted(auth):
    decision = auth.authorize(client_host="127.0.0.1", headers={})
    assert decision.allowed
    assert decision.via == "local"


@pytest.mark.parametrize(
    "header",
    [
        "x-forwarded-for",
        "X-Forwarded-For",
        "cf-connecting-ip",
        "x-real-ip",
        "forwarded",
        "x-forwarded-host",
        "x-forwarded-proto",
    ],
)
def test_loopback_with_forwarding_header_is_not_trusted(auth, header):
    """cloudflared dials the origin from 127.0.0.1 — the socket lies."""
    decision = auth.authorize(client_host="127.0.0.1", headers={header: "203.0.113.9"})
    assert not decision.allowed
    assert decision.reason == "unauthenticated"


def test_lan_client_is_not_trusted(auth):
    assert not auth.authorize(client_host="192.168.1.50", headers={}).allowed


def test_forwarded_header_detection_is_case_insensitive():
    assert is_forwarded({"X-FORWARDED-FOR": "1.2.3.4"})
    assert not is_forwarded({"user-agent": "curl"})


def test_loopback_addr_forms():
    assert is_loopback_addr("127.0.0.1")
    assert is_loopback_addr("::1")
    assert is_loopback_addr("::ffff:127.0.0.1")
    assert not is_loopback_addr("10.0.0.4")
    assert not is_loopback_addr("")


# ---- device token -------------------------------------------------------


def test_token_is_persisted_and_stable(auth):
    first = auth.token()
    assert first
    assert auth.token() == first
    assert auth.token_path.read_text(encoding="utf-8").strip() == first


def test_token_file_is_owner_only(auth):
    auth.token()
    mode = stat.S_IMODE(auth.token_path.stat().st_mode)
    if os.name != "nt":
        assert mode == 0o600, f"token readable beyond owner: {oct(mode)}"


def test_rotate_token_changes_it_and_kills_sessions(auth):
    session = auth.issue_session()
    assert auth.verify_session(session)
    old = auth.token()
    new = auth.rotate_token()
    assert new != old
    assert not auth.verify_session(session), "session survived token rotation"


# ---- sessions -----------------------------------------------------------


def test_session_roundtrip(auth):
    assert auth.verify_session(auth.issue_session())


def test_expired_session_is_rejected(auth):
    now = time.time()
    session = auth.issue_session(now=now - (auth.ttl_s + 10))
    assert not auth.verify_session(session, now=now)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda s: s[:-1] + ("A" if s[-1] != "A" else "B"),  # tampered signature
        lambda s: s.split(".")[0] + ".99999999999." + s.split(".")[2],  # extended expiry
        lambda s: "",
        lambda s: "garbage",
        lambda s: "1.2",
    ],
)
def test_tampered_sessions_are_rejected(auth, mangle):
    assert not auth.verify_session(mangle(auth.issue_session()))


def test_session_from_another_node_is_rejected(tmp_path):
    a = NodeAuth(home=tmp_path / "a")
    b = NodeAuth(home=tmp_path / "b")
    assert not b.verify_session(a.issue_session())


def test_session_cookie_authorizes_a_remote_request(auth):
    session = auth.issue_session()
    decision = auth.authorize(
        client_host="127.0.0.1",
        headers={"cf-connecting-ip": "203.0.113.9"},
        cookie=session,
    )
    assert decision.allowed
    assert decision.via == "session"


# ---- unlock -------------------------------------------------------------


def test_unlock_with_correct_token(auth):
    session = auth.unlock(auth.token(), client="203.0.113.9")
    assert session and auth.verify_session(session)


def test_unlock_with_wrong_token_fails(auth):
    assert auth.unlock("nope", client="203.0.113.9") == ""


def test_unlock_is_rate_limited(auth):
    for _ in range(8):
        assert auth.unlock("nope", client="203.0.113.9") == ""
    assert auth.is_rate_limited("203.0.113.9")
    # Correct token is now refused too — the lockout is not bypassable.
    assert auth.unlock(auth.token(), client="203.0.113.9") == ""
    # A different client is unaffected.
    assert auth.unlock(auth.token(), client="198.51.100.7")


def test_rate_limit_window_expires(auth):
    now = time.time()
    for _ in range(8):
        auth.unlock("nope", client="203.0.113.9", now=now)
    assert auth.is_rate_limited("203.0.113.9", now=now)
    assert not auth.is_rate_limited("203.0.113.9", now=now + 301)


# ---- bearer token -------------------------------------------------------


def test_bearer_token_authorizes_api_clients(auth):
    decision = auth.authorize(
        client_host="203.0.113.9",
        headers={"authorization": f"Bearer {auth.token()}"},
    )
    assert decision.allowed


def test_wrong_bearer_token_is_refused(auth):
    decision = auth.authorize(
        client_host="203.0.113.9", headers={"authorization": "Bearer wrong"}
    )
    assert not decision.allowed


# ---- cloudflare access --------------------------------------------------


def _unsigned_jwt(payload: dict, kid: str = "k1") -> str:
    def seg(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'RS256', 'kid': kid})}.{seg(payload)}.{seg({'sig': 'fake'})}"


def test_access_header_presence_alone_never_authorizes(tmp_path):
    """The clawpad defect: trusting the header because it exists."""
    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    forged = _unsigned_jwt({"aud": "aud1", "exp": time.time() + 3600})
    decision = auth.authorize(
        client_host="127.0.0.1",
        headers={"cf-connecting-ip": "203.0.113.9", "cf-access-jwt-assertion": forged},
    )
    assert not decision.allowed, "forged Access assertion was accepted"


def test_access_disabled_when_unconfigured(auth):
    assert not auth.verify_access_jwt(_unsigned_jwt({"aud": "a", "exp": time.time() + 60}))


def test_access_rejects_wrong_audience(tmp_path, monkeypatch):
    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    monkeypatch.setattr(auth, "_access_keys", lambda **_: [{"kid": "k1", "e": "AQAB", "n": "AQAB"}])
    assert not auth.verify_access_jwt(_unsigned_jwt({"aud": "other", "exp": time.time() + 60}))


def test_access_rejects_expired(tmp_path, monkeypatch):
    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    monkeypatch.setattr(auth, "_access_keys", lambda **_: [{"kid": "k1", "e": "AQAB", "n": "AQAB"}])
    assert not auth.verify_access_jwt(_unsigned_jwt({"aud": "aud1", "exp": time.time() - 10}))


def test_access_accepts_a_properly_signed_assertion(tmp_path):
    """Positive path: a real RS256 signature over the team's published key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()

    def b64u_int(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    auth._jwks = {"keys": [{"kid": "k1", "e": b64u_int(numbers.e), "n": b64u_int(numbers.n)}]}
    auth._jwks_fetched_at = time.time()

    def seg(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    signing_input = f"{seg({'alg': 'RS256', 'kid': 'k1'})}.{seg({'aud': 'aud1', 'exp': time.time() + 600})}"
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    token = signing_input + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

    assert auth.verify_access_jwt(token)
    # And the same token with a flipped signature must fail.
    assert not auth.verify_access_jwt(signing_input + ".AAAA")


def test_cookie_name_is_stable():
    assert COOKIE_NAME == "ryn_session"


# ---- CSRF / DNS rebinding ------------------------------------------------
# Loopback trust would otherwise let any website drive the node: a malicious
# page can make the visitor's own browser call http://127.0.0.1:<port>, and
# that request genuinely originates from loopback with no proxy headers.


def test_cross_site_browser_request_is_not_trusted(auth):
    decision = auth.authorize(
        client_host="127.0.0.1",
        headers={
            "host": "127.0.0.1:8791",
            "origin": "https://evil.example",
            "sec-fetch-site": "cross-site",
        },
    )
    assert not decision.allowed, "a website could drive the node from the user's browser"


def test_cross_site_without_fetch_metadata_is_caught_by_origin(auth):
    """Older browsers omit Sec-Fetch-Site; Origin still gives it away."""
    decision = auth.authorize(
        client_host="127.0.0.1",
        headers={"host": "127.0.0.1:8791", "origin": "https://evil.example"},
    )
    assert not decision.allowed


def test_same_origin_browser_request_is_trusted(auth):
    decision = auth.authorize(
        client_host="127.0.0.1",
        headers={
            "host": "127.0.0.1:8791",
            "origin": "http://127.0.0.1:8791",
            "sec-fetch-site": "same-origin",
        },
    )
    assert decision.allowed


def test_desktop_shell_origin_is_trusted(auth):
    """Tauri and the dev server are local origins, not another site."""
    for origin in ("tauri://localhost", "http://localhost:5173"):
        decision = auth.authorize(
            client_host="127.0.0.1", headers={"host": "127.0.0.1:8791", "origin": origin}
        )
        assert decision.allowed, origin


def test_dns_rebinding_host_is_not_trusted(auth):
    """A name that resolves to 127.0.0.1 still arrives on loopback."""
    decision = auth.authorize(
        client_host="127.0.0.1", headers={"host": "rebind.evil.example"}
    )
    assert not decision.allowed


def test_plain_loopback_host_forms_stay_trusted(auth):
    for host in ("127.0.0.1:8791", "localhost:8791", "[::1]:8791", "127.0.0.1", ""):
        assert auth.authorize(client_host="127.0.0.1", headers={"host": host}).allowed, host


def test_non_browser_clients_are_unaffected(auth):
    """curl and agents send neither Origin nor Sec-Fetch-Site."""
    assert auth.authorize(
        client_host="127.0.0.1", headers={"host": "127.0.0.1:8791", "user-agent": "curl/8"}
    ).allowed


def test_cross_site_request_can_still_use_a_bearer_token(auth):
    """The CSRF check gates loopback trust, not explicit credentials."""
    decision = auth.authorize(
        client_host="127.0.0.1",
        headers={
            "host": "127.0.0.1:8791",
            "origin": "https://evil.example",
            "authorization": f"Bearer {auth.token()}",
        },
    )
    assert decision.allowed


def test_tauri_shell_origin_is_trusted_despite_fetch_metadata(auth):
    """The desktop shell is a local origin, and browsers label it cross-site."""
    decision = auth.authorize(
        client_host="127.0.0.1",
        headers={
            "host": "127.0.0.1:8791",
            "origin": "tauri://localhost",
            "sec-fetch-site": "cross-site",
        },
    )
    assert decision.allowed, "the Tauri desktop shell was locked out"


def test_cross_site_navigation_without_origin_is_caught(auth):
    """A GET from another site omits Origin; Fetch Metadata is the only tell."""
    decision = auth.authorize(
        client_host="127.0.0.1",
        headers={"host": "127.0.0.1:8791", "sec-fetch-site": "cross-site"},
    )
    assert not decision.allowed


# ---- JWKS rotation and refetch throttling --------------------------------


def _signed_access_jwt(key, kid="k1", aud="aud1", exp_in=600):
    """A genuinely RS256-signed Access assertion."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def seg(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    signing_input = f"{seg({'alg': 'RS256', 'kid': kid})}.{seg({'aud': aud, 'exp': time.time() + exp_in})}"
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _jwk_for(key, kid):
    numbers = key.public_key().public_numbers()

    def b64u(value):
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return {"kid": kid, "e": b64u(numbers.e), "n": b64u(numbers.n)}


class _FakeJWKS:
    """Stands in for the network so the real _access_keys logic runs."""

    def __init__(self, keys):
        self.keys = keys
        self.calls = 0

    def __call__(self, url, timeout=10, context=None):
        self.calls += 1
        return _FakeResponse(json.dumps({"keys": self.keys}).encode())


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_key_rotation_is_picked_up_without_waiting_for_the_cache(tmp_path, monkeypatch):
    """Cloudflare rotates signing keys.

    Caching the JWKS for an hour and never refetching on an unknown kid would
    break Access sign-in for up to that hour after every rotation.
    """
    from cryptography.hazmat.primitives.asymmetric import rsa

    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    # Cache holds only the old key, and is still within its TTL.
    auth._jwks = {"keys": [_jwk_for(old_key, "old")]}
    auth._jwks_fetched_at = time.time()

    # The endpoint has since rotated in a new key.
    fake = _FakeJWKS([_jwk_for(old_key, "old"), _jwk_for(new_key, "new")])
    monkeypatch.setattr("rynmesh.node_auth.urllib.request.urlopen", fake)

    assert auth.verify_access_jwt(_signed_access_jwt(new_key, kid="new"))
    assert fake.calls == 1, "an unknown kid did not trigger a refetch"

    # The still-valid old key keeps working, and needs no further fetch.
    assert auth.verify_access_jwt(_signed_access_jwt(old_key, kid="old"))
    assert fake.calls == 1


def test_a_known_kid_never_hits_the_network(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    auth._jwks = {"keys": [_jwk_for(key, "k1")]}
    auth._jwks_fetched_at = time.time()

    fake = _FakeJWKS([])
    monkeypatch.setattr("rynmesh.node_auth.urllib.request.urlopen", fake)
    assert auth.verify_access_jwt(_signed_access_jwt(key, kid="k1"))
    assert fake.calls == 0


def test_rotation_refetch_does_not_accept_a_bad_signature(tmp_path, monkeypatch):
    """The refetch must not become a way to slip past verification."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    real_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    auth._jwks = {"keys": []}
    auth._jwks_fetched_at = time.time()
    monkeypatch.setattr(
        "rynmesh.node_auth.urllib.request.urlopen", _FakeJWKS([_jwk_for(real_key, "new")])
    )
    # Signed by the attacker but claiming the freshly-rotated kid.
    assert not auth.verify_access_jwt(_signed_access_jwt(attacker_key, kid="new"))


def test_unknown_kid_refetch_is_throttled(tmp_path, monkeypatch):
    """An attacker can mint unknown kids freely; that must not amplify."""
    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    auth._jwks = {"keys": []}
    auth._jwks_fetched_at = time.time()

    calls = []

    def fake_urlopen(url, timeout=10, context=None):
        calls.append(url)
        raise RuntimeError("network blocked in test")

    monkeypatch.setattr("rynmesh.node_auth.urllib.request.urlopen", fake_urlopen)

    now = time.time()
    for _ in range(10):
        auth._access_keys(now=now, force=True)
    assert len(calls) == 1, f"refetch not throttled: {len(calls)} network calls"

    # After the floor elapses, one more is allowed.
    auth._access_keys(now=now + 61, force=True)
    assert len(calls) == 2


def test_transient_jwks_failure_keeps_serving_cached_keys(tmp_path, monkeypatch):
    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    cached = [{"kid": "k1", "e": "AQAB", "n": "AQAB"}]
    auth._jwks = {"keys": cached}
    auth._jwks_fetched_at = 0.0  # stale, so a fetch is attempted

    monkeypatch.setattr(
        "rynmesh.node_auth.urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert auth._access_keys(now=time.time()) == cached


def test_warm_access_keys_is_a_noop_when_unconfigured(tmp_path):
    assert NodeAuth(home=tmp_path).warm_access_keys() == 0


def test_warm_access_keys_prefetches(tmp_path, monkeypatch):
    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    monkeypatch.setattr(auth, "_access_keys", lambda **_: [{"kid": "a"}, {"kid": "b"}])
    assert auth.warm_access_keys() == 2


def test_ssl_context_falls_back_to_certifi_when_the_store_is_empty(monkeypatch):
    """python.org builds ship without a CA bundle until you run their script.

    Without this fallback every JWKS fetch fails and Access verification
    silently never succeeds — the app just keeps asking for the token.
    """
    import ssl

    class EmptyStore:
        def cert_store_stats(self):
            return {"x509_ca": 0}

    made = {}

    def fake_create(*args, **kwargs):
        if "cafile" in kwargs:
            made["cafile"] = kwargs["cafile"]
            return "context-with-certifi"
        return EmptyStore()

    monkeypatch.setattr(ssl, "create_default_context", fake_create)
    assert NodeAuth._ssl_context() == "context-with-certifi"
    assert made["cafile"].endswith(".pem")


def test_ssl_context_uses_the_system_store_when_populated(monkeypatch):
    import ssl

    class GoodStore:
        def cert_store_stats(self):
            return {"x509_ca": 150}

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: GoodStore())
    assert isinstance(NodeAuth._ssl_context(), GoodStore)


def test_jwks_failure_is_recorded_not_swallowed(tmp_path, monkeypatch):
    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    monkeypatch.setattr(
        "rynmesh.node_auth.urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("CERTIFICATE_VERIFY_FAILED")),
    )
    assert auth.warm_access_keys() == 0
    assert "CERTIFICATE_VERIFY_FAILED" in auth.access_error
    assert auth.access_configured


def test_access_error_clears_on_a_successful_fetch(tmp_path, monkeypatch):
    auth = NodeAuth(
        home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_audience="aud1"
    )
    auth.access_error = "stale failure"
    monkeypatch.setattr("rynmesh.node_auth.urllib.request.urlopen", _FakeJWKS([{"kid": "k"}]))
    assert auth.warm_access_keys() == 1
    assert auth.access_error == ""
