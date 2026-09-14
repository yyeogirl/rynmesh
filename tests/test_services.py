"""End-to-end tests for the three Ryn-network services.

Unit-tests the stub backends (determinism, PNG validity, vector normalization)
and runs a full work-order roundtrip via an in-process file-backed registry
shared between two RynmeshStore instances — the same pattern the existing
test suite uses for polling/mailbox tests.
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path

import pytest

from rynmesh.services.embeddings import EmbeddingsWorker, embed_stub
from rynmesh.services.image import ImageWorker, make_rgba_png, stub_image
from rynmesh.services.llm import LLMWorker, llm_stub_complete
from rynmesh.store import RynmeshStore


# ---------------------------------------------------------------- backends ---
def test_llm_stub_is_deterministic_and_nontrivial() -> None:
    a = llm_stub_complete("rynmesh hello", max_tokens=24)
    b = llm_stub_complete("rynmesh hello", max_tokens=24)
    c = llm_stub_complete("different prompt", max_tokens=24)
    assert a == b
    assert a != c
    assert len(a.split()) >= 24


def test_embed_stub_is_l2_normalized_and_dimmed() -> None:
    for dim in (16, 64, 256, 1024):
        v = embed_stub("a sentence", dim=dim)
        assert len(v) == dim
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, abs_tol=1e-6)
    v1 = embed_stub("alpha", dim=128)
    v2 = embed_stub("alpha", dim=128)
    v3 = embed_stub("beta", dim=128)
    assert v1 == v2
    assert v1 != v3


def test_stub_image_is_a_valid_png() -> None:
    png = stub_image("a sunset", width=32, height=32)
    # PNG file signature.
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # IHDR chunk dimensions match.
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    assert (width, height) == (32, 32)
    # IEND terminator present.
    assert png.endswith(b"IEND\xaeB`\x82")


def test_make_rgba_png_rejects_wrong_buffer_length() -> None:
    import pytest
    with pytest.raises(ValueError):
        make_rgba_png(4, 4, b"\x00" * 10)  # need 64 bytes for 4x4 RGBA


# ------------------------------------------------------- end-to-end mailbox ---
def _new_store(home: Path, net: Path, name: str) -> RynmeshStore:
    home.mkdir(parents=True, exist_ok=True)
    net.mkdir(parents=True, exist_ok=True)
    return RynmeshStore(home=home, network_dir=net, node_name=name)


def _roundtrip(
    worker, requester: RynmeshStore, provider: RynmeshStore,
    *, network_id: str, params: dict,
) -> dict:
    provider.register_job_capacity(network_id=network_id, capabilities=[worker.capability])
    sub = requester.submit_work_order(
        provider_peer_id=provider.peer_id,
        capability=worker.capability,
        operation=worker.operation,
        params=params,
        network_id=network_id,
    )
    handled = worker.serve_once(provider, network_id=network_id)
    assert handled >= 1, "worker did not pick up the order"
    wo_id = sub["order"]["work_order_id"]
    results = requester.list_work_results(
        work_order_id=wo_id, network_id=network_id,
    ).get("work_results", [])
    completed = [r for r in results if str(r.get("status", "")).lower() == "completed"]
    assert completed, f"no completed result: {results}"
    return json.loads(completed[-1]["message"])


def test_legacy_llm_work_order_rejects_plaintext(tmp_path) -> None:
    provider = _new_store(tmp_path / "p", tmp_path / "net", "p")
    requester = _new_store(tmp_path / "r", tmp_path / "net", "r")
    with pytest.raises(ValueError, match="private task protocol"):
        _roundtrip(
            LLMWorker(), requester, provider,
            network_id="rynmesh-test-llm",
            params={"prompt": "must not enter registry", "max_tokens": 16},
        )

    with pytest.raises(ValueError, match="private task protocol"):
        requester.submit_work_order(
            provider_peer_id=provider.peer_id,
            capability="rynmesh.llm.private.v1",
            operation="rynmesh.llm.private.infer.v1.p2p_offer",
            params={
                "session_id": "session",
                "ice_signal": {"username": "u", "password": "p", "candidates": ["candidate"]},
                "timeout_seconds": 30,
                "payload": {"prompt": "nested plaintext must not enter registry"},
            },
        )


def test_embeddings_worker_end_to_end(tmp_path) -> None:
    provider = _new_store(tmp_path / "p", tmp_path / "net", "p")
    requester = _new_store(tmp_path / "r", tmp_path / "net", "r")
    payload = _roundtrip(
        EmbeddingsWorker(), requester, provider,
        network_id="rynmesh-test-embed",
        params={"text": "an embeddable sentence", "dim": 64},
    )
    assert payload["backend"] == "stub"
    assert payload["dim"] == 64
    vec = payload["vector"]
    assert len(vec) == 64
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, abs_tol=1e-6)


def test_image_worker_end_to_end(tmp_path) -> None:
    provider = _new_store(tmp_path / "p", tmp_path / "net", "p")
    requester = _new_store(tmp_path / "r", tmp_path / "net", "r")
    payload = _roundtrip(
        ImageWorker(), requester, provider,
        network_id="rynmesh-test-image",
        params={"prompt": "a calm lake", "width": 32, "height": 32},
    )
    png = base64.b64decode(payload["png_b64"])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert payload["width"] == 32 and payload["height"] == 32
    assert payload["bytes"] == len(png)
