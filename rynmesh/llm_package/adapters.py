"""Uniform adapters for local OpenAI-compatible and Ollama runtimes."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from .chat import ChatAccumulator, validate_chat


class AdapterError(RuntimeError):
    pass


class LLMAdapter(Protocol):
    def health(self) -> dict[str, Any]: ...
    def models(self) -> list[dict[str, Any]]: ...
    def capabilities(self) -> dict[str, Any]: ...
    def infer(self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float) -> dict[str, Any]: ...
    def chat(self, body: dict[str, Any], *, task_id: str, timeout_s: float, on_event: Any = None) -> dict[str, Any]: ...
    def cancel(self, task_id: str) -> bool: ...
    def metrics(self) -> dict[str, Any]: ...
    def shutdown(self) -> None: ...


def validate_local_url(base_url: str, *, allow_non_loopback: bool = False) -> str:
    cleaned = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AdapterError("local API URL must be http(s) with a hostname")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise AdapterError("credentials, fragments, and query strings are not allowed in the base URL")
    if allow_non_loopback:
        return cleaned
    host = parsed.hostname.lower()
    if host == "localhost":
        return cleaned
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 80)}
    except OSError as exc:
        raise AdapterError(f"cannot resolve local API host: {exc}") from exc
    if not addresses or any(not ipaddress.ip_address(addr).is_loopback for addr in addresses):
        raise AdapterError("non-loopback local API blocked; use explicit allow_non_loopback only on a trusted LAN")
    return cleaned


@dataclass
class AdapterMetrics:
    requests: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_duration_ms: int = 0


class OpenAICompatibleAdapter:
    def __init__(self, *, base_url: str, model: str = "", api_key_env: str = "",
                 allow_non_loopback: bool = False, timeout_s: float = 120.0) -> None:
        self.base_url = validate_local_url(base_url, allow_non_loopback=allow_non_loopback)
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self._cancelled: set[str] = set()
        self._active_responses: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._metrics = AdapterMetrics()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            secret = os.environ.get(self.api_key_env, "")
            if not secret:
                raise AdapterError(f"API key environment variable {self.api_key_env!r} is not set")
            headers["Authorization"] = "Bearer " + secret
        return headers

    def _json(self, path: str, payload: dict[str, Any] | None, timeout_s: float,
              *, task_id: str = "") -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers=self._headers(), method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                if task_id:
                    with self._lock:
                        self._active_responses[task_id] = response
                try:
                    raw = response.read(4 * 1024 * 1024 + 1)
                finally:
                    if task_id:
                        with self._lock:
                            self._active_responses.pop(task_id, None)
        except (OSError, urllib.error.HTTPError) as exc:
            raise AdapterError(f"local API request failed ({path}): {exc}") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise AdapterError("local API response exceeded 4 MiB")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("local API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AdapterError("local API JSON root must be an object")
        return value

    def models(self) -> list[dict[str, Any]]:
        result = self._json("/v1/models", None, min(8.0, self.timeout_s))
        entries = result.get("data", result.get("models", []))
        return [dict(item) for item in entries if isinstance(item, dict)]

    def health(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            models = self.models()
            names = [str(item.get("id") or item.get("name") or item.get("model") or "") for item in models]
            selected = self.model or next((name for name in names if name), "")
            if self.model and self.model not in names:
                return {"ok": False, "error": "configured model not reported", "model_count": len(models)}
            if not self.model:
                self.model = selected
            return {"ok": bool(selected), "model_count": len(models), "model": selected,
                    "latency_ms": int((time.monotonic() - started) * 1000)}
        except AdapterError as exc:
            return {"ok": False, "error": str(exc), "latency_ms": int((time.monotonic() - started) * 1000)}

    def capabilities(self) -> dict[str, Any]:
        if not self.model and not self.health().get("ok"):
            return {"chat_completions": False, "streaming": False, "cancel": "best_effort"}
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps({
                "model": self.model, "messages": [{"role": "user", "content": "Reply: ok"}],
                "max_tokens": 2, "stream": True,
            }).encode("utf-8"),
            headers=self._headers(), method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=min(15.0, self.timeout_s)) as response:
                sample = response.read(16 * 1024).decode("utf-8", errors="replace")
            streaming = "data:" in sample or "text/event-stream" in sample.lower()
        except (OSError, urllib.error.HTTPError):
            streaming = False
        return {"chat_completions": True, "streaming": streaming, "cancel": "best_effort"}

    def infer(self, *, prompt: str, max_tokens: int, task_id: str, timeout_s: float) -> dict[str, Any]:
        if not prompt:
            raise AdapterError("prompt is required")
        if task_id in self._cancelled:
            raise AdapterError("task_cancelled")
        if not self.model and not self.health().get("ok"):
            raise AdapterError("local API has no usable model")
        started = time.monotonic()
        try:
            result = self._json("/v1/chat/completions", {
                "model": self.model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": int(max_tokens), "stream": False,
            }, min(float(timeout_s), self.timeout_s), task_id=task_id)
            if task_id in self._cancelled:
                raise AdapterError("task_cancelled")
            choices = result.get("choices") or []
            text = ""
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                text = str(
                    message.get("content")
                    or message.get("reasoning_content")
                    or choices[0].get("text")
                    or ""
                )
            if not text:
                raise AdapterError("local API returned no completion text")
            usage = result.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens") or max(1, len(prompt) // 4))
            output_tokens = int(usage.get("completion_tokens") or max(1, len(text) // 4))
            duration_ms = int((time.monotonic() - started) * 1000)
            with self._lock:
                self._metrics.requests += 1
                self._metrics.input_tokens += input_tokens
                self._metrics.output_tokens += output_tokens
                self._metrics.total_duration_ms += duration_ms
            return {"text": text, "model": self.model, "input_tokens": input_tokens,
                    "output_tokens": output_tokens, "duration_ms": duration_ms}
        except Exception:
            with self._lock:
                self._metrics.failures += 1
            raise
        finally:
            self._cancelled.discard(task_id)

    def chat(self, body: dict[str, Any], *, task_id: str, timeout_s: float,
             on_event: Any = None) -> dict[str, Any]:
        body = validate_chat(body)
        if task_id in self._cancelled:
            raise AdapterError("task_cancelled")
        if not self.model and not self.health().get("ok"):
            raise AdapterError("local API has no usable model")
        body["model"] = self.model
        streaming = bool(body.get("stream"))
        if streaming:
            body["stream_options"] = {"include_usage": True}
        started = time.monotonic()
        deadline = started + timeout_s
        try:
            if not streaming:
                raw = self._json("/v1/chat/completions", body, timeout_s, task_id=task_id)
                choice = raw["choices"][0]
                message = choice["message"]
                usage = raw.get("usage") or {}
                finish = choice.get("finish_reason") or "stop"
            else:
                request = urllib.request.Request(self.base_url + "/v1/chat/completions",
                                                 data=json.dumps(body).encode(), headers=self._headers())
                accumulator = ChatAccumulator()
                with urllib.request.urlopen(request, timeout=timeout_s) as response:
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise AdapterError("upstream_streaming_not_supported")
                    with self._lock:
                        self._active_responses[task_id] = response
                    total = 0
                    for line in response:
                        total += len(line)
                        if total > 32 * 1024 * 1024:
                            raise AdapterError("stream exceeds 32 MiB")
                        if task_id in self._cancelled:
                            raise AdapterError("task_cancelled")
                        if time.monotonic() > deadline:
                            raise AdapterError("inference timed out")
                        if not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            break
                        chunk = json.loads(data)
                        if "error" in chunk:
                            raise AdapterError("upstream_stream_error")
                        accumulator.add(chunk)
                        if on_event:
                            on_event(chunk)
                if not accumulator.finished:
                    raise AdapterError("upstream_stream_truncated")
                message, usage, finish = accumulator.message(), accumulator.usage, accumulator.finish_reason
            if task_id in self._cancelled:
                raise AdapterError("task_cancelled")
            if not message.get("content") and not message.get("tool_calls") and not message.get("reasoning_content"):
                raise AdapterError("upstream_empty_completion")
            input_tokens = int(usage.get("prompt_tokens") or max(1, len(json.dumps(body["messages"])) // 4))
            output_tokens = int(usage.get("completion_tokens") or max(1, len(json.dumps(message)) // 4))
            return {"text": message.get("content") or "", "message": message, "finish_reason": finish,
                    "input_tokens": input_tokens, "output_tokens": output_tokens,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        except (OSError, ValueError, KeyError, IndexError) as exc:
            if task_id in self._cancelled:
                raise AdapterError("task_cancelled") from exc
            raise AdapterError("upstream_chat_failed") from exc
        finally:
            with self._lock:
                self._active_responses.pop(task_id, None)
                self._cancelled.discard(task_id)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            self._cancelled.add(task_id)
            response = self._active_responses.get(task_id)
        if response is not None:
            try:
                response.close()
            except OSError:
                pass
        return True

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return vars(self._metrics).copy()

    def shutdown(self) -> None:
        self._cancelled.clear()


class OllamaAdapter(OpenAICompatibleAdapter):
    """Ollama adapter using its OpenAI-compatible API plus native tag probing."""

    def models(self) -> list[dict[str, Any]]:
        result = self._json("/api/tags", None, min(8.0, self.timeout_s))
        return [dict(item) for item in result.get("models", []) if isinstance(item, dict)]

    def capabilities(self) -> dict[str, Any]:
        detected = super().capabilities()
        return {**detected, "ollama": True}


def adapter_from_manifest(manifest: Any) -> LLMAdapter:
    kwargs = {
        "base_url": manifest.base_url, "model": manifest.model,
        "api_key_env": manifest.api_key_env, "allow_non_loopback": manifest.allow_non_loopback,
        "timeout_s": manifest.timeout_seconds,
    }
    return OllamaAdapter(**kwargs) if manifest.adapter == "ollama" else OpenAICompatibleAdapter(**kwargs)
