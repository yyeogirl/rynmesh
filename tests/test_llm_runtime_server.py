from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rynmesh.llm_runtime_server import create_app


@pytest.mark.parametrize("opening", ["", "<think>", "\n<think>"])
@pytest.mark.parametrize("chunk_size", [1, 3, 1000])
def test_qwen_thinking_separation_across_chunks(opening, chunk_size):
    from rynmesh.llm_package.chat import ChatAccumulator
    from rynmesh.llm_runtime_server import ThinkingSplitter
    text = opening + 'Consider <tool_call> as text.</think>Hello!'
    splitter = ThinkingSplitter(True)
    result = ChatAccumulator()
    for start in range(0, len(text), chunk_size):
        reasoning, answer = splitter.feed(text[start:start + chunk_size])
        result.add({"choices": [{"delta": {"reasoning_content": reasoning, "content": answer}}]})
    reasoning, answer = splitter.feed("", final=True)
    result.add({"choices": [{"delta": {"reasoning_content": reasoning, "content": answer}}]})
    assert result.message() == {"role": "assistant", "reasoning_content": "Consider <tool_call> as text.", "content": "Hello!"}


def test_qwen_thinking_truncation_and_disabled_mode():
    from rynmesh.llm_runtime_server import ThinkingSplitter
    splitter = ThinkingSplitter(True)
    first, _ = splitter.feed("<think>Unfinished")
    last, answer = splitter.feed("", final=True)
    assert first + last == "Unfinished" and answer == ""
    assert ThinkingSplitter(False).feed("literal <think> text") == ("", "literal <think> text")


def test_runtime_limits_allow_long_output_and_enforce_native_context(monkeypatch):
    from rynmesh.llm_runtime_server import TransformersChatRuntime
    runtime = TransformersChatRuntime("unused")
    runtime._model = SimpleNamespace(config=SimpleNamespace(max_position_embeddings=40960))
    monkeypatch.setenv("RYNMESH_TRANSFORMERS_CONTEXT", "40960")
    monkeypatch.setenv("RYNMESH_TRANSFORMERS_MAX_OUTPUT", "32768")
    monkeypatch.setenv("RYNMESH_TRANSFORMERS_TIMEOUT", "7200")
    runtime.validate_length(8192, 32768)
    with pytest.raises(ValueError, match="context window"):
        runtime.validate_length(8193, 32768)
    with pytest.raises(ValueError, match="runtime limit"):
        runtime.validate_length(1, 32769)
    monkeypatch.setenv("RYNMESH_TRANSFORMERS_CONTEXT", "131072")
    assert runtime.generation_limits() == (40960, 32768, 7200)


def test_qwen_template_accepts_openai_tool_only_assistant():
    from rynmesh.llm_runtime_server import template_messages

    original = [{"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "weather", "arguments": '{"city":"Shanghai"}'}}]}]
    cleaned = template_messages(original)
    assert cleaned[0]["content"] == ""
    assert cleaned[0]["tool_calls"][0]["function"]["arguments"] == {"city": "Shanghai"}
    assert original[0]["content"] is None


class FakeRuntime:
    model_id = "fake-qwen"

    def health(self):
        return {"ok": True, "model": self.model_id, "loaded": True}

    def complete(self, messages, *, max_tokens):
        assert messages[-1]["content"] == "你好"
        assert max_tokens == 12
        return {"text": "你好，节点正常。", "input_tokens": 3, "output_tokens": 6, "duration_ms": 9}


def test_openai_compatible_chat_surface():
    client = TestClient(create_app(runtime=FakeRuntime()))
    assert client.get("/health").json()["ok"] is True
    assert client.get("/v1/models").json()["data"][0]["id"] == "fake-qwen"
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fake-qwen", "messages": [{"role": "user", "content": "你好"}], "max_tokens": 12},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "你好，节点正常。"
    assert body["usage"]["completion_tokens"] == 6


def test_streaming_fails_explicitly():
    client = TestClient(create_app(runtime=FakeRuntime()))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fake-qwen", "messages": [{"role": "user", "content": "你好"}], "stream": True},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "streaming_not_supported"
