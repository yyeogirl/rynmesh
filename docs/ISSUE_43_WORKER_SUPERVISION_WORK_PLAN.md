# Issue #43 — supervise background workers, adopt the ad-hoc loops (work plan)

Status: in progress (system track). Tracks
[#43](https://github.com/yeogirlyun/rynmesh/issues/43). The private LLM
service, and the peer mailbox once [#44](https://github.com/yeogirlyun/rynmesh/pull/44)
merges, both depend on this registry staying alive.

## Problem

`rynmesh/background_workers.py` supervises *scheduling* but not *survival*:

- `_run` catches `Exception`, so an exception-derived failure backs off and
  retries. Anything else — a `BaseException` subclass raised by a bug, a stray
  cancellation, an error inside the registry's own scheduling code — kills the
  task silently. `status()` keeps reporting the worker with its last success
  timestamp, and `running` flips to `False` with no error and no restart. The
  LLM relay poll (and soon the mailbox poll) simply stops until the app is
  restarted, which for a desktop node can be days.
- `stop()` awaits `asyncio.gather` with no timeout. A worker blocked inside
  `asyncio.to_thread` (a hung HTTP call, a stuck disk) cannot be cancelled,
  so shutdown hangs forever and the desktop shell kills the daemon instead of
  letting it exit cleanly.
- `register()` is sealed at first `start()`, so a package that installs its
  routes after startup cannot register a worker at all.
- Three loops in `peer_http.create_app`'s lifespan are still detached
  `asyncio.create_task` bodies with `except Exception: pass`: the update poll,
  the daily recap, and background discovery. They have no backoff, no error
  surface, no status, and no bounded stop.

## Design

### Supervision (`rynmesh/background_workers.py`)

Every spawn attaches a done callback. The callback is the only place that
learns a task ended, and it distinguishes three endings:

| ending | action |
|---|---|
| cancelled | nothing (this is `stop()`, or a replaced worker) |
| exception / BaseException | record `crash_class`, bump `restarts`, notify the sink once, `_log.error`, respawn after `policy.error_max_s` |
| returned | `_run` never returns, so this is a bug: same as a crash |

The restart timer is a registry-owned task, cancelled by `stop()`. `stop()`
sets `_started = False` **before** cancelling, and the callback refuses to
respawn once stopped, so shutdown cannot race a restart into existence.

State gains `restarts: int` and `crash_class: str`; both appear in `status()`.
`error` keeps carrying only the sanitized failure class (never a message) and
`crash_class` follows the same rule.

`stop()` becomes bounded: cancel every task and restart timer, then
`asyncio.wait(tasks, timeout=self._stop_timeout_s)` (default 5.0 s,
constructor argument). A worker still stuck in a thread after the timeout is
reported in the return value (`{"stopped": [...], "abandoned": [...]}`) and
logged. `stop()` only bounds how long the node *waits* for it; it cannot
terminate it. A sync worker stuck inside `asyncio.to_thread` still leaks its
OS thread, and because `asyncio.to_thread` runs on the loop's default
`ThreadPoolExecutor`, whose module registers an `atexit` handler that joins
every such thread with no timeout, a permanently wedged sync worker can still
stall process exit after `uvicorn.run()` returns — unchanged from before this
branch.

`register(spec, *, replace=False)` drops the seal: registering after `start()`
spawns immediately. `replace=True` cancels the running task for that name and
re-registers, so an idempotent `install_*` helper can be called twice.

`BackoffPolicy.fixed(interval_s)` builds an interval-only policy (busy = idle
= error = interval, multipliers 1.0), and `__post_init__` gains
`error_max_s >= busy_delay_s` — an error ceiling below the busy delay makes a
failing worker retry *faster* than a healthy one, which is never intended.

### Adopting the ad-hoc loops (`rynmesh/peer_http.py`)

| worker | policy | initial delay | notes |
|---|---|---|---|
| `updates.poll` | `fixed(RYNMESH_UPDATE_POLL_S, default 1800)` | same as the interval | preserves today's sleep-then-check order, so a boot never triggers an immediate update while the crash-loop rollback window is open |
| `recap.daily` | `fixed(900)` | 20 s | unchanged semantics: poll, not timer, so a laptop asleep at the send hour still gets the recap on wake |

Both bodies keep their current logic and lose their `except Exception: pass`
— the registry records the failure class, backs off, and surfaces it in
`status()` and through an error sink on `app.state`.

`_confirm_after_grace` stays an ad-hoc task: it is one-shot, not repeatable.
`_discover` stays ad-hoc for now — its delay is computed from the digest
service's `next_refresh_unix`, which the fixed/idle policy model cannot
express; adopting it needs a dynamic-delay policy and is out of scope here.

### Conflict note

[#44](https://github.com/yeogirlyun/rynmesh/pull/44) (peer mailbox) also adds
`register(..., replace=...)` and registers a `mailbox.poll` worker. Whichever
merges second resolves a small conflict in `background_workers.py` and in the
`create_app` worker-set assertion. The assertion in this branch therefore
checks that the workers it owns are present and that no *unknown* worker name
appears, rather than pinning an exact list.

## Tasks

1. Registry hardening: supervision + bounded stop + late registration/replace
   + `restarts`/`crash_class` in status + `BackoffPolicy.fixed` and the
   `error_max_s` rule, with tests.
2. Adopt `updates.poll` and `recap.daily`, expose their errors, extend the
   `create_app` test, and document the supervision contract.

## Acceptance

- [x] A worker whose task dies from a non-`Exception` `BaseException` is
      restarted after `error_max_s`, and `status()` shows `restarts >= 1` with
      a `crash_class` and no message text.
- [x] `stop()` returns within the timeout even when a sync worker is stuck in
      a thread, and names the abandoned worker.
- [x] A worker registered after `start()` runs; `replace=True` cancels and
      replaces the running one; a duplicate name without `replace` raises.
- [x] `error_max_s < busy_delay_s` is rejected; `BackoffPolicy.fixed` builds a
      valid interval policy.
- [x] `create_app` registers `updates.poll` and `recap.daily` with the
      documented policies; the update poll does not run before its first
      interval; no `except Exception: pass` remains in either body.
- [x] Failures in either adopted worker appear in the worker status and in an
      error field on `app.state`.
- [x] No prompt, response, model path, or private path in any worker status,
      error field, or log line.
- [x] `python -m ruff check rynmesh/ tests/` and the full pytest suite pass.

`_discover` remains an ad-hoc `asyncio.create_task` loop (not moved onto the
registry): its delay is computed from the digest service's own
`next_refresh_unix`, which the current fixed/idle `BackoffPolicy` model has no
way to express. Adopting it needs a dynamic-delay policy shape and is left as
follow-up work, tracked in the "Background Workers" section of
`docs/ARCHITECTURE.md`.
