from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rynmesh.crypto import public_key_from_private, sign_payload
from scripts.audit_public_p2p import AuditError, audit_records

ROOT = Path(__file__).resolve().parents[1]


def _private_key() -> bytes:
    return Ed25519PrivateKey.generate().private_bytes_raw()


def _signed_records(
    *,
    provider_public_host: str = "203.0.113.8",
    use_host_pair: bool = False,
    nominated_remote_port: int | None = None,
    relay_offer: bool = False,
):
    requester_key = _private_key()
    provider_key = _private_key()
    requester = public_key_from_private(requester_key)
    provider = public_key_from_private(provider_key)
    order_id = "wo_public_audit"
    task_id = "task_public_audit"
    order = sign_payload({
        "kind": "work_order",
        "work_order_id": order_id,
        "requester_peer_id": requester,
        "provider_peer_id": provider,
        "capability": "rynmesh.llm.private.v1",
        "operation": "rynmesh.llm.private.infer.v1.p2p_offer",
        "network_id": "rynmesh-llm-e2e",
        "idempotency_key": "p2p:" + task_id,
        "params": {
            "session_id": task_id,
            "timeout_seconds": 60,
            "ice_signal": {
                "username": "consumer",
                "password": "consumer-password",
                "candidates": [
                    "consumer-relay 1 udp 16777215 203.0.113.99 55000 typ relay"
                    if relay_offer else
                    "consumer-host 1 udp 2130706431 10.0.0.2 50000 typ host",
                    "consumer 1 udp 1694498815 198.51.100.7 50001 typ srflx "
                    "raddr 10.0.0.2 rport 50001",
                ],
            },
        },
    }, private_key_bytes=requester_key).to_dict()
    accepted = sign_payload({
        "kind": "work_result",
        "work_order_id": order_id,
        "requester_peer_id": requester,
        "provider_peer_id": provider,
        "network_id": "rynmesh-llm-e2e",
        "status": "accepted",
        "result_refs": {
            "relay_allowed": False,
            "ice_signal": {
                "username": "provider",
                "password": "provider-password",
                "candidates": [
                    "provider-host 1 udp 2130706431 192.168.1.2 50000 typ host",
                    f"provider 1 udp 1694498815 {provider_public_host} 50002 typ srflx "
                    "raddr 192.168.1.2 rport 50002",
                ],
            },
        },
    }, private_key_bytes=provider_key).to_dict()
    completed = sign_payload({
        "kind": "work_result",
        "work_order_id": order_id,
        "requester_peer_id": requester,
        "provider_peer_id": provider,
        "network_id": "rynmesh-llm-e2e",
        "status": "completed",
        "result_refs": {
            "transport_evidence": {
                "transport": "ice_udp_direct",
                "relay_used": False,
                "public_nat_traversal_required": False,
                "distinct_public_egress_required": False,
                "peer_public_mapping_nominated": not use_host_pair,
                "request_bytes": 512,
                "response_bytes": 384,
                "local": {
                    "type": "host" if use_host_pair else "srflx",
                    "transport": "udp",
                    "host": "192.168.1.2" if use_host_pair else provider_public_host,
                    "port": 50000 if use_host_pair else 50002,
                },
                "remote": {
                    "type": "host" if use_host_pair else "srflx",
                    "transport": "udp",
                    "host": "10.0.0.2" if use_host_pair else "198.51.100.7",
                    "port": nominated_remote_port or (50000 if use_host_pair else 50001),
                },
            },
        },
    }, private_key_bytes=provider_key).to_dict()
    return order, accepted, completed, task_id


def test_public_p2p_audit_accepts_signed_distinct_egress_evidence():
    order, accepted, completed, task_id = _signed_records()

    report = audit_records(
        work_orders=[order],
        work_results=[accepted, completed],
        task_id=task_id,
    )

    assert report["ok"] is True
    assert report["transport"] == "ice_udp_direct"
    assert report["relay_used"] is False
    assert report["consumer_public_mappings"] == ["198.51.100.7"]
    assert report["provider_public_mappings"] == ["203.0.113.8"]


def test_public_p2p_audit_accepts_shared_public_exit():
    order, accepted, completed, task_id = _signed_records(
        provider_public_host="198.51.100.7",
    )

    report = audit_records(
        work_orders=[order],
        work_results=[accepted, completed],
        task_id=task_id,
    )

    assert report["ok"] is True
    assert report["shared_public_egress"] is True


def test_public_p2p_audit_accepts_signed_private_direct_pair():
    order, accepted, completed, task_id = _signed_records(
        provider_public_host="198.51.100.7", use_host_pair=True,
    )

    report = audit_records(
        work_orders=[order],
        work_results=[accepted, completed],
        task_id=task_id,
    )

    assert report["ok"] is True
    assert report["nominated_remote"]["type"] == "host"
    assert report["relay_used"] is False


def test_public_p2p_audit_rejects_candidate_not_in_signed_signal():
    order, accepted, completed, task_id = _signed_records(nominated_remote_port=59999)

    with pytest.raises(AuditError, match="not present in signed signaling"):
        audit_records(
            work_orders=[order], work_results=[accepted, completed], task_id=task_id,
        )


def test_public_p2p_audit_rejects_relay_candidate_in_signaling():
    order, accepted, completed, task_id = _signed_records(relay_offer=True)

    with pytest.raises(AuditError, match="non-direct UDP candidate"):
        audit_records(
            work_orders=[order], work_results=[accepted, completed], task_id=task_id,
        )


def test_physical_acceptance_entries_allow_one_public_gateway_without_relay():
    consumer = (ROOT / "deploy" / "llm-e2e" / "windows-consumer" / "entry.py").read_text(
        encoding="utf-8",
    )
    provider = (ROOT / "deploy" / "llm-e2e" / "p2p_provider_entry.py").read_text(
        encoding="utf-8",
    )

    for source in (consumer, provider):
        assert '"RYNMESH_P2P_REQUIRE_PUBLIC"] = "0"' in source \
            or '"RYNMESH_P2P_REQUIRE_PUBLIC": "0"' in source
        assert '"RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC"] = "0"' in source \
            or '"RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC": "0"' in source
    assert 'os.environ["RYNMESH_LLM_FORCE_RELAY"] = "0"' in consumer
    assert '"RYNMESH_LLM_RELAY_URL": ""' in provider
