from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package.api import InferenceKeys, install_inference_api
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.routes import ProviderService, _expires, _open_provider_response
from rynmesh.llm_package.streaming import events
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import (
    TaskOrderStore,
    TaskProtocolError,
    open_task,
    seal_task,
)
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class ChatAdapter:
    calls = 0

    def health(self):
        return {"ok": True}

    def chat(self, body, *, task_id, timeout_s, on_event=None):
        self.calls += 1
        self.last_body = body
        message = {"role": "assistant", "content": "Hello world"}
        finish = "stop"
        deltas = [{"content": "Hello "}, {"content": "world"}]
        if body.get("tools") and body["messages"][-1]["role"] != "tool":
            function = {"name": "weather", "arguments": '{"city":"Shanghai"}'}
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": function}]}
            finish = "tool_calls"
            deltas = [{"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "weather", "arguments": '{"city":'}}]},
                      {"tool_calls": [{"index": 0, "function": {"arguments": '"Shanghai"}'}}]}]
        if body.get("enable_thinking"):
            message["reasoning_content"] = "Test reasoning"
            deltas.insert(0, {"reasoning_content": "Test reasoning"})
        if body.get("stream") and on_event:
            for delta in deltas:
                on_event({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            on_event({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            on_event({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})
        return {"text": message["content"] or "", "message": message, "finish_reason": finish,
                "input_tokens": 7, "output_tokens": 3, "duration_ms": 4}

    def cancel(self, task_id):
        return True


@pytest.fixture
def api(tmp_path):
    adapter = ChatAdapter()
    manager = SimpleNamespace(adapter=adapter, manifest=LLMPackageManifest(package_id="test", mode="openai_compatible", public_model_alias="test-model", base_url="http://127.0.0.1:1"),
                              _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0)
    app = FastAPI()
    install_inference_api(app, home=tmp_path, store=SimpleNamespace(peer_id="self"), active_manager=lambda: manager,
                          discover=lambda _: [], execute_order=None, cancel_order=None)
    client = TestClient(app)
    key = client.post("/api/local/llm/api-keys", json={"name": "agent", "output_token_limit": 1000}).json()
    client.headers["Authorization"] = "Bearer " + key["key"]
    return client, adapter, key


def test_key_auth_loopback_revocation_and_quota(api, tmp_path):
    client, adapter, key = api
    assert client.get("/v1/models").json()["data"][0]["id"] == "local/test"
    assert client.get("/v1/models", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get("/v1/models", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    assert client.get("/v1/models", headers={"Host": "attacker.example"}).status_code == 403
    assert client.get("/v1/models", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert key["key"] not in (tmp_path / "llm" / "inference-keys.sqlite3").read_bytes().decode(errors="ignore")
    assert InferenceKeys(tmp_path).authenticate(key["key"]) == key["id"]
    small = client.post("/api/local/llm/api-keys", json={"name": "small", "output_token_limit": 4}).json()
    payload = {"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4}
    assert client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer " + small["key"]}).status_code == 200
    assert client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer " + small["key"]}).status_code == 429
    client.delete("/api/local/llm/api-keys/" + key["id"])
    assert client.get("/v1/models").status_code == 401
    assert adapter.calls == 1


@pytest.mark.parametrize("protocol,path", [("chat", "chat/completions"), ("responses", "responses"), ("messages", "messages")])
@pytest.mark.parametrize("stream", [False, True])
def test_three_protocols_tool_call_round_trip(api, protocol, path, stream):
    client, adapter, _ = api
    assert client.put("/api/local/llm/model-aliases/qwen", json={"target": "local/test"}).status_code == 200
    body = {"model": "qwen", "stream": stream}
    if protocol == "responses":
        body.update(input="weather?", max_output_tokens=30, tools=[{"type": "function", "name": "weather", "parameters": {"type": "object"}}])
    elif protocol == "messages":
        body.update(messages=[{"role": "user", "content": "weather?"}], max_tokens=30, tools=[{"name": "weather", "input_schema": {"type": "object"}}])
    else:
        body.update(messages=[{"role": "user", "content": "weather?"}], max_tokens=30, tools=[{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}])
    response = client.post("/v1/" + path, json=body)
    assert response.status_code == 200, response.text
    if stream:
        assert "text/event-stream" in response.headers["content-type"]
        if protocol == "chat":
            assert '"arguments": "{\\"city\\":"' in response.text
            assert response.text.endswith("data: [DONE]\n\n")
        else:
            expected = "response.function_call_arguments.delta" if protocol == "responses" else "input_json_delta"
            assert expected in response.text
            assert "response.completed" in response.text if protocol == "responses" else "message_stop" in response.text
        body["stream"] = False
        response = client.post("/v1/" + path, json=body)
    result = response.json()
    assert result["model"] == "qwen"
    if protocol == "responses":
        call = result["output"][0]
        body["input"] = [{"role": "user", "content": "weather?"}, call, {"type": "function_call_output", "call_id": call["call_id"], "output": "Sunny"}]
    elif protocol == "messages":
        call = result["content"][0]
        body["messages"] += [{"role": "assistant", "content": result["content"]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "Sunny"}]}]
    else:
        message = result["choices"][0]["message"]
        body["messages"] += [message, {"role": "tool", "tool_call_id": message["tool_calls"][0]["id"], "content": "Sunny"}]
    response = client.post("/v1/" + path, json=body)
    assert response.status_code == 200, response.text
    assert "Hello world" in response.text
    assert adapter.last_body["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"}


def test_unsupported_features_fail_before_inference(api):
    client, adapter, _ = api
    for values in ({"previous_response_id": "resp_old"}, {"store": True}, {"tools": [{"type": "web_search"}]}):
        response = client.post("/v1/responses", json={"model": "local/test", "input": "hi", **values})
        assert response.status_code == 400
    assert adapter.calls == 0


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("nested", [True, False])
@pytest.mark.parametrize("stream", [True, False])
def test_qwen_thinking_option(api, enabled, nested, stream):
    client, adapter, _ = api
    flag = {"enable_thinking": enabled}
    body = {"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64, "stream": stream}
    body.update({"chat_template_kwargs": flag} if nested else flag)
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200, response.text
    assert adapter.last_body["enable_thinking"] is enabled
    assert "chat_template_kwargs" not in adapter.last_body
    assert ("reasoning_content" in response.text) is enabled
    if stream:
        assert response.text.endswith("data: [DONE]\n\n")
    else:
        assert response.json()["choices"][0]["message"]["content"] == "Hello world"


def test_invalid_thinking_options_fail_before_inference(api):
    client, adapter, _ = api
    for options in [{"enable_thinking": "false"}, {"enable_thinking": 1},
                    {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": True}},
                    {"chat_template_kwargs": {"unknown": True}}, {"chat_template_kwargs": []}]:
        response = client.post("/v1/chat/completions", json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], **options})
        assert response.status_code == 400
    assert adapter.calls == 0


@pytest.mark.parametrize("protocol,path", [("chat", "chat/completions"), ("responses", "responses"), ("messages", "messages")])
def test_large_client_output_budget_is_capped_before_quota_reservation(api, protocol, path):
    client, adapter, key = api
    body = {"model": "local/test"}
    if protocol == "responses":
        body.update(input="hi", max_output_tokens=128000)
    else:
        body.update(messages=[{"role": "user", "content": "hi"}], max_tokens=128000)
    response = client.post("/v1/" + path, json=body)
    assert response.status_code == 200, response.text
    assert adapter.last_body["max_tokens"] == 512
    # The project's 1000-token quota covers the effective 512-token reservation.
    access = client.get("/api/local/llm/api-access").json()
    project = next(k for k in access["keys"] if k["id"] == key["id"])
    assert project["used_output_tokens"] == 3


def test_output_budget_fits_remaining_context_without_truncating_prompt(api):
    client, adapter, _ = api
    text = "x" * 15000
    body = {"model": "local/test", "messages": [{"role": "user", "content": text}], "max_tokens": 128000}
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200, response.text
    assert 0 < adapter.last_body["max_tokens"] < 512
    assert (len(json.dumps(adapter.last_body, ensure_ascii=False)) + 3) // 4 + adapter.last_body["max_tokens"] <= 4096
    assert adapter.last_body["messages"][0]["content"] == text
    body["messages"][0]["content"] = "x" * 17000
    assert client.post("/v1/chat/completions", json=body).status_code == 400
    assert adapter.calls == 1


def test_alias_validation_and_persistence(api, tmp_path):
    client, _, _ = api
    for name, target in [("bad name", "local/test"), ("x" * 65, "local/test"), ("qwen", "missing")]:
        assert client.put("/api/local/llm/model-aliases/" + name, json={"target": target}).status_code == 400
    assert client.put("/api/local/llm/model-aliases/qwen", json={"target": "local/test"}).status_code == 200
    assert InferenceKeys(tmp_path).aliases() == {"qwen": "local/test"}
    assert [m["id"] for m in client.get("/v1/models").json()["data"]] == ["qwen"]
    access = client.get("/api/local/llm/api-access").json()
    assert access["aliases"] == {"qwen": "local/test"}
    assert access["targets"][0]["id"] == "local/test"
    # Previously configured clients still accept the canonical ID.
    assert client.post("/v1/chat/completions", json={"model": "local/test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}).status_code == 200


def test_alias_pins_remote_provider_when_discovery_changes(tmp_path):
    providers = [{"peer_id": name, "online": True, "chat_protocol": "rynmesh.chat.v1",
                  "service": {"package_id": "qwen", "model_alias": "Qwen", "max_output_tokens": 64, "context_window": 4096}}
                 for name in ["provider-a", "provider-b"]]
    calls = []

    async def execute(order, emit):
        calls.append(order)
        return {"state": "succeeded", **ChatAdapter().chat(order["chat"], task_id=order["task_id"], timeout_s=10)}

    def client_for_node():
        app = FastAPI()
        install_inference_api(app, home=tmp_path, store=SimpleNamespace(peer_id="self"), active_manager=lambda: None,
                              discover=lambda _: providers, execute_order=execute, cancel_order=lambda _: None)
        return TestClient(app)

    client = client_for_node()
    key = client.post("/api/local/llm/api-keys", json={"name": "agent"}).json()["key"]
    target = client.get("/api/local/llm/api-access").json()["targets"][0]["id"]
    assert client.put("/api/local/llm/model-aliases/qwen", json={"target": target}).status_code == 200
    client = client_for_node()  # A restarted node reads the same saved mapping and key.
    client.headers["Authorization"] = "Bearer " + key
    payload = {"model": "qwen", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    providers.reverse()
    result = client.post("/v1/chat/completions", json=payload)
    assert result.status_code == 200 and result.json()["model"] == "qwen"
    assert calls[0]["provider_peer_id"] == "provider-a"
    assert calls[0]["transport"] == "p2p"
    providers.pop()
    assert client.post("/v1/chat/completions", json=payload).status_code == 503
    assert len(calls) == 1
    assert "qwen" not in [m["id"] for m in client.get("/v1/models").json()["data"]]
    # Switching a provider is an explicit local configuration change.
    target = client.get("/api/local/llm/api-access").json()["targets"][0]["id"]
    assert client.put("/api/local/llm/model-aliases/qwen", json={"target": target}).status_code == 200
    assert client.post("/v1/chat/completions", json=payload).status_code == 200
    assert calls[-1]["provider_peer_id"] == "provider-b"


def test_provider_keeps_full_messages_and_seals_each_delta(tmp_path):
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "net")
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "net")
    pk = peer_box.load_or_create_messaging_key(tmp_path / "pk")
    ck = peer_box.load_or_create_messaging_key(tmp_path / "ck")
    adapter = ChatAdapter()
    service = ProviderService(manifest=LLMPackageManifest(package_id="svc", mode="openai_compatible", public_model_alias="alias", base_url="http://127.0.0.1:1"),
                              adapter=adapter, store=provider, task_store=TaskOrderStore(tmp_path / "orders"), balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=pk)
    chat = {"messages": [{"role": "system", "content": "SECRET SYSTEM"}, {"role": "user", "content": "SECRET USER"}], "stream": True, "max_tokens": 20, "enable_thinking": True}
    request = seal_task(body={"task_id": "test", "service_id": "svc", "chat": chat, "max_tokens": 20, "max_amount": 1, "reply_messaging_pub": peer_box.public_key_b64(ck)},
                        task_id="test", kind="llm_request", sender_peer_id=consumer.peer_id, recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes, recipient_messaging_pub=peer_box.public_key_b64(pk), expires_at=_expires(300)).to_dict()
    deltas = []
    result = service.handle(request, deltas.append)
    for index, envelope in enumerate(deltas):
        assert "Hello" not in json.dumps(envelope)
        _, opened = open_task(envelope, recipient_peer_id=consumer.peer_id, recipient_messaging_key=ck, expected_kind="llm_stream")
        assert opened["sequence"] == index
    _, opened = _open_provider_response(result, recipient_peer_id=consumer.peer_id, messaging_key=ck, task_id="test", provider_peer_id=provider.peer_id, service_id="svc")
    assert opened["message"]["content"] == "Hello world"
    assert adapter.last_body["messages"] == chat["messages"]
    assert adapter.last_body["enable_thinking"] is True
    assert opened["message"]["reasoning_content"] == "Test reasoning"
    with pytest.raises(TaskProtocolError, match="task mismatch"):
        _open_provider_response(result, recipient_peer_id=consumer.peer_id, messaging_key=ck, task_id="other", provider_peer_id=provider.peer_id, service_id="svc")
    disk = "".join(path.read_text() for path in tmp_path.rglob("*.json"))
    assert "SECRET" not in disk and "Hello world" not in disk


def test_stream_bridge_delivers_before_completion_and_cancels():
    async def scenario():
        emitted = threading.Event()
        cancelled = threading.Event()

        def worker(emit):
            emit({"content": "early"})
            emitted.set()
            assert cancelled.wait(3)
            return {"done": True}

        async def run(emit):
            return await asyncio.to_thread(worker, emit)

        stream = events(run, cancelled.set)
        kind, value = await anext(stream)
        assert kind == "event" and value["content"] == "early"
        await stream.aclose()
        assert await asyncio.to_thread(cancelled.wait, 2)
    asyncio.run(scenario())


def test_inference_key_cannot_manage_node(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "desktop-secret")
    monkeypatch.delenv("RYNMESH_LLM_SERVICE_MANIFEST", raising=False)
    from rynmesh.peer_http import create_app
    app = create_app(RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net"))
    with TestClient(app) as client:
        denied = client.post("/api/local/llm/api-keys", json={"name": "agent"})
        assert denied.status_code == 403
        key = client.post("/api/local/llm/api-keys", json={"name": "agent"}, headers={"x-ryn-local-token": "desktop-secret"}).json()["key"]
        assert client.get("/v1/models", headers={"Authorization": "Bearer " + key}).status_code == 200
        assert client.get("/api/local/node/status", headers={"Authorization": "Bearer " + key}).status_code == 403
        assert client.put("/api/local/llm/model-aliases/qwen", json={"target": "local/test"}, headers={"Authorization": "Bearer " + key}).status_code == 403


def test_p2p_transmits_incremental_events_before_final(monkeypatch):
    from rynmesh.llm_package.p2p import consumer_exchange, provider_exchange

    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_PUBLIC", "0")
    monkeypatch.setenv("RYNMESH_P2P_REQUIRE_DISTINCT_PUBLIC", "0")
    monkeypatch.setenv("RYNMESH_P2P_STUN", "")

    async def scenario():
        loop = asyncio.get_running_loop()
        answer = loop.create_future()
        jobs = []
        seen = []

        def handler(request, emit):
            assert request == {"request": True}
            for i in range(3):
                emit({"payload": {"kind": "llm_stream"}, "sequence": i})
            return {"payload": {"kind": "llm_response"}, "done": True}

        async def offer(signal):
            jobs.append(asyncio.create_task(provider_exchange(offer=signal, publish_answer=answer.set_result,
                                                             handle_request=None, handle_stream_request=handler, timeout_s=10)))
            return await answer

        result, _ = await consumer_exchange(signed_request={"request": True}, publish_offer=offer,
                                            timeout_s=10, on_event=lambda event: seen.append(event["sequence"]))
        assert result["done"] and seen == [0, 1, 2]
        await asyncio.gather(*jobs)
    asyncio.run(scenario())


def test_order_reads_share_writer_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    orders = TaskOrderStore(tmp_path / "orders")
    orders.claim(task_id="concurrent", bindings={"consumer_peer_id": "p"})

    def update():
        for i in range(100):
            orders.checkpoint(task_id="concurrent", metadata={"duration_ms": i})

    def read():
        for _ in range(100):
            assert orders.get("concurrent")["task_id"] == "concurrent"

    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(update), pool.submit(read), pool.submit(read), pool.submit(read)]
        for job in jobs:
            job.result()


def test_responses_stream_keeps_tool_first_output_indexes():
    from rynmesh.llm_package.api_formats import StreamFormat
    from rynmesh.llm_package.chat import completion

    formatter = StreamFormat("responses", "local/test", "r1")
    formatter.delta({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "weather", "arguments": "{}"}}]}}]})
    formatter.delta({"choices": [{"delta": {"content": "Checking"}}]})
    final = completion({"input_tokens": 1, "output_tokens": 2, "message": {"role": "assistant", "content": "Checking", "tool_calls": [{"id": "c1", "function": {"name": "weather", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}, "local/test", "r1")
    last = formatter.finish(final)[-1]
    payload = json.loads(last.split("data: ", 1)[1])
    assert [item["type"] for item in payload["response"]["output"]] == ["function_call", "message"]
