from __future__ import annotations

import json
import os
import queue
import select
import socket
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest

from rynmesh import (
    CreditLedgerError,
    FilePeerRegistry,
    FileRelayStore,
    HttpPeerClient,
    HttpPeerRegistry,
    JobCapacityRecord,
    KeywordSafetyScanner,
    PeerError,
    PeerRecord,
    PeerTransportError,
    RegistryError,
    RunReceipt,
    RynmeshPeer,
    RynmeshStore,
    SafetyOutcome,
    StoreError,
    WorkOrder,
    build_media_asset,
    create_app,
    create_registry_app,
    make_manifest,
    public_key_from_private,
    sign_job_capacity,
    sign_peer_record,
    sign_safety_receipt,
    sign_work_order,
    validate_manifest,
    verify_peer_record,
)
from rynmesh.credits import CreditPolicy, distribution_weight
from rynmesh.crypto import SignatureError, SignedPayload, sha256_bytes, verify_signed_payload
from rynmesh.identity import (
    IdentityPolicy,
    IdentityTier,
    PeerVouch,
    ProofOfResource,
    StakeCommitment,
    assess_identity,
    sign_peer_vouch,
    sign_proof_of_resource,
    sign_stake_commitment,
    tier_can_issue,
    tier_distribution_cap,
    verify_peer_vouch,
)
from rynmesh.provenance import (
    LINK_GENESIS,
    LINK_PEER_ATTESTATION,
    LINK_SAFETY_SCAN,
    LINK_WORK_ENVELOPE,
    ProvenanceError,
    ProvenanceLink,
    append_link,
    chain_to_payload,
    make_genesis_link,
    sign_link,
    signed_link_hash,
    verify_chain,
)
from rynmesh.store import _content_kind_from_tags, _guess_content_kind

ROOT = Path(__file__).resolve().parents[1]
MCP_RUNNER = [sys.executable, "-m", "rynmesh.mcp_server"]
PEER_RUNNER = [sys.executable, "-m", "rynmesh.peer_http"]
REGISTRY_RUNNER = [sys.executable, "-m", "rynmesh.registry_http"]


def _key() -> bytes:
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate().private_bytes_raw()


def _receipt() -> RunReceipt:
    return RunReceipt(
        run_id="rnv_test",
        work_id="sha256:work",
        envelope_hash="sha256:envelope",
        workflow="@media.generate -> @safety.scan -> @mesh.announce",
    )


def _valid_manifest(tmp_path, publisher_key: bytes, scanner_key: bytes):
    media = tmp_path / "clip.mp4"
    preview = tmp_path / "preview.mp4"
    media.write_bytes(b"generated clip bytes")
    preview.write_bytes(b"preview bytes")
    asset = build_media_asset(media, preview_path=preview, transcript_text="calm generated scene")
    receipt = KeywordSafetyScanner().scan(
        clip_id=asset.clip_id,
        transcript="calm generated scene",
    )
    signed_safety = sign_safety_receipt(receipt, private_key_bytes=scanner_key)
    return (
        make_manifest(
            asset=asset,
            run_receipt=_receipt(),
            safety_receipts=[signed_safety],
            publisher=public_key_from_private(publisher_key),
            private_key_bytes=publisher_key,
            title="Calm generated scene",
        ),
        media.read_bytes(),
        preview.read_bytes(),
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http_json(endpoint: str, path: str, proc: subprocess.Popen[str]) -> dict:
    deadline = time.time() + 15
    last_error: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            raise AssertionError(f"Peer server exited early: {stderr}")
        try:
            with urlopen(endpoint.rstrip("/") + path, timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
                assert isinstance(payload, dict)
                return payload
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for peer server: {last_error}")


def test_signed_safety_receipt_verifies_and_tamper_fails() -> None:
    key = _key()
    receipt = KeywordSafetyScanner().scan(clip_id="sha256:clip", transcript="safe text")
    signed = sign_safety_receipt(receipt, private_key_bytes=key)
    verify_signed_payload(signed)

    tampered = SignedPayload(
        payload={**signed.payload, "outcome": SafetyOutcome.BLOCK.value},
        signature=signed.signature,
        public_key=signed.public_key,
    )
    with pytest.raises(SignatureError):
        verify_signed_payload(tampered)


def test_keyword_scanner_blocks_matching_transcript() -> None:
    scanner = KeywordSafetyScanner(blocked_terms=("do-not-propagate",))
    receipt = scanner.scan(clip_id="sha256:clip", transcript="please do-not-propagate this")
    assert receipt.outcome is SafetyOutcome.BLOCK
    assert receipt.checks["blocked_terms"] == ["do-not-propagate"]


def test_manifest_with_passing_receipt_allows_propagation(tmp_path) -> None:
    signed_manifest, _media, _preview = _valid_manifest(tmp_path, _key(), _key())
    validation = validate_manifest(signed_manifest)
    assert validation.errors == []
    assert validation.propagates
    assert validation.pass_receipts == 1


def test_manifest_without_safety_receipt_does_not_propagate(tmp_path) -> None:
    publisher_key = _key()
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"generated clip bytes")
    asset = build_media_asset(media)
    signed_manifest = make_manifest(
        asset=asset,
        run_receipt=_receipt(),
        safety_receipts=[],
        publisher=public_key_from_private(publisher_key),
        private_key_bytes=publisher_key,
    )
    validation = validate_manifest(signed_manifest)
    assert not validation.propagates
    assert "not_enough_passing_safety_receipts" in validation.errors


def test_manifest_rejects_publisher_key_mismatch(tmp_path) -> None:
    publisher_key = _key()
    scanner_key = _key()
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"generated clip bytes")
    asset = build_media_asset(media, transcript_text="safe")
    signed_safety = sign_safety_receipt(
        KeywordSafetyScanner().scan(clip_id=asset.clip_id, transcript="safe"),
        private_key_bytes=scanner_key,
    )
    signed_manifest = make_manifest(
        asset=asset,
        run_receipt=_receipt(),
        safety_receipts=[signed_safety],
        publisher="forged-publisher",
        private_key_bytes=publisher_key,
    )
    validation = validate_manifest(signed_manifest)
    assert not validation.propagates
    assert "manifest_publisher_key_mismatch" in validation.errors


def test_blocking_safety_receipt_prevents_propagation(tmp_path) -> None:
    publisher_key = _key()
    scanner_key = _key()
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"generated clip bytes")
    asset = build_media_asset(media, transcript_text="blocked")
    scanner = KeywordSafetyScanner(blocked_terms=("blocked",))
    signed_safety = sign_safety_receipt(
        scanner.scan(clip_id=asset.clip_id, transcript="blocked"),
        private_key_bytes=scanner_key,
    )
    signed_manifest = make_manifest(
        asset=asset,
        run_receipt=_receipt(),
        safety_receipts=[signed_safety],
        publisher=public_key_from_private(publisher_key),
        private_key_bytes=publisher_key,
    )
    validation = validate_manifest(signed_manifest)
    assert not validation.propagates
    assert "blocked_by_safety_receipt" in validation.errors


def test_peer_preview_first_then_full_fetch(tmp_path) -> None:
    publisher_key = _key()
    scanner_key = _key()
    signed_manifest, media, preview = _valid_manifest(tmp_path, publisher_key, scanner_key)
    publisher = RynmeshPeer(private_key_bytes=publisher_key)
    consumer = RynmeshPeer(private_key_bytes=_key())

    clip_id = publisher.publish_local_clip(signed_manifest, preview=preview, media=media)
    assert consumer.fetch_preview_from(publisher, clip_id) == preview
    assert consumer.has_clip(clip_id)
    assert not consumer.has_full_media(clip_id)

    assert consumer.fetch_full_media_from(publisher, clip_id) == media
    assert consumer.has_full_media(clip_id)


def test_peer_refuses_mismatched_full_media(tmp_path) -> None:
    publisher_key = _key()
    scanner_key = _key()
    signed_manifest, _media, preview = _valid_manifest(tmp_path, publisher_key, scanner_key)
    publisher = RynmeshPeer(private_key_bytes=publisher_key)

    with pytest.raises(PeerError, match="media_hash_mismatch"):
        publisher.publish_local_clip(signed_manifest, preview=preview, media=b"tampered")


def test_peer_refuses_banned_provider(tmp_path) -> None:
    publisher_key = _key()
    scanner_key = _key()
    signed_manifest, media, preview = _valid_manifest(tmp_path, publisher_key, scanner_key)
    publisher = RynmeshPeer(private_key_bytes=publisher_key)
    consumer = RynmeshPeer(private_key_bytes=_key())
    clip_id = publisher.publish_local_clip(signed_manifest, preview=preview, media=media)

    consumer.ban_peer(publisher.peer_id)
    with pytest.raises(PeerError, match="provider_banned"):
        consumer.fetch_preview_from(publisher, clip_id)


def test_build_media_asset_uses_content_hash_as_clip_id(tmp_path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"generated")
    asset = build_media_asset(media)
    assert asset.clip_id == sha256_bytes(b"generated")
    assert asset.media_hash == asset.clip_id


def test_content_kind_inference_and_tag_fallback() -> None:
    assert _guess_content_kind("text/csv") == "dataset"
    assert _guess_content_kind("application/json") == "dataset"
    assert _guess_content_kind("application/pdf") == "document"
    assert _guess_content_kind("video/mp4") == "video"
    assert _content_kind_from_tags(("content-kind:presentation",), "application/octet-stream") == "presentation"


def test_file_backed_store_publishes_and_second_node_fetches(tmp_path) -> None:
    pytest.importorskip("cryptography")
    network = tmp_path / "mesh"
    node_a = RynmeshStore(home=tmp_path / "ms-1", network_dir=network, node_name="ms-1")
    node_b = RynmeshStore(home=tmp_path / "ms-2", network_dir=network, node_name="ms-2")
    media = tmp_path / "open-video.mp4"
    media.write_bytes(b"video bytes")

    published = node_a.publish_clip(
        media,
        title="Open video protocol test",
        transcript="calm generated open video",
    )
    assert published["status"] == "published"

    visible = node_b.list_clips()
    assert [item["clip_id"] for item in visible["clips"]] == [published["clip_id"]]
    preview = node_b.fetch_preview(published["clip_id"])
    assert Path(preview["preview_path"]).exists()
    full = node_b.fetch_full(published["clip_id"])
    assert Path(full["media_path"]).read_bytes() == b"video bytes"


def test_file_backed_store_publishes_generic_content(tmp_path) -> None:
    pytest.importorskip("cryptography")
    network = tmp_path / "mesh"
    node_a = RynmeshStore(home=tmp_path / "ms-1", network_dir=network, node_name="ms-1")
    node_b = RynmeshStore(home=tmp_path / "ms-2", network_dir=network, node_name="ms-2")
    report = tmp_path / "agent-report.md"
    report.write_text("# Agent report\n\nGenerated analysis for Rynmesh.\n", encoding="utf-8")

    published = node_a.publish_content(
        report,
        title="Agent report",
        transcript="safe generated analysis report",
        content_type="text/markdown",
        content_kind="document",
        tags=["topic:strategy"],
    )

    assert published["status"] == "published"
    assert published["content_id"] == published["clip_id"]
    assert published["content_type"] == "text/markdown"
    assert published["content_kind"] == "document"
    assert published["credit"]["kind"] == "content_published"
    assert node_a.get_local_manifest(published["content_id"]).payload["asset"]["content_kind"] == "document"
    [announcement] = node_a._iter_announcements()
    assert announcement.to_dict()["content_id"] == published["content_id"]

    visible = node_b.list_content()
    assert [item["content_id"] for item in visible["content"]] == [published["content_id"]]
    assert visible["content"][0]["content_kind"] == "document"
    assert visible["content"][0]["content_hash"] == published["content_id"]

    preview = node_b.fetch_content_preview(published["content_id"])
    assert Path(preview["preview_path"]).exists()
    full = node_b.fetch_content_full(published["content_id"])
    assert Path(full["content_path"]).read_text(encoding="utf-8").startswith("# Agent report")

    ranked = node_b.rank_content()
    assert ranked["content"][0]["content_id"] == published["content_id"]


def test_file_backed_store_refuses_tampered_preview(tmp_path) -> None:
    pytest.importorskip("cryptography")
    network = tmp_path / "mesh"
    node_a = RynmeshStore(home=tmp_path / "ms-1", network_dir=network, node_name="ms-1")
    node_b = RynmeshStore(home=tmp_path / "ms-2", network_dir=network, node_name="ms-2")
    media = tmp_path / "preview-check.mp4"
    media.write_bytes(b"video bytes")

    published = node_a.publish_clip(media, transcript="calm generated open video")
    [record] = node_b._iter_announcements()
    Path(record.preview_path).write_bytes(b"tampered preview")

    with pytest.raises(StoreError, match="preview_hash_mismatch"):
        node_b.fetch_preview(published["clip_id"])


def test_file_backed_store_blocks_flagged_clip(tmp_path, monkeypatch) -> None:
    pytest.importorskip("cryptography")
    monkeypatch.setenv("RYNMESH_BLOCKED_TERMS", "blocked-token")
    node = RynmeshStore(home=tmp_path / "ms-1", network_dir=tmp_path / "mesh")
    media = tmp_path / "blocked.mp4"
    media.write_bytes(b"video bytes")

    published = node.publish_clip(media, transcript="this has blocked-token")
    assert published["status"] == "blocked"
    assert "blocked_by_safety_receipt" in published["errors"]
    assert node.list_clips()["clips"] == []


def test_signed_peer_record_roundtrips_through_file_registry(tmp_path) -> None:
    key = _key()
    registry = FilePeerRegistry(tmp_path / "registry")
    peer_id = RynmeshPeer(private_key_bytes=key).peer_id
    record = PeerRecord(
        peer_id=peer_id,
        node_name="ms-1",
        endpoints=("libp2p://peer",),
        network_id="testnet",
    )
    signed = sign_peer_record(record, private_key_bytes=key)
    published = registry.publish(signed)
    assert published["status"] == "registered"

    [discovered] = registry.list_peers(network_id="testnet")
    verified = verify_peer_record(discovered)
    assert verified.peer_id == peer_id
    assert verified.endpoints == ("libp2p://peer",)


def test_registry_filters_stale_peer_records(tmp_path) -> None:
    fresh_key = _key()
    stale_key = _key()
    registry = FilePeerRegistry(tmp_path / "registry")
    fresh = PeerRecord(
        peer_id=public_key_from_private(fresh_key),
        node_name="fresh",
        endpoints=("http://fresh.local:8791",),
        network_id="testnet",
    )
    stale = PeerRecord(
        peer_id=public_key_from_private(stale_key),
        node_name="stale",
        endpoints=("http://stale.local:8792",),
        network_id="testnet",
        updated_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    )
    registry.publish(sign_peer_record(fresh, private_key_bytes=fresh_key))
    registry.publish(sign_peer_record(stale, private_key_bytes=stale_key))

    records = registry.list_peers(network_id="testnet", max_age_hours=24)
    assert [verify_peer_record(record).node_name for record in records] == ["fresh"]


def test_store_registers_and_discovers_peers(tmp_path) -> None:
    pytest.importorskip("cryptography")
    network = tmp_path / "mesh"
    node_a = RynmeshStore(home=tmp_path / "ms-1", network_dir=network, node_name="ms-1")
    node_b = RynmeshStore(home=tmp_path / "ms-2", network_dir=network, node_name="ms-2")

    registered_a = node_a.register_node(endpoints=("file:///ms-1",), network_id="testnet")
    registered_b = node_b.register_node(endpoints=("file:///ms-2",), network_id="testnet")
    assert registered_a["status"] == "registered"
    assert registered_b["status"] == "registered"

    discovered = node_b.discover_peers(network_id="testnet")
    assert [peer["node_name"] for peer in discovered["peers"]] == ["ms-1"]
    assert discovered["peers"][0]["endpoints"] == ["file:///ms-1"]


def test_polling_work_orders_roundtrip_through_file_registry(tmp_path) -> None:
    pytest.importorskip("cryptography")
    registry = FilePeerRegistry(tmp_path / "registry")
    requester = RynmeshStore(home=tmp_path / "requester", network_dir=tmp_path / "req-mesh")
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "prov-mesh")
    requester.registry = registry
    provider.registry = registry

    capacity = provider.register_job_capacity(
        capabilities=("signal50.veo_motion.v1",),
        network_id="jobs-test",
        price_credits={"signal50.veo_motion.v1": 12.5},
        metadata={"route": "polling-relay"},
    )
    assert capacity["status"] == "registered"

    capacities = requester.list_job_capacities(
        network_id="jobs-test",
        capability="signal50.veo_motion.v1",
    )
    assert [item["peer_id"] for item in capacities["capacities"]] == [provider.peer_id]

    submitted = requester.submit_work_order(
        provider_peer_id=provider.peer_id,
        capability="signal50.veo_motion.v1",
        operation="signal50.veo.complete_flow_video",
        params={"video_id": "casefile-1"},
        network_id="jobs-test",
        input_content_ids=("sha256:" + "a" * 64,),
        max_credit_cost=20.0,
    )
    work_order_id = submitted["work_order_id"]

    polled = provider.poll_work_orders(
        network_id="jobs-test",
        capability="signal50.veo_motion.v1",
    )
    assert [item["work_order_id"] for item in polled["work_orders"]] == [work_order_id]
    assert polled["work_orders"][0]["params"]["video_id"] == "casefile-1"

    provider.publish_work_result(
        work_order_id=work_order_id,
        requester_peer_id=requester.peer_id,
        status="accepted",
        message="claimed by provider",
        network_id="jobs-test",
    )
    assert provider.poll_work_orders(network_id="jobs-test")["work_orders"] == []

    provider.publish_work_result(
        work_order_id=work_order_id,
        requester_peer_id=requester.peer_id,
        status="completed",
        message="clip set complete",
        result_content_ids=("sha256:" + "b" * 64,),
        result_refs={"relay": {"content_hash": "sha256:" + "c" * 64}},
        credit_amount=12.5,
        network_id="jobs-test",
    )

    results = requester.list_work_results(work_order_id=work_order_id, network_id="jobs-test")
    assert [item["status"] for item in results["work_results"]] == ["accepted", "completed"]
    assert provider.credit_summary(category="jobs")["score"] >= 12.5


def test_registry_http_job_mailbox_and_relay_blob(tmp_path) -> None:
    pytest.importorskip("cryptography")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    registry = FilePeerRegistry(tmp_path / "registry")
    relay = FileRelayStore(tmp_path / "relay")
    client = TestClient(create_registry_app(registry, relay_store=relay))
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "provider-mesh")
    requester = RynmeshStore(home=tmp_path / "requester", network_dir=tmp_path / "requester-mesh")

    capacity = JobCapacityRecord(
        peer_id=provider.peer_id,
        node_name="m4-mini",
        capabilities=("signal50.veo_motion.v1",),
        network_id="jobs-http",
    )
    signed_capacity = sign_job_capacity(capacity, private_key_bytes=provider.private_key_bytes)
    response = client.post("/api/v1/jobs/capacity/register", json=signed_capacity.to_dict())
    assert response.status_code == 200
    listed = client.get(
        "/api/v1/jobs/capacity",
        params={"network_id": "jobs-http", "capability": "signal50.veo_motion.v1"},
    ).json()
    assert len(listed["capacities"]) == 1

    order = WorkOrder(
        work_order_id="wo_http_test",
        requester_peer_id=requester.peer_id,
        provider_peer_id=provider.peer_id,
        capability="signal50.veo_motion.v1",
        operation="signal50.veo.complete_flow_video",
        params={"video_id": "wild-thread-1"},
        network_id="jobs-http",
    )
    signed_order = sign_work_order(order, private_key_bytes=requester.private_key_bytes)
    response = client.post("/api/v1/jobs/work-orders", json=signed_order.to_dict())
    assert response.status_code == 200
    mailbox = client.get(
        "/api/v1/jobs/work-orders",
        params={"network_id": "jobs-http", "provider_peer_id": provider.peer_id},
    ).json()
    assert mailbox["work_orders"][0]["payload"]["work_order_id"] == "wo_http_test"

    blob = b"relay video artifact bytes"
    expected_hash = sha256_bytes(blob)
    response = client.post(
        "/api/v1/relay/blobs",
        content=blob,
        headers={
            "content-type": "video/mp4",
            "x-rynmesh-expected-hash": expected_hash,
            "x-rynmesh-filename": "clip.mp4",
            "x-rynmesh-uploader-peer-id": provider.peer_id,
        },
    )
    assert response.status_code == 200
    assert response.json()["blob"]["content_hash"] == expected_hash
    meta = client.get(f"/api/v1/relay/meta/{expected_hash}").json()
    assert meta["filename"] == "clip.mp4"
    assert client.get(f"/api/v1/relay/blobs/{expected_hash}").content == blob

    chunk_a = b"chunk-a-"
    chunk_b = b"chunk-b"
    chunk_a_hash = sha256_bytes(chunk_a)
    chunk_b_hash = sha256_bytes(chunk_b)
    assert client.post(
        "/api/v1/relay/blobs",
        content=chunk_a,
        headers={"x-rynmesh-expected-hash": chunk_a_hash},
    ).status_code == 200
    assert client.post(
        "/api/v1/relay/blobs",
        content=chunk_b,
        headers={"x-rynmesh-expected-hash": chunk_b_hash},
    ).status_code == 200
    assembled = chunk_a + chunk_b
    assembled_hash = sha256_bytes(assembled)
    response = client.post(
        "/api/v1/relay/blobs/assemble",
        json={
            "expected_hash": assembled_hash,
            "media_type": "application/zip",
            "filename": "assembled.zip",
            "uploader_peer_id": provider.peer_id,
            "chunks": [
                {"content_hash": chunk_a_hash, "size_bytes": len(chunk_a)},
                {"content_hash": chunk_b_hash, "size_bytes": len(chunk_b)},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["blob"]["content_hash"] == assembled_hash
    assert client.get(f"/api/v1/relay/blobs/{assembled_hash}").content == assembled


def test_http_relay_client_streams_artifacts_through_registry_server(tmp_path, monkeypatch) -> None:
    pytest.importorskip("cryptography")
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    registry_port = _free_port()
    registry_endpoint = f"http://127.0.0.1:{registry_port}"
    registry_env = os.environ.copy()
    registry_env["RYNMESH_REGISTRY_DIR"] = str(tmp_path / "registry")
    registry_env["RYNMESH_REGISTRY_HOST"] = "127.0.0.1"
    registry_env["RYNMESH_REGISTRY_PORT"] = str(registry_port)
    proc = subprocess.Popen(
        REGISTRY_RUNNER,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=registry_env,
    )
    try:
        health = _wait_http_json(registry_endpoint, "/health", proc)
        assert health["kind"] == "rynmesh-registry"

        provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "provider-mesh")
        artifact = tmp_path / "relay-source.mp4"
        artifact.write_bytes(b"streamed relay artifact bytes")
        uploaded = provider.upload_relay_artifact(
            artifact,
            relay_url=registry_endpoint,
            media_type="video/mp4",
        )
        content_hash = uploaded["blob"]["content_hash"]
        assert content_hash == sha256_bytes(artifact.read_bytes())

        downloaded = provider.download_relay_artifact(
            content_hash,
            tmp_path / "downloaded.mp4",
            relay_url=registry_endpoint,
        )
        assert Path(downloaded["path"]).read_bytes() == artifact.read_bytes()

        monkeypatch.setenv("RYNMESH_RELAY_DIRECT_UPLOAD_MAX_BYTES", "32")
        monkeypatch.setenv("RYNMESH_RELAY_CHUNK_BYTES", "17")
        large_artifact = tmp_path / "relay-large-source.zip"
        large_artifact.write_bytes((b"chunked relay artifact bytes-" * 25) + b"tail")
        uploaded = provider.upload_relay_artifact(
            large_artifact,
            relay_url=registry_endpoint,
            media_type="application/zip",
        )
        large_hash = uploaded["blob"]["content_hash"]
        assert large_hash == sha256_bytes(large_artifact.read_bytes())
        assert uploaded["blob"]["metadata"]["relay_upload_mode"] == "chunked"
        assert uploaded["blob"]["metadata"]["relay_chunk_count"] > 1
        downloaded = provider.download_relay_artifact(
            large_hash,
            tmp_path / "downloaded-large.zip",
            relay_url=registry_endpoint,
        )
        assert Path(downloaded["path"]).read_bytes() == large_artifact.read_bytes()

        from rynmesh.relay import CHUNKED_MANIFEST_MEDIA_TYPE, HttpRelayClient, RelayError

        def old_relay_without_assemble(*args, **kwargs):
            raise RelayError('relay_http_error: 404 b\'{"detail":"Not Found"}\'')

        monkeypatch.setattr(HttpRelayClient, "_assemble_chunks", old_relay_without_assemble)
        manifest_artifact = tmp_path / "relay-manifest-source.zip"
        manifest_artifact.write_bytes((b"manifest relay artifact bytes-" * 20) + b"tail")
        uploaded = provider.upload_relay_artifact(
            manifest_artifact,
            relay_url=registry_endpoint,
            media_type="application/zip",
        )
        manifest_hash = uploaded["blob"]["content_hash"]
        assert uploaded["blob"]["media_type"] == CHUNKED_MANIFEST_MEDIA_TYPE
        assert uploaded["chunked_artifact"]["content_hash"] == sha256_bytes(manifest_artifact.read_bytes())
        downloaded = provider.download_relay_artifact(
            manifest_hash,
            tmp_path / "downloaded-manifest.zip",
            relay_url=registry_endpoint,
        )
        assert downloaded["content_hash"] == sha256_bytes(manifest_artifact.read_bytes())
        assert downloaded["relay_manifest_hash"] == manifest_hash
        assert Path(downloaded["path"]).read_bytes() == manifest_artifact.read_bytes()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_http_relay_download_uses_certifi_context_for_https() -> None:
    from rynmesh.relay import _urlopen_tls_kwargs

    secure = _urlopen_tls_kwargs("https://registry.rynmesh.ai", 12.5)
    plain = _urlopen_tls_kwargs("http://127.0.0.1:8000", 12.5)

    assert secure["timeout"] == 12.5
    assert "context" in secure
    assert plain == {"timeout": 12.5}


def test_signal50_relay_bundle_relocates_flow_paths(tmp_path) -> None:
    from rynmesh.signal50_service import (
        _find_flow_job_dir,
        _relay_bundle_from_params,
        _relocate_flow_job_paths,
        _safe_extract_zip,
    )

    source = tmp_path / "source"
    flow_job = source / "flow_job"
    (flow_job / "downloads" / "scene_01").mkdir(parents=True)
    (flow_job / "prompts").mkdir()
    (flow_job / "downloads" / "scene_01" / "old_start.png").write_bytes(b"img")
    storyboard = {
        "video_id": "history-1",
        "topic_title": "History",
        "category": "history",
        "storyboard_path": "/ms1/job/storyboard.json",
        "scene_prompts_dir": "/ms1/job/prompts",
        "downloads_dir": "/ms1/job/downloads",
        "scenes": [
            {
                "scene_id": "scene_01",
                "order": 1,
                "narration_text": "A canal changes trade.",
                "start_sec": 0,
                "end_sec": 8,
                "target_duration_sec": 8,
                "prompt": "1914 canal locks",
                "prompt_path": "/ms1/job/prompts/scene_01.txt",
                "expected_asset": "/ms1/job/downloads/scene_01/old.mp4",
                "kling_start_keyframe_path": "/ms1/job/downloads/scene_01/old_start.png",
            }
        ],
    }
    (flow_job / "storyboard.json").write_text(json.dumps(storyboard), encoding="utf-8")
    bundle = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        for path in flow_job.rglob("*"):
            if path.is_file():
                archive.write(path, Path("flow_job") / path.relative_to(flow_job))

    extracted = tmp_path / "extracted"
    _safe_extract_zip(bundle, extracted)
    relocated = _find_flow_job_dir(extracted)
    _relocate_flow_job_paths(relocated)
    payload = json.loads((relocated / "storyboard.json").read_text(encoding="utf-8"))
    scene = payload["scenes"][0]

    assert payload["downloads_dir"] == str(relocated / "downloads")
    assert scene["expected_asset"] == str(relocated / "downloads" / "scene_01" / "old.mp4")
    assert scene["kling_start_keyframe_path"] == str(
        relocated / "downloads" / "scene_01" / "old_start.png"
    )
    assert _relay_bundle_from_params(
        {"flow_job_bundle": {"content_hash": "sha256:" + "a" * 64}}
    ).content_hash.startswith("sha256:")


def test_signal50_media_ops_queue_runs_relay_jobs_serially(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import rynmesh.signal50_media_ops as media_ops

    calls: list[str] = []

    def fake_run_relay_bundle_order(*args, **kwargs):
        order = kwargs["order"]
        calls.append(str(order["work_order_id"]))
        time.sleep(0.01)
        return {
            "status": "completed",
            "message": "done",
            "result_content_ids": ["sha256:" + "b" * 64],
            "result_refs": {"relay_result_bundle": {"blob": {"content_hash": "sha256:" + "b" * 64}}},
        }

    monkeypatch.setattr(media_ops, "_run_relay_bundle_order", fake_run_relay_bundle_order)
    app = media_ops.create_app(
        store=RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "mesh"),
        work_dir=tmp_path / "ops",
        relay_url="https://registry.rynmesh.ai",
    )
    with TestClient(app) as client:
        first = client.post(
            "/api/jobs",
            json={
                "operation": media_ops.RELAY_BUNDLE_OPERATION,
                "params": {"flow_job_bundle": {"content_hash": "sha256:" + "a" * 64}},
            },
        ).json()["job"]
        second = client.post(
            "/api/jobs",
            json={
                "operation": media_ops.RELAY_BUNDLE_OPERATION,
                "params": {"flow_job_bundle": {"content_hash": "sha256:" + "c" * 64}},
            },
        ).json()["job"]

        first_done = _wait_media_ops_test_job(client, first["job_id"])
        second_done = _wait_media_ops_test_job(client, second["job_id"])

    assert first_done["status"] == "completed"
    assert second_done["status"] == "completed"
    assert calls == [first["job_id"], second["job_id"]]


def test_signal50_media_ops_job_reads_remain_valid_during_updates(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import rynmesh.signal50_media_ops as media_ops

    def fake_run_relay_bundle_order(*args, **kwargs):
        time.sleep(0.05)
        return {"status": "completed", "message": "done"}

    monkeypatch.setattr(media_ops, "_run_relay_bundle_order", fake_run_relay_bundle_order)
    app = media_ops.create_app(
        store=RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "mesh"),
        work_dir=tmp_path / "ops",
    )
    with TestClient(app) as client:
        submitted = client.post(
            "/api/jobs",
            json={
                "operation": media_ops.RELAY_BUNDLE_OPERATION,
                "params": {"flow_job_bundle": {"content_hash": "sha256:" + "a" * 64}},
            },
        ).json()["job"]

        responses = [client.get(f"/api/jobs/{submitted['job_id']}") for _ in range(100)]
        completed = _wait_media_ops_test_job(client, submitted["job_id"])

    assert all(response.status_code == 200 for response in responses)
    assert all(response.json().get("job", {}).get("job_id") == submitted["job_id"] for response in responses)
    assert completed["status"] == "completed"


def test_signal50_service_forwards_relay_bundle_to_media_ops(monkeypatch, tmp_path) -> None:
    import rynmesh.signal50_service as service

    seen: dict[str, object] = {}

    def fake_submit(media_ops_url, **kwargs):
        seen["media_ops_url"] = media_ops_url
        seen["kwargs"] = kwargs
        return {"job_id": "mops_1"}

    def fake_wait(media_ops_url, job_id, *, timeout_sec):
        return {
            "job_id": job_id,
            "status": "completed",
            "result": {
                "status": "completed",
                "message": "queued media ops done",
                "result_refs": {"relay_mode": "rynmesh_http_relay"},
            },
        }

    monkeypatch.setattr(service, "_submit_media_ops_job", fake_submit)
    monkeypatch.setattr(service, "_wait_media_ops_job", fake_wait)
    result = service._run_order(
        None,
        order={
            "work_order_id": "wo_1",
            "params": {"flow_job_bundle": {"content_hash": "sha256:" + "a" * 64}},
        },
        api_url="http://signal50",
        token="token",
        timeout_sec=10,
        work_dir=tmp_path,
        relay_url="https://registry.rynmesh.ai",
        signal50_repo="",
        media_ops_url="http://127.0.0.1:5055",
    )

    assert seen["media_ops_url"] == "http://127.0.0.1:5055"
    assert result["status"] == "completed"
    assert result["result_refs"]["relay_mode"] == "rynmesh_http_relay"


def _wait_media_ops_test_job(client, job_id: str) -> dict:
    deadline = time.time() + 3
    latest = {}
    while time.time() < deadline:
        latest = client.get(f"/api/jobs/{job_id}").json()["job"]
        if latest["status"] in {"completed", "failed", "cancelled"}:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"media ops job did not finish: {latest}")


def test_store_registration_includes_machine_ip_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RYNMESH_MACHINE_NAME", "MS-1")
    monkeypatch.setenv("RYNMESH_MACHINE_IP", "10.0.0.11")
    monkeypatch.setenv("RYNMESH_PEER_HOST", "0.0.0.0")
    monkeypatch.setenv("RYNMESH_PEER_PORT", "8791")
    monkeypatch.delenv("RYNMESH_PEER_ENDPOINT", raising=False)

    node = RynmeshStore(home=tmp_path / "ms-1", network_dir=tmp_path / "mesh", node_name="MS-1")
    registered = node.register_node(network_id="home-four-node")

    record = registered["record"]
    assert record["endpoints"] == ["http://10.0.0.11:8791"]
    assert record["metadata"]["machine_name"] == "MS-1"
    assert record["metadata"]["primary_ip"] == "10.0.0.11"
    assert "10.0.0.11" in record["metadata"]["ip_addresses"]


def test_store_uses_cached_peers_when_registry_unavailable(tmp_path) -> None:
    class BrokenRegistry:
        def publish(self, signed_record: SignedPayload) -> dict:
            raise RegistryError("registry_down")

        def list_peers(self, *, network_id: str = "rynmesh-main", max_age_hours=None) -> list:
            raise RegistryError("registry_down")

    pytest.importorskip("cryptography")
    network = tmp_path / "mesh"
    node_a = RynmeshStore(home=tmp_path / "ms-1", network_dir=network, node_name="ms-1")
    node_b = RynmeshStore(home=tmp_path / "ms-2", network_dir=network, node_name="ms-2")
    node_a.register_node(endpoints=("http://ms-1.local:8791",), network_id="cache-test")

    discovered = node_b.discover_peers(network_id="cache-test")
    assert discovered["source"] == "registry"
    assert discovered["peers"][0]["node_name"] == "ms-1"

    node_b.registry = BrokenRegistry()
    cached = node_b.discover_peers(network_id="cache-test")
    assert cached["source"] == "cache"
    assert cached["peers"][0]["node_name"] == "ms-1"


def test_peer_cache_replaces_stale_records_after_successful_discovery(tmp_path) -> None:
    class StaticRegistry:
        def __init__(self, records) -> None:
            self.records = records

        def publish(self, signed_record: SignedPayload) -> dict:
            return {"status": "registered"}

        def list_peers(self, *, network_id: str = "rynmesh-main", max_age_hours=None) -> list:
            return list(self.records)

    class BrokenRegistry:
        def publish(self, signed_record: SignedPayload) -> dict:
            raise RegistryError("registry_down")

        def list_peers(self, *, network_id: str = "rynmesh-main", max_age_hours=None) -> list:
            raise RegistryError("registry_down")

    pytest.importorskip("cryptography")
    peer_key = _key()
    signed_peer = sign_peer_record(
        PeerRecord(
            peer_id=public_key_from_private(peer_key),
            node_name="ms-1",
            endpoints=("http://ms-1.local:8791",),
            network_id="cache-test",
        ),
        private_key_bytes=peer_key,
    )
    node_b = RynmeshStore(
        home=tmp_path / "ms-2",
        network_dir=tmp_path / "mesh",
        node_name="ms-2",
    )

    node_b.registry = StaticRegistry([signed_peer])
    discovered = node_b.discover_peers(network_id="cache-test")
    assert discovered["source"] == "registry"
    assert [peer["node_name"] for peer in discovered["peers"]] == ["ms-1"]

    node_b.registry = StaticRegistry([])
    refreshed = node_b.discover_peers(network_id="cache-test")
    assert refreshed["source"] == "registry"
    assert refreshed["peers"] == []

    node_b.registry = BrokenRegistry()
    cached = node_b.discover_peers(network_id="cache-test")
    assert cached["source"] == "cache"
    assert cached["peers"] == []


def test_http_clients_reject_blocked_metadata_endpoints() -> None:
    with pytest.raises(PeerTransportError, match="peer_endpoint_host_blocked"):
        HttpPeerClient("http://169.254.169.254")
    with pytest.raises(RegistryError, match="registry_url_host_blocked"):
        HttpPeerRegistry("http://169.254.169.254")


def test_peer_http_rejects_invalid_route_ids(tmp_path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    node = RynmeshStore(home=tmp_path / "ms-1", network_dir=tmp_path / "mesh", node_name="ms-1")
    client = TestClient(create_app(node))

    assert client.get("/api/v1/content/bad%20id/manifest").status_code == 400
    assert client.get("/api/v1/clips/bad%20id/preview").status_code == 400
    assert client.get("/api/v1/content/bad%20id/bytes").status_code == 400


def test_peer_http_local_control_api_uses_real_store_data(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_NETWORK_ID", "home-four-node")

    registry = FilePeerRegistry(tmp_path / "registry")
    node_a = RynmeshStore(home=tmp_path / "ms-1", network_dir=tmp_path / "mesh-a", node_name="MS-1")
    node_b = RynmeshStore(home=tmp_path / "ms-2", network_dir=tmp_path / "mesh-b", node_name="MS-2")
    node_a.registry = registry
    node_b.registry = registry
    node_b.register_node(endpoints=("http://10.0.0.12:8791",), network_id="home-four-node")

    content = tmp_path / "ms-1-note.md"
    content.write_text("# MS-1 note\n\nReal LAN QA material.\n", encoding="utf-8")
    published = node_a.publish_content(
        content,
        title="MS-1 real note",
        summary_text="safe LAN QA material",
        content_type="text/markdown",
        content_kind="document",
    )
    assert published["status"] == "published"

    client = TestClient(create_app(node_a))
    peers = client.get("/api/local/peers").json()
    assert {peer["name"] for peer in peers} == {"MS-1", "MS-2"}
    assert any(peer["endpoint"] == "http://10.0.0.12:8791" for peer in peers)

    items = client.get("/api/local/content").json()
    [item] = [item for item in items if item["title"] == "MS-1 real note"]
    assert item["fetch_status"] == "local"
    assert item["content_kind"] == "document"
    assert item["safety_outcome"] == "passed"

    node_b.register_job_capacity(
        capabilities=("signal50.veo_motion.v1",),
        network_id="home-four-node",
        price_credits={"signal50.veo_motion.v1": 20},
        metadata={"route": "m4_mini_hammerspoon_chrome_cdp"},
    )
    capacity = client.get(
        "/api/local/jobs/capacity",
        params={"capability": "signal50.veo_motion.v1"},
    ).json()
    assert capacity[0]["peer_id"] == node_b.peer_id
    assert capacity[0]["provider_name"] == "MS-2"

    submitted = client.post(
        "/api/local/jobs/work-orders",
        json={
            "provider_peer_id": node_b.peer_id,
            "capability": "signal50.veo_motion.v1",
            "operation": "signal50.remote_action.complete_flow_video_veo_motion_clips",
            "params": {"video_id": "casefile-tt-deep-1", "skip_existing": True},
            "max_credit_cost": 20,
        },
    ).json()
    assert submitted["work_order_id"]
    polled = node_b.poll_work_orders(network_id="home-four-node", capability="signal50.veo_motion.v1")
    assert polled["work_orders"][0]["work_order_id"] == submitted["work_order_id"]

    node_b.publish_work_result(
        work_order_id=submitted["work_order_id"],
        requester_peer_id=node_a.peer_id,
        status="completed",
        message="remote Veo render completed",
        network_id="home-four-node",
    )
    results = client.get(
        "/api/local/jobs/work-results",
        params={"work_order_id": submitted["work_order_id"]},
    ).json()
    assert results["work_results"][0]["status"] == "completed"


def test_mcp_rejects_manual_positive_credit_events(tmp_path) -> None:
    from rynmesh.mcp_server import _dispatch_tool

    node = RynmeshStore(home=tmp_path / "ms-1", network_dir=tmp_path / "mesh", node_name="ms-1")
    with pytest.raises(ValueError, match="manual_positive_credit_event_disabled"):
        _dispatch_tool(
            node,
            "rynmesh_record_credit_event",
            {"kind": "registry_operated", "dedupe_key": "fake-registry"},
        )


def test_mcp_rejects_manual_external_penalty_events(tmp_path) -> None:
    from rynmesh.mcp_server import _dispatch_tool

    network = tmp_path / "mesh"
    node = RynmeshStore(home=tmp_path / "ms-1", network_dir=network, node_name="ms-1")
    target = RynmeshStore(home=tmp_path / "ms-2", network_dir=network, node_name="ms-2")
    with pytest.raises(ValueError, match="manual_external_penalty_event_disabled"):
        _dispatch_tool(
            node,
            "rynmesh_record_credit_event",
            {
                "kind": "protocol_violation",
                "subject_peer_id": target.peer_id,
                "dedupe_key": "fake-penalty",
            },
        )


def test_credit_ledger_rewards_useful_work_and_slashes_violations(tmp_path) -> None:
    pytest.importorskip("cryptography")
    network = tmp_path / "mesh"
    publisher = RynmeshStore(home=tmp_path / "ms-1", network_dir=network, node_name="ms-1")
    consumer = RynmeshStore(home=tmp_path / "ms-2", network_dir=network, node_name="ms-2")
    media = tmp_path / "credit-video.mp4"
    media.write_bytes(b"credit video bytes")

    registered = publisher.register_node(network_id="credits-test")
    assert registered["credit"]["status"] == "recorded"

    published = publisher.publish_clip(
        media,
        title="Credit video",
        transcript="agent generated credit video",
        category="education",
    )
    assert published["credit"]["kind"] == "clip_published"

    summary = publisher.credit_summary(peer_id=publisher.peer_id)
    assert summary["score"] > 8.9
    assert summary["distribution_weight"] > 1.0

    fetched = consumer.fetch_full(published["clip_id"])
    assert fetched["credit"]["kind"] == "full_served"

    provider_summary = publisher.credit_summary(peer_id=publisher.peer_id)
    assert provider_summary["score"] > summary["score"]

    with pytest.raises(CreditLedgerError, match="credit_penalty_issuer_untrusted"):
        consumer.record_credit_event(
            kind="protocol_violation",
            subject_peer_id=publisher.peer_id,
            role="provider",
            subject_id=published["clip_id"],
            evidence_hash=published["manifest_hash"],
            reason="untrusted external violation",
            dedupe_key="untrusted-violation",
        )

    with pytest.raises(CreditLedgerError, match="credit_event_amount_mismatch"):
        publisher.record_credit_event(kind="full_served", amount=9999)

    slashed = publisher.record_credit_event(
        kind="protocol_violation",
        role="provider",
        subject_id=published["clip_id"],
        evidence_hash=published["manifest_hash"],
        reason="self-reported test violation",
        dedupe_key="test-violation",
    )
    assert slashed["amount"] == -25.0

    slashed_summary = publisher.credit_summary(peer_id=publisher.peer_id)
    assert slashed_summary["score"] < 0
    assert slashed_summary["distribution_weight"] < 1.0

    scoreboard = consumer.credit_scoreboard()
    assert scoreboard["accounts"][0]["peer_id"] == publisher.peer_id
    assert scoreboard["accounts"][0]["raw_negative"] == -25.0


def test_rank_clips_uses_credit_distribution_weight(tmp_path) -> None:
    pytest.importorskip("cryptography")
    network = tmp_path / "mesh"
    high_credit = RynmeshStore(home=tmp_path / "ms-1", network_dir=network, node_name="ms-1")
    low_credit = RynmeshStore(home=tmp_path / "ms-2", network_dir=network, node_name="ms-2")
    viewer = RynmeshStore(home=tmp_path / "viewer", network_dir=network, node_name="viewer")
    high_media = tmp_path / "high.mp4"
    low_media = tmp_path / "low.mp4"
    high_media.write_bytes(b"high credit video")
    low_media.write_bytes(b"low credit video")

    high_credit.publish_clip(high_media, title="High credit", transcript="safe generated video")
    low_credit.publish_clip(low_media, title="Low credit", transcript="safe generated video")
    high_credit.record_credit_event(
        kind="registry_operated",
        role="registry",
        subject_id="tier-2-registry",
        reason="operated a useful second-layer registry",
        dedupe_key="tier-2-registry",
    )

    ranked = viewer.rank_clips()
    assert ranked["clips"][0]["title"] == "High credit"
    assert ranked["clips"][0]["distribution_weight"] > ranked["clips"][1]["distribution_weight"]


def test_direct_http_peer_transport_lists_and_fetches_media(tmp_path, monkeypatch) -> None:
    pytest.importorskip("cryptography")
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    network = tmp_path / "mesh"
    publisher_home = tmp_path / "ms-1"
    consumer_home = tmp_path / "ms-2"
    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("RYNMESH_PEER_HOST", "127.0.0.1")
    monkeypatch.setenv("RYNMESH_PEER_PORT", str(port))

    publisher = RynmeshStore(home=publisher_home, network_dir=network, node_name="ms-1")
    media = tmp_path / "direct-peer.mp4"
    media.write_bytes(b"direct peer video bytes")
    published = publisher.publish_clip(
        media,
        title="Direct peer video",
        transcript="agent generated direct peer video",
    )
    assert published["status"] == "published"

    registered = publisher.register_node(network_id="phase3-test")
    assert registered["record"]["endpoints"] == [endpoint]

    env = os.environ.copy()
    env["RYNMESH_HOME"] = str(publisher_home)
    env["RYNMESH_NETWORK_DIR"] = str(network)
    env["RYNMESH_NODE_NAME"] = "ms-1"
    env["RYNMESH_PEER_HOST"] = "127.0.0.1"
    env["RYNMESH_PEER_PORT"] = str(port)
    proc = subprocess.Popen(
        PEER_RUNNER,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        health = _wait_http_json(endpoint, "/health", proc)
        assert health["peer_id"] == publisher.peer_id
        credit_status = _wait_http_json(endpoint, "/api/v1/credits", proc)
        assert credit_status["peer_id"] == publisher.peer_id
        assert credit_status["score"] > 0
        content_status = _wait_http_json(endpoint, "/api/v1/content", proc)
        assert content_status["content"][0]["content_id"] == published["content_id"]

        consumer = RynmeshStore(home=consumer_home, network_dir=network, node_name="ms-2")
        discovered = consumer.discover_peers(network_id="phase3-test")
        assert discovered["peers"][0]["endpoints"] == [endpoint]

        peer_clips = consumer.list_peer_clips(endpoint)
        assert peer_clips["peer"]["peer_id"] == publisher.peer_id
        assert peer_clips["clips"][0]["clip_id"] == published["clip_id"]
        assert peer_clips["clips"][0]["provider_peer_id"] == publisher.peer_id
        assert peer_clips["clips"][0]["publisher_peer_id"] == publisher.peer_id
        peer_content = consumer.list_peer_content(endpoint)
        assert peer_content["content"][0]["content_id"] == published["content_id"]

        http_client = HttpPeerClient(endpoint)
        manifest = http_client.get_content_manifest(published["content_id"])
        assert manifest.subject_hash == published["manifest_hash"]
        assert http_client.get_content_preview(published["content_id"]) == b"direct peer video bytes"
        downloaded = http_client.download_content(
            published["content_id"],
            tmp_path / "downloaded-content.bin",
        )
        assert downloaded.read_bytes() == b"direct peer video bytes"

        with pytest.raises(StoreError, match="peer_endpoint_id_mismatch"):
            consumer.fetch_peer_preview(
                endpoint,
                published["clip_id"],
                expected_peer_id="not-the-publisher",
            )

        preview = consumer.fetch_peer_preview(
            endpoint,
            published["clip_id"],
            expected_peer_id=publisher.peer_id,
        )
        assert Path(preview["preview_path"]).read_bytes() == b"direct peer video bytes"
        assert preview["provider_peer_id"] == publisher.peer_id

        full = consumer.fetch_peer_full(
            endpoint,
            published["clip_id"],
            expected_peer_id=publisher.peer_id,
        )
        assert Path(full["media_path"]).read_bytes() == b"direct peer video bytes"
        assert full["publisher_peer_id"] == publisher.peer_id

        local = consumer.list_local_clips()
        assert local["clips"][0]["provider_peer_id"] == consumer.peer_id
        assert local["clips"][0]["publisher_peer_id"] == publisher.peer_id
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_http_registry_discovery_and_peer_fetch_without_shared_filesystem(
    tmp_path,
    monkeypatch,
) -> None:
    pytest.importorskip("cryptography")
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    registry_port = _free_port()
    peer_port = _free_port()
    registry_endpoint = f"http://127.0.0.1:{registry_port}"
    peer_endpoint = f"http://127.0.0.1:{peer_port}"

    registry_env = os.environ.copy()
    registry_env["RYNMESH_REGISTRY_DIR"] = str(tmp_path / "registry")
    registry_env["RYNMESH_REGISTRY_HOST"] = "127.0.0.1"
    registry_env["RYNMESH_REGISTRY_PORT"] = str(registry_port)
    registry_proc = subprocess.Popen(
        REGISTRY_RUNNER,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=registry_env,
    )
    peer_proc: subprocess.Popen[str] | None = None
    try:
        health = _wait_http_json(registry_endpoint, "/health", registry_proc)
        assert health["kind"] == "rynmesh-registry"

        monkeypatch.setenv("RYNMESH_REGISTRY_URL", registry_endpoint)
        publisher_home = tmp_path / "publisher-home"
        publisher_network = tmp_path / "publisher-local-network"
        consumer_network = tmp_path / "consumer-local-network"
        publisher = RynmeshStore(
            home=publisher_home,
            network_dir=publisher_network,
            node_name="ms-1",
        )
        content = tmp_path / "no-shared-fs.md"
        content.write_text("# No shared filesystem\n\nFetched through peer HTTP.\n", encoding="utf-8")
        published = publisher.publish_content(
            content,
            title="No shared filesystem",
            summary_text="safe generated registry discovery test",
            content_type="text/markdown",
            content_kind="document",
        )

        peer_env = os.environ.copy()
        peer_env["RYNMESH_HOME"] = str(publisher_home)
        peer_env["RYNMESH_NETWORK_DIR"] = str(publisher_network)
        peer_env["RYNMESH_NODE_NAME"] = "ms-1"
        peer_env["RYNMESH_PEER_HOST"] = "127.0.0.1"
        peer_env["RYNMESH_PEER_PORT"] = str(peer_port)
        peer_proc = subprocess.Popen(
            PEER_RUNNER,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=peer_env,
        )
        peer_health = _wait_http_json(peer_endpoint, "/health", peer_proc)
        assert peer_health["peer_id"] == publisher.peer_id

        registered = publisher.register_node(
            endpoints=(peer_endpoint,),
            network_id="no-shared-filesystem",
        )
        assert registered["status"] == "registered"

        consumer = RynmeshStore(
            home=tmp_path / "consumer-home",
            network_dir=consumer_network,
            node_name="ms-2",
        )
        assert consumer.list_content()["content"] == []
        discovered = consumer.discover_peers(network_id="no-shared-filesystem")
        assert discovered["peers"][0]["endpoints"] == [peer_endpoint]

        peer_content = consumer.list_peer_content(peer_endpoint)
        assert peer_content["content"][0]["content_id"] == published["content_id"]
        fetched = consumer.fetch_peer_content_full(
            peer_endpoint,
            published["content_id"],
            expected_peer_id=publisher.peer_id,
        )
        assert Path(fetched["content_path"]).read_text(encoding="utf-8").startswith(
            "# No shared filesystem"
        )
    finally:
        if peer_proc is not None:
            peer_proc.terminate()
            try:
                peer_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                peer_proc.kill()
                peer_proc.wait(timeout=5)
        registry_proc.terminate()
        try:
            registry_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            registry_proc.kill()
            registry_proc.wait(timeout=5)


def _mcp_request(proc: subprocess.Popen[str], message: dict, *, timeout_s: float = 30.0) -> dict:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()
    if os.name == "nt":
        lines: queue.Queue[str] = queue.Queue(maxsize=1)
        threading.Thread(target=lambda: lines.put(proc.stdout.readline()), daemon=True).start()
        try:
            line = lines.get(timeout=timeout_s)
        except queue.Empty:
            pytest.fail(f"Timed out waiting for MCP response: {message['method']}")
    else:
        ready, _, _ = select.select([proc.stdout], [], [], timeout_s)
        assert ready, f"Timed out waiting for MCP response: {message['method']}"
        line = proc.stdout.readline()
    assert line, f"MCP server closed unexpectedly while handling {message['method']}"
    return json.loads(line)


def test_rynmesh_mcp_publish_and_list_smoke(tmp_path) -> None:
    pytest.importorskip("cryptography")
    env = os.environ.copy()
    env["RYNMESH_HOME"] = str(tmp_path / "ms-1")
    env["RYNMESH_NETWORK_DIR"] = str(tmp_path / "mesh")
    env["RYNMESH_NODE_NAME"] = "ms-1"
    media = tmp_path / "mcp-video.mp4"
    media.write_bytes(b"mcp video bytes")
    report = tmp_path / "mcp-report.md"
    report.write_text("# MCP report\n\nGenerated by an agent.\n", encoding="utf-8")

    proc = subprocess.Popen(
        MCP_RUNNER,
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    try:
        init = _mcp_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            },
        )
        assert init["result"]["serverInfo"]["name"] == "rynmesh-local"

        listed = _mcp_request(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tool_names = {tool["name"] for tool in listed["result"]["tools"]}
        assert {
            "rynmesh_node_info",
            "rynmesh_publish_clip",
            "rynmesh_publish_content",
            "rynmesh_list_clips",
            "rynmesh_list_content",
            "rynmesh_register_node",
            "rynmesh_discover_peers",
            "rynmesh_register_job_capacity",
            "rynmesh_list_job_capacities",
            "rynmesh_submit_work_order",
            "rynmesh_poll_work_orders",
            "rynmesh_publish_work_result",
            "rynmesh_list_work_results",
            "rynmesh_upload_relay_artifact",
            "rynmesh_download_relay_artifact",
            "rynmesh_relay_artifact_info",
            "rynmesh_fetch_preview",
            "rynmesh_fetch_full",
            "rynmesh_fetch_content_preview",
            "rynmesh_fetch_content_full",
            "rynmesh_list_peer_clips",
            "rynmesh_list_peer_content",
            "rynmesh_fetch_peer_preview",
            "rynmesh_fetch_peer_full",
            "rynmesh_fetch_peer_content_preview",
            "rynmesh_fetch_peer_content_full",
            "rynmesh_credit_summary",
            "rynmesh_credit_scoreboard",
            "rynmesh_record_credit_event",
            "rynmesh_rank_clips",
            "rynmesh_rank_content",
        }.issubset(tool_names)

        registered = _mcp_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "rynmesh_register_node",
                    "arguments": {"network_id": "mcp-test"},
                },
            },
        )["result"]["structuredContent"]
        assert registered["status"] == "registered"

        peers = _mcp_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "rynmesh_discover_peers",
                    "arguments": {"network_id": "mcp-test", "include_self": True},
                },
            },
        )["result"]["structuredContent"]
        assert peers["peers"][0]["node_name"] == "ms-1"

        published = _mcp_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "rynmesh_publish_clip",
                    "arguments": {
                        "media_path": str(media),
                        "title": "MCP video",
                        "transcript": "agent generated video",
                    },
                },
            },
        )["result"]["structuredContent"]
        assert published["status"] == "published"

        content_published = _mcp_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "rynmesh_publish_content",
                    "arguments": {
                        "content_path": str(report),
                        "title": "MCP report",
                        "summary_text": "safe generated agent report",
                        "content_type": "text/markdown",
                        "content_kind": "document",
                        "tags": ["topic:mcp"],
                    },
                },
            },
        )["result"]["structuredContent"]
        assert content_published["status"] == "published"
        assert content_published["content_kind"] == "document"

        credit = _mcp_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "rynmesh_credit_summary", "arguments": {}},
            },
        )["result"]["structuredContent"]
        assert credit["score"] > 0
        assert credit["distribution_weight"] > 1.0

        clips = _mcp_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "rynmesh_list_clips", "arguments": {}},
            },
        )["result"]["structuredContent"]
        assert published["clip_id"] in {item["clip_id"] for item in clips["clips"]}

        content = _mcp_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "rynmesh_list_content", "arguments": {}},
            },
        )["result"]["structuredContent"]
        assert {item["content_id"] for item in content["content"]} == {
            published["content_id"],
            content_published["content_id"],
        }

        ranked = _mcp_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "rynmesh_rank_clips", "arguments": {}},
            },
        )["result"]["structuredContent"]
        assert published["clip_id"] in {item["clip_id"] for item in ranked["clips"]}

        ranked_content = _mcp_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "rynmesh_rank_content", "arguments": {}},
            },
        )["result"]["structuredContent"]
        assert {item["content_id"] for item in ranked_content["content"]} == {
            published["content_id"],
            content_published["content_id"],
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_file_credit_ledger_enforces_issuer_tier_via_resolver(tmp_path) -> None:
    pytest.importorskip("cryptography")
    from rynmesh.credits import (
        EVENT_WEIGHTS,
        CreditEvent,
        CreditLedgerError,
        CreditPolicy,
        FileCreditLedger,
        sign_credit_event,
    )

    issuer_key = _key()
    subject_key = _key()
    issuer_id = public_key_from_private(issuer_key)
    subject_id = public_key_from_private(subject_key)

    tier_lookup = {issuer_id: IdentityTier.UNVERIFIED}
    ledger = FileCreditLedger(
        tmp_path / "credits",
        policy=CreditPolicy(enforce_issuer_tier=True),
        tier_resolver=lambda pid: tier_lookup.get(pid, IdentityTier.UNVERIFIED),
    )

    event = CreditEvent(
        subject_peer_id=subject_id,
        issuer_peer_id=issuer_id,
        kind="registry_operated",
        amount=EVENT_WEIGHTS["registry_operated"],
    )
    signed = sign_credit_event(event, private_key_bytes=issuer_key)

    with pytest.raises(CreditLedgerError, match="credit_issuer_tier_too_low"):
        ledger.append(signed)

    # Promote the issuer; the same append now succeeds.
    tier_lookup[issuer_id] = IdentityTier.ATTESTED
    result = ledger.append(signed)
    assert result["status"] == "recorded"


def test_file_credit_ledger_distribution_cap_applies_via_policy(tmp_path) -> None:
    pytest.importorskip("cryptography")
    from rynmesh.credits import (
        EVENT_WEIGHTS,
        CreditEvent,
        CreditPolicy,
        FileCreditLedger,
        sign_credit_event,
    )

    subject_key = _key()
    subject_id = public_key_from_private(subject_key)
    # Force the issuer to also be the subject so penalty/penalty checks pass.
    publish_amount = EVENT_WEIGHTS["content_published"]

    capped_policy = CreditPolicy(enforce_distribution_tier_cap=True)
    capped_ledger = FileCreditLedger(
        tmp_path / "capped",
        policy=capped_policy,
        tier_resolver=lambda _pid: IdentityTier.UNVERIFIED,
    )

    for i in range(20):
        event = CreditEvent(
            subject_peer_id=subject_id,
            issuer_peer_id=subject_id,
            kind="content_published",
            amount=publish_amount,
            dedupe_key=f"event-{i}",
        )
        capped_ledger.append(sign_credit_event(event, private_key_bytes=subject_key))

    account = capped_ledger.account(subject_id)
    # Raw decayed score >> 1, but the unverified cap clamps the weight.
    assert account.score > 1.0
    assert account.distribution_weight == 1.0

    # Same ledger, no enforcement → uncapped weight.
    uncapped_ledger = FileCreditLedger(
        tmp_path / "capped",
        policy=CreditPolicy(enforce_distribution_tier_cap=False),
        tier_resolver=lambda _pid: IdentityTier.UNVERIFIED,
    )
    uncapped_account = uncapped_ledger.account(subject_id)
    assert uncapped_account.distribution_weight > 1.0


def test_file_credit_ledger_refuses_enforcement_without_resolver(tmp_path) -> None:
    pytest.importorskip("cryptography")
    from rynmesh.credits import CreditLedgerError, CreditPolicy, FileCreditLedger

    with pytest.raises(CreditLedgerError, match="credit_distribution_tier_cap_resolver_required"):
        FileCreditLedger(
            tmp_path / "credits-cap",
            policy=CreditPolicy(enforce_distribution_tier_cap=True),
        )

    with pytest.raises(CreditLedgerError, match="credit_issuer_tier_resolver_required"):
        FileCreditLedger(
            tmp_path / "credits-issuer",
            policy=CreditPolicy(enforce_issuer_tier=True),
        )


def test_file_credit_ledger_re_enforces_gate_on_reads(tmp_path) -> None:
    """Events written before the gate enabled should be filtered out on re-read."""

    pytest.importorskip("cryptography")
    from rynmesh.credits import (
        EVENT_WEIGHTS,
        CreditEvent,
        CreditPolicy,
        FileCreditLedger,
        sign_credit_event,
    )

    issuer_key = _key()
    subject_key = _key()
    issuer_id = public_key_from_private(issuer_key)
    subject_id = public_key_from_private(subject_key)

    # Phase 1: gate disabled, append succeeds.
    relaxed = FileCreditLedger(tmp_path / "credits", policy=CreditPolicy())
    event = CreditEvent(
        subject_peer_id=subject_id,
        issuer_peer_id=issuer_id,
        kind="registry_operated",
        amount=EVENT_WEIGHTS["registry_operated"],
    )
    relaxed.append(sign_credit_event(event, private_key_bytes=issuer_key))
    assert len(relaxed.list_events()) == 1

    # Phase 2: gate enabled, issuer resolves to UNVERIFIED → event drops.
    strict = FileCreditLedger(
        tmp_path / "credits",
        policy=CreditPolicy(enforce_issuer_tier=True),
        tier_resolver=lambda _pid: IdentityTier.UNVERIFIED,
    )
    assert strict.list_events() == []
    assert strict.scoreboard() == {}


def test_file_credit_ledger_dedupe_scans_with_candidate_issuer_tier(tmp_path) -> None:
    pytest.importorskip("cryptography")
    from rynmesh.credits import (
        EVENT_WEIGHTS,
        CreditEvent,
        CreditPolicy,
        FileCreditLedger,
        sign_credit_event,
    )

    root_key = _key()
    low_key = _key()
    subject_key = _key()
    root_id = public_key_from_private(root_key)
    low_id = public_key_from_private(low_key)
    subject_id = public_key_from_private(subject_key)
    tiers = {
        root_id: IdentityTier.ATTESTED,
        low_id: IdentityTier.UNVERIFIED,
        subject_id: IdentityTier.UNVERIFIED,
    }
    ledger = FileCreditLedger(
        tmp_path / "credits",
        policy=CreditPolicy(enforce_issuer_tier=True),
        tier_resolver=lambda pid: tiers.get(pid, IdentityTier.UNVERIFIED),
    )

    high_tier_event = CreditEvent(
        subject_peer_id=subject_id,
        issuer_peer_id=root_id,
        kind="registry_operated",
        amount=EVENT_WEIGHTS["registry_operated"],
    )
    assert ledger.append(sign_credit_event(high_tier_event, private_key_bytes=root_key))[
        "status"
    ] == "recorded"

    low_tier_event = CreditEvent(
        subject_peer_id=subject_id,
        issuer_peer_id=low_id,
        kind="content_published",
        amount=EVENT_WEIGHTS["content_published"],
        dedupe_key="low-publish",
    )
    signed_low = sign_credit_event(low_tier_event, private_key_bytes=low_key)
    assert ledger.append(signed_low)["status"] == "recorded"
    assert ledger.append(signed_low)["status"] == "already_recorded"


def test_sim_run_json_respects_expected_failures(capsys) -> None:
    from sim.run import main

    assert main(["--json"]) == 0
    captured = capsys.readouterr()
    assert "sybil_farm_without_enforcement" in captured.out
    assert '"matched_expectation": true' in captured.out


def test_store_promotes_remote_peer_via_configured_trust_roots(tmp_path) -> None:
    pytest.importorskip("cryptography")

    # Three external "trust root" peers vouch for a remote candidate.
    root_keys = [_key() for _ in range(3)]
    root_ids = tuple(public_key_from_private(k) for k in root_keys)
    candidate_key = _key()
    candidate_id = public_key_from_private(candidate_key)

    store = RynmeshStore(
        home=tmp_path / "node",
        network_dir=tmp_path / "mesh",
        node_name="self",
        trusted_root_peer_ids=root_ids,
    )

    # Without vouches: candidate is unverified.
    assert store._resolve_identity_tier(candidate_id) is IdentityTier.UNVERIFIED

    # Drop signed vouches into identity-claims/peer_vouch/. Use safe numeric
    # filenames — base64 peer IDs contain '/' which would otherwise create
    # unintended subdirectories and fail intermittently.
    vouch_dir = store.identity_dir / "peer_vouch"
    vouch_dir.mkdir(parents=True, exist_ok=True)
    for index, (issuer_key, issuer_id) in enumerate(
        zip(root_keys, root_ids, strict=True)
    ):
        vouch = PeerVouch(subject_peer_id=candidate_id, issuer_peer_id=issuer_id)
        signed = sign_peer_vouch(vouch, private_key_bytes=issuer_key)
        path = vouch_dir / f"vouch-{index}.json"
        path.write_text(json.dumps(signed.to_dict(), indent=2, sort_keys=True))

    # With three valid vouches: candidate reaches attested.
    assert store._resolve_identity_tier(candidate_id) is IdentityTier.ATTESTED


def test_store_default_resolver_treats_self_as_attested(tmp_path) -> None:
    pytest.importorskip("cryptography")
    store = RynmeshStore(
        home=tmp_path / "node",
        network_dir=tmp_path / "mesh",
        node_name="self",
    )
    assert store._resolve_identity_tier(store.peer_id) is IdentityTier.ATTESTED
    other_id = public_key_from_private(_key())
    assert store._resolve_identity_tier(other_id) is IdentityTier.UNVERIFIED


def _build_full_chain(_tmp_path) -> tuple[list[SignedPayload], bytes, bytes, bytes, str]:
    """Return (chain, publisher_key, scanner_key, peer_key, content_id)."""

    pub_key = _key()
    scanner_key = _key()
    peer_key = _key()
    pub_id = public_key_from_private(pub_key)
    scanner_id = public_key_from_private(scanner_key)
    peer_id = public_key_from_private(peer_key)
    content_id = "sha256:" + "a" * 64

    genesis = make_genesis_link(
        content_id=content_id,
        publisher_peer_id=pub_id,
        private_key_bytes=pub_key,
        run_id="run-prov-1",
        model_id="claude-opus-4-7",
        prompt_hash="sha256:" + "b" * 64,
    )
    chain = [genesis]
    append_link(
        chain,
        link_type=LINK_WORK_ENVELOPE,
        issuer_peer_id=pub_id,
        private_key_bytes=pub_key,
        payload={"work_id": "work-1", "envelope_hash": "sha256:" + "c" * 64},
    )
    append_link(
        chain,
        link_type=LINK_SAFETY_SCAN,
        issuer_peer_id=scanner_id,
        private_key_bytes=scanner_key,
        payload={"scanner_id": "kw-v1", "outcome": "pass", "policy_version": "v0"},
    )
    append_link(
        chain,
        link_type=LINK_PEER_ATTESTATION,
        issuer_peer_id=peer_id,
        private_key_bytes=peer_key,
        payload={"peer_id": peer_id, "bytes_seen_at": "2026-05-13T00:00:00Z"},
    )
    return chain, pub_key, scanner_key, peer_key, content_id


def test_provenance_chain_happy_path_verifies(tmp_path) -> None:
    chain, _pub, _scan, _peer, content_id = _build_full_chain(tmp_path)
    result = verify_chain(chain, expected_content_id=content_id)
    assert result.link_count == 4
    assert result.link_types == (
        LINK_GENESIS,
        LINK_WORK_ENVELOPE,
        LINK_SAFETY_SCAN,
        LINK_PEER_ATTESTATION,
    )
    assert result.head_hash == signed_link_hash(chain[-1])


def test_provenance_chain_rejects_tampered_link_payload(tmp_path) -> None:
    chain, *_ = _build_full_chain(tmp_path)
    bad_link = chain[2]
    tampered_payload = {**bad_link.payload, "outcome": "block"}
    chain[2] = SignedPayload(
        payload=tampered_payload,
        signature=bad_link.signature,
        public_key=bad_link.public_key,
    )
    with pytest.raises(ProvenanceError, match="provenance_link_signature_invalid"):
        verify_chain(chain)


def test_provenance_chain_rejects_dropped_link(tmp_path) -> None:
    chain, *_ = _build_full_chain(tmp_path)
    truncated = [chain[0], chain[1], chain[3]]
    with pytest.raises(ProvenanceError, match="provenance_link_(prev_hash_mismatch|sequence_mismatch)"):
        verify_chain(truncated)


def test_provenance_chain_rejects_reordered_links(tmp_path) -> None:
    chain, *_ = _build_full_chain(tmp_path)
    swapped = [chain[0], chain[2], chain[1], chain[3]]
    with pytest.raises(ProvenanceError, match="provenance_link_(prev_hash_mismatch|sequence_mismatch)"):
        verify_chain(swapped)


def test_provenance_chain_rejects_missing_genesis(tmp_path) -> None:
    chain, *_ = _build_full_chain(tmp_path)
    with pytest.raises(
        ProvenanceError,
        match="provenance_chain_root_not_genesis|provenance_link_sequence_mismatch",
    ):
        verify_chain(chain[1:])


def test_provenance_chain_rejects_content_id_drift(tmp_path) -> None:
    chain, pub_key, _scan, _peer, _content_id = _build_full_chain(tmp_path)
    pub_id = public_key_from_private(pub_key)
    bad_link = ProvenanceLink(
        sequence=len(chain),
        prev_link_hash=signed_link_hash(chain[-1]),
        link_type=LINK_WORK_ENVELOPE,
        content_id="sha256:" + "z" * 64,
        issuer_peer_id=pub_id,
        payload={"work_id": "work-x", "envelope_hash": "sha256:" + "d" * 64},
    )
    chain.append(sign_link(bad_link, private_key_bytes=pub_key))
    with pytest.raises(ProvenanceError, match="provenance_content_id_drift"):
        verify_chain(chain)


def test_provenance_chain_rejects_head_hash_mismatch(tmp_path) -> None:
    chain, *_ = _build_full_chain(tmp_path)
    with pytest.raises(ProvenanceError, match="provenance_head_hash_mismatch"):
        verify_chain(chain, expected_head_hash="sha256:" + "0" * 64)


def test_provenance_chain_rejects_issuer_key_mismatch(tmp_path) -> None:
    chain, _pub, scanner_key, _peer, content_id = _build_full_chain(tmp_path)
    impostor_key = _key()
    real_scanner_id = public_key_from_private(scanner_key)
    bad_link = ProvenanceLink(
        sequence=2,
        prev_link_hash=signed_link_hash(chain[1]),
        link_type=LINK_SAFETY_SCAN,
        content_id=content_id,
        issuer_peer_id=real_scanner_id,
        payload={"scanner_id": "kw-v1", "outcome": "pass", "policy_version": "v0"},
    )
    chain[2] = sign_link(bad_link, private_key_bytes=impostor_key)
    assert chain[2].public_key != real_scanner_id
    with pytest.raises(ProvenanceError, match="provenance_link_issuer_key_mismatch"):
        verify_chain(chain)


def test_provenance_chain_rejects_missing_required_field(tmp_path) -> None:
    pub_key = _key()
    pub_id = public_key_from_private(pub_key)
    content_id = "sha256:" + "a" * 64
    bad_genesis_link = ProvenanceLink(
        sequence=0,
        prev_link_hash="",
        link_type=LINK_GENESIS,
        content_id=content_id,
        issuer_peer_id=pub_id,
        payload={"content_hash": content_id, "model_id": "x"},
    )
    chain = [sign_link(bad_genesis_link, private_key_bytes=pub_key)]
    with pytest.raises(ProvenanceError, match="provenance_link_payload_missing_fields"):
        verify_chain(chain)


def test_chain_to_payload_round_trips_through_serialization(tmp_path) -> None:
    chain, *_, content_id = _build_full_chain(tmp_path)
    serialized = chain_to_payload(chain)
    revived = [SignedPayload.from_dict(item) for item in serialized]
    result = verify_chain(revived, expected_content_id=content_id)
    assert result.link_count == 4


def test_manifest_with_provenance_chain_validates(tmp_path) -> None:
    publisher_key = _key()
    scanner_key = _key()
    peer_key = _key()
    pub_id = public_key_from_private(publisher_key)
    scanner_id = public_key_from_private(scanner_key)
    peer_id = public_key_from_private(peer_key)

    media = tmp_path / "asset.mp4"
    media.write_bytes(b"chained payload")
    asset = build_media_asset(media, transcript_text="safe content")

    receipt = KeywordSafetyScanner().scan(clip_id=asset.clip_id, transcript="safe content")
    signed_safety = sign_safety_receipt(receipt, private_key_bytes=scanner_key)

    chain = [
        make_genesis_link(
            content_id=asset.clip_id,
            publisher_peer_id=pub_id,
            private_key_bytes=publisher_key,
            run_id="rnv_test",
            model_id="claude-opus-4-7",
        )
    ]
    append_link(
        chain,
        link_type=LINK_SAFETY_SCAN,
        issuer_peer_id=scanner_id,
        private_key_bytes=scanner_key,
        payload={"scanner_id": "kw", "outcome": receipt.outcome.value, "policy_version": "v0"},
    )
    append_link(
        chain,
        link_type=LINK_PEER_ATTESTATION,
        issuer_peer_id=peer_id,
        private_key_bytes=peer_key,
        payload={"peer_id": peer_id, "bytes_seen_at": "2026-05-13T00:00:00Z"},
    )

    signed_manifest = make_manifest(
        asset=asset,
        run_receipt=_receipt(),
        safety_receipts=[signed_safety],
        publisher=pub_id,
        private_key_bytes=publisher_key,
        provenance_chain=chain,
    )
    validation = validate_manifest(signed_manifest)
    assert validation.errors == [], validation.errors
    assert validation.has_chain
    assert validation.provenance_link_count == 3
    assert validation.provenance_head_hash == signed_link_hash(chain[-1])
    assert validation.propagates


def test_manifest_rejects_provenance_chain_with_wrong_publisher(tmp_path) -> None:
    publisher_key = _key()
    other_key = _key()
    scanner_key = _key()
    other_id = public_key_from_private(other_key)
    pub_id = public_key_from_private(publisher_key)

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"bytes")
    asset = build_media_asset(media, transcript_text="safe text")

    receipt = KeywordSafetyScanner().scan(clip_id=asset.clip_id, transcript="safe text")
    signed_safety = sign_safety_receipt(receipt, private_key_bytes=scanner_key)

    chain = [
        make_genesis_link(
            content_id=asset.clip_id,
            publisher_peer_id=other_id,
            private_key_bytes=other_key,
            run_id="rnv_test",
            model_id="claude-opus-4-7",
        )
    ]

    signed_manifest = make_manifest(
        asset=asset,
        run_receipt=_receipt(),
        safety_receipts=[signed_safety],
        publisher=pub_id,
        private_key_bytes=publisher_key,
        provenance_chain=chain,
    )
    validation = validate_manifest(signed_manifest)
    assert "provenance_genesis_publisher_mismatch" in validation.errors


def test_store_publish_content_attaches_provenance_chain(tmp_path) -> None:
    pytest.importorskip("cryptography")
    store = RynmeshStore(
        home=tmp_path / "node",
        network_dir=tmp_path / "mesh",
        node_name="prov-node",
    )
    media = tmp_path / "doc.txt"
    media.write_bytes(b"benign generated content")
    result = store.publish_content(
        media,
        title="prov test",
        transcript="benign",
        model_id="claude-opus-4-7",
        prompt_hash="sha256:" + "f" * 64,
    )
    signed_manifest = store.get_local_manifest(result["content_id"])
    validation = validate_manifest(signed_manifest)
    assert validation.errors == [], validation.errors
    assert validation.has_chain
    assert validation.provenance_link_count == 3
    assert validation.provenance_link_types[0] == LINK_GENESIS
    assert LINK_WORK_ENVELOPE in validation.provenance_link_types
    assert LINK_SAFETY_SCAN in validation.provenance_link_types


def _vouch_for(subject_id: str, *, issuer_key: bytes) -> SignedPayload:
    vouch = PeerVouch(
        subject_peer_id=subject_id,
        issuer_peer_id=public_key_from_private(issuer_key),
        reason="trusted",
    )
    return sign_peer_vouch(vouch, private_key_bytes=issuer_key)


def _stake_for(subject_key: bytes, amount: float = 1.0) -> SignedPayload:
    subject_id = public_key_from_private(subject_key)
    stake = StakeCommitment(
        subject_peer_id=subject_id,
        stake_kind="cash",
        stake_amount=amount,
        evidence_hash="sha256:" + "0" * 64,
    )
    return sign_stake_commitment(stake, private_key_bytes=subject_key)


def _proof_for(
    subject_id: str,
    *,
    issuer_key: bytes,
    metric: str,
    value: float,
) -> SignedPayload:
    proof = ProofOfResource(
        subject_peer_id=subject_id,
        issuer_peer_id=public_key_from_private(issuer_key),
        metric=metric,
        measured_value=value,
        window_start="2026-05-01T00:00:00Z",
        window_end="2026-05-13T00:00:00Z",
    )
    return sign_proof_of_resource(proof, private_key_bytes=issuer_key)


def test_identity_default_tier_is_unverified() -> None:
    subject_key = _key()
    subject_id = public_key_from_private(subject_key)
    assessment = assess_identity(peer_id=subject_id)
    assert assessment.tier is IdentityTier.UNVERIFIED
    assert assessment.vouch_count == 0
    assert assessment.stake_amount == 0.0


def test_identity_attested_requires_minimum_distinct_vouches() -> None:
    subject_key = _key()
    subject_id = public_key_from_private(subject_key)
    trusted_keys = [_key() for _ in range(3)]
    trusted_ids = [public_key_from_private(k) for k in trusted_keys]
    vouches = [_vouch_for(subject_id, issuer_key=k) for k in trusted_keys]

    # Only two vouches → still unverified.
    assessment_under = assess_identity(
        peer_id=subject_id,
        vouches=vouches[:2],
        trusted_vouch_issuers=trusted_ids,
    )
    assert assessment_under.tier is IdentityTier.UNVERIFIED

    # Three vouches → attested.
    assessment_full = assess_identity(
        peer_id=subject_id,
        vouches=vouches,
        trusted_vouch_issuers=trusted_ids,
    )
    assert assessment_full.tier is IdentityTier.ATTESTED
    assert assessment_full.vouch_count == 3


def test_identity_duplicate_vouches_from_same_issuer_do_not_count(tmp_path) -> None:
    subject_key = _key()
    subject_id = public_key_from_private(subject_key)
    issuer_key = _key()
    issuer_id = public_key_from_private(issuer_key)
    duplicates = [_vouch_for(subject_id, issuer_key=issuer_key) for _ in range(5)]
    assessment = assess_identity(
        peer_id=subject_id,
        vouches=duplicates,
        trusted_vouch_issuers=[issuer_id],
    )
    assert assessment.vouch_count == 1
    assert assessment.tier is IdentityTier.UNVERIFIED


def test_identity_rejects_self_issued_vouch() -> None:
    from rynmesh.identity import IdentityError

    subject_key = _key()
    subject_id = public_key_from_private(subject_key)
    self_vouch = PeerVouch(subject_peer_id=subject_id, issuer_peer_id=subject_id)
    signed = sign_peer_vouch(self_vouch, private_key_bytes=subject_key)
    with pytest.raises(IdentityError, match="vouch_self_issued"):
        verify_peer_vouch(signed)


def test_identity_stake_alone_does_not_promote_unverified_peer() -> None:
    subject_key = _key()
    subject_id = public_key_from_private(subject_key)
    assessment = assess_identity(
        peer_id=subject_id,
        stake_commitments=[_stake_for(subject_key, amount=100.0)],
    )
    assert assessment.stake_amount == 100.0
    assert assessment.tier is IdentityTier.UNVERIFIED
    assert "stake_present_but_unverified_peer" in assessment.reasons


def test_identity_proven_requires_resource_thresholds() -> None:
    subject_key = _key()
    subject_id = public_key_from_private(subject_key)
    trusted_keys = [_key() for _ in range(3)]
    trusted_ids = [public_key_from_private(k) for k in trusted_keys]
    vouches = [_vouch_for(subject_id, issuer_key=k) for k in trusted_keys]
    observer_key = _key()
    observer_id = public_key_from_private(observer_key)
    proofs = [
        _proof_for(subject_id, issuer_key=observer_key, metric="full_served", value=200.0),
        _proof_for(subject_id, issuer_key=observer_key, metric="uptime_hours", value=240.0),
    ]
    assessment = assess_identity(
        peer_id=subject_id,
        vouches=vouches,
        proofs=proofs,
        trusted_vouch_issuers=trusted_ids + [observer_id],
    )
    assert assessment.tier is IdentityTier.PROVEN
    assert assessment.proof_metrics["full_served"] == 200.0


def test_distribution_weight_capped_by_identity_tier() -> None:
    # Use a very large score so the uncapped weight saturates at the policy max
    high_score_uncapped = distribution_weight(10_000.0, CreditPolicy())
    unverified_cap = distribution_weight(10_000.0, CreditPolicy(), identity_tier=IdentityTier.UNVERIFIED)
    attested_cap = distribution_weight(10_000.0, CreditPolicy(), identity_tier=IdentityTier.ATTESTED)
    proven_cap = distribution_weight(10_000.0, CreditPolicy(), identity_tier=IdentityTier.PROVEN)

    assert high_score_uncapped > 1.0
    assert unverified_cap == tier_distribution_cap(IdentityTier.UNVERIFIED) == 1.0
    assert attested_cap == 2.0
    assert proven_cap == 5.0
    assert unverified_cap < attested_cap < proven_cap <= high_score_uncapped + 1e-6


def test_tier_gates_high_risk_event_kinds() -> None:
    assert tier_can_issue(IdentityTier.UNVERIFIED, "content_published") is True
    assert tier_can_issue(IdentityTier.UNVERIFIED, "registry_operated") is False
    assert tier_can_issue(IdentityTier.ATTESTED, "registry_operated") is True
    assert tier_can_issue(IdentityTier.ATTESTED, "illegal_content") is False
    assert tier_can_issue(IdentityTier.STAKED, "illegal_content") is True
    assert tier_can_issue(IdentityTier.PROVEN, "illegal_content") is True


def test_credit_policy_enforces_issuer_tier_when_required() -> None:
    from rynmesh.credits import (
        EVENT_WEIGHTS,
        CreditEvent,
        CreditLedgerError,
        CreditPolicy,
        sign_credit_event,
        verify_credit_event_with_policy,
    )

    issuer_key = _key()
    issuer_id = public_key_from_private(issuer_key)
    subject_key = _key()
    subject_id = public_key_from_private(subject_key)
    event = CreditEvent(
        subject_peer_id=subject_id,
        issuer_peer_id=issuer_id,
        kind="registry_operated",
        amount=EVENT_WEIGHTS["registry_operated"],
    )
    signed = sign_credit_event(event, private_key_bytes=issuer_key)
    policy_enforced = CreditPolicy(enforce_issuer_tier=True)
    with pytest.raises(CreditLedgerError, match="credit_issuer_tier_too_low"):
        verify_credit_event_with_policy(
            signed,
            policy=policy_enforced,
            issuer_tier=IdentityTier.UNVERIFIED,
        )
    # ATTESTED issuer is allowed
    verify_credit_event_with_policy(
        signed,
        policy=policy_enforced,
        issuer_tier=IdentityTier.ATTESTED,
    )
    # When not enforced, tier is ignored
    policy_off = CreditPolicy(enforce_issuer_tier=False)
    verify_credit_event_with_policy(
        signed,
        policy=policy_off,
        issuer_tier=IdentityTier.UNVERIFIED,
    )


def test_identity_filters_expired_vouches() -> None:
    subject_key = _key()
    subject_id = public_key_from_private(subject_key)
    trusted_keys = [_key() for _ in range(3)]
    trusted_ids = [public_key_from_private(k) for k in trusted_keys]
    old_time = "2024-01-01T00:00:00Z"
    fresh_time = "2026-05-13T00:00:00Z"
    expired_vouches = [
        sign_peer_vouch(
            PeerVouch(subject_peer_id=subject_id, issuer_peer_id=public_key_from_private(k), issued_at=old_time),
            private_key_bytes=k,
        )
        for k in trusted_keys
    ]
    policy = IdentityPolicy(vouch_validity_days=30.0)
    now = datetime.fromisoformat(fresh_time.replace("Z", "+00:00"))
    assessment = assess_identity(
        peer_id=subject_id,
        vouches=expired_vouches,
        trusted_vouch_issuers=trusted_ids,
        policy=policy,
        now=now,
    )
    assert assessment.vouch_count == 0
    assert assessment.tier is IdentityTier.UNVERIFIED


def test_manifest_rejects_safety_receipts_when_chain_has_no_scan_link(tmp_path) -> None:
    publisher_key = _key()
    scanner_key = _key()
    pub_id = public_key_from_private(publisher_key)

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"bytes")
    asset = build_media_asset(media, transcript_text="safe text")

    receipt = KeywordSafetyScanner().scan(clip_id=asset.clip_id, transcript="safe text")
    signed_safety = sign_safety_receipt(receipt, private_key_bytes=scanner_key)

    # Genesis-only chain: no safety_scan link at all.
    chain = [
        make_genesis_link(
            content_id=asset.clip_id,
            publisher_peer_id=pub_id,
            private_key_bytes=publisher_key,
            run_id="rnv_test",
            model_id="claude-opus-4-7",
        )
    ]

    signed_manifest = make_manifest(
        asset=asset,
        run_receipt=_receipt(),
        safety_receipts=[signed_safety],
        publisher=pub_id,
        private_key_bytes=publisher_key,
        provenance_chain=chain,
    )
    validation = validate_manifest(signed_manifest)
    assert "provenance_chain_missing_safety_scan_link" in validation.errors


def test_manifest_rejects_safety_receipt_not_in_chain(tmp_path) -> None:
    publisher_key = _key()
    chain_scanner_key = _key()
    rogue_scanner_key = _key()
    pub_id = public_key_from_private(publisher_key)
    chain_scanner_id = public_key_from_private(chain_scanner_key)

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"bytes")
    asset = build_media_asset(media, transcript_text="safe text")

    rogue_receipt = KeywordSafetyScanner().scan(clip_id=asset.clip_id, transcript="safe text")
    signed_rogue = sign_safety_receipt(rogue_receipt, private_key_bytes=rogue_scanner_key)

    chain = [
        make_genesis_link(
            content_id=asset.clip_id,
            publisher_peer_id=pub_id,
            private_key_bytes=publisher_key,
            run_id="rnv_test",
            model_id="claude-opus-4-7",
        )
    ]
    append_link(
        chain,
        link_type=LINK_SAFETY_SCAN,
        issuer_peer_id=chain_scanner_id,
        private_key_bytes=chain_scanner_key,
        payload={"scanner_id": "kw", "outcome": "pass", "policy_version": "v0"},
    )

    signed_manifest = make_manifest(
        asset=asset,
        run_receipt=_receipt(),
        safety_receipts=[signed_rogue],
        publisher=pub_id,
        private_key_bytes=publisher_key,
        provenance_chain=chain,
    )
    validation = validate_manifest(signed_manifest)
    assert "safety_receipt_not_in_provenance_chain" in validation.errors
