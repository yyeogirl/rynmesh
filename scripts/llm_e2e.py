"""One-command Docker two-node LLM E2E launcher and verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rynmesh.transport import network_key_header

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "llm-e2e" / "docker-compose.yml"
REGISTRY = "http://127.0.0.1:18890"
PROVIDER = "http://127.0.0.1:18891"
CONSUMER = "http://127.0.0.1:18892"
TOKEN_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer rynmesh-e2e-browser-token",
}
TOKEN_HEADERS.update(network_key_header())


def _compose(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], cwd=ROOT,
                   env=env, check=True)


def _json(url: str, body: dict[str, Any] | None = None, timeout: float = 20,
          *, method: str | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers=TOKEN_HEADERS, method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _wait(url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _json(url, timeout=2)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"timed out waiting for {url}")


def up(mode: str) -> None:
    env = os.environ.copy()
    profile = ("real" if mode == "real"
               else "test" if mode in {"test", "relay-test", "mailbox"} else "")
    if mode == "real":
        if not env.get("RYNMESH_REAL_MODEL_PATH"):
            raise RuntimeError("set RYNMESH_REAL_MODEL_PATH to a readable GGUF file")
        env["RYNMESH_LLM_MANIFEST"] = "/config/real-manifest.json"
    elif mode == "host-real":
        env["RYNMESH_LLM_MANIFEST"] = "/config/host-real-manifest.json"
    if mode == "relay-test":
        env["RYNMESH_LLM_FORCE_RELAY"] = "1"
    if mode == "mailbox":
        # Direct peer delivery is switched off on the consumer only, so the send
        # has to take the registry mailbox. The provider stays normal, proving the
        # store-and-forward path end to end rather than a symmetric outage.
        env["RYNMESH_MESSAGING_FORCE_MAILBOX"] = "1"
    args = ["up", "-d", "--build"]
    if profile:
        args = ["--profile", profile, *args]
    _compose(*args, env=env)
    _wait(REGISTRY + "/health")
    _wait(PROVIDER + "/health")
    _wait(CONSUMER + "/health")


def _authorize_consumer(provider_id: str, service_id: str) -> dict[str, Any]:
    """Exercise owner pairing/grant APIs; registry visibility grants no AI access."""
    relationships = _json(CONSUMER + "/api/local/friends")["friends"]
    relationship = next((row for row in relationships
                         if row["peer_id"] == provider_id and row["status"] == "active"), None)
    if relationship is None:
        invitation = _json(PROVIDER + "/api/local/friends/invites", {})
        relationship = _json(CONSUMER + "/api/local/friends/join", {
            "invite_uri": invitation["invite_uri"],
        })
    relationship_id = relationship["relationship_id"]
    grants = _json(PROVIDER + "/api/local/ai-access")["grants"]
    prior = next((row for row in grants if row["service_id"] == service_id
                  and row["relationship_id"] == relationship_id), {})
    _json(PROVIDER + "/api/local/ai-access/" + quote(service_id, safe="") + "/" + relationship_id,
          {"allowed": True, "expected_revision": prior.get("revision", 0)}, method="PUT")
    catalog = _json(CONSUMER + "/api/local/ai-access/friend-services?peer_id="
                    + quote(provider_id, safe=""), {})
    if catalog.get("status") != "authorized":
        raise RuntimeError("E2E consumer did not receive explicit AI authorization")
    selected = next((row for row in catalog.get("services", [])
                     if row.get("service", {}).get("package_id") == service_id), None)
    if selected is None or not selected.get("online"):
        raise RuntimeError("E2E authorized service is unavailable")
    return selected["ai_permission"]


def verify(mode: str) -> dict[str, Any]:
    service_id = {"test": "e2e-test-service", "relay-test": "e2e-test-service", "real": "e2e-real-service",
                  "host-real": "e2e-host-real-service"}[mode]
    published = _json("http://127.0.0.1:18891/api/local/llm/services/publish", {
        "network_id": "rynmesh-llm-e2e", "benchmark": True,
    }, timeout=180)
    provider_id = str(published["record"]["peer_id"])
    permission = _authorize_consumer(provider_id, service_id)
    deadline = time.monotonic() + 30
    discovered: dict[str, Any] = {}
    while time.monotonic() < deadline:
        discovered = _json("http://127.0.0.1:18892/api/local/llm/services?network_id=rynmesh-llm-e2e")
        if any(item.get("peer_id") == provider_id for item in discovered.get("services", [])):
            break
        time.sleep(1)
    expected_transport = "encrypted_relay" if mode == "relay-test" else (
        "ice_udp_direct" if mode == "test" else ""
    )
    requested_transport = "relay" if mode == "relay-test" else (
        "p2p" if mode == "test" else "auto"
    )
    result = _json("http://127.0.0.1:18892/api/local/llm/orders", {
        "network_id": "rynmesh-llm-e2e", "provider_peer_id": provider_id,
        "service_id": service_id,
        "ai_permission": permission,
        "prompt": "Reply with a short confirmation that the encrypted two-node path works.",
        "max_tokens": 32, "transport": requested_transport,
    }, timeout=240)
    consumer_balance = _json("http://127.0.0.1:18892/api/local/task-balance")
    provider_balance: dict[str, Any] = {}
    earning_id = "earning:" + str(result.get("task_id") or "")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        provider_balance = _json("http://127.0.0.1:18891/api/local/task-balance")
        if any(event.get("event_id") == earning_id for event in provider_balance.get("events", [])):
            break
        time.sleep(0.5)
    orders = _json("http://127.0.0.1:18892/api/local/llm/orders")
    provider_orders = _json("http://127.0.0.1:18891/api/local/llm/provider-orders")
    output = str(result.get("output") or "")
    consumer_record = next((item for item in orders.get("orders", [])
                            if item.get("task_id") == result.get("task_id")), {})
    provider_record = next((item for item in provider_orders.get("orders", [])
                            if item.get("task_id") == result.get("task_id")), {})
    report = {
        "profile": "deterministic-test" if mode == "test" else mode, "provider_peer_id": provider_id,
        "discovered_service_count": len(discovered.get("services", [])), "task_id": result.get("task_id"),
        "state": result.get("state"), "model_alias": result.get("model_alias"),
        "input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens"),
        "duration_ms": result.get("duration_ms"), "amount": result.get("amount"),
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(), "output_preview": output[:160],
        "consumer_task_balance": {k: consumer_balance.get(k) for k in ("available", "held", "earned")},
        "provider_task_balance": {k: provider_balance.get(k) for k in ("available", "held", "earned")},
        "provider_earning_recorded_once": sum(
            event.get("event_id") == earning_id for event in provider_balance.get("events", [])
        ) == 1,
        "order_states": consumer_record.get("history", []),
        "provider_order_states": provider_record.get("history", []),
    }
    if result.get("state") != "succeeded" or not output:
        raise RuntimeError(f"E2E task failed: {report}")
    if expected_transport and result.get("transport") != expected_transport:
        raise RuntimeError(
            f"E2E transport mismatch: expected {expected_transport}, got {result.get('transport')}"
        )
    if expected_transport and bool(dict(result.get("transport_evidence") or {}).get("relay_used")) \
            != (expected_transport == "encrypted_relay"):
        raise RuntimeError("E2E relay evidence does not match the requested transport")
    results_dir = ROOT / "deploy" / "llm-e2e" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / (mode + "-result.json")).write_text(
        json.dumps(report, indent=2), encoding="utf-8",
    )
    print(json.dumps(report, indent=2))
    return report


# The provider's `mailbox.poll` worker starts at a 3 s delay and drops to a 2 s
# busy delay, but an idle worker backs off to a 60 s cap — and the provider is
# not recreated between E2E steps, so by the time this runs it has usually been
# idle for the whole preceding LLM flow. 60 s is therefore exactly the worst
# case with no margin; this budget is that worst case plus room for the poll,
# the dispatch and the ack.
MAILBOX_DELIVERY_TIMEOUT_S = 90


def mailbox_verify() -> dict[str, Any]:
    """Peer message from consumer to provider with direct delivery switched off.

    Only the marker text ever leaves this function; message bodies (and the
    history entries that carry them) are never printed.
    """

    provider_id = str(_json(PROVIDER + "/health")["peer_id"])
    consumer_id = str(_json(CONSUMER + "/health")["peer_id"])
    marker = f"mailbox-e2e-{int(time.time())}"
    sent = _json(CONSUMER + "/api/local/messages/send",
                 {"peer_id": provider_id, "text": marker})
    if sent.get("via") != "mailbox" or sent.get("delivered") is not False:
        raise RuntimeError(
            "E2E mailbox send did not queue: "
            f"via={sent.get('via')!r} delivered={sent.get('delivered')!r}"
        )
    # peer ids are base64 and contain '/', '+' and '=' — the node takes them as a
    # query parameter, so they have to be fully escaped.
    history_url = (PROVIDER + "/api/local/messages?peer_id="
                   + quote(consumer_id, safe=""))
    deadline = time.monotonic() + MAILBOX_DELIVERY_TIMEOUT_S
    delivered_at = 0.0
    started = time.monotonic()
    while time.monotonic() < deadline:
        history = _json(history_url)
        if any(item.get("text") == marker and item.get("dir") == "in"
               for item in history):
            delivered_at = time.monotonic() - started
            break
        time.sleep(2)
    consumer_status = _json(CONSUMER + "/api/local/mailbox/status")
    provider_status = _json(PROVIDER + "/api/local/mailbox/status")
    report = {
        "profile": "mailbox-store-and-forward",
        "marker": marker,
        "consumer_peer_id": consumer_id,
        "provider_peer_id": provider_id,
        "send_via": sent.get("via"),
        "send_delivered": sent.get("delivered"),
        "delivered_after_s": round(delivered_at, 1) if delivered_at else None,
        "consumer_mailbox": {k: consumer_status.get(k)
                             for k in ("handled_total", "dropped_total",
                                       "undecryptable", "last_error")},
        "provider_mailbox": {k: provider_status.get(k)
                             for k in ("handled_total", "dropped_total",
                                       "undecryptable", "last_error")},
    }
    if not delivered_at:
        raise RuntimeError(f"E2E mailbox message never arrived: {report}")
    # The consumer only deposits, so it should have handled nothing and, more to
    # the point, hit no errors and dropped nothing while its own box stayed empty.
    if consumer_status.get("last_error") != "":
        raise RuntimeError(f"E2E consumer mailbox reported an error: {report}")
    if int(consumer_status.get("dropped_total") or 0) != 0:
        raise RuntimeError(f"E2E consumer mailbox dropped mail: {report}")
    if int(provider_status.get("handled_total") or 0) < 1:
        raise RuntimeError(f"E2E provider mailbox handled nothing: {report}")
    # An unopenable envelope is neither handled nor dropped; it would otherwise
    # sit in the box until its TTL with nothing else in this report noticing.
    if int(provider_status.get("undecryptable") or 0) != 0:
        raise RuntimeError(f"E2E provider mailbox could not open mail: {report}")
    results_dir = ROOT / "deploy" / "llm-e2e" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "mailbox-result.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8",
    )
    print(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=[
        "up", "verify", "run", "real-up", "real-verify", "real-run",
        "host-real-up", "host-real-verify", "host-real-run", "down", "clean",
        "relay-up", "relay-verify", "relay-run",
        "mailbox-up", "mailbox-verify", "mailbox-run",
    ])
    args = parser.parse_args()
    mode = ("host-real" if args.command.startswith("host-real-") else "real"
            if args.command.startswith("real-") else "relay-test"
            if args.command.startswith("relay-") else "mailbox"
            if args.command.startswith("mailbox-") else "test")
    if args.command in {"up", "real-up", "host-real-up", "relay-up", "mailbox-up"}:
        up(mode)
    elif args.command in {"verify", "real-verify", "host-real-verify", "relay-verify"}:
        verify(mode)
    elif args.command == "mailbox-verify":
        mailbox_verify()
    elif args.command == "mailbox-run":
        up(mode)
        mailbox_verify()
    elif args.command in {"run", "real-run", "host-real-run", "relay-run"}:
        up(mode)
        verify(mode)
    elif args.command == "down":
        _compose("--profile", "test", "--profile", "real", "down")
    elif args.command == "clean":
        # Removes only named E2E containers, networks, and Docker volumes. It
        # never touches host GGUF files mounted read-only into the real profile.
        _compose("--profile", "test", "--profile", "real", "down", "--volumes", "--remove-orphans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
