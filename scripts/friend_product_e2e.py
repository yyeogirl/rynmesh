"""Exercise first sharing through real node/registry processes and owner HTTP APIs.

Fresh homes only. No relationship, message, card, cache or receipt is seeded.
The article source is deterministic local HTTP; this is not desktop or NAT proof.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PERMISSIONS = {"friend.message", "friend.attachment.small", "friend.content-card"}
ARTICLE = "A small shared reading for the first friend. This complete paragraph belongs to the acceptance fixture."


class HttpFailure(RuntimeError):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        super().__init__(f"HTTP {status}: {detail}")


class Node:
    def __init__(self, home, registry=None):
        self.home = home
        home.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.token = uuid.uuid4().hex
        self.registry = registry
        self.process = None
        self.log = None
        self.peer_id = None

    def start(self):
        assert self.process is None or self.process.poll() is not None
        # Do not inherit an operator's keys, model paths, endpoints or home.
        env = {k: v for k, v in os.environ.items() if not k.startswith("RYNMESH_")}
        env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", RYNMESH_HOME=str(self.home),
            RYNMESH_AUTO_REGISTER="0", RYNMESH_DISABLE_DISCOVERY="1", RYNMESH_MODEL_PROVIDER="none",
            RYNMESH_DEFAULT_DISCOVERY="0", RYNMESH_REGISTRY_DIR=str(self.home / "registry"),
            RYNMESH_REGISTRY_HOST="127.0.0.1", RYNMESH_REGISTRY_PORT=str(self.port),
            RYNMESH_PEER_HOST="127.0.0.1", RYNMESH_PEER_PORT=str(self.port),
            RYNMESH_PEER_ENDPOINT=self.url, RYNMESH_FRIEND_ENDPOINT=self.url,
            RYNMESH_FRIEND_ALLOW_LOOPBACK="1", RYNMESH_LOCAL_TOKEN=self.token,
            RYNMESH_LLM_HOME=str(self.home / "llm"), RYNMESH_NETWORK_ID="friend-product-e2e",
            RYNMESH_REGISTRY_URL=self.registry.url if self.registry else "")
        module = "rynmesh.peer_http" if self.registry else "rynmesh.registry_http"
        self.log = (self.home / "process.log").open("ab")
        self.process = subprocess.Popen([sys.executable, "-m", module], cwd=ROOT, env=env,
            stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.home.name} exited before readiness; inspect its local process.log")
            try:
                health = self.request("/health", timeout=1)
                if self.registry:
                    assert health.get("peer_id")
                    if self.peer_id is not None:
                        assert health["peer_id"] == self.peer_id
                    self.peer_id = health["peer_id"]
                return
            except (OSError, HttpFailure):
                time.sleep(0.2)
        raise RuntimeError(f"{self.home.name} did not become ready")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log:
            self.log.close()

    def request(self, path, body=None, method=None, *, raw=False, timeout=20):
        request = urllib.request.Request(self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-Ryn-Local-Token": self.token},
            method=method or ("POST" if body is not None else "GET"))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read() if raw else json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.load(error).get("detail", "request_failed")
            except ValueError:
                detail = "request_failed"
            raise HttpFailure(error.code, detail) from None

    def friends(self):
        return self.request("/api/local/friends")["friends"]

    def history(self, other):
        return self.request(f"/api/local/friends/{quote(other.peer_id, safe='')}/messages")["messages"]


def denied(node, path, body, expected, method=None):
    try:
        node.request(path, body, method)
    except HttpFailure as failure:
        assert failure.status == 409 and failure.detail in ({expected} if isinstance(expected, str) else expected)
        return failure.detail
    raise AssertionError(f"Expected {expected}")


def eventually(check, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.5)
    raise AssertionError("Expected background delivery did not finish within its deadline")


def protected_fetch_status(sender, receiver, relationship_id, secret, card_id):
    """Probe the actual peer boundary with credentials earned by normal pairing.

    Reading this isolated fixture's existing secret does not create a relation
    or bypass authentication. Neither the secret nor response body is logged.
    """
    from rynmesh.crypto import canonical_json
    from rynmesh.friends.crypto import auth_headers
    path = "/api/peer/friends/content-card/fetch"
    body = canonical_json({"v": 1, "relationship_id": relationship_id,
        "from": sender.peer_id, "to": receiver.peer_id, "card_id": card_id})
    headers = auth_headers(secret, method="POST", path=path, body=body,
        sender=sender.peer_id, receiver=receiver.peer_id, relationship_id=relationship_id,
        timestamp=int(time.time()), nonce=uuid.uuid4().hex)
    request = urllib.request.Request(receiver.url + path, data=body,
        headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        error.close()
        return error.code


def run(work, report):
    registry = Node(work / "registry")
    alice = Node(work / "alice", registry)
    candidates = [Node(work / name, registry) for name in ("bob", "carol")]
    nodes = [registry, alice, *candidates]
    source = None

    def passed(name, **details):
        report["checks"].append({"name": name, "result": "passed", **details})
        print(name + ": passed", flush=True)

    try:
        for node in nodes:
            node.start()
        assert alice.friends() == candidates[0].friends() == candidates[1].friends() == []
        expired = alice.request("/api/local/friends/invites", {"ttl_minutes": 1})
        cancelled = alice.request("/api/local/friends/invites", {})
        alice.request("/api/local/friends/invites/" + cancelled["invite"]["invite_id"], method="DELETE")
        denied(candidates[0], "/api/local/friends/join", {"invite_uri": cancelled["invite_uri"]}, "invite_cancelled")
        invitation = alice.request("/api/local/friends/invites", {})
        start = time.monotonic()
        alice.stop()
        preview = candidates[0].request("/api/local/friends/invites/inspect", {"invite_uri": invitation["invite_uri"]})
        assert preview["peer_id"] == alice.peer_id and set(preview["permissions"]) == PERMISSIONS
        assert candidates[0].friends() == []
        passed("cancelled_invite_and_offline_local_preview")
        alice.start()

        def accept(node):
            try:
                return node.request("/api/local/friends/join", {"invite_uri": invitation["invite_uri"]})
            except HttpFailure as failure:
                return failure.detail
        with ThreadPoolExecutor(max_workers=2) as pool:
            joined = list(pool.map(accept, candidates))
        assert sum(isinstance(value, dict) for value in joined) == 1
        index = next(i for i, value in enumerate(joined) if isinstance(value, dict))
        bob, loser = candidates[index], candidates[1 - index]
        relation = joined[index]
        assert joined[1 - index] == "invite_used"
        assert bob.request("/api/local/friends/join", {"invite_uri": invitation["invite_uri"]}) == relation
        assert len(alice.friends()) == len(bob.friends()) == 1 and loser.friends() == []
        assert all(set(row["permissions"]) == PERMISSIONS for node in (alice, bob) for row in node.friends())
        passed("concurrent_accept_one_relationship_and_retry", invite_to_accept_seconds=round(time.monotonic() - start, 3))

        accepted = time.monotonic()
        for sender, receiver in ((alice, bob), (bob, alice)):
            message = {"message_id": uuid.uuid4().hex, "text": "First sharing: 你好，朋友。"}
            path = f"/api/local/friends/{quote(receiver.peer_id, safe='')}/messages"
            sent = sender.request(path, message)
            assert sent["delivery_state"] == "delivered" and sent["delivered"]
            assert sender.request(path, message)["msg_id"] == message["message_id"]
            rows = [r for r in receiver.history(sender) if r["msg_id"] == message["message_id"]]
            assert len(rows) == 1 and rows[0]["text"] == message["text"]
            content = b"Small attachment\n" + bytes(range(256))
            attachment = {"message_id": uuid.uuid4().hex, "text": "", "attachment": {
                "filename": "first.bin", "mime": "application/octet-stream",
                "data_base64": base64.b64encode(content).decode()}}
            assert sender.request(path, attachment)["delivery_state"] == "delivered"
            downloaded = receiver.request(f"/api/local/friends/{quote(sender.peer_id, safe='')}/attachments/{attachment['message_id']}", raw=True)
            assert downloaded == content
        passed("bidirectional_text_attachment_and_dedup", accept_to_confirm_seconds=round(time.monotonic() - accepted, 3))

        class ArticleHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/feed":
                    data = f'<rss version="2.0"><channel><title>Acceptance reading</title><link>{source_url}</link><item><title>First article</title><link>{source_url}/article</link><guid>first-article</guid><description>A complete local article.</description></item></channel></rss>'.encode()
                elif self.path == "/article":
                    data = f"<html><head><title>First article</title></head><body><article><p>{ARTICLE}</p></article></body></html>".encode()
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        source = ThreadingHTTPServer(("127.0.0.1", 0), ArticleHandler)
        source_url = f"http://127.0.0.1:{source.server_port}"
        threading.Thread(target=source.serve_forever, daemon=True).start()
        alice.request("/api/local/sources", {"url": source_url + "/feed"})
        alice.request("/api/local/digest/refresh", {})
        items = alice.request("/api/local/digest")["items"]
        assert len(items) == 1
        item = items[0]
        body = alice.request("/api/local/reader?url=" + quote(item["link"], safe=""))
        expected_body = "\n\n".join(block["text"] for block in body["blocks"])
        assert ARTICLE in expected_body
        alice.request("/api/local/consumption", {"item": item, "action": "opened"})
        alice.request("/api/local/consumption", {"item": item, "action": "bookmark"})
        card = {"peer_id": bob.peer_id, "item_id": item["item_id"], "card_id": uuid.uuid4().hex, "prefer_source": True}
        assert alice.request("/api/local/friends/share", card)["delivery_state"] == "delivered"
        assert bob.request("/api/local/friends/documents")["documents"] == []
        received = bob.request("/api/local/friends/cards")["cards"]
        assert len(received) == 1 and received[0]["fetch_state"] == "available"
        fetched = bob.request(f"/api/local/friends/cards/{card['card_id']}/fetch", {})
        assert fetched["sha256_verified"]
        document = "/api/local/friends/documents/" + fetched["library_id"].removeprefix("import:") + "/body"
        assert bob.request(document)["text"] == expected_body
        assert alice.request("/api/local/friends/share", card)["card_id"] == card["card_id"]
        assert len(bob.request("/api/local/friends/cards")["cards"]) == 1
        returned = bob.request("/api/local/friends/share", {"peer_id": alice.peer_id,
            "item_id": fetched["library_id"], "card_id": uuid.uuid4().hex, "prefer_source": True})
        returned_fetch = alice.request(f"/api/local/friends/cards/{returned['card_id']}/fetch", {})
        returned_body = alice.request("/api/local/friends/documents/" + returned_fetch["library_id"].removeprefix("import:") + "/body")
        assert returned_body["text"] == expected_body
        passed("read_save_share_explicit_fetch_and_share_back", body_sha256=hashlib.sha256(expected_body.encode()).hexdigest())

        bob.stop()
        pending = {"message_id": uuid.uuid4().hex, "text": "Read after restart"}
        path = f"/api/local/friends/{quote(bob.peer_id, safe='')}/messages"
        queued = alice.request(path, pending)
        assert queued["delivery_state"] == "mailbox" and not queued["delivered"]
        old_pid = alice.process.pid
        alice.stop()
        alice.start()
        assert alice.process.pid != old_pid
        assert next(r for r in alice.history(bob) if r["msg_id"] == pending["message_id"])["delivery_state"] == "mailbox"
        # Keep the sender offline while B consumes the registry mailbox, proving
        # recovery did not silently fall back to direct sender retry.
        alice.stop()
        bob.start()
        eventually(lambda: any(r["msg_id"] == pending["message_id"] for r in bob.history(alice)))
        alice.start()
        eventually(lambda: any(r["msg_id"] == pending["message_id"] and r["delivery_state"] == "delivered" for r in alice.history(bob)))
        alice.request(path, pending)
        assert sum(r["msg_id"] == pending["message_id"] for r in bob.history(alice)) == 1
        assert bob.request(document)["text"] == expected_body
        passed("real_http_mailbox_receiver_confirmation_and_process_restart")

        unfetched = {**card, "card_id": uuid.uuid4().hex}
        assert alice.request("/api/local/friends/share", unfetched)["delivery_state"] == "delivered"
        from rynmesh.friends.store import FriendStore
        # Capture an existing peer credential solely for an explicit access
        # boundary probe. Positive control uses the already fetched card.
        old_secret = FriendStore(bob.home).secret(relation["relationship_id"])
        assert old_secret
        assert protected_fetch_status(bob, alice, relation["relationship_id"], old_secret, card["card_id"]) == 200
        bob.stop()
        removed = alice.request("/api/local/friends/" + relation["relationship_id"], method="DELETE")
        assert removed["status"] == "revoked" and removed["revocation_delivery"] == "pending"
        assert protected_fetch_status(bob, alice, relation["relationship_id"], old_secret, unfetched["card_id"]) == 403
        denied(alice, path, {"message_id": uuid.uuid4().hex, "text": "Must be denied"}, "active_friend_required")
        alice.stop()
        alice.start()
        assert protected_fetch_status(bob, alice, relation["relationship_id"], old_secret, unfetched["card_id"]) == 403
        denied(alice, path, pending, "active_friend_required")
        bob.start()
        denial = denied(bob, f"/api/local/friends/cards/{unfetched['card_id']}/fetch", {},
                        {"friend_card_fetch_failed", "active_friend_required"})
        assert bob.request(document)["text"] == expected_body
        eventually(lambda: all(r["status"] == "revoked" for r in bob.friends()))
        passed("offline_revocation_survives_restart_blocks_new_fetch_retains_saved_copy", fetch_denial=denial,
               protected_fetch_before_revocation=200, protected_fetch_after_revocation=403,
               protected_fetch_after_restart=403)

        expiry = datetime.fromisoformat(expired["invite"]["expires_at"]).timestamp()
        if expiry >= time.time():
            print("Waiting for the real one-minute invitation expiry", flush=True)
        eventually(lambda: time.time() > expiry + 0.1, timeout=65)
        denied(loser, "/api/local/friends/join", {"invite_uri": expired["invite_uri"]}, "invite_expired")
        passed("real_clock_invite_expiry")
    finally:
        if source:
            source.shutdown()
            source.server_close()
        for node in reversed(nodes):
            node.stop()


def run_quota(work, report):
    registry = Node(work / "registry")
    alice = Node(work / "alice", registry)
    bob = Node(work / "bob", registry)
    nodes = [registry, alice, bob]
    try:
        for node in nodes:
            node.start()
        invitation = alice.request("/api/local/friends/invites", {})
        bob.request("/api/local/friends/join", {"invite_uri": invitation["invite_uri"]})
        bob.stop()
        path = f"/api/local/friends/{quote(bob.peer_id, safe='')}/messages"
        messages = [{"message_id": uuid.uuid4().hex, "text": f"Quota acceptance {i}"} for i in range(17)]
        for message in messages[:16]:
            result = alice.request(path, message)
            assert result["delivery_state"] == "mailbox" and not result["delivered"]
        blocked = alice.request(path, messages[-1])
        assert blocked["delivery_state"] == "failed" and not blocked["delivered"]
        assert blocked["error"] == "sender_quota"
        report["checks"].append({"name": "real_default_sender_quota", "result": "passed",
            "pending_envelopes": 16, "refused_message": 17, "safe_error": blocked["error"]})
        print("Default HTTP mailbox quota: 16 stored, 17th refused without claiming delivery", flush=True)
        alice.stop()
        alice.start()
        retained = next(row for row in alice.history(bob) if row["msg_id"] == messages[-1]["message_id"])
        assert retained["error"] == "sender_quota" and retained["delivery_state"] == "failed"
        bob.start()
        eventually(lambda: len(bob.history(alice)) == 16)
        eventually(lambda: sum(row.get("delivery_state") == "delivered" for row in alice.history(bob)) == 16)
        assert alice.request(f"/api/local/friends/{quote(bob.peer_id, safe='')}/retry-messages", {})["attempted"] == 1
        assert len(bob.history(alice)) == 17
        assert alice.request(path, messages[-1])["delivered"]
        assert len(bob.history(alice)) == 17
        bob.stop()
        bob.start()
        assert {row["msg_id"] for row in bob.history(alice)} == {row["message_id"] for row in messages}
        report["checks"].append({"name": "quota_failure_restart_recovery_and_same_id_retry", "result": "passed",
            "received_messages": 17, "duplicate_messages": 0})
        print("Quota recovery and restart: 17 messages, no duplicates", flush=True)
    finally:
        for node in reversed(nodes):
            node.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=["first-sharing", "mailbox-quota"], default="first-sharing")
    args = parser.parse_args()
    work = args.work_root.resolve()
    if work.exists():
        raise SystemExit("Use a new work root; existing acceptance evidence is never overwritten.")
    if args.output.exists():
        raise SystemExit("Use a new output file; prior results are never overwritten.")
    work.mkdir(parents=True)
    report = {"version": 1, "started_at": datetime.now(UTC).isoformat(), "passed": False,
        "scope": "isolated real TCP processes, HTTP registry mailbox, deterministic local article source",
        "not_proven": ["desktop installation/UI", "different public network exits", "mailbox quota/expiry failures", "full privacy export audit"],
        "checks": []}
    report["scenario"] = args.scenario
    if args.scenario == "mailbox-quota":
        report["scope"] = "fresh real TCP nodes and default HTTP Registry sender quota; no artificial quota settings"
        report["not_proven"] = ["desktop installation/UI", "different public network exits",
            "recipient-wide capacity over TCP", "full-hour real-clock message expiry", "full privacy export audit"]
    try:
        (run_quota if args.scenario == "mailbox-quota" else run)(work, report)
        report["passed"] = True
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
