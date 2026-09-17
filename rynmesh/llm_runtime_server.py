"""Minimal loopback OpenAI-compatible runtime for local Transformers models.

This exists for GPU providers that cannot run the bundled Ollama/llama.cpp
binary but already have official Hugging Face/ModelScope weights available.
The process must stay on loopback; remote Consumers use the encrypted Rynmesh
LLM package instead of reaching this runtime directly.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any

from .llm_package.chat import ChatAccumulator, validate_chat


class ThinkingSplitter:
    """Separate Qwen reasoning from answer text even across partial markers."""

    def __init__(self, enabled: bool):
        self.thinking = enabled
        self.initial = enabled
        self.pending = ""

    def feed(self, text: str, *, final: bool = False) -> tuple[str, str]:
        if not self.thinking:
            return "", text
        self.pending += text
        if self.initial:
            stripped = self.pending.lstrip()
            if not final and "<think>".startswith(stripped):
                return "", ""
            if stripped.startswith("<think>"):
                self.pending = stripped[len("<think>"):]
            self.initial = False
        marker = "</think>"
        if marker in self.pending:
            reasoning, answer = self.pending.split(marker, 1)
            self.pending = ""
            self.thinking = False
            return reasoning, answer
        keep = 0 if final else len(marker) - 1
        count = max(0, len(self.pending) - keep)
        reasoning, self.pending = self.pending[:count], self.pending[count:]
        return reasoning, ""


def template_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = json.loads(json.dumps(messages))
    for message in cleaned:
        # Qwen's conversation history uses final answers, without old reasoning.
        message.pop("reasoning_content", None)
        # OpenAI uses null for tool-only assistant messages; Qwen's Jinja
        # template performs string membership checks and requires empty text.
        message["content"] = message.get("content") or ""
        if message["role"] == "developer":
            message["role"] = "system"
        for call in message.get("tool_calls", []):
            call["function"]["arguments"] = json.loads(call["function"]["arguments"])
    return cleaned


class TransformersChatRuntime:
    def __init__(self, model_path: str, *, model_id: str = "Qwen3-14B-Q4") -> None:
        self.model_path = model_path
        self.model_id = model_id
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._load_lock = threading.Lock()
        self._generation_lock = threading.Lock()

    def generation_limits(self) -> tuple[int, int, float]:
        context = int(os.environ.get("RYNMESH_TRANSFORMERS_CONTEXT", "4096"))
        native = getattr(getattr(self._model, "config", None), "max_position_embeddings", context)
        context = min(context, native)
        output = min(context, int(os.environ.get("RYNMESH_TRANSFORMERS_MAX_OUTPUT", "2048")))
        timeout = float(os.environ.get("RYNMESH_TRANSFORMERS_TIMEOUT", "120"))
        if context < 1 or output < 1 or not 0 < timeout <= 86400:
            raise ValueError("invalid runtime generation limits")
        return context, output, timeout

    def validate_length(self, input_tokens: int, max_tokens: int) -> None:
        context, output, _ = self.generation_limits()
        if not 1 <= max_tokens <= output:
            raise ValueError(f"max_tokens exceeds runtime limit of {output}")
        if input_tokens + max_tokens > context:
            raise ValueError("runtime context window exceeded")

    def prefill(self, encoded, stopped, deadline):
        """Bound attention working memory while building a long prompt's KV cache."""
        length = int(encoded["input_ids"].shape[-1])
        if length <= 512:
            return None
        cache = None
        # Leave the final prompt token for generate(), which reuses this cache.
        for start in range(0, length - 1, 512):
            if stopped.is_set():
                raise RuntimeError("generation_cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("generation_timed_out")
            end = min(start + 512, length - 1)
            output = self._model(input_ids=encoded["input_ids"][:, start:end],
                                 attention_mask=encoded["attention_mask"][:, :end],
                                 past_key_values=cache, use_cache=True, logits_to_keep=1)
            cache = output.past_key_values
        return cache

    def load(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            try:
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
            except ImportError as exc:
                raise RuntimeError("transformers_runtime_dependencies_missing") from exc
            if not torch.cuda.is_available():
                raise RuntimeError("cuda_unavailable")
            quantization = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.float16,
            )
            self._tokenizer = AutoTokenizer.from_pretrained(
                self.model_path, local_files_only=True, trust_remote_code=False
            )
            self._model = AutoModelForCausalLM.from_pretrained(
                self.model_path,
                local_files_only=True,
                trust_remote_code=False,
                quantization_config=quantization,
                device_map={"": 0},
                torch_dtype=torch.float16,
                low_cpu_mem_usage=True,
            )
            self._model.eval()

    def health(self) -> dict[str, Any]:
        try:
            import torch

            return {
                "ok": torch.cuda.is_available(),
                "model": self.model_id,
                "loaded": self._model is not None,
                "torch": torch.__version__,
                "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "",
            }
        except ImportError:
            return {"ok": False, "model": self.model_id, "error": "torch_not_installed"}

    def complete(self, messages: list[dict[str, str]], *, max_tokens: int) -> dict[str, Any]:
        self.load()
        import torch

        cleaned = [
            {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
            for item in messages
            if isinstance(item, dict)
        ]
        if not cleaned or not cleaned[-1]["content"]:
            raise ValueError("messages_required")
        max_tokens = int(max_tokens or 256)
        try:
            rendered = self._tokenizer.apply_chat_template(
                cleaned,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
        except TypeError:
            rendered = self._tokenizer.apply_chat_template(
                cleaned, tokenize=False, add_generation_prompt=True
            )
        encoded = self._tokenizer(rendered, return_tensors="pt")
        encoded = {key: value.to("cuda") for key, value in encoded.items()}
        input_tokens = int(encoded["input_ids"].shape[-1])
        self.validate_length(input_tokens, max_tokens)
        started = time.monotonic()
        with self._generation_lock, torch.inference_mode():
            timeout = self.generation_limits()[2]
            cache = self.prefill(encoded, threading.Event(), started + timeout)
            output = self._model.generate(
                **encoded,
                past_key_values=cache,
                max_new_tokens=max_tokens,
                max_time=max(0.01, timeout - (time.monotonic() - started)),
                do_sample=False,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        generated = output[0, input_tokens:]
        text = self._tokenizer.decode(generated, skip_special_tokens=True).strip()
        return {
            "text": text,
            "input_tokens": input_tokens,
            "output_tokens": int(generated.shape[-1]),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    def stream_chat(self, body: dict[str, Any]):
        """Real token streaming with Qwen's native tool-call chat template."""
        self.load()
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer

        body = validate_chat(body)
        if body.get("stop"):
            raise ValueError("custom stop sequences are not supported by this runtime")
        messages = template_messages(body["messages"])
        choice = body.get("tool_choice", "auto")
        tools = body.get("tools") if choice != "none" else None
        if tools and choice != "auto":
            instruction = "You must call a provided tool in your next response."
            if isinstance(choice, dict):
                instruction = "You must call the tool named " + choice["function"]["name"] + " in your next response."
            messages.insert(0, {"role": "system", "content": instruction})
        rendered = self._tokenizer.apply_chat_template(messages, tools=tools, tokenize=False,
                                                        add_generation_prompt=True, enable_thinking=body.get("enable_thinking", False))
        encoded = self._tokenizer(rendered, return_tensors="pt")
        input_tokens = int(encoded["input_ids"].shape[-1])
        self.validate_length(input_tokens, body["max_tokens"])
        if not self._generation_lock.acquire(blocking=False):
            raise RuntimeError("model_busy")
        stopped = threading.Event()
        deadline = time.monotonic() + self.generation_limits()[2]
        outcome: dict[str, Any] = {}
        streamer = TextIteratorStreamer(self._tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=5)

        class Cancelled(StoppingCriteria):
            def __call__(self, *_args, **_kwargs):
                return stopped.is_set() or time.monotonic() >= deadline

        def generate():
            try:
                temperature = body.get("temperature", 0)
                options = {"do_sample": temperature > 0}
                if temperature > 0:
                    options.update(temperature=temperature, top_p=body.get("top_p", 1))
                with torch.inference_mode():
                    gpu_inputs = {k: v.to("cuda") for k, v in encoded.items()}
                    cache = self.prefill(gpu_inputs, stopped, deadline)
                    result = self._model.generate(
                        **gpu_inputs, past_key_values=cache, max_new_tokens=body["max_tokens"],
                        streamer=streamer, stopping_criteria=StoppingCriteriaList([Cancelled()]),
                        pad_token_id=self._tokenizer.eos_token_id, **options,
                    )
                if time.monotonic() >= deadline:
                    raise TimeoutError("generation_timed_out")
                outcome["tokens"] = int(result.shape[-1]) - input_tokens
            except Exception as exc:
                outcome["error"] = exc
                streamer.end()
            finally:
                self._generation_lock.release()

        worker = threading.Thread(target=generate, daemon=True)
        worker.start()
        pending = ""
        thinking = ThinkingSplitter(body.get("enable_thinking", False))
        in_tool = False
        calls = 0
        ident = "chatcmpl-" + uuid.uuid4().hex

        def chunk(delta, finish=None, usage=None):
            value = {"id": ident, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": self.model_id, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage is not None:
                value["usage"] = usage
            return value

        try:
            yield chunk({"role": "assistant"})
            import queue
            while True:
                try:
                    piece = next(streamer)
                except queue.Empty:
                    if not worker.is_alive():
                        break
                    continue
                except StopIteration:
                    break
                reasoning, answer = thinking.feed(piece)
                if reasoning:
                    yield chunk({"reasoning_content": reasoning})
                pending += answer
                while pending:
                    marker = "</tool_call>" if in_tool else "<tool_call>"
                    pos = pending.find(marker)
                    if pos < 0:
                        if not in_tool and len(pending) > len(marker):
                            yield chunk({"content": pending[:-len(marker)]})
                            pending = pending[-len(marker):]
                        break
                    prefix, pending = pending[:pos], pending[pos + len(marker):]
                    if in_tool:
                        call = json.loads(prefix.strip())
                        names = {t["function"]["name"] for t in tools or []}
                        if call.get("name") not in names or (isinstance(choice, dict) and call["name"] != choice["function"]["name"]):
                            raise ValueError("model returned an unknown tool")
                        yield chunk({"tool_calls": [{"index": calls, "id": "call_" + uuid.uuid4().hex,
                                                    "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False)}}]})
                        calls += 1
                    elif prefix:
                        yield chunk({"content": prefix})
                    in_tool = not in_tool
            worker.join(timeout=5)
            if "error" in outcome:
                raise RuntimeError("generation_failed") from outcome["error"]
            reasoning, _ = thinking.feed("", final=True)
            if reasoning:
                yield chunk({"reasoning_content": reasoning})
            if in_tool:
                raise ValueError("model produced an incomplete tool call")
            if pending.strip():
                yield chunk({"content": re.sub(r"^\s+", "", pending) if calls else pending})
            if tools and choice != "auto" and not calls:
                raise ValueError("model did not satisfy tool_choice")
            count = outcome.get("tokens", 0)
            yield chunk({}, "tool_calls" if calls else "length" if count >= body["max_tokens"] else "stop")
            usage = {"prompt_tokens": input_tokens, "completion_tokens": count, "total_tokens": input_tokens + count}
            yield {"id": ident, "object": "chat.completion.chunk", "created": int(time.time()), "model": self.model_id, "choices": [], "usage": usage}
        finally:
            stopped.set()
            worker.join(timeout=2)


def create_app(*, runtime: TransformersChatRuntime | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi import Request as FastAPIRequest
    except ImportError as exc:  # pragma: no cover
        raise ImportError("transformers LLM runtime requires fastapi") from exc
    globals()["FastAPIRequest"] = FastAPIRequest
    runtime = runtime or TransformersChatRuntime(
        os.environ.get("RYNMESH_TRANSFORMERS_MODEL_PATH", "/model/ModelScope/Qwen/Qwen3-14B"),
        model_id=os.environ.get("RYNMESH_TRANSFORMERS_MODEL_ID", "Qwen3-14B-Q4"),
    )
    app = FastAPI(title="Rynmesh loopback Transformers LLM", version="0.1")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return runtime.health()

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": runtime.model_id, "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def chat(request: FastAPIRequest) -> dict[str, Any]:
        body = await request.json()
        if hasattr(runtime, "stream_chat"):
            from fastapi.responses import StreamingResponse
            from starlette.concurrency import run_in_threadpool

            if body.get("model") not in {None, "", runtime.model_id}:
                raise HTTPException(status_code=404, detail="model_not_found")
            try:
                validated = validate_chat({k: v for k, v in body.items() if k not in {"model", "stream_options"}})
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

            def chunks():
                try:
                    for value in runtime.stream_chat(validated):
                        yield "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"
                    yield "data: [DONE]\n\n"
                except Exception:
                    yield 'data: {"error":{"message":"runtime_generation_failed","type":"api_error"}}\n\n'

            if validated.get("stream"):
                return StreamingResponse(chunks(), media_type="text/event-stream")

            def complete_chat():
                accumulator = ChatAccumulator()
                for value in runtime.stream_chat(validated):
                    accumulator.add(value)
                return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()), "model": runtime.model_id,
                        "choices": [{"index": 0, "message": accumulator.message(), "finish_reason": accumulator.finish_reason}], "usage": accumulator.usage}

            try:
                return await run_in_threadpool(complete_chat)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(503, str(exc)) from exc
        if body.get("stream"):
            raise HTTPException(status_code=400, detail="streaming_not_supported")
        if body.get("model") not in {None, "", runtime.model_id}:
            raise HTTPException(status_code=404, detail="model_not_found")
        try:
            result = runtime.complete(
                list(body.get("messages") or []), max_tokens=int(body.get("max_tokens") or 256)
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": runtime.model_id,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": result["text"]}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": result["input_tokens"],
                "completion_tokens": result["output_tokens"],
                "total_tokens": result["input_tokens"] + result["output_tokens"],
            },
            "rynmesh": {"duration_ms": result["duration_ms"]},
        }

    return app


def main() -> int:
    import uvicorn

    host = os.environ.get("RYNMESH_TRANSFORMERS_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("transformers runtime must bind to loopback")
    uvicorn.run(
        "rynmesh.llm_runtime_server:create_app",
        factory=True,
        host=host,
        port=int(os.environ.get("RYNMESH_TRANSFORMERS_PORT", "8080")),
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
