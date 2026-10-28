"""Tests for the service background-worker registry and LLM migration."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from rynmesh.background_workers import (
    BackgroundWorkerRegistry,
    BackgroundWorkerSpec,
    BackoffPolicy,
    WorkerRunResult,
)


class _Death(BaseException):
    """A crash that is not an `Exception` (e.g. asyncio's own cancel-adjacent
    internals can raise `BaseException` subclasses). Never `KeyboardInterrupt`
    or `SystemExit` here — those would abort the pytest run itself.
    """


def policy(**overrides) -> BackoffPolicy:
    values = {
        "busy_delay_s": 1.0,
        "idle_initial_s": 1.0,
        "idle_multiplier": 2.0,
        "idle_max_s": 4.0,
        "error_multiplier": 2.0,
        "error_max_s": 8.0,
    }
    values.update(overrides)
    return BackoffPolicy(**values)


class RecordingSleep:
    def __init__(self, stop_after: int) -> None:
        self.stop_after = stop_after
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self.stop_after:
            raise asyncio.CancelledError


def run_worker(spec: BackgroundWorkerSpec, *, stop_after: int) -> tuple[list[float], dict]:
    sleeper = RecordingSleep(stop_after)
    registry = BackgroundWorkerRegistry(sleep=sleeper)
    registry.register(spec)

    async def scenario() -> dict:
        with pytest.raises(asyncio.CancelledError):
            await registry._run(spec)
        return registry.status()[spec.name]

    return sleeper.delays, asyncio.run(scenario())


def test_registration_is_unique_and_deterministic() -> None:
    registry = BackgroundWorkerRegistry()
    second = BackgroundWorkerSpec("service.z", lambda: None, policy())
    first = BackgroundWorkerSpec("service.a", lambda: None, policy())
    registry.register(second)
    registry.register(first)
    assert [item.name for item in registry.specs()] == ["service.a", "service.z"]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(first)


def test_late_registration_runs_and_replace_swaps_the_running_worker() -> None:
    async def scenario() -> None:
        registry = BackgroundWorkerRegistry()
        await registry.start()

        late_ran = asyncio.Event()
        registry.register(
            BackgroundWorkerSpec("late", lambda: late_ran.set(), policy()),
        )
        await asyncio.wait_for(late_ran.wait(), timeout=2)

        with pytest.raises(ValueError, match="already registered"):
            registry.register(BackgroundWorkerSpec("late", lambda: None, policy()))

        old_calls = 0
        old_ran = asyncio.Event()

        def old_body() -> bool:
            nonlocal old_calls
            old_calls += 1
            old_ran.set()
            return True

        registry.register(
            BackgroundWorkerSpec("late", old_body, policy(busy_delay_s=0.01)),
            replace=True,
        )
        await asyncio.wait_for(old_ran.wait(), timeout=2)

        new_ran = asyncio.Event()

        def new_body() -> bool:
            new_ran.set()
            return True

        registry.register(
            BackgroundWorkerSpec("late", new_body, policy()), replace=True,
        )
        await asyncio.wait_for(new_ran.wait(), timeout=2)
        calls_at_replace = old_calls
        # Give the (cancelled) old task every chance to sneak in one more
        # call before asserting it truly stopped running.
        await asyncio.sleep(0.05)

        status = registry.status()["late"]
        await registry.stop()
        # The old task was cancelled before the new one was spawned; at most
        # one already-dispatched call could have still been in flight.
        assert old_calls <= calls_at_replace + 1
        assert status["restarts"] == 0

    asyncio.run(scenario())


def test_initial_delay_s_defers_the_first_invocation() -> None:
    """`initial_delay_s` must be slept through before `run_once` is ever
    called — not after. This is the mechanism `updates.poll` relies on to
    keep a boot from checking for an update while the crash-loop rollback
    window (`_confirm_after_grace`) is still open.
    """
    calls: list[int] = []

    def once() -> bool:
        calls.append(len(calls))
        return False

    spec = BackgroundWorkerSpec(
        "delayed", once, policy(busy_delay_s=5.0), initial_delay_s=1800.0,
    )

    # Cancelling at the very first recorded sleep call proves that sleep is
    # the *initial* delay, and that it happens before any invocation.
    delays, status = run_worker(spec, stop_after=1)
    assert delays == [1800.0]
    assert calls == []
    assert status["last_started_monotonic"] is None

    # Letting one more sleep happen proves `run_once` *does* eventually run,
    # and only after that first delay — not before it and not skipped.
    delays, status = run_worker(spec, stop_after=2)
    assert delays[0] == 1800.0
    assert calls == [0]
    assert status["last_started_monotonic"] is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("busy_delay_s", 0.0),
        ("idle_initial_s", -1.0),
        ("idle_multiplier", 0.5),
        ("idle_max_s", float("inf")),
        ("error_multiplier", float("nan")),
        ("error_max_s", 0.0),
    ],
)
def test_policy_rejects_invalid_backoff(field: str, value: float) -> None:
    values = policy().__dict__
    values[field] = value
    with pytest.raises(ValueError):
        BackoffPolicy(**values)


def test_sync_worker_runs_off_event_loop_and_async_worker_is_awaited() -> None:
    async def scenario() -> None:
        main_thread = threading.get_ident()
        sync_threads: list[int] = []
        async_called = asyncio.Event()

        def sync_once() -> bool:
            sync_threads.append(threading.get_ident())
            return True

        async def async_once() -> WorkerRunResult:
            async_called.set()
            return WorkerRunResult(activity=True)

        registry = BackgroundWorkerRegistry()
        registry.register(BackgroundWorkerSpec("sync", sync_once, policy()))
        registry.register(BackgroundWorkerSpec("async", async_once, policy()))
        await registry.start()
        await asyncio.wait_for(async_called.wait(), timeout=2)
        deadline = asyncio.get_running_loop().time() + 2
        while not sync_threads and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)
        await registry.stop()
        assert sync_threads and sync_threads[0] != main_thread

    asyncio.run(scenario())


def test_idle_backoff_caps_and_activity_resets_to_busy_delay() -> None:
    results = iter([False, False, True])
    spec = BackgroundWorkerSpec(
        "poll", lambda: next(results),
        policy(busy_delay_s=0.25, idle_initial_s=1, idle_multiplier=2, idle_max_s=3),
    )
    delays, status = run_worker(spec, stop_after=3)
    assert delays == [2, 3, 0.25]
    assert status["consecutive_failures"] == 0
    assert status["error"] == ""


def test_failure_backoff_is_bounded_and_success_clears_error_sink() -> None:
    calls = 0
    errors: list[str] = []

    def worker() -> bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("registry unavailable")
        return True

    spec = BackgroundWorkerSpec(
        "publish", worker,
        policy(busy_delay_s=1, error_multiplier=3, error_max_s=5),
        error_sink=errors.append,
    )
    delays, status = run_worker(spec, stop_after=3)
    assert delays == [3, 5, 1]
    assert errors[0] == "RuntimeError: background worker invocation failed"
    assert errors[-1] == ""
    assert status["consecutive_failures"] == 0
    assert status["last_failure_at"]
    assert status["last_success_at"]


def test_failing_worker_isolated_from_other_worker() -> None:
    async def scenario() -> None:
        healthy_ran = asyncio.Event()

        def failing() -> None:
            raise RuntimeError("temporary outage")

        async def healthy() -> bool:
            healthy_ran.set()
            return True

        registry = BackgroundWorkerRegistry()
        registry.register(BackgroundWorkerSpec("failing", failing, policy()))
        registry.register(BackgroundWorkerSpec("healthy", healthy, policy()))
        await registry.start()
        await asyncio.wait_for(healthy_ran.wait(), timeout=2)
        deadline = asyncio.get_running_loop().time() + 2
        while (
            registry.status()["failing"]["consecutive_failures"] == 0
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.001)
        snapshot = registry.status()
        await registry.stop()
        assert snapshot["failing"]["consecutive_failures"] == 1
        assert snapshot["healthy"]["last_success_at"]

    asyncio.run(scenario())


def test_stop_cancels_and_awaits_workers_without_recording_cancellation() -> None:
    async def scenario() -> tuple[bool, dict]:
        entered = asyncio.Event()
        finalized = asyncio.Event()

        async def worker() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        registry = BackgroundWorkerRegistry()
        registry.register(BackgroundWorkerSpec("long", worker, policy()))
        await registry.start()
        await asyncio.wait_for(entered.wait(), timeout=2)
        await registry.stop()
        return finalized.is_set(), registry.status()["long"]

    finalized, status = asyncio.run(scenario())
    assert finalized is True
    assert status["running"] is False
    assert status["consecutive_failures"] == 0
    assert status["error"] == ""


def test_status_is_bounded_metadata_and_uses_monotonic_schedule() -> None:
    ticks = iter([10.0, 11.0, 12.0])
    sleeper = RecordingSleep(stop_after=1)
    registry = BackgroundWorkerRegistry(sleep=sleeper, monotonic=lambda: next(ticks))
    private_marker = "PRIVATE_PROMPT_MARKER"
    spec = BackgroundWorkerSpec(
        "safe", lambda: {"prompt": private_marker}, policy(idle_multiplier=1),
    )
    registry.register(spec)

    async def scenario() -> dict:
        with pytest.raises(asyncio.CancelledError):
            await registry._run(spec)
        return registry.status()["safe"]

    status = asyncio.run(scenario())
    assert status["last_started_monotonic"] == 10.0
    assert status["next_run_monotonic"] is None
    assert private_marker not in json.dumps(status)

    private_error = "PRIVATE_ERROR_BODY_MARKER" * 100
    failing = BackgroundWorkerSpec(
        "bounded", lambda: (_ for _ in ()).throw(ValueError(private_error)), policy(),
    )
    _, failed_status = run_worker(failing, stop_after=1)
    assert private_error not in failed_status["error"]
    assert len(failed_status["error"]) <= 512


def test_register_replaces_a_worker_only_when_asked() -> None:
    registry = BackgroundWorkerRegistry()
    first = BackgroundWorkerSpec("service.a", lambda: None, policy())
    second = BackgroundWorkerSpec("service.a", lambda: True, policy())
    registry.register(first)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(second)
    registry.register(second, replace=True)
    assert registry.specs() == (second,)


# Every service package registers into this one registry, so pinning an exact
# worker-name list makes this test a merge-conflict magnet. It asserts instead
# that the expected workers are present and that no unrecognized worker sneaks
# in.
_KNOWN_WORKER_NAMES = {
    "llm.publish-refresh",
    "llm.friend-permissions",
    "ai-access.discovery",
    "llm.relay-poll",
    "updates.poll",
    "recap.daily",
    "mailbox.poll",
    "friends.delivery",
    "ask-ryn-runs",
    "local-search.index",
    "friend-feed.refresh",
    "offline-reading.download",
    "device-sync.pairing",
}


def test_create_app_registers_the_service_and_node_workers(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    app = create_app(store)
    registry = app.state.background_workers
    # The service packages register during `create_app`; the two adopted node
    # loops only appear once the ASGI lifespan has started (below).
    build_time = {spec.name: spec for spec in registry.specs()}
    assert build_time["mailbox.poll"].initial_delay_s == 3
    assert build_time["mailbox.poll"].policy.busy_delay_s == 2
    assert build_time["mailbox.poll"].policy.idle_max_s == 60
    assert build_time["llm.relay-poll"].initial_delay_s == 1
    assert build_time["llm.relay-poll"].policy.busy_delay_s == 1
    assert build_time["llm.relay-poll"].policy.idle_multiplier == 1.5
    assert build_time["llm.relay-poll"].policy.idle_max_s == 10
    assert build_time["llm.relay-poll"].policy.error_max_s == 30
    assert build_time["llm.publish-refresh"].initial_delay_s == 1
    assert build_time["llm.publish-refresh"].policy.busy_delay_s == 30
    assert build_time["llm.friend-permissions"].policy.busy_delay_s == 1
    assert build_time["llm.friend-permissions"].policy.idle_max_s == 1
    assert build_time["llm.friend-permissions"].policy.error_max_s == 10
    assert build_time["local-search.index"].initial_delay_s == 1
    assert build_time["local-search.index"].policy.busy_delay_s == 1
    assert build_time["friend-feed.refresh"].initial_delay_s == 3
    assert build_time["friend-feed.refresh"].policy.busy_delay_s == 3
    assert build_time["offline-reading.download"].initial_delay_s == 1
    assert build_time["offline-reading.download"].policy.busy_delay_s == 1
    assert not hasattr(app.state, "llm_publish_once")
    assert not hasattr(app.state, "llm_relay_once")

    # The two adopted loops register inside `lifespan`, so they only appear
    # once the ASGI lifespan has actually started.
    with TestClient(app) as client:
        names = {spec.name for spec in registry.specs()}
        assert {"llm.publish-refresh", "llm.relay-poll", "updates.poll", "recap.daily"} <= names
        assert names <= _KNOWN_WORKER_NAMES

        specs = {spec.name: spec for spec in registry.specs()}
        updates_policy = specs["updates.poll"].policy
        assert specs["updates.poll"].initial_delay_s == 1800.0
        assert updates_policy.busy_delay_s == 1800.0
        assert (
            updates_policy.idle_initial_s
            == updates_policy.idle_max_s
            == updates_policy.error_max_s
            == 1800.0
        )
        assert updates_policy.idle_multiplier == 1.0
        assert updates_policy.error_multiplier == 1.0

        recap_policy = specs["recap.daily"].policy
        assert specs["recap.daily"].initial_delay_s == 20.0
        assert recap_policy.busy_delay_s == 900.0

        running = registry.status()
        assert set(running) >= {
            "llm.publish-refresh", "llm.relay-poll", "updates.poll", "recap.daily",
        }
        assert all(item["running"] for item in running.values())

        status_body = client.get("/api/local/node/status").json()
        assert set(status_body["workers"]) >= {
            "llm.publish-refresh", "llm.relay-poll", "updates.poll", "recap.daily",
        }

    stopped = registry.status()
    assert all(not item["running"] for item in stopped.values())


def test_worker_errors_surfaces_sink_writes_on_status_endpoint(tmp_path, monkeypatch) -> None:
    """`app.state.update_error`/`app.state.recap_error` are written by the
    workers' `error_sink`s but were read nowhere. `GET
    /api/local/node/status` must surface them under `worker_errors` next to
    `workers` so an operator can see a sink write.
    """
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    app = create_app(store)

    with TestClient(app) as client:
        body = client.get("/api/local/node/status").json()
        assert body["worker_errors"] == {"updates.poll": "", "recap.daily": ""}

        registry = app.state.background_workers
        specs = {spec.name: spec for spec in registry.specs()}
        assert specs["updates.poll"].error_sink is not None
        specs["updates.poll"].error_sink("RuntimeError: background worker invocation failed")

        body = client.get("/api/local/node/status").json()
        assert body["worker_errors"] == {
            "updates.poll": "RuntimeError: background worker invocation failed",
            "recap.daily": "",
        }


def test_reentering_the_lifespan_on_the_same_app_does_not_raise(tmp_path, monkeypatch) -> None:
    """`stop()` cancels a worker's task but never removes its spec from the
    registry's bookkeeping, so a process that re-enters this app's lifespan
    (startup -> shutdown -> startup, e.g. a supervisor restarting the ASGI
    server without recreating the app object) must be able to register
    `updates.poll`/`recap.daily` a second time without raising "already
    registered". The registration calls pass `replace=True` for exactly this
    reason.
    """
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    app = create_app(store)
    registry = app.state.background_workers

    with TestClient(app):
        first_names = {spec.name for spec in registry.specs()}
        assert {"updates.poll", "recap.daily"} <= first_names
        assert all(item["running"] for item in registry.status().values())

    assert all(not item["running"] for item in registry.status().values())

    # Re-entering must not raise ValueError("already registered: ...").
    with TestClient(app):
        second_names = {spec.name for spec in registry.specs()}
        assert second_names == first_names
        assert all(item["running"] for item in registry.status().values())

    assert all(not item["running"] for item in registry.status().values())


def test_update_poll_interval_env_var_moves_interval_and_initial_delay(
    tmp_path, monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_UPDATE_POLL_S", "60")
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    app = create_app(store)
    registry = app.state.background_workers

    with TestClient(app):
        spec = next(s for s in registry.specs() if s.name == "updates.poll")
        assert spec.initial_delay_s == 60.0
        assert spec.policy.busy_delay_s == 60.0


def test_updates_poll_does_not_check_before_its_initial_delay_elapses(
    tmp_path, monkeypatch,
) -> None:
    """Pins the generic `initial_delay_s` ordering test to the actual
    registered `updates.poll` worker and its env-derived interval: the real
    spec, driven through `_run` with a fake sleep, must not call
    `Updater.check` before the recorded first sleep completes.
    """
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.services.updater import Updater
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_UPDATE_POLL_S", "1800")

    checked = False

    def recording_check(self) -> dict:
        nonlocal checked
        checked = True
        return {"available": False}

    monkeypatch.setattr(Updater, "check", recording_check)

    app = create_app(RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network"))
    with TestClient(app):
        registry = app.state.background_workers
        spec = next(s for s in registry.specs() if s.name == "updates.poll")

    # The spec object itself is immutable and detached from the (now
    # stopped) registry above; drive it through a fresh registry with an
    # injected fake sleep so we can observe ordering without a real
    # 1800-second wait.
    sleeper = RecordingSleep(stop_after=1)
    fresh_registry = BackgroundWorkerRegistry(sleep=sleeper)
    fresh_registry.register(spec)

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await fresh_registry._run(spec)

    asyncio.run(scenario())
    assert sleeper.delays == [1800.0]
    assert checked is False


def test_update_poll_once_propagates_failures_and_reports_apply_as_activity(
    tmp_path, monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.services.updater import Updater
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")

    def raising_check(self) -> dict:
        raise RuntimeError("update check failed")

    monkeypatch.setattr(Updater, "check", raising_check)
    app = create_app(RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network"))
    with TestClient(app):
        registry = app.state.background_workers
        spec = next(s for s in registry.specs() if s.name == "updates.poll")
        with pytest.raises(RuntimeError, match="update check failed"):
            spec.run_once()

    # A successful check for an auto-applied update reports activity (True).
    monkeypatch.setattr(Updater, "check", lambda self: {"available": True})
    monkeypatch.setattr(Updater, "status", lambda self: {"autoUpdate": True})
    monkeypatch.setattr(Updater, "check_manifest", lambda self: {"manifest": True})
    monkeypatch.setattr(Updater, "apply", lambda self, manifest: {"ok": True})
    app2 = create_app(
        RynmeshStore(home=tmp_path / "home2", network_dir=tmp_path / "network2")
    )
    with TestClient(app2):
        registry2 = app2.state.background_workers
        spec2 = next(s for s in registry2.specs() if s.name == "updates.poll")
        assert spec2.run_once() is True


def test_recap_once_propagates_failures_and_reports_send_as_activity(
    tmp_path, monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.services import recap as recap_service
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_MODEL_PROVIDER", "none")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    app = create_app(RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network"))

    with TestClient(app) as client:
        # send_hour_utc=0 and a never-sent recap make the "due" check true
        # regardless of wall-clock time.
        client.patch(
            "/api/local/recap/settings",
            json={
                "to_address": "me@example.com",
                "smtp_host": "smtp.example.com",
                "enabled": True,
                "send_hour_utc": 0,
            },
        )
        registry = app.state.background_workers
        spec = next(s for s in registry.specs() if s.name == "recap.daily")

        def raising_send(*args, **kwargs):
            raise recap_service.RecapError("recap_send_failed: boom")

        monkeypatch.setattr(recap_service, "send_email", raising_send)
        with pytest.raises(recap_service.RecapError, match="recap_send_failed"):
            spec.run_once()

        monkeypatch.setattr(recap_service, "send_email", lambda *a, **k: {"ok": True})
        assert spec.run_once() is True


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.005)


def test_crash_from_a_non_exception_baseexception_restarts_the_worker() -> None:
    async def scenario() -> tuple[int, dict, list[str]]:
        calls = 0

        def flaky() -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _Death
            return True

        errors: list[str] = []
        spec = BackgroundWorkerSpec(
            "crashy",
            flaky,
            policy(busy_delay_s=0.01, idle_initial_s=0.01, idle_max_s=0.01, error_max_s=0.02),
            error_sink=errors.append,
        )
        registry = BackgroundWorkerRegistry()
        registry.register(spec)
        await registry.start()
        # Capture status right after the crash is recorded, before the
        # restart's later success has a chance to clear `error`.
        await _wait_until(lambda: registry.status()["crashy"]["crash_class"] != "")
        crash_status = registry.status()["crashy"]
        await _wait_until(lambda: calls >= 2)
        final_status = registry.status()["crashy"]
        await registry.stop()
        return calls, crash_status, final_status, errors

    calls, crash_status, final_status, errors = asyncio.run(scenario())
    assert calls >= 2
    assert final_status["restarts"] >= 1
    assert crash_status["crash_class"] == "_Death"
    assert crash_status["error"] == "_Death: background worker crashed"
    assert errors.count("_Death: background worker crashed") == 1


def test_no_restart_scheduled_after_stop() -> None:
    async def scenario() -> tuple[int, dict]:
        calls = 0

        def always_dies() -> None:
            nonlocal calls
            calls += 1
            raise _Death

        spec = BackgroundWorkerSpec(
            "crashy2", always_dies, policy(error_max_s=5.0),
        )
        registry = BackgroundWorkerRegistry()
        registry.register(spec)
        await registry.start()
        await _wait_until(lambda: registry.status()["crashy2"]["crash_class"] != "")
        await registry.stop()
        # The restart timer (error_max_s=5.0) would fire long after this
        # window; if stop() failed to cancel it, calls would grow past 1.
        await asyncio.sleep(0.05)
        return calls, registry.status()["crashy2"]

    calls, status = asyncio.run(scenario())
    assert calls == 1
    assert status["running"] is False


def test_stop_is_bounded_and_reports_abandoned_workers() -> None:
    # Note: a sync `run_once` blocked on a `threading.Event` (as sketched in
    # the task brief) turns out not to reproduce a stuck `stop()` here --
    # cancelling a task suspended on `asyncio.to_thread` resolves the *task*
    # immediately (it just leaks the underlying OS thread), which is a real
    # but different hazard than a hung `stop()`. To exercise the bounded-wait
    # and `abandoned` bookkeeping itself, this uses a worker that -- like a
    # shielded critical section -- does not honor the first cancellation
    # request immediately.
    async def scenario() -> tuple[float, dict]:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        async def stubborn() -> bool:
            entered.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                await asyncio.sleep(0.4)
                raise
            return True

        spec = BackgroundWorkerSpec("stuck", stubborn, policy())
        registry = BackgroundWorkerRegistry(stop_timeout_s=0.2)
        registry.register(spec)
        await registry.start()
        await asyncio.wait_for(entered.wait(), timeout=2)

        start = loop.time()
        result = await registry.stop()
        elapsed = loop.time() - start

        # Let the stubborn worker actually finish so it doesn't linger past
        # the test.
        await asyncio.sleep(0.5)
        return elapsed, result

    elapsed, result = asyncio.run(scenario())
    assert elapsed < 1.0
    assert result == {"stopped": [], "abandoned": ["stuck"]}


def test_error_max_s_below_busy_delay_s_is_rejected() -> None:
    with pytest.raises(ValueError, match="error_max_s must be at least busy_delay_s"):
        policy(busy_delay_s=1.0, error_max_s=0.5)


def test_backoff_policy_fixed_uses_one_flat_interval() -> None:
    fixed = BackoffPolicy.fixed(5.0)
    assert fixed.busy_delay_s == 5.0
    assert fixed.idle_initial_s == 5.0
    assert fixed.idle_multiplier == 1.0
    assert fixed.idle_max_s == 5.0
    assert fixed.error_multiplier == 1.0
    assert fixed.error_max_s == 5.0

    results = iter([False, False, True])
    spec = BackgroundWorkerSpec("clockwork", lambda: next(results), BackoffPolicy.fixed(5.0))
    delays, _ = run_worker(spec, stop_after=3)
    assert delays == [5.0, 5.0, 5.0]


def test_sink_notified_once_for_repeated_identical_failure_then_on_recovery() -> None:
    calls = 0
    errors: list[str] = []

    def worker() -> bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("boom")
        return True

    spec = BackgroundWorkerSpec(
        "dedup", worker, policy(error_max_s=8), error_sink=errors.append,
    )
    run_worker(spec, stop_after=3)
    assert errors == ["RuntimeError: background worker invocation failed", ""]


def test_sent_error_sentinel_clears_stale_error_after_replace() -> None:
    """Regression for the stale-error bug: `register(..., replace=True)`
    installs a fresh `_WorkerState`, and if `sent_error` defaulted to `""`
    the first success after the replace would compute `"" == ""` and skip
    the sink -- leaving whatever error the previous incarnation last sent
    (e.g. on `app.state.update_error`) stuck forever. `sent_error` must
    default to a sentinel no legitimate `error` value can equal, so the
    first send after a fresh state always fires.
    """

    async def scenario() -> list[str]:
        errors: list[str] = []

        def failing() -> None:
            raise RuntimeError("boom")

        spec = BackgroundWorkerSpec(
            "flaky", failing, policy(error_max_s=5.0), error_sink=errors.append,
        )
        registry = BackgroundWorkerRegistry()
        registry.register(spec)
        await registry.start()
        await _wait_until(lambda: bool(errors))
        assert errors[-1] == "RuntimeError: background worker invocation failed"

        healthy_ran = asyncio.Event()

        def healthy() -> bool:
            healthy_ran.set()
            return True

        registry.register(
            BackgroundWorkerSpec("flaky", healthy, policy(), error_sink=errors.append),
            replace=True,
        )
        await asyncio.wait_for(healthy_ran.wait(), timeout=2)
        await _wait_until(lambda: errors[-1] == "")
        await registry.stop()
        return errors

    errors = asyncio.run(scenario())
    # The replacement's first (successful) run must clear the sink, not skip
    # it because the fresh state's `sent_error` happened to equal `""`.
    assert errors[-1] == ""


def test_restart_timer_failure_is_logged_and_visible(caplog: pytest.LogCaptureFixture) -> None:
    """`_restart_after_delay` used to catch only `asyncio.CancelledError`
    around its sleep. Anything else killed the timer task silently and
    abandoned that worker forever with no log -- the one unsupervised
    failure mode left in the supervision code. It must now be logged, with
    only the exception's class name (never its message).
    """

    class _SleepFailure(RuntimeError):
        def __str__(self) -> str:
            return "PRIVATE_SLEEP_FAILURE_MARKER"

    async def scenario() -> None:
        sleep_calls = 0

        async def flaky_sleep(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            raise _SleepFailure

        def crash() -> None:
            raise _Death

        spec = BackgroundWorkerSpec(
            "timer-broken", crash, policy(busy_delay_s=0.01, error_max_s=0.01),
        )
        registry = BackgroundWorkerRegistry(sleep=flaky_sleep)
        registry.register(spec)
        await registry.start()
        await _wait_until(lambda: registry.status()["timer-broken"]["crash_class"] != "")
        await _wait_until(lambda: sleep_calls >= 1)
        await asyncio.sleep(0.05)
        status = registry.status()["timer-broken"]
        await registry.stop()
        return status

    with caplog.at_level("ERROR"):
        status = asyncio.run(scenario())

    assert status["restarts"] == 0
    assert status["running"] is False
    messages = [record.message for record in caplog.records]
    assert any("timer-broken" in message and "_SleepFailure" in message for message in messages)
    assert not any("PRIVATE_SLEEP_FAILURE_MARKER" in message for message in messages)


def test_crash_message_is_redacted_from_status() -> None:
    marker = "PRIVATE_CRASH_MARKER"

    class _DeathWithMarker(_Death):
        def __str__(self) -> str:
            return marker

    async def scenario() -> dict:
        def die() -> None:
            raise _DeathWithMarker

        spec = BackgroundWorkerSpec("redact", die, policy(error_max_s=5.0))
        registry = BackgroundWorkerRegistry()
        registry.register(spec)
        await registry.start()
        await _wait_until(lambda: registry.status()["redact"]["crash_class"] != "")
        status = registry.status()
        await registry.stop()
        return status

    status = asyncio.run(scenario())
    assert marker not in json.dumps(status)
