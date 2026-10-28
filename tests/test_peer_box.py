from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from rynmesh.services import peer_box


def test_keypair_roundtrip_seal_open(tmp_path):
    a = peer_box.load_or_create_messaging_key(tmp_path / "a.x25519")
    b = peer_box.load_or_create_messaging_key(tmp_path / "b.x25519")
    a_pub = peer_box.public_key_b64(a)
    b_pub = peer_box.public_key_b64(b)
    nonce, ct = peer_box.seal(a, b_pub, b"hello \xf0\x9f\x91\x8b")
    assert peer_box.open_sealed(b, a_pub, nonce, ct) == b"hello \xf0\x9f\x91\x8b"

def test_load_is_stable_and_0600(tmp_path):
    p = tmp_path / "k.x25519"
    k1 = peer_box.public_key_b64(peer_box.load_or_create_messaging_key(p))
    k2 = peer_box.public_key_b64(peer_box.load_or_create_messaging_key(p))
    assert k1 == k2  # persisted, not regenerated
    if os.name != "nt":
        assert (p.stat().st_mode & 0o777) == 0o600

def test_wrong_key_fails(tmp_path):
    a = peer_box.load_or_create_messaging_key(tmp_path / "a")
    b = peer_box.load_or_create_messaging_key(tmp_path / "b")
    c = peer_box.load_or_create_messaging_key(tmp_path / "c")
    nonce, ct = peer_box.seal(a, peer_box.public_key_b64(b), b"secret")
    # Wrong recipient key must fail authentication, not silently decrypt.
    with pytest.raises(InvalidTag):
        peer_box.open_sealed(c, peer_box.public_key_b64(a), nonce, ct)
