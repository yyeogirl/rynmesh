"""Exercise a running loopback node with the official OpenAI and Anthropic SDKs.

Creates a temporary project key, revokes it afterwards, writes only sanitized evidence.
Usage: python scripts/inference_api_acceptance.py --base http://127.0.0.1:18796 --output report.json
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path


def main():
    import anthropic
    import openai

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8791")
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="")
    args = parser.parse_args()

    def control(path, body=None, method=None):
        request = urllib.request.Request(args.base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    key = control("/api/local/llm/api-keys", {"name": "API acceptance (temporary)", "output_token_limit": 10000})
    evidence = {"checks": [], "base": args.base}
    try:
        client = openai.OpenAI(base_url=args.base + "/v1", api_key=key["key"], timeout=180, max_retries=0)
        claude = anthropic.Anthropic(base_url=args.base, api_key=key["key"], timeout=180, max_retries=0)
        models = client.models.list()
        model = args.model or models.data[0].id
        evidence["model"] = model

        def record(name, **data):
            item = {"name": name, "passed": True, **data}
            evidence["checks"].append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)

        messages = [{"role": "system", "content": "Reply briefly. Follow the user's instructions."},
                    {"role": "user", "content": "Remember the code word ORCHID."},
                    {"role": "assistant", "content": "I will remember ORCHID."},
                    {"role": "user", "content": "What is the code word?"}]
        result = client.chat.completions.create(model=model, messages=messages, max_tokens=48)
        assert "ORCHID" in result.choices[0].message.content.upper()
        record("openai_multiturn", usage=result.usage.model_dump())
        start = time.monotonic()
        chunks = []
        first = None
        for chunk in client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Count from 1 to 20, separated by spaces."}], max_tokens=128, stream=True, stream_options={"include_usage": True}):
            if chunk.choices and chunk.choices[0].delta.content:
                first = first or time.monotonic()
                chunks.append(chunk.choices[0].delta.content)
        end = time.monotonic()
        assert len(chunks) > 1 and "20" in "".join(chunks)
        record("openai_real_stream", content_events=len(chunks), first_content_ms=int((first-start)*1000), total_ms=int((end-start)*1000))
        tools = [{"type": "function", "function": {"name": "get_weather", "description": "Get the current weather for a city", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
        history = [{"role": "user", "content": "Use get_weather to check the weather in Shanghai."}]
        result = client.chat.completions.create(model=model, messages=history, tools=tools, tool_choice="required", max_tokens=200)
        calls = result.choices[0].message.tool_calls
        assert calls and calls[0].function.name == "get_weather"
        assert json.loads(calls[0].function.arguments)["city"]
        history.append(result.choices[0].message.model_dump(exclude_none=True))
        for call in calls:
            history.append({"role": "tool", "tool_call_id": call.id, "content": '{"temperature_c":23,"conditions":"sunny"}'})
        followup = client.chat.completions.create(model=model, messages=history, tools=tools, max_tokens=100)
        assert "23" in followup.choices[0].message.content
        record("openai_agent_tool_roundtrip", tool=calls[0].function.name)
        response = client.responses.create(model=model, input="Reply with exactly READY", max_output_tokens=40, store=False)
        assert "READY" in response.output_text
        record("responses_nonstream")
        types = []
        for event in client.responses.create(model=model, input="Count from 1 to 5", max_output_tokens=64, store=False, stream=True):
            types.append(event.type)
        assert "response.output_text.delta" in types and types[-1] == "response.completed"
        record("responses_stream", events=len(types))
        message = claude.messages.create(model=model, messages=[{"role": "user", "content": "Reply with exactly READY"}], max_tokens=40)
        assert "READY" in message.content[0].text
        record("anthropic_nonstream")
        with claude.messages.stream(model=model, messages=[{"role": "user", "content": "Count from 1 to 5"}], max_tokens=64) as stream:
            text = "".join(stream.text_stream)
            final = stream.get_final_message()
        assert "5" in text and final.stop_reason == "end_turn"
        record("anthropic_stream", usage=final.usage.model_dump())
        response_tools = [{"type": "function", **tools[0]["function"]}]
        response_events = list(client.responses.create(model=model, input=history[0]["content"], tools=response_tools,
                                                       tool_choice="required", max_output_tokens=200, store=False, stream=True))
        final_response = response_events[-1].response
        response_calls = [item for item in final_response.output if item.type == "function_call"]
        assert response_calls and any(event.type == "response.function_call_arguments.delta" for event in response_events)
        response_history = [{"role": "user", "content": history[0]["content"]}]
        response_history += [item.model_dump(exclude_none=True) for item in final_response.output]
        response_history += [{"type": "function_call_output", "call_id": item.call_id, "output": '{"temperature_c":23}'} for item in response_calls]
        result = client.responses.create(model=model, input=response_history, tools=response_tools, max_output_tokens=100, store=False)
        assert "23" in result.output_text
        record("responses_streamed_tool_roundtrip")
        claude_tools = [{"name": "get_weather", "description": "Get current weather", "input_schema": tools[0]["function"]["parameters"]}]
        claude_history = [{"role": "user", "content": history[0]["content"]}]
        with claude.messages.stream(model=model, messages=claude_history, tools=claude_tools, tool_choice={"type": "any"}, max_tokens=200) as stream:
            tool_message = stream.get_final_message()
        claude_calls = [item for item in tool_message.content if item.type == "tool_use"]
        assert claude_calls and tool_message.stop_reason == "tool_use"
        claude_history += [{"role": "assistant", "content": [item.model_dump(exclude_none=True) for item in tool_message.content]},
                           {"role": "user", "content": [{"type": "tool_result", "tool_use_id": item.id, "content": '{"temperature_c":23}'} for item in claude_calls]}]
        result = claude.messages.create(model=model, messages=claude_history, tools=claude_tools, max_tokens=100)
        assert "23" in "".join(item.text for item in result.content if item.type == "text")
        record("anthropic_streamed_tool_roundtrip")
        evidence["orders"] = control("/api/local/llm/orders")["orders"][:12]
        # Local metadata endpoint does not return prompt/output content.
        evidence["passed"] = True
    except Exception as exc:
        evidence["passed"] = False
        evidence["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        control("/api/local/llm/api-keys/" + key["id"], method="DELETE")
        Path(args.output).write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
