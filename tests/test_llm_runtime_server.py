from fastapi.testclient import TestClient

from rynmesh.llm_runtime_server import create_app


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
