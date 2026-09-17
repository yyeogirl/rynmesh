# Local inference API

Rynmesh exposes a loopback API for projects and agents. Both local inference and
remote Rynmesh providers use the same model list and credentials. This is protocol
compatibility; it does not require an OpenAI or Anthropic account.

## Setup

1. Start the updated Rynmesh node. Configure a local model, or connect it to a
   network containing an updated, published provider.
2. Open **Services → Manage → API access → Configure API access**.
3. Create a project key and save it when displayed. Set its total output-token budget.
4. Set a **Model alias**, such as `qwen`, choose its **Routes to** model and save.
   Copy the API base URL and use `qwen` as the model in your project or agent.
   The mapping is stored on this node and survives restarts. You can edit its
   target later without changing your agents. An unavailable target returns 503;
   an alias never silently switches providers.

The default OpenAI base URL is `http://127.0.0.1:8791/v1`; the Anthropic base URL
is `http://127.0.0.1:8791`. The UI shows the node's configured port. Use the model
ID returned by `GET /v1/models`, which prefers your saved short aliases.
The original `local/<package>` and `peer/<provider-sha256>/<package>` IDs remain
accepted for existing clients. Aliases can be configured through the protected
local control API (`PUT /api/local/llm/model-aliases/qwen` with a JSON `target`
containing the original model ID); inference keys cannot manage aliases.

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8791/v1",
    api_key=os.environ["RYNMESH_API_KEY"],
)
model = client.models.list().data[0].id
response = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Hello"}],
    max_tokens=64,
)
print(response.choices[0].message.content)
```

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8791", api_key=os.environ["RYNMESH_API_KEY"])
with client.messages.stream(
    model=model,
    max_tokens=64,
    messages=[{"role": "user", "content": "Hello"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
```

## Supported contract

OpenAI Chat Completions also accepts Qwen's boolean `enable_thinking` option,
either at the top level or inside `chat_template_kwargs`. Both forms normalize
to the same setting and conflicting values are rejected. The bundled Qwen
runtime applies the switch to its chat template; reasoning is returned in
`reasoning_content`, separately from final `content`, including SSE deltas.
Assistant history can include `reasoning_content`; the Qwen runtime uses only
the final answer when rebuilding history, following the
[Qwen model guidance](https://huggingface.co/Qwen/Qwen3-14B).
When omitted, the bundled runtime retains its non-thinking default.

The Qwen3-14B V100 test deployment is configured for 32768 output tokens within
a 40960-token total context (prompt, history, tools and output combined).
For custom clients such as ZCode, configure the model's context limit as 40960
and output limit as 32768 rather than the client's generic defaults. The local
API treats the requested output maximum as a ceiling: it caps it to the selected
model and estimated remaining context before reserving the project quota and
dispatching the request. Prompts are preserved; a prompt that already exceeds
the estimated context still fails explicitly. The provider tokenizer remains
authoritative for the actual token count.
Clients must request their desired `max_tokens` / `max_output_tokens`; a large
limit does not force the model to produce that many tokens. Long generations
have a 7200-second provider deadline. The GPU deployment scripts set matching
runtime limits; its service manifest must also set `max_output_tokens=32768`,
`context_window=40960`, and `timeout_seconds=7200` before publishing.
Long prompts are prefetched in 512-token chunks to bound attention memory.
The generic runtime defaults remain 2048 output tokens / 4096 context unless
overridden by `RYNMESH_TRANSFORMERS_MAX_OUTPUT` / `RYNMESH_TRANSFORMERS_CONTEXT`.

| Endpoint | Supported |
| --- | --- |
| `GET /v1/models` | Configured healthy local model and online updated remote providers |
| `POST /v1/chat/completions` | Text messages, system/developer roles, tool calls/results, SSE, usage; one choice |
| `POST /v1/responses` | Stateless text/function-tool input and output; typed SSE events; send full history and `store=false` |
| `POST /v1/messages` | Text, system instructions, tool_use/tool_result, Messages SSE and usage |

Function tools are executed by the calling project/agent. Rynmesh returns their
arguments and accepts results in the next request. The underlying model must
support tool use. Strict schema enforcement, images/audio, hosted tools, stored
Responses/`previous_response_id`, and other unimplemented options return an
explicit error. This is a documented subset, not a claim of complete vendor API
or every agent-client compatibility.

Generic OpenAI/Ollama runtimes receive temperature/top_p/stop and function-tool
options. The bundled Qwen Transformers runtime supports temperature/top_p and
Qwen tool templates but rejects custom stop sequences. It parses complete Qwen
tool blocks before emitting a tool delta; ordinary text streams during generation.

## Routing and privacy

- Local calls share the provider's inference semaphore but work without publishing
  the model. They do not create remote orders or spend development Task Balance.
- Remote calls preserve existing discovery, signed encrypted envelopes, capacity
  control, task holds, settlement and encrypted-result retention.
- Remote calls through the local inference API always request strict ICE/UDP P2P.
  The local loopback HTTP API is only the project/agent entry point. It does not
  select HTTP for node-to-node inference. Services UI also defaults to P2P.
  Explicit strict P2P cannot be overridden by HTTP or forced-relay environment
  settings. ICE failure produces an error with no HTTP, TURN or blob-relay fallback.
  Registry signaling and STUN establish the connection; encrypted prompts and
  results travel directly between the two nodes. Real incremental streaming is
  supported over that same P2P connection.
  Each delta is independently signed and encrypted and bound to the task, provider,
  service and sequence. The final response is also verified against the task ID.
- The lower-level order API retains explicit HTTP/relay modes for diagnostics;
  those modes are not used by the local inference API. A configured blob relay
  supports nonstreaming requests only.
- Both nodes must be updated for structured chat. Old published records lacking
  `chat_protocol=rynmesh.chat.v1` are excluded from the API model list.
- Inference keys are accepted only on loopback, with forwarded and cross-site
  browser requests blocked. Keys cannot administer the node. The normal local
  control authentication continues to protect key creation/revocation.
- Key hashes and budget counters are kept in `llm/inference-keys.sqlite3`; plaintext
  keys are returned only on creation. Each key permits up to two concurrent calls.
- The project output-token budget reserves `max_tokens` and settles actual output
  usage. Failed/interrupted calls with unknown usage conservatively retain the
  reservation. This is an output-token allowance, not a monetary spending limit.
- Remote usage additionally uses the node's existing development Task Balance.
  Request/response bodies and key values are not written into API diagnostics.
- Disconnect cancellation is best effort. Adapters close their upstream response;
  the bundled runtime signals its generation stopping criterion. Remote consumer
  holds are released through the existing signed cancellation route.

## Verification

Run focused backend tests and frontend verification:

```text
python -m pytest tests/test_inference_api.py tests/test_llm_package.py tests/test_llm_hardening.py tests/test_llm_runtime_server.py tests/test_peer_http_auth.py tests/test_video_package.py -q
ruff check rynmesh/llm_package rynmesh/llm_runtime_server.py rynmesh/provider_gateway.py tests/test_inference_api.py scripts/inference_api_acceptance.py
cd webapp
npm run build
npm test -- --run src/screens/Services.llm.test.tsx
```

For real-model acceptance, install the optional `openai` and `anthropic` SDKs in
the test environment and run:

```text
python scripts/inference_api_acceptance.py --base http://127.0.0.1:8791 --output report.json
```

The script creates and then revokes a temporary key, tests multi-turn recall,
actual streaming, and function-tool round trips using official SDKs. It writes
sanitized results. It expects a model capable of the described text/tool tasks.
For a desktop node with a per-launch control token, use the UI for key provisioning
or adapt the control request helper to supply that token from the environment.
