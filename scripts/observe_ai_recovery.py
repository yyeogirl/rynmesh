"""Record bounded metadata for the dedicated real-model recovery fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    home = args.home.resolve()
    if home.name != "rynmesh-ai-recovery-acceptance":
        raise SystemExit("Use the dedicated AI recovery acceptance directory.")
    marker = json.loads((home / ".ai-recovery-fixture.json").read_text(encoding="utf-8"))
    if marker.get("kind") != "ryn.ai-recovery-acceptance.v1":
        raise SystemExit("Not this acceptance fixture.")
    observation = {"label": args.label, "at": datetime.now(UTC).isoformat()}
    if args.offline:
        job = json.loads((home / "llm/setup-job.json").read_text(encoding="utf-8"))
    else:
        with httpx.Client(base_url="http://127.0.0.1:18924", timeout=15) as client:
            privacy = client.get("/api/local/privacy/status")
            privacy.raise_for_status()
            if Path(privacy.json()["storage_root"]).resolve() != home:
                raise SystemExit("The server is not this fixture.")
            started = time.perf_counter()
            health = client.get("/health")
            observation["health"] = {"status": health.status_code, "seconds": time.perf_counter() - started}
            result = client.get("/api/local/llm/setup/status")
            result.raise_for_status()
            job = result.json()
            service = client.get("/api/local/llm/service/status")
            service.raise_for_status()
            observation["service"] = {key: service.json().get(key) for key in ("configured", "ready", "online", "publication_enabled")}
    observation["job"] = {key: job.get(key) for key in ("job_id", "state", "stage", "progress", "error_code", "recovery_state", "message", "resume_configuration")}
    files = []
    for path in sorted((home / "llm/models").rglob("*")):
        if path.is_file():
            row = {"name": path.relative_to(home).as_posix()}
            with path.open("rb") as source:
                row["bytes"] = source.seek(0, 2)
                # The managed downloader publishes .gguf only after verifying
                # the complete .part file; runtime preparation may still run.
                if args.offline or path.suffix == ".gguf" or job.get("state") in {"failed", "cancelled", "succeeded"}:
                    source.seek(0)
                    row["sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
            files.append(row)
    observation["model_files"] = files
    observation["recovery_record_present"] = (home / "llm/setup-recovery.json").exists()
    trace = home / ".download-http.json"
    observation["download_responses"] = json.loads(trace.read_text(encoding="utf-8")) if trace.exists() else []
    destination = Path(__file__).resolve().parents[1] / "docs/acceptance/local-ai-development/windows-install-recovery.json"
    record = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {
        "packaged_desktop": False, "observations": [],
    }
    record["environment"] = "Windows AMD64; production browser UI; real HTTPS model download; no response or progress stubs"
    record["observations"].append(observation)
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(observation, ensure_ascii=False))


if __name__ == "__main__":
    main()
