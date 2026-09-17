"""Verify SDK stream close cancels a remote task and releases its balance hold."""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path


def main():
    from openai import OpenAI

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    def control(path, body=None, method=None):
        request = urllib.request.Request(args.base + path, data=json.dumps(body).encode() if body else None,
                                         headers={"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    previous = {order["task_id"] for order in control("/api/local/llm/orders")["orders"]}
    key = control("/api/local/llm/api-keys", {"name": "Cancellation acceptance", "output_token_limit": 1000})
    evidence = {}
    try:
        client = OpenAI(base_url=args.base + "/v1", api_key=key["key"], timeout=180, max_retries=0)
        model = client.models.list().data[0].id
        stream = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Write a very long story about a forest. Keep writing for at least 1000 words."}], max_tokens=512, stream=True)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                break
        started = time.monotonic()
        stream.close()
        task = None
        while time.monotonic() - started < 20:
            tasks = control("/api/local/llm/orders")["orders"]
            task = next((item for item in tasks if item["task_id"] not in previous), None)
            if task and task["state"] == "cancelled":
                break
            time.sleep(0.25)
        balance = control("/api/local/task-balance")
        assert task and task["state"] == "cancelled", task
        assert balance["held"] == 0, balance
        evidence = {"passed": True, "task_id": task["task_id"], "state": task["state"], "held": balance["held"], "cancellation_ms": int((time.monotonic() - started)*1000)}
        print(json.dumps(evidence), flush=True)
    finally:
        control("/api/local/llm/api-keys/" + key["id"], method="DELETE")
        Path(args.output).write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
