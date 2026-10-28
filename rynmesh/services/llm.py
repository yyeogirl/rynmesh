"""Disabled legacy LLM work-order service.

This worker used to carry task bodies in registry-visible work-order params.
It is disabled because registry work-order params
are control-plane data and must never contain prompts or outputs. Use
``rynmesh.llm_package`` and the signed end-to-end encrypted peer task routes.
The deterministic helper remains importable for unit tests only.
"""
from __future__ import annotations

import hashlib
import sys
from typing import Any

from ._base import ServiceWorker

CAPABILITY = "rynmesh.llm.complete"
OPERATION = "rynmesh.llm.complete"

_STUB_WORDS = (
    "agent", "mesh", "credit", "trust", "value", "content", "service",
    "provider", "consumer", "verify", "receipt", "propagate", "earn",
    "stake", "anchor", "explore", "weight", "carve", "saturate", "signal",
)


def llm_stub_complete(prompt: str, max_tokens: int = 64) -> str:
    n = max(8, min(256, int(max_tokens or 64)))
    h = hashlib.sha256(prompt.encode("utf-8", errors="replace")).digest()
    out: list[str] = []
    for i in range(n):
        out.append(_STUB_WORDS[h[i % len(h)] % len(_STUB_WORDS)])
        if i % 8 == 7:
            out.append(".")
        h = hashlib.sha256(h + bytes([i & 0xFF])).digest()
    return " ".join(out)


class LLMWorker(ServiceWorker):
    capability = CAPABILITY
    operation = OPERATION

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        raise RuntimeError(
            "legacy_plaintext_llm_work_orders_disabled: use rynmesh.llm.private.v1"
        )


def main(argv: list[str] | None = None) -> int:
    del argv
    print(
        "rynmesh-llm-worker is disabled: use rynmesh-llm plus the private node task API",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
