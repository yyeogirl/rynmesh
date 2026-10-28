"""Explicit, bounded multi-turn input carried by the existing encrypted prompt."""
from __future__ import annotations

import json

CHAT_FORMAT = "chat_messages_v1"


def decode_chat_prompt(prompt: str, prompt_format: str) -> list[dict[str, str]] | None:
    if prompt_format == "text":
        return None
    if prompt_format != CHAT_FORMAT or len(prompt.encode()) > 64 * 1024:
        raise ValueError("unsupported or oversized prompt format")
    try:
        messages = json.loads(prompt)
    except (ValueError, RecursionError):
        raise ValueError("invalid chat messages") from None
    if not isinstance(messages, list) or not 1 <= len(messages) <= 512:
        raise ValueError("invalid chat messages")
    for index, row in enumerate(messages):
        if not isinstance(row, dict) or set(row) != {"role", "content"}:
            raise ValueError("invalid chat message")
        if row["role"] not in ("system", "user", "assistant") or (row["role"] == "system" and index != 0):
            raise ValueError("invalid chat role")
        if not isinstance(row["content"], str) or not row["content"]:
            raise ValueError("invalid chat content")
    if messages[-1]["role"] != "user":
        raise ValueError("chat messages must end with the current user question")
    return messages
