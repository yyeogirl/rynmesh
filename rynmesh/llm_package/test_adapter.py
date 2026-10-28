"""Deterministic OpenAI-compatible server for automated protocol tests only.

This server is deliberately labelled as a test adapter and must never be used
as evidence of non-mock inference. The real-inference Compose profile connects
the Provider to llama.cpp instead.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import FastAPI

app = FastAPI(title="Rynmesh deterministic test LLM")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "test_only": True}


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": "rynmesh-test-adapter", "owned_by": "test-only"}]}


@app.post("/v1/chat/completions")
def complete(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    prompt = str(messages[-1].get("content") if messages else "")
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    text = "rynmesh encrypted e2e ok " + digest
    return {
        "id": "test-" + digest, "object": "chat.completion", "model": "rynmesh-test-adapter",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": max(1, len(prompt) // 4),
                  "completion_tokens": max(1, len(text) // 4),
                  "total_tokens": max(2, len(prompt) // 4 + len(text) // 4)},
    }


def main() -> int:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
