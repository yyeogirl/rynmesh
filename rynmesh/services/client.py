"""Client-side helpers — invoke Ryn services from a requester node.

Usage shape (sync, polls for result):

    from rynmesh.store import RynmeshStore
    from rynmesh.services.client import request_embedding
    store = RynmeshStore()
    out = request_embedding(store, provider_peer_id=PROVIDER_PID,
                             text="hello", network_id="rynmesh-home-qa")

The helpers wrap submit_work_order + list_work_results polling and JSON-
decode the result payload that the worker emitted into result.message.

Note: request_llm is retired. Plaintext prompts must not ride registry work
orders; use the encrypted private task protocol via the node API instead
(POST /api/local/llm/orders — see rynmesh/llm_package/).
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

from rynmesh.store import RynmeshStore

from .embeddings import OPERATION as EMBED_OP
from .image import OPERATION as IMAGE_OP


def _await_result(
    store: RynmeshStore,
    *,
    work_order_id: str,
    network_id: str,
    timeout_s: float,
    poll_interval_s: float,
) -> dict[str, Any]:
    deadline = time.time() + max(0.0, timeout_s)
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        results = store.list_work_results(
            work_order_id=work_order_id, network_id=network_id,
        ).get("work_results", [])
        for entry in results:
            status = str(entry.get("status", "")).lower()
            if status in ("completed", "failed"):
                return entry
            last = entry
        time.sleep(poll_interval_s)
    if last is not None:
        return {"status": "timeout", "message": "no terminal result before timeout"}
    return {"status": "timeout", "message": "no result observed before timeout"}


def _submit_and_await(
    store: RynmeshStore,
    *,
    provider_peer_id: str,
    operation: str,
    params: dict[str, Any],
    network_id: str,
    timeout_s: float,
    poll_interval_s: float,
) -> dict[str, Any]:
    submission = store.submit_work_order(
        provider_peer_id=provider_peer_id,
        capability=operation,
        operation=operation,
        params=params,
        network_id=network_id,
    )
    wo_id = str(submission.get("order", {}).get("work_order_id", "")) or str(
        submission.get("work_order_id", ""),
    )
    if not wo_id:
        raise RuntimeError(f"submit_work_order returned no id: {submission}")
    return _await_result(
        store,
        work_order_id=wo_id,
        network_id=network_id,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
    )


def _decode_payload(result: dict[str, Any]) -> dict[str, Any]:
    if str(result.get("status", "")).lower() != "completed":
        raise RuntimeError(f"service result not completed: {result}")
    msg = result.get("message", "")
    try:
        return json.loads(msg) if msg else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"service result message is not JSON: {exc}: {msg!r}") from exc


def request_llm(
    store: RynmeshStore,
    *,
    provider_peer_id: str,
    prompt: str,
    max_tokens: int = 64,
    network_id: str = "rynmesh-main",
    timeout_s: float = 20.0,
    poll_interval_s: float = 0.5,
) -> dict[str, Any]:
    del store, provider_peer_id, prompt, max_tokens, network_id, timeout_s, poll_interval_s
    raise RuntimeError(
        "legacy plaintext LLM work orders are disabled; submit through the local "
        "/api/local/llm/orders private task endpoint"
    )


def request_embedding(
    store: RynmeshStore,
    *,
    provider_peer_id: str,
    text: str,
    dim: int = 256,
    network_id: str = "rynmesh-main",
    timeout_s: float = 20.0,
    poll_interval_s: float = 0.5,
) -> dict[str, Any]:
    res = _submit_and_await(
        store, provider_peer_id=provider_peer_id, operation=EMBED_OP,
        params={"text": text, "dim": dim},
        network_id=network_id, timeout_s=timeout_s, poll_interval_s=poll_interval_s,
    )
    return _decode_payload(res)


def request_image(
    store: RynmeshStore,
    *,
    provider_peer_id: str,
    prompt: str,
    width: int = 64,
    height: int = 64,
    network_id: str = "rynmesh-main",
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.5,
) -> dict[str, Any]:
    """Returns the decoded service payload; raw PNG bytes available as
    `bytes_obj = base64.b64decode(payload['png_b64'])`."""
    res = _submit_and_await(
        store, provider_peer_id=provider_peer_id, operation=IMAGE_OP,
        params={"prompt": prompt, "width": width, "height": height},
        network_id=network_id, timeout_s=timeout_s, poll_interval_s=poll_interval_s,
    )
    payload = _decode_payload(res)
    if "png_b64" in payload:
        payload["png"] = base64.b64decode(payload["png_b64"])
    return payload
