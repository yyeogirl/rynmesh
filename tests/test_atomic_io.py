"""Tests for the shared durable JSON/bytes writer in `rynmesh.atomic_io`."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from rynmesh.atomic_io import (
    AtomicIOError,
    atomic_write_bytes,
    atomic_write_json,
    migration_backup,
    read_json,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _tmp_files(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.name.endswith(".tmp")]


class _Cancelled(BaseException):
    """A stand-in for KeyboardInterrupt/asyncio.CancelledError in tests."""


# --------------------------------------------------------------------- 1, 2


def test_round_trip_permissions_and_no_tmp_left(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "record.json"
    atomic_write_json(path, {"a": 1, "b": 2})

    assert read_json(path) == {"a": 1, "b": 2}
    if os.name != "nt":
        assert _mode(path) == 0o600
        assert _mode(path.parent) == 0o700
    assert _tmp_files(path.parent) == []


def test_format_preservation_indent_sort_keys(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    value = {"z": 1, "a": [3, 2, 1], "m": {"y": 1, "x": 2}}
    atomic_write_json(path, value, indent=2, sort_keys=True)

    expected = json.dumps(value, indent=2, sort_keys=True).encode("utf-8")
    assert path.read_bytes() == expected


def test_format_preservation_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    value = {"a": 1}
    atomic_write_json(path, value, trailing_newline=True)

    expected = json.dumps(value, sort_keys=True).encode("utf-8") + b"\n"
    assert path.read_bytes() == expected


# ------------------------------------------------------------------------ 3


def test_replace_failure_leaves_old_content_and_no_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "record.json"
    atomic_write_json(path, {"version": 1})

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(AtomicIOError):
        atomic_write_json(path, {"version": 2})

    assert read_json(path) == {"version": 1}
    assert _tmp_files(path.parent) == []


# ------------------------------------------------------------------------ 4


def test_base_exception_mid_write_leaves_no_tmp_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"

    def _boom(_fd: int) -> None:
        raise _Cancelled("cancelled mid-write")

    monkeypatch.setattr(os, "fsync", _boom)
    with pytest.raises(_Cancelled):
        atomic_write_json(path, {"version": 1})

    assert not path.exists()
    assert _tmp_files(tmp_path) == []


# ------------------------------------------------------------------------ 5


def test_write_over_max_bytes_raises_before_touching_disk(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    with pytest.raises(AtomicIOError):
        atomic_write_bytes(path, b"x" * 10, max_bytes=5)

    assert not path.exists()
    assert not path.parent.exists() or _tmp_files(path.parent) == []


def test_read_over_max_bytes_raises_without_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"[1, 2, 3]")

    def _fail_read_bytes(_self: Path) -> bytes:
        raise AssertionError("read_json must not read an oversize file")

    monkeypatch.setattr(Path, "read_bytes", _fail_read_bytes)
    with pytest.raises(AtomicIOError):
        read_json(path, max_bytes=1)


# ------------------------------------------------------------------------ 6


def test_read_json_missing_file_with_default_returns_it(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"
    assert read_json(path, default=None) is None
    assert read_json(path, default={"x": 1}) == {"x": 1}


def test_read_json_missing_file_without_default_raises(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"
    with pytest.raises(AtomicIOError):
        read_json(path)


def test_read_json_corrupt_with_default_returns_it(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_json(path, default="fallback") == "fallback"


def test_read_json_corrupt_without_default_raises(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(AtomicIOError):
        read_json(path)


# ------------------------------------------------------------------------ 7


def test_concurrent_writers_leave_one_valid_value_and_no_tmp(tmp_path: Path) -> None:
    path = tmp_path / "shared.json"
    atomic_write_json(path, {"writer": "init"})

    errors: list[BaseException] = []

    def _hammer(label: str) -> None:
        try:
            for i in range(50):
                atomic_write_json(path, {"writer": label, "i": i})
        except BaseException as exc:  # pragma: no cover - fails the test below
            errors.append(exc)

    threads = [threading.Thread(target=_hammer, args=(label,)) for label in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    final = read_json(path)
    assert final["writer"] in {"a", "b"}
    assert 0 <= final["i"] < 50
    assert _tmp_files(path.parent) == []


def test_windows_transient_rename_is_retried_but_permanent_failure_preserves_data(tmp_path, monkeypatch):
    import rynmesh.atomic_io as module

    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    real_replace = module.os.replace
    attempts = []

    def transient(*args):
        attempts.append(args)
        if len(attempts) < 3:
            error = PermissionError("sharing violation")
            error.winerror = 32
            raise error
        return real_replace(*args)

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(module.os, "replace", transient)
    module._replace(source, target)
    assert target.read_bytes() == b"new" and len(attempts) == 3
    source.write_bytes(b"next")
    calls = []

    def denied(*args):
        calls.append(args)
        error = PermissionError("denied")
        error.winerror = 5
        raise error

    monkeypatch.setattr(module.os, "replace", denied)
    with pytest.raises(PermissionError):
        module._replace(source, target)
    assert len(calls) == 8
    assert target.read_bytes() == b"new" and source.read_bytes() == b"next"


def test_private_acl_failure_prevents_new_data_and_cleans_temporary_file(tmp_path, monkeypatch):
    import rynmesh.atomic_io as module

    path = tmp_path / "private.json"
    atomic_write_json(path, {"original": True})
    before = path.read_bytes()
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    def refuse(path):
        assert path.stat().st_size == 0  # No secret bytes before access is restricted.
        raise OSError("private_acl_write_failed")
    monkeypatch.setattr(module, "restrict_windows_acl", refuse)
    with pytest.raises(AtomicIOError):
        atomic_write_json(path, {"secret": "do not write"})
    assert path.read_bytes() == before
    assert _tmp_files(tmp_path) == []


# ------------------------------------------------------------------------ 8


def test_migration_backup_copies_and_returns_path(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b'{"a": 1}')

    backup = migration_backup(path)

    assert backup == path.with_name(path.name + ".migrated")
    assert backup is not None
    assert backup.read_bytes() == b'{"a": 1}'


def test_migration_backup_missing_source_returns_none(tmp_path: Path) -> None:
    assert migration_backup(tmp_path / "missing.json") is None


def test_migration_backup_overwrites_existing_backup(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"new-content")
    backup_path = path.with_name(path.name + ".migrated")
    backup_path.write_bytes(b"stale-content")

    backup = migration_backup(path)

    assert backup == backup_path
    assert backup.read_bytes() == b"new-content"


# ------------------------------------------------------------------------ 9


def test_error_message_carries_no_path_or_content(tmp_path: Path) -> None:
    marker_path = "SECRET_PATH_MARKER"
    marker_content = "SECRET_CONTENT_MARKER"
    path = tmp_path / f"{marker_path}.json"

    with pytest.raises(AtomicIOError) as excinfo:
        atomic_write_bytes(path, marker_content.encode("utf-8"), max_bytes=1)
    message = str(excinfo.value)
    assert marker_path not in message
    assert marker_content not in message
    assert str(path) not in message

    path.write_text(f'{{"secret": "{marker_content}", "broken": ', encoding="utf-8")
    with pytest.raises(AtomicIOError) as excinfo:
        read_json(path)
    message = str(excinfo.value)
    assert marker_path not in message
    assert marker_content not in message
    assert str(path) not in message


def test_not_json_serializable_raises_atomic_io_error(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    with pytest.raises(AtomicIOError):
        atomic_write_json(path, {"bad": object()})
    assert not path.exists()


def test_compact_json_is_explicit_and_preserves_byte_limit_atomicity(tmp_path: Path) -> None:
    path = tmp_path / 'record.json'
    value = {'b': '内容', 'a': 1}
    atomic_write_json(path, value, ensure_ascii=False)
    assert path.read_text(encoding='utf-8') == '{"a": 1, "b": "内容"}'
    compact = '{"a":1,"b":"内容"}'.encode('utf-8')
    atomic_write_json(path, value, ensure_ascii=False, separators=(',', ':'), max_bytes=len(compact))
    assert path.read_bytes() == compact and read_json(path) == value
    with pytest.raises(AtomicIOError):
        atomic_write_json(path, value, ensure_ascii=False, separators=(',', ':'), max_bytes=len(compact) - 1)
    assert path.read_bytes() == compact and _tmp_files(tmp_path) == []


# ---------------------------------------------------------- adoption format checks


def test_settings_store_format_unchanged(tmp_path: Path) -> None:
    from rynmesh.settings_store import SettingsStore

    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    data = store.patch({"auto_update": False})

    raw = path.read_text(encoding="utf-8")
    assert raw == json.dumps(data, indent=2)


def test_recommendation_profile_format_unchanged(tmp_path: Path) -> None:
    from rynmesh.recommendation_profile import RecommendationProfileStore

    path = tmp_path / "profile.json"
    store = RecommendationProfileStore(path)
    store.patch({"direction": "more ai and open-source"})

    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, indent=2, sort_keys=True)


def test_consumption_store_format_unchanged(tmp_path: Path) -> None:
    from rynmesh.services.consumption import ConsumptionStore

    path = tmp_path / "consumption.json"
    store = ConsumptionStore(path)
    store.record(
        {"item_id": "abc", "link": "https://example.com/abc"}, "progress", progress=0.5
    )

    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, indent=2, sort_keys=True, ensure_ascii=False)


def test_llm_manifest_format_unchanged(tmp_path: Path) -> None:
    from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest

    manifest = LLMPackageManifest(
        package_id="test-pkg",
        mode="openai_compatible",
        public_model_alias="alias",
        base_url="http://127.0.0.1:8080",
        model="test-model",
        runtime_api_key="loopback-secret-token",
    )
    path = tmp_path / "manifest.json"
    save_manifest(manifest, path)

    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, indent=2, sort_keys=True)
    if os.name != "nt":
        assert _mode(path) == 0o600
        assert _mode(path.parent) == 0o700


def test_task_order_store_format_unchanged(tmp_path: Path) -> None:
    from rynmesh.llm_package.task_protocol import TaskOrderStore

    store = TaskOrderStore(tmp_path / "orders")
    record, created = store.claim(task_id="task-1", bindings={"a": "b"})
    assert created is True

    raw = (tmp_path / "orders" / "task-1.json").read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, indent=2, sort_keys=True)
    assert parsed == record


def test_task_balance_ledger_format_unchanged(tmp_path: Path) -> None:
    from rynmesh.llm_package.task_balance import TaskBalanceLedger

    path = tmp_path / "task-balance.json"
    TaskBalanceLedger(path)  # standalone mode writes its fresh state on construction

    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, indent=2, sort_keys=True)


def test_reader_cache_format_unchanged(tmp_path: Path) -> None:
    from rynmesh.services.reader import ReaderCache

    cache = ReaderCache(tmp_path / "cache")
    cache.put("https://example.com/a", {"title": "t", "blocks": []}, now=123.0)

    raw = cache._path("https://example.com/a").read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, ensure_ascii=False)


def test_digest_service_sources_format_unchanged(tmp_path: Path) -> None:
    from rynmesh.services.digest import DigestService

    DigestService(tmp_path, bootstrap_defaults=True)

    raw = (tmp_path / "digest" / "sources.json").read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, indent=2, sort_keys=True)


def test_relay_store_meta_format_unchanged(tmp_path: Path) -> None:
    from rynmesh.relay import FileRelayStore

    store = FileRelayStore(tmp_path / "relay")
    record = store.put_chunks(iter([b"hello world"]), filename="a.txt")

    meta_path = store._meta_path(record.content_hash)
    raw = meta_path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, indent=2, sort_keys=True)
