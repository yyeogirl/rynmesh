"""Crash a rebuilding search index in a dedicated scale-acceptance home.

Original content is hashed before and after. Only the rebuildable encrypted
index is deliberately damaged; an encrypted backup is retained. A child
process is terminated after it reports building, then the node's actual worker
must recover the full index. Never run against an everyday user's node home.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from accept_local_search import configure, create


def originals(home):
    paths = [home / "consumption.json", home / "ask-ryn" / "history.json", home / "friends" / "state.json"]
    paths.extend((home / "reader-cache").glob("*.json"))
    paths.extend((home / "messages").glob("*.jsonl"))
    return {str(path.relative_to(home)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def child(home):
    app = create(home)
    index = app.state.local_search.index
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(index.rebuild)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if index.status()["state"] == "building":
                print(json.dumps({"pid": os.getpid(), "observed_state": "building"}), flush=True)
                future.result(timeout=30)
                return
            if future.done():
                raise RuntimeError("Could not observe an active rebuild")
            time.sleep(0.01)
        raise RuntimeError("Rebuild observation timed out")


def recover(home, output):
    from fastapi.testclient import TestClient

    from rynmesh.atomic_io import atomic_write_bytes, atomic_write_json, migration_backup
    before = originals(home)
    path = home / "local-search" / "index.json"
    migration_backup(path, suffix=".before-recovery")
    atomic_write_bytes(path, b"{deliberately-interrupted-cache")
    process = subprocess.Popen([sys.executable, __file__, "--home", str(home), "--child"], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        output_line = queue.Queue()
        threading.Thread(target=lambda: output_line.put(process.stdout.readline()), daemon=True).start()
        observation = json.loads(output_line.get(timeout=30))
        assert observation["observed_state"] == "building"
        assert type(observation["pid"]) is int and observation["pid"] > 0 and observation["pid"] != os.getpid()
        if os.name != "nt":
            assert observation["pid"] == process.pid
        assert process.poll() is None
        # Windows venv launchers can own the Popen handle while their actual
        # Python child owns the rebuild. Its PID comes from our child program's
        # private stdout pipe, never from a process-name search.
        os.kill(observation["pid"], signal.SIGTERM)
        process.wait(timeout=10)
        print("Observed building and terminated the acceptance child", flush=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
    app = create(home)
    initial = app.state.local_search.index.status()
    assert initial["state"] == "needs_rebuild"
    start = time.perf_counter()
    with TestClient(app) as client:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            status = client.get("/api/local/search/status").json()
            if status["state"] == "ready":
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Automatic index recovery did not finish")
        elapsed = time.perf_counter() - start
        reply = client.post("/api/local/search/query", json={"query": "testing"})
        reply.raise_for_status()
        assert reply.json()["total"] == 10_000
    after = originals(home)
    assert before == after
    result = {"recorded_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Dedicated synthetic scale node; process interruption during index rebuild followed by actual node-worker recovery. Not packaged desktop or power-loss hardware evidence.",
        "terminated_child": observation, "launcher_pid": process.pid, "child_exit_code": process.returncode,
        "initial_state_after_restart": initial["state"], "automatic_recovery_seconds": elapsed,
        "recovered_record_count": reply.json()["total"], "source_file_count": len(before), "source_files_unchanged": True,
        "source_snapshot_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()}
    atomic_write_json(output, result)
    print(json.dumps({"recovered": result["recovered_record_count"], "source_files_unchanged": True}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.name.startswith("rynmesh-search-scale-") or not (home / "local-search" / "index.json").is_file():
        raise SystemExit("A dedicated, completed search-scale acceptance home is required.")
    configure(home, 18852)
    if args.child:
        child(home)
    elif args.output:
        recover(home, args.output)
    else:
        raise SystemExit("--output is required")


if __name__ == "__main__":
    main()
