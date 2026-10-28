"""X25519 messaging keypair + authenticated seal/open for peer messaging.

Defense-in-depth over the Nebula transport: a message is sealed for the
recipient's X25519 messaging key (ECDH -> HKDF-SHA256 -> ChaCha20Poly1305), so
relays/operators see only ciphertext. Uses only `cryptography` (no new deps).
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO = b"rynmesh-peer-msg-v1"


def load_or_create_messaging_key(path: str | Path) -> X25519PrivateKey:
    path = Path(path)
    if path.exists():
        return X25519PrivateKey.from_private_bytes(path.read_bytes())
    key = X25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key.private_bytes_raw())
    return key


def public_key_b64(priv: X25519PrivateKey) -> str:
    return base64.b64encode(priv.public_key().public_bytes_raw()).decode("ascii")


def _shared(priv: X25519PrivateKey, their_pub_b64: str, info: bytes = _INFO) -> bytes:
    their_pub = X25519PublicKey.from_public_bytes(base64.b64decode(their_pub_b64))
    raw = priv.exchange(their_pub)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(raw)


def seal(
    priv: X25519PrivateKey,
    their_pub_b64: str,
    plaintext: bytes,
    *,
    info: bytes = _INFO,
) -> tuple[str, str]:
    """Seal for `their_pub_b64`. `info` domain-separates one channel from another.

    The default is the direct peer-message channel. A caller that seals for a
    different channel (the registry-hosted mailbox, say) passes its own label so
    a ciphertext captured on one channel cannot be replayed into the other: the
    HKDF output differs, so the AEAD open fails.
    """

    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(_shared(priv, their_pub_b64, info)).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce).decode("ascii"), base64.b64encode(ct).decode("ascii")


def open_sealed(
    priv: X25519PrivateKey,
    their_pub_b64: str,
    nonce_b64: str,
    ct_b64: str,
    *,
    info: bytes = _INFO,
) -> bytes:
    """Open a message sealed with the same `info` label. Others fail to decrypt."""

    return ChaCha20Poly1305(_shared(priv, their_pub_b64, info)).decrypt(
        base64.b64decode(nonce_b64), base64.b64decode(ct_b64), None
    )
