# Issue #27 work plan: service background-worker registry

Status: implemented and formally accepted, 2026-09-02
Issue: https://github.com/yeogirlyun/rynmesh/issues/27
Recommended order: implement after or independently from #28

## 中文执行摘要

当前 Ryn node 的 lifespan 直接写死了 `_llm_relay_poll` 和
`_llm_publish_refresh`。新增服务如果需要后台轮询或发布刷新，只能继续修改
`peer_http.py` 并复制循环、退避、错误处理和关闭逻辑。

本任务要新增一个小型后台 Worker 注册表。服务包在安装路由时注册
`run_once`、初始延迟、忙碌/空闲间隔、失败退避和错误出口；node lifespan 只
负责统一启动和关闭。第一版只迁移两个 LLM Worker，不顺便重构 updater、
Digest discovery 或 Daily Recap。

## 1. Problem

`create_app` currently owns service-specific background behavior:

- `_llm_relay_poll` polls encrypted Relay work, backs off while idle, and writes
  `app.state.llm_relay_error`;
- `_llm_publish_refresh` republishes Provider discovery data every 30 seconds
  and writes `app.state.llm_publication_error`.

The LLM package installs callable functions on `app.state`, and the node
lifespan discovers them through `getattr`. This creates an undocumented
duck-typed contract and requires a `peer_http.py` change for every new service
worker.

The target architecture is:

```text
create_app
  -> create BackgroundWorkerRegistry
  -> install service routes
       -> service registers worker specifications
  -> lifespan starts every registered worker
  -> lifespan cancels and awaits every worker on shutdown
```

## 2. Goals

- Define a small explicit worker specification.
- Allow service packages to register background work during route installation.
- Centralize task creation, thread offload, scheduling, error capture, and
  shutdown.
- Preserve the current LLM relay busy/idle backoff behavior.
- Preserve the current 30-second LLM publication refresh.
- Preserve the existing LLM status response fields and error messages.
- Isolate a failing worker so it cannot terminate another worker or the node.
- Make future service packages additive: register a worker without editing the
  node lifespan.

## 3. Non-goals

- Do not build a distributed job queue.
- Do not replace user-submitted LLM background order threads.
- Do not migrate updater polling, Digest discovery, or Daily Recap in the first
  patch.
- Do not introduce persistent scheduling state.
- Do not add cron syntax.
- Do not change LLM Relay protocol or publication records.
- Do not hide worker failures; status visibility must remain.

## 4. Proposed module and API

Create `rynmesh/background_workers.py`.

### 4.1 Worker result

```python
@dataclass(frozen=True)
class WorkerRunResult:
    activity: bool = False
```

The result lets a polling worker distinguish “work was processed” from “idle”.
`None` is treated as `activity=False` for fixed-interval workers.

### 4.2 Backoff policy

```python
@dataclass(frozen=True)
class BackoffPolicy:
    busy_delay_s: float
    idle_initial_s: float
    idle_multiplier: float
    idle_max_s: float
    error_multiplier: float
    error_max_s: float
```

For a fixed interval publisher, set busy/idle delays to the same value and use
an appropriate error maximum. Validation must reject zero, negative, NaN, and
infinite delays.

### 4.3 Worker specification

```python
@dataclass(frozen=True)
class BackgroundWorkerSpec:
    name: str
    run_once: Callable[[], object]
    policy: BackoffPolicy
    initial_delay_s: float = 0.0
    error_sink: Callable[[str], None] | None = None
```

Contract:

- names are unique and stable;
- `run_once` may be synchronous or asynchronous;
- synchronous work runs through `asyncio.to_thread` so it cannot block the
  event loop;
- an awaitable return value is awaited;
- bool results are converted to `WorkerRunResult(activity=value)` for the
  existing LLM relay callable;
- `error_sink("")` clears the visible error after success;
- `error_sink(str(exc))` records a sanitized failure;
- `asyncio.CancelledError` is always re-raised, never recorded as a worker
  failure.

### 4.4 Registry

```python
class BackgroundWorkerRegistry:
    def register(self, spec: BackgroundWorkerSpec) -> None: ...
    def specs(self) -> tuple[BackgroundWorkerSpec, ...]: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def status(self) -> dict[str, dict[str, object]]: ...
```

Rules:

- duplicate names fail during application construction;
- registering after `start()` fails clearly in v1;
- `start()` creates one supervised `asyncio.Task` per worker;
- `stop()` cancels all tasks and awaits them with `gather(...,
  return_exceptions=True)`;
- a worker exception is caught inside that worker's loop;
- the registry stores metadata only: running state, last success/failure time,
  consecutive failures, and sanitized error text;
- no service task body or result body enters registry status.

Dynamic registration and hot unloading are intentionally deferred.

## 5. Scheduling behavior

### 5.1 Success with activity

- reset consecutive failure count;
- reset idle delay;
- clear the error sink;
- wait `busy_delay_s`.

### 5.2 Success while idle

- reset consecutive failure count;
- clear the error sink;
- multiply the current idle delay by `idle_multiplier`;
- cap at `idle_max_s`.

### 5.3 Failure

- increment consecutive failure count;
- record only exception type/sanitized message;
- call the error sink;
- multiply the current error delay by `error_multiplier`;
- cap at `error_max_s`;
- continue running unless shutdown was requested.

The runner should use `time.monotonic()` for elapsed/scheduling metadata. Wall
clock may be used only for display timestamps.

## 6. LLM migration

### 6.1 Relay polling worker

Current behavior to preserve:

- initial delay: 1 second;
- when work is processed: next run after 1 second;
- when idle: multiply by 1.5 up to 10 seconds;
- after failure: multiply by 2 up to 30 seconds;
- visible error: `app.state.llm_relay_error`.

Registration belongs in `install_llm_routes`, after `relay_once` exists:

```python
registry.register(BackgroundWorkerSpec(
    name="llm.relay-poll",
    run_once=relay_once,
    initial_delay_s=1.0,
    policy=...,
    error_sink=lambda value: setattr(app.state, "llm_relay_error", value),
))
```

### 6.2 Provider publication worker

Current behavior to preserve:

- initial delay: 1 second;
- publish every 30 seconds;
- no node crash during Registry/runtime outage;
- visible error: `app.state.llm_publication_error`;
- a later successful publish clears the error.

Registration also belongs in `install_llm_routes` after `publish_once` exists.
The worker must remain harmless when no Provider is configured; the package
callable may return idle without raising.

### 6.3 Remove old wiring

After both registrations are covered by tests:

- remove `_llm_relay_poll` from `peer_http.py`;
- remove `_llm_publish_refresh` from `peer_http.py`;
- remove their direct `create_task` calls and cancellation calls;
- remove `app.state.llm_relay_once` and `app.state.llm_publish_once` if no
  compatibility consumer remains;
- retain `llm_relay_error` and `llm_publication_error` until the status API is
  deliberately migrated.

Do not remove old wiring before registry-start tests prove the replacement is
active; running both implementations would duplicate polling/publication.

## 7. Application lifecycle integration

File: `rynmesh/peer_http.py`

Implementation order:

1. construct `BackgroundWorkerRegistry` immediately after creating the FastAPI
   app;
2. store it on `app.state.background_workers`;
3. install all service routes, allowing them to register specs;
4. when lifespan enters, call `await registry.start()` after essential startup
   initialization;
5. when lifespan exits, call `await registry.stop()` in a `finally` block;
6. preserve the existing non-service tasks unchanged in v1.

Because FastAPI receives the lifespan function before service routes are
installed, the lifespan closure must resolve the registry from the application
state when startup actually runs. Add a test that constructs the app first and
then confirms both LLM specifications are present before startup.

## 8. Error and observability contract

The registry status is diagnostic metadata, not user content.

Recommended per-worker fields:

```json
{
  "name": "llm.relay-poll",
  "running": true,
  "last_success_at": "...",
  "last_failure_at": "...",
  "consecutive_failures": 0,
  "error": ""
}
```

Rules:

- never include function arguments, return values, prompts, outputs, task
  envelopes, model paths, or secrets;
- cap error text length;
- avoid raw `repr` of service objects;
- keep `/api/local/llm/service/status` fields compatible;
- a worker failure must not set the Provider offline automatically unless the
  existing service rules already require that state.

No new public endpoint is required for v1. A local diagnostics endpoint may be
proposed separately.

## 9. Test plan

### Registry unit tests

Create `tests/test_background_workers.py` covering:

- unique registration and deterministic spec listing;
- duplicate-name rejection;
- invalid delay/backoff rejection;
- synchronous worker executes off the event loop;
- asynchronous worker is awaited;
- busy result uses busy delay;
- idle results back off and cap correctly;
- a success resets failure backoff and clears the error sink;
- one failing worker does not stop another;
- `CancelledError` exits without recording a service failure;
- stop cancels and awaits all tasks;
- registration after start is rejected;
- status contains metadata only and caps error text;
- scheduling uses monotonic time.

Use injected sleep/clock functions or a zero-time deterministic runner helper;
do not make unit tests wait for real 30-second intervals.

### LLM integration tests

Add coverage proving:

- application construction registers exactly `llm.relay-poll` and
  `llm.publish-refresh` when LLM routes are installed;
- Relay polling processes a queued item exactly once;
- idle Relay polling backs off without disappearing;
- publication runs at startup and on its configured cadence;
- Relay failure updates `llm_relay_error`;
- publication failure updates `llm_publication_error`;
- a later success clears each error;
- stopping the application leaves no LLM worker task running;
- old and new loops cannot run simultaneously;
- service status keeps its existing `background` shape;
- no task body appears in registry status or captured logs.

### Regression verification

```bash
python -m pytest tests/test_background_workers.py tests/test_llm_package.py tests/test_llm_hardening.py -q
python -m ruff check rynmesh/background_workers.py rynmesh/peer_http.py rynmesh/llm_package/routes.py tests/test_background_workers.py tests/test_llm_package.py tests/test_llm_hardening.py
python -m pytest tests/ -q
python scripts/llm_e2e.py run
python scripts/llm_e2e.py relay-run
```

Verify application shutdown explicitly; a green functional request is not
sufficient if worker tasks leak.

## 10. Acceptance criteria

- [x] A documented `BackgroundWorkerRegistry` and validated worker spec exist.
- [x] Service packages can register workers without editing node lifespan code.
- [x] Sync work does not block the asyncio event loop.
- [x] Worker failures are isolated and use bounded backoff.
- [x] Shutdown cancels and awaits every registered worker.
- [x] LLM Relay polling is registered through the new API.
- [x] LLM Provider publication refresh is registered through the new API.
- [x] The old LLM-specific lifespan loops and task wiring are removed.
- [x] Existing LLM background status fields remain compatible.
- [x] No prompt, output, secret, private path, or task envelope enters status or
      logs.
- [x] Focused tests, complete tests, direct E2E, and Relay E2E pass.
- [x] A future service can add a worker by changing only its package installer
      and tests.

Formal execution evidence and the sign-off decision are maintained in
`docs/acceptance/issue-27/ACCEPTANCE_REPORT.md`. This plan intentionally remains
the implementation design; the acceptance report is the authoritative record
of what was actually run.

## 11. Suggested commits

1. `feat: add supervised background-worker registry`
2. `test: cover worker scheduling isolation and shutdown`
3. `refactor: register LLM relay polling as a worker`
4. `refactor: register LLM publication refresh as a worker`
5. `docs: document service background-worker lifecycle`

## 12. Risks and rollback

| Risk | Mitigation |
|---|---|
| Worker runs twice during migration | Assert unique name and remove old task in the same migration commit |
| Sync worker blocks the event loop | Always offload non-awaitable callables with `asyncio.to_thread` |
| Shutdown leaks tasks | Cancel, await, and test app lifespan exit |
| Rapid failure loop consumes CPU | Validate positive delays and cap exponential backoff |
| Error status leaks private data | Sanitized, capped metadata-only error sink |
| Generic abstraction becomes too large | Migrate only two LLM workers in v1 |
| Registry starts before service registration | Construct/register before lifespan entry and test ordering |

Rollback should restore the two previous LLM loops as one coherent change. Do
not leave both old and new scheduling paths active, and do not remove error
visibility while rolling back.
