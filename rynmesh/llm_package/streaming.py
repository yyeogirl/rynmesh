"""Bounded thread-to-async streaming bridge and encrypted peer SSE reader."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
import urllib.request
from typing import Any

from rynmesh.transport import network_key_header


async def events(run: Any, cancel: Any):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    closed = threading.Event()

    def emit(value: dict[str, Any]) -> None:
        if closed.is_set():
            raise RuntimeError("task_cancelled")
        future = asyncio.run_coroutine_threadsafe(queue.put(("event", value)), loop)
        try:
            future.result(timeout=15)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RuntimeError("stream consumer too slow") from exc

    async def work():
        try:
            result = await run(emit)
            if not closed.is_set():
                await queue.put(("result", result))
        except Exception as exc:
            if not closed.is_set():
                await queue.put(("error", exc))

    task = asyncio.create_task(work())
    try:
        while True:
            kind, value = await queue.get()
            if kind == "error":
                raise value
            yield kind, value
            if kind == "result":
                break
    finally:
        closed.set()
        if not task.done():
            # Do not abandon settlement/hold cleanup by cancelling its coroutine.
            # Starlette closes generators inside an already-cancelled AnyIO scope;
            # run cancellation independently so that scope cannot skip the I/O.
            cancel_task = asyncio.create_task(asyncio.to_thread(cancel))
            cancel_task.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        task.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)


def peer_stream(url: str, body: dict[str, Any], *, timeout_s: float, on_event: Any) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", **network_key_header()}
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    deadline = time.monotonic() + timeout_s
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise ValueError("peer_streaming_not_supported")
        total = 0
        for line in response:
            total += len(line)
            if total > 128 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError("peer stream limit exceeded")
            if not line.startswith(b"data:"):
                continue
            envelope = json.loads(line[5:])
            if envelope.get("payload", {}).get("kind") == "llm_response":
                return envelope
            on_event(envelope)
    raise ValueError("peer stream truncated")
