"""Loopback OpenAI-compatible runtime for local Transformers models.

This exists for GPU providers that cannot run the bundled Ollama/llama.cpp
binary but already have official Hugging Face/ModelScope weights available.
The process must stay on loopback; remote Consumers use the encrypted Rynmesh
LLM package instead of reaching this runtime directly.
Weights are validated at startup; inference is admitted one request at a time.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any


class RuntimeBusyError(RuntimeError):
    pass


class TransformersChatRuntime:
    def __init__(self, model_path: str, *, model_id: str = "Qwen3-14B-Q4") -> None:
        self.model_path = model_path
        self.model_id = model_id
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._load_lock = threading.Lock()
        self._generation_lock = threading.Lock()
        self._load_error = ""

    def load(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            if not (Path(self.model_path) / "config.json").is_file():
                raise RuntimeError("model_configuration_missing")
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
            tokenizer = AutoTokenizer.from_pretrained(
                self.model_path, local_files_only=True, trust_remote_code=False
            )
            model = AutoModelForCausalLM.from_pretrained(
                self.model_path,
                local_files_only=True,
                trust_remote_code=False,
                quantization_config=quantization,
                device_map={"": 0},
                torch_dtype=torch.float16,
                low_cpu_mem_usage=True,
            )
            model.eval()
            self._tokenizer = tokenizer
            self._model = model
            self._load_error = ""

    def health(self) -> dict[str, Any]:
        try:
            import torch

            return {
                "ok": self._model is not None and torch.cuda.is_available(),
                "model": self.model_id,
                "loaded": self._model is not None,
                "torch": torch.__version__,
                "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "",
                "error": self._load_error or ("" if self._model is not None else "model_not_loaded"),
            }
        except ImportError:
            return {"ok": False, "model": self.model_id, "error": "torch_not_installed"}

    def complete(self, messages: list[dict[str, str]], *, max_tokens: int) -> dict[str, Any]:
        # Admit before loading or allocating CUDA tensors. Concurrent requests
        # must not accumulate GPU allocations while waiting for generation.
        if not self._generation_lock.acquire(blocking=False):
            raise RuntimeBusyError("runtime_busy")
        try:
            return self._complete(messages, max_tokens=max_tokens)
        finally:
            self._generation_lock.release()

    def _complete(self, messages: list[dict[str, str]], *, max_tokens: int) -> dict[str, Any]:
        self.load()
        import torch

        cleaned = [
            {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
            for item in messages
            if isinstance(item, dict)
        ]
        if not cleaned or not cleaned[-1]["content"]:
            raise ValueError("messages_required")
        max_tokens = max(1, min(2048, int(max_tokens or 256)))
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
        started = time.monotonic()
        with torch.inference_mode():
            output = self._model.generate(
                **encoded,
                max_new_tokens=max_tokens,
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


def create_app(*, runtime: TransformersChatRuntime | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi import Request as FastAPIRequest
    except ImportError as exc:  # pragma: no cover
        raise ImportError("transformers LLM runtime requires fastapi") from exc
    globals()["FastAPIRequest"] = FastAPIRequest
    from starlette.concurrency import run_in_threadpool

    runtime = runtime or TransformersChatRuntime(
        os.environ.get("RYNMESH_TRANSFORMERS_MODEL_PATH", "/model/ModelScope/Qwen/Qwen3-14B"),
        model_id=os.environ.get("RYNMESH_TRANSFORMERS_MODEL_ID", "Qwen3-14B-Q4"),
    )
    @asynccontextmanager
    async def lifespan(_app):
        # Validate weights/dependencies before advertising the model. Failure
        # leaves a live, degraded process that can report its readiness state.
        try:
            await run_in_threadpool(runtime.load)
        except Exception as exc:
            runtime._load_error = f"model_load_failed:{type(exc).__name__}"
        yield

    app = FastAPI(title="Rynmesh loopback Transformers LLM", version="0.1", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return runtime.health()

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        if not runtime.health().get("ok"):
            raise HTTPException(status_code=503, detail="model_not_ready")
        return {"object": "list", "data": [{"id": runtime.model_id, "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def chat(request: FastAPIRequest) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request_body_must_be_an_object")
        if body.get("stream"):
            raise HTTPException(status_code=400, detail="streaming_not_supported")
        if body.get("model") not in {None, "", runtime.model_id}:
            raise HTTPException(status_code=404, detail="model_not_found")
        try:
            result = await run_in_threadpool(
                runtime.complete, list(body.get("messages") or []),
                max_tokens=int(body.get("max_tokens") or 256),
            )
        except RuntimeBusyError as exc:
            raise HTTPException(status_code=503, detail="runtime_busy") from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid_completion_request") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="runtime_unavailable") from exc
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
