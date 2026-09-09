"""Command-line server and local Consumer client for the video package."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _request(base_url: str, token: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30) -> tuple[dict[str, Any], bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method="POST" if body is not None else "GET",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        content_type = response.headers.get("content-type", "")
    return (json.loads(raw) if "json" in content_type else {}), raw


def client_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rynmesh-video", description="Call a remote Rynmesh video provider")
    parser.add_argument("--base-url", default=os.environ.get("RYNMESH_VIDEO_BASE_URL", ""))
    parser.add_argument("--token-env", default="RYNMESH_VIDEO_API_TOKEN")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    generate = sub.add_parser("generate")
    generate.add_argument("--prompt", required=True)
    generate.add_argument("--output", required=True)
    generate.add_argument("--width", type=int, default=480)
    generate.add_argument("--height", type=int, default=272)
    generate.add_argument("--frames", type=int, default=33)
    generate.add_argument("--steps", type=int, default=20)
    generate.add_argument("--fps", type=int, default=8)
    generate.add_argument("--seed", type=int, default=42)
    generate.add_argument("--poll-seconds", type=float, default=5)
    generate.add_argument("--timeout-seconds", type=float, default=3600)
    args = parser.parse_args(argv)
    if not args.base_url:
        parser.error("--base-url or RYNMESH_VIDEO_BASE_URL is required")
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        parser.error(f"{args.token_env} is not set")
    try:
        if args.command == "health":
            value, _ = _request(args.base_url, token, "/health")
            print(json.dumps(value, indent=2, ensure_ascii=False))
            return 0
        submitted, _ = _request(
            args.base_url,
            token,
            "/v1/videos/generations",
            {
                "prompt": args.prompt,
                "width": args.width,
                "height": args.height,
                "num_frames": args.frames,
                "num_inference_steps": args.steps,
                "fps": args.fps,
                "seed": args.seed,
            },
        )
        job_id = submitted["id"]
        deadline = time.monotonic() + args.timeout_seconds
        while time.monotonic() < deadline:
            status, _ = _request(args.base_url, token, f"/v1/videos/generations/{job_id}")
            print(json.dumps({k: status.get(k) for k in ("id", "state", "elapsed_seconds", "error")}, ensure_ascii=False))
            if status.get("state") == "failed":
                return 2
            if status.get("state") == "succeeded":
                _, raw = _request(args.base_url, token, f"/v1/videos/generations/{job_id}/content", timeout=120)
                target = Path(args.output).expanduser().resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                print(json.dumps({"saved": str(target), "bytes": len(raw), "job": status}, indent=2, ensure_ascii=False))
                return 0
            time.sleep(max(0.2, args.poll_seconds))
        print("video generation timed out", file=sys.stderr)
        return 2
    except (KeyError, OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print(f"rynmesh-video: {exc}", file=sys.stderr)
        return 2


def server_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rynmesh-video-provider")
    parser.add_argument("--host", default=os.environ.get("RYNMESH_VIDEO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RYNMESH_VIDEO_PORT", "8800")))
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run("rynmesh.video_package.service:create_app", host=args.host, port=args.port, factory=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(client_main())
