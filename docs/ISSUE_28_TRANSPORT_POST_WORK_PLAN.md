# Issue #28 work plan: route LLM peer POSTs through Transport

Status: implemented and formally accepted, 2026-09-02
Issue: https://github.com/yeogirlyun/rynmesh/issues/28
Recommended order: complete before Private AI streaming work (#23)

## 中文执行摘要

当前普通 Peer GET/download 已统一经过 `Transport`，但 LLM 的任务提交、结算、
取消仍在 `rynmesh/llm_package/routes.py` 中直接调用 `urllib`。本任务要给所有
Transport 实现补齐有大小限制的 POST 请求能力，再通过 `HttpPeerClient` 提供
JSON POST，最后迁移 LLM 调用点。

这不是简单替换一行代码。必须同时覆盖默认 HTTPS、fronted HTTPS、CDN
WebSocket 和插件 Transport，保留网络密钥、TLS/profile、代理、禁止重定向、
2 MiB 响应上限和统一错误分类。完成后，LLM 模块不得再自行执行 Peer HTTP
POST。

## 1. Problem

`HttpPeerClient` already routes peer GET and file-download traffic through the
pluggable transport seam. LLM peer operations do not. They use the private
`_peer_post_json` helper, which builds an `urllib.request.Request` and calls
`urllib.request.urlopen` directly.

The bypass has four consequences:

1. configured TLS, proxy, fronting, CDN-WebSocket, and plugin transports do not
   apply to LLM service traffic;
2. redirect suppression and transport error normalization are not shared;
3. network behavior is implemented twice;
4. future direct-response streaming would otherwise create a third transport
   path.

Current call flow:

```text
LLM routes
  -> _peer_post_json
     -> urllib.request.urlopen

Other peer calls
  -> HttpPeerClient
     -> Transport
        -> StdlibHttpsTransport | FrontedHttpsTransport |
           CdnWebSocketTransport | registered plugin
```

Target call flow:

```text
LLM routes
  -> HttpPeerClient.post_json
     -> Transport.post_bytes
        -> active Transport implementation
```

## 2. Goals

- Add a bounded POST operation to the public `Transport` protocol.
- Implement the operation for every bundled Transport.
- Add a typed JSON POST helper to `HttpPeerClient`.
- Route LLM task submission, settlement, and cancellation through that helper.
- Preserve the existing 2 MiB LLM peer-response limit.
- Preserve the shared network-key header and configured transport profile.
- Preserve redirect suppression and fail-closed response parsing.
- Normalize transport failures into the existing peer/task error model.
- Keep the synchronous wire API compatible with the existing
  `asyncio.to_thread` call sites.

## 3. Non-goals

- Do not implement token streaming in this issue.
- Do not change the LLM task envelope, encryption, signatures, or settlement
  schema.
- Do not change transport selection or fallback policy.
- Do not add redirects.
- Do not add a generic unrestricted HTTP client.
- Do not log request or response bodies.
- Do not change Registry or Relay payload storage.

## 4. Proposed API

### 4.1 Transport protocol

Add one bounded method to `rynmesh.transport.Transport`:

```python
def post_bytes(
    self,
    url: str,
    body: bytes,
    *,
    timeout_s: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> bytes: ...
```

Rules:

- `body` is already serialized by the caller.
- The transport must not inspect or log it.
- Read at most `max_bytes + 1` response bytes.
- Raise `TransportError(reason="too_large")` when the response exceeds the
  bound.
- Map connection, TLS, timeout, malformed tunneled response, and HTTP status
  failures to `TransportError`.
- Merge caller headers after profile/network-key headers, matching GET
  behavior.
- Never follow redirects.

A general `request_bytes(method=...)` API is not required for this issue. POST
is the only missing operation and a narrow interface reduces accidental use.

### 4.2 HttpPeerClient

Add a public helper with an explicit response bound:

```python
def post_json(
    self,
    path: str,
    payload: dict[str, Any],
    *,
    max_bytes: int = MAX_JSON_BYTES,
) -> dict[str, Any]: ...
```

Behavior:

1. serialize with compact JSON and UTF-8;
2. send `Content-Type: application/json`;
3. call `transport.post_bytes`;
4. decode UTF-8 and JSON;
5. require a JSON object response;
6. map `too_large` to `PeerTransportError("peer_response_too_large")`;
7. map invalid JSON and non-object responses to the same stable errors used by
   GET JSON parsing;
8. never include payload contents in an exception message.

The LLM adapter should construct a client using the resolved Provider endpoint
and the LLM timeout, then translate `PeerTransportError` to the existing task
error surface without exposing request bodies.

## 5. Implementation steps

### Step 1: add shared bounded-response helpers

File: `rynmesh/transport.py`

- Extract a small bounded read helper if it removes duplication between GET
  and POST.
- Keep the extra-byte technique so exact-limit responses are accepted and
  limit-plus-one responses fail.
- Do not broaden exception messages to include request bytes.

Deliverable: protocol and internal helpers compile with no behavior change to
GET/download.

### Step 2: implement StdlibHttpsTransport POST

File: `rynmesh/transport.py`

- Build `urllib.request.Request(..., data=body, method="POST")`.
- Use `_headers` so camouflage/profile and network-key headers remain active.
- Use the existing opener containing `_NoRedirect` and explicit proxy policy.
- Perform a bounded response read.
- Normalize `HTTPError`, `URLError`, timeout, TLS, and OS failures.

Deliverable: default and direct profiles can POST without bypassing the
configured opener.

### Step 3: implement FrontedHttpsTransport POST

File: `rynmesh/transport.py`

- Generalize `_open` to accept method, optional body, and extra headers.
- Continue separating connect host, TLS SNI, and HTTP Host.
- Set `Content-Length` for POST.
- Preserve `Connection: close`.
- Reject non-success HTTP status exactly as GET does.
- Close sockets/connections on every success and failure path.

Deliverable: fronted requests retain the configured connect-host/SNI/Host
split for POST.

### Step 4: implement CdnWebSocketTransport POST

File: `rynmesh/transport.py`

- Generalize `_do_request` from hard-coded GET to method plus body.
- Encode an HTTP/1.1 POST request inside the binary WebSocket frame.
- Include Host, Content-Type supplied by the caller, and Content-Length.
- Append exactly one header/body separator followed by the raw body.
- Enforce the response-frame bound before allocating unbounded data.
- Validate the tunneled HTTP status rather than returning an error page body as
  successful JSON.

Deliverable: `cdn-ws` supports the same LLM POST operation as HTTPS profiles.

### Step 5: define plugin compatibility

Files: `rynmesh/transport.py`, transport documentation if necessary.

- Update the `Transport` protocol and docstring.
- Update bundled test doubles/plugins.
- Fail clearly when an old third-party Transport lacks `post_bytes`; do not
  silently bypass it with urllib.
- Document this as a transport interface extension.

Deliverable: selecting a plugin never causes an invisible downgrade to the
default transport.

### Step 6: add HttpPeerClient.post_json

File: `rynmesh/peer_http.py`

- Reuse JSON parsing behavior instead of creating another error vocabulary.
- Keep URL construction under the validated peer endpoint.
- Accept an explicit response limit so LLM can retain 2 MiB without changing
  the smaller generic JSON default.
- Ensure error messages contain metadata only.

Deliverable: unit-testable JSON POST at the peer-client boundary.

### Step 7: migrate LLM peer calls

File: `rynmesh/llm_package/routes.py`

Migrate all direct peer POST operations:

- `/api/peer/llm/tasks`;
- `/api/peer/llm/settlements`;
- `/api/peer/llm/cancellations`.

Implementation rules:

- remove or reduce `_peer_post_json` to a thin adapter around
  `HttpPeerClient.post_json`;
- keep long inference calls inside `asyncio.to_thread`;
- preserve endpoint and timeout resolution;
- preserve `_MAX_PEER_RESPONSE_BYTES = 2 * 1024 * 1024`;
- do not change relay or strict ICE/UDP paths;
- do not change retry or settlement idempotency behavior.

Deliverable: no LLM Peer HTTP POST invokes `urllib.request.urlopen` directly.

### Step 8: update documentation

Files:

- `docs/RYNMESH_TRANSPORT_CENSORSHIP.md`;
- `docs/LOCAL_LLM_SERVICE_MVP.md` if its call-path description changes.

Document that direct LLM Peer HTTP uses the active Transport profile. Do not
claim that strict ICE/UDP or encrypted Relay traffic uses HTTP Transport.

## 6. Test plan

### Transport unit tests

Add coverage in `tests/test_transport.py` for:

- default HTTPS POST sends exact body and content type;
- network-key header is present on POST;
- caller headers do not remove required shared headers accidentally;
- exact maximum response size succeeds;
- maximum plus one fails with `reason="too_large"`;
- redirects are rejected;
- HTTP error, timeout, TLS, and connection errors are normalized;
- fronted POST preserves connect host, SNI, and Host;
- CDN-WebSocket frame contains a valid POST line, headers, Content-Length, and
  exact body;
- CDN-WebSocket rejects a malformed or oversized response frame;
- registered plugin Transport receives the POST call.

### HttpPeerClient tests

Add tests for:

- dictionary round trip;
- UTF-8 request/response content;
- invalid JSON;
- valid JSON array/non-object rejection;
- response too large;
- transport error mapping;
- exception strings do not contain a unique request-body marker.

### LLM regression tests

Add focused tests in `tests/test_llm_package.py` or
`tests/test_llm_hardening.py` proving:

- direct task submission uses the injected Transport;
- settlement uses the injected Transport;
- cancellation uses the injected Transport;
- the 2 MiB bound remains active;
- transport failure releases the Consumer hold as before;
- no prompt/output marker appears in logs or persisted control data;
- direct, strict P2P, and encrypted Relay behavior remain distinct.

### Required verification commands

```bash
python -m pytest tests/test_transport.py tests/test_llm_package.py tests/test_llm_hardening.py -q
python -m ruff check rynmesh/transport.py rynmesh/peer_http.py rynmesh/llm_package/routes.py tests/test_transport.py tests/test_llm_package.py tests/test_llm_hardening.py
python -m pytest tests/ -q
python scripts/llm_e2e.py run
python scripts/llm_e2e.py relay-run
```

The isolated stack must be stopped afterward with its documented `down`
command. Do not delete host models or unrelated data.

## 7. Acceptance criteria

- [x] `Transport` exposes bounded POST bytes.
- [x] Stdlib HTTPS, fronted HTTPS, CDN-WebSocket, and plugin test doubles support
      POST without fallback bypasses.
- [x] `HttpPeerClient.post_json` provides stable JSON/error behavior.
- [x] LLM task, settlement, and cancellation POSTs use `HttpPeerClient` and the
      active Transport.
- [x] The 2 MiB LLM response cap remains enforced.
- [x] Network-key, TLS/profile, explicit proxy, and no-redirect behavior apply
      to POST.
- [x] No request/response body is logged or included in errors.
- [x] Direct LLM E2E and encrypted Relay E2E remain green.
- [x] Focused and complete test suites pass.
- [x] Transport documentation describes the new path accurately.

Formal execution evidence and the sign-off decision are maintained in
`docs/acceptance/issue-28/ACCEPTANCE_REPORT.md`. This plan intentionally remains
the implementation design; the acceptance report is the authoritative record
of what was actually run.

## 8. Suggested commits

Keep reviewable boundaries:

1. `feat: add bounded POST support to peer transports`
2. `refactor: route peer JSON POST through HttpPeerClient`
3. `refactor: route LLM peer calls through Transport`
4. `test: cover LLM POST transport profiles and limits`
5. `docs: document Transport-backed LLM peer calls`

## 9. Risks and rollback

| Risk | Mitigation |
|---|---|
| A plugin silently lacks POST | Fail clearly; never bypass the selected plugin |
| Fronted request uses the wrong Host/SNI | Dedicated socket-level test |
| CDN tunnel encodes an invalid HTTP request | Assert the exact tunneled request bytes |
| Response limit changes accidentally | Boundary tests at limit and limit + 1 |
| Prompt appears in error/log output | Unique-marker tests and metadata-only exceptions |
| Settlement/cancellation regress | Separate injected-Transport tests for each endpoint |

Rollback is limited: restore the LLM helper to its previous implementation only
if necessary, while leaving the additive Transport API unused. Do not weaken
size, redirect, authentication, or privacy checks during rollback.
