"""Validated, text/tool chat contract shared by local and encrypted inference."""
from __future__ import annotations

import copy
import json
import math
import time
from typing import Any


def validate_chat(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {"messages", "max_tokens", "temperature", "top_p", "stop", "tools", "tool_choice", "stream", "enable_thinking", "chat_template_kwargs"}
    if set(value) - allowed:
        raise ValueError("unsupported chat parameters: " + ", ".join(sorted(set(value) - allowed)))
    body = copy.deepcopy(value)
    if "chat_template_kwargs" in body:
        options = body.pop("chat_template_kwargs")
        if not isinstance(options, dict) or set(options) - {"enable_thinking"}:
            raise ValueError("only enable_thinking is supported in chat_template_kwargs")
        if "enable_thinking" in options:
            if "enable_thinking" in body and (type(body["enable_thinking"]) is not type(options["enable_thinking"]) or body["enable_thinking"] != options["enable_thinking"]):
                raise ValueError("conflicting enable_thinking values")
            body["enable_thinking"] = options["enable_thinking"]
    if "enable_thinking" in body and type(body["enable_thinking"]) is not bool:
        raise ValueError("enable_thinking must be boolean")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty array")
    pending: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("invalid message role")
        if set(message) - {"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"}:
            raise ValueError("unsupported message fields")
        if "reasoning_content" in message and (message["role"] != "assistant" or (message["reasoning_content"] is not None and not isinstance(message["reasoning_content"], str))):
            raise ValueError("reasoning_content must be assistant text")
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str) for part in content):
                raise ValueError("only text content is supported")
            message["content"] = content = "".join(part["text"] for part in content)
        if content is not None and not isinstance(content, str):
            raise ValueError("message content must be text")
        if message["role"] == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ValueError("tool result must reference a pending tool call")
            pending.remove(call_id)
        elif pending:
            raise ValueError("all tool results must precede the next message")
        calls = message.get("tool_calls", [])
        if calls and (message["role"] != "assistant" or not isinstance(calls, list)):
            raise ValueError("tool_calls require an assistant message")
        for call in calls:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            if not isinstance(call, dict) or not isinstance(function, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str) or not call["id"] or not function.get("name") or not isinstance(function.get("arguments"), str):
                raise ValueError("invalid tool call")
            if call["id"] in pending:
                raise ValueError("duplicate tool call id")
            pending.add(call["id"])
    if pending:
        raise ValueError("missing tool results")
    tokens = body.get("max_tokens", 512)
    if type(tokens) is not int or not 1 <= tokens <= 131072:
        raise ValueError("max_tokens must be a positive integer <= 131072")
    body["max_tokens"] = tokens
    if type(body.get("stream", False)) is not bool:
        raise ValueError("stream must be boolean")
    for key, limit in (("temperature", 2), ("top_p", 1)):
        if key in body and (type(body[key]) not in (float, int) or not math.isfinite(body[key]) or not 0 <= body[key] <= limit):
            raise ValueError(f"invalid {key}")
    if "stop" in body and body["stop"] is not None:
        stop = body["stop"]
        if not isinstance(stop, str) and not (isinstance(stop, list) and len(stop) <= 4 and all(isinstance(x, str) for x in stop)):
            raise ValueError("stop must be text or up to four strings")
    tools = body.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 128:
        raise ValueError("invalid tools")
    names = set()
    for tool in tools:
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError("only named function tools are supported")
        if function.get("strict"):
            raise ValueError("strict tool schemas are not supported")
        names.add(function["name"])
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, dict):
        if choice.get("type") != "function" or choice.get("function", {}).get("name") not in names:
            raise ValueError("unknown tool_choice function")
    elif not isinstance(choice, str) or choice not in {"auto", "none", "required"}:
        raise ValueError("invalid tool_choice")
    if choice != "auto" and choice != "none" and not tools:
        raise ValueError("tool_choice requires tools")
    if len(json.dumps(body, ensure_ascii=False).encode()) > 1024 * 1024:
        raise ValueError("chat exceeds 1 MiB")
    return body


class ChatAccumulator:
    """Assemble actual upstream deltas without losing tool arguments or usage."""

    def __init__(self) -> None:
        self.text = ""
        self.reasoning = ""
        self.calls: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] = {}
        self.finish_reason = "stop"
        self.finished = False

    def add(self, chunk: dict[str, Any]) -> None:
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                raise ValueError("only one completion is supported")
            delta = choice.get("delta", {})
            self.text += delta.get("content") or ""
            self.reasoning += delta.get("reasoning_content") or ""
            for item in delta.get("tool_calls") or []:
                call = self.calls.setdefault(item["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if item.get("id"):
                    call["id"] = item["id"]
                for field in ("name", "arguments"):
                    call["function"][field] += item.get("function", {}).get(field) or ""
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
                self.finished = True

    def message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if self.calls:
            message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return message


def completion(result: dict[str, Any], model: str, request_id: str) -> dict[str, Any]:
    usage = {"prompt_tokens": result["input_tokens"], "completion_tokens": result["output_tokens"]}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return {"id": "chatcmpl-" + request_id, "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": result.get("message") or {"role": "assistant", "content": result.get("output", result.get("text", ""))},
                                          "finish_reason": result.get("finish_reason", "stop")}], "usage": usage}
