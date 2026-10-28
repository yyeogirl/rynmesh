"""The ledger-backed Task Balance: one signed escrow history, zero effect on
reputation, O(1) hot path via a rebuildable snapshot, and a one-time migration
from the standalone v1 file."""
from __future__ import annotations

import json

import pytest

from rynmesh.credits import GLOBAL_CATEGORY, TASK_BALANCE_CATEGORY
from rynmesh.llm_package.task_balance import (
    LEDGER_VERSION,
    SNAPSHOT_VERSION,
    TaskBalanceError,
    TaskBalanceLedger,
)
from rynmesh.store import RynmeshStore


def _ledger(store: RynmeshStore, path, **kwargs) -> TaskBalanceLedger:
    return TaskBalanceLedger(
        path, credit_ledger=store.credit_ledger, peer_id=store.peer_id,
        private_key_bytes=store.private_key_bytes, **kwargs,
    )


@pytest.fixture()
def store(tmp_path) -> RynmeshStore:
    return RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net")


def test_transitions_match_the_standalone_semantics(store, tmp_path):
    ledger = _ledger(store, tmp_path / "balance.json", initial_dev_balance=10)
    first = ledger.hold(task_id="one", amount=2, service_id="svc", provider_peer_id="p")
    assert ledger.hold(task_id="one", amount=2, service_id="svc", provider_peer_id="p") == first
    assert ledger.settle(task_id="one", amount=1.25, input_tokens=3, output_tokens=4,
                         duration_ms=5, service_id="svc", provider_peer_id="p")["state"] == "settled"
    ledger.hold(task_id="two", amount=3, service_id="svc", provider_peer_id="p")
    assert ledger.release(task_id="two", reason="cancelled")["state"] == "released"
    assert ledger.hold(task_id="two", amount=3, service_id="svc", provider_peer_id="p")["state"] == "held"
    ledger.release(task_id="two", reason="retry_failed")
    with pytest.raises(TaskBalanceError):
        ledger.settle(task_id="two", amount=1, input_tokens=1, output_tokens=1,
                      duration_ms=1, service_id="svc", provider_peer_id="p")
    with pytest.raises(TaskBalanceError, match="idempotency conflict"):
        ledger.hold(task_id="one", amount=2, service_id="svc", provider_peer_id="other")
    summary = ledger.summary()
    assert summary["available"] == 8.75 and summary["held"] == 0
    assert summary["ledger_backed"] is True
    assert summary["development_only"] is True


def test_every_transition_is_a_signed_event_in_its_own_category(store, tmp_path):
    ledger = _ledger(store, tmp_path / "balance.json", initial_dev_balance=10)
    ledger.hold(task_id="t", amount=2, service_id="svc", provider_peer_id="p")
    ledger.settle(task_id="t", amount=1.5, input_tokens=1, output_tokens=1,
                  duration_ms=1, service_id="svc", provider_peer_id="p")
    ledger.earn(task_id="in", amount=0.4, input_tokens=1, output_tokens=1,
                duration_ms=1, service_id="svc", consumer_peer_id="c")

    signed = store.credit_ledger.list_events(subject_peer_id=store.peer_id,
                                             category=TASK_BALANCE_CATEGORY)
    kinds = sorted(item.payload["kind"] for item in signed)
    assert kinds == ["task_balance_opening", "task_earning", "task_hold", "task_settle"]
    assert all(item.payload["category"] == TASK_BALANCE_CATEGORY for item in signed)
    # No prompt-like data, only accounting metadata.
    assert "prompt" not in json.dumps([item.to_dict() for item in signed])


def test_escrow_traffic_never_moves_reputation(store, tmp_path):
    """The whole point of the namespace: Credits stay non-monetary."""
    before = store.credit_ledger.account(store.peer_id).to_dict()
    ledger = _ledger(store, tmp_path / "balance.json", initial_dev_balance=100)
    for i in range(5):
        ledger.hold(task_id=f"t{i}", amount=9, service_id="svc", provider_peer_id="p")
        ledger.settle(task_id=f"t{i}", amount=9, input_tokens=1, output_tokens=1,
                      duration_ms=1, service_id="svc", provider_peer_id="p")
    ledger.earn(task_id="big", amount=50, input_tokens=1, output_tokens=1,
                duration_ms=1, service_id="svc", consumer_peer_id="c")
    after = store.credit_ledger.account(store.peer_id).to_dict()
    assert after["score"] == before["score"]
    assert after["event_count"] == before["event_count"]
    assert TASK_BALANCE_CATEGORY not in after["categories"]
    assert store.credit_ledger.list_events(category=GLOBAL_CATEGORY) == \
        [e for e in store.credit_ledger.list_events(category=GLOBAL_CATEGORY)
         if e.payload["category"] != TASK_BALANCE_CATEGORY]
    # ...but the view is there when asked for explicitly.
    explicit = store.credit_ledger.list_events(subject_peer_id=store.peer_id,
                                               category=TASK_BALANCE_CATEGORY)
    assert len(explicit) == 1 + 5 * 2 + 1


def test_snapshot_is_rebuilt_from_the_ledger_when_lost_or_stale(store, tmp_path):
    path = tmp_path / "balance.json"
    ledger = _ledger(store, path, initial_dev_balance=10)
    ledger.hold(task_id="a", amount=4, service_id="svc", provider_peer_id="p")
    ledger.settle(task_id="a", amount=3, input_tokens=1, output_tokens=1,
                  duration_ms=1, service_id="svc", provider_peer_id="p")
    ledger.hold(task_id="b", amount=2, service_id="svc", provider_peer_id="p")
    expected = ledger.summary()

    path.unlink()  # lost snapshot
    rebuilt = _ledger(store, path, initial_dev_balance=999)  # initial must NOT reapply
    assert rebuilt.summary()["available"] == expected["available"] == 5.0
    assert rebuilt.summary()["held"] == 2.0
    # The open hold survived replay with its idempotency bindings intact.
    with pytest.raises(TaskBalanceError, match="idempotency conflict"):
        rebuilt.hold(task_id="b", amount=2, service_id="svc", provider_peer_id="someone-else")

    # Stale snapshot (fewer folded events than the ledger holds) also rebuilds.
    snapshot = json.loads(path.read_text())
    snapshot["folded_events"] = 1
    snapshot["available"] = 42.0
    path.write_text(json.dumps(snapshot))
    again = _ledger(store, path)
    assert again.summary()["available"] == 5.0


def test_legacy_v1_file_is_migrated_once(store, tmp_path):
    path = tmp_path / "balance.json"
    legacy = TaskBalanceLedger(path, initial_dev_balance=20)  # standalone v1
    legacy.hold(task_id="open", amount=5, service_id="svc", provider_peer_id="p")
    legacy.hold(task_id="done", amount=4, service_id="svc", provider_peer_id="p")
    legacy.settle(task_id="done", amount=4, input_tokens=1, output_tokens=1,
                  duration_ms=1, service_id="svc", provider_peer_id="p")
    legacy.earn(task_id="e", amount=7, input_tokens=1, output_tokens=1,
                duration_ms=1, service_id="svc", consumer_peer_id="c")
    assert json.loads(path.read_text())["version"] == LEDGER_VERSION

    migrated = _ledger(store, path)
    summary = migrated.summary()
    assert summary["available"] == 11.0   # 20 - 5 (open) - 4 (settled)
    assert summary["held"] == 5.0
    assert summary["earned"] == 7.0
    assert json.loads(path.read_text())["version"] == SNAPSHOT_VERSION
    assert path.with_suffix(".json.migrated").exists()
    kinds = sorted(e.payload["kind"] for e in store.credit_ledger.list_events(
        subject_peer_id=store.peer_id, category=TASK_BALANCE_CATEGORY))
    assert kinds == ["task_balance_opening", "task_hold"]
    # Idempotent: constructing again does not migrate or grant twice.
    assert _ledger(store, path).summary()["available"] == 11.0


def test_ledger_mode_requires_an_identity(tmp_path, store):
    with pytest.raises(TaskBalanceError, match="peer_id"):
        TaskBalanceLedger(tmp_path / "b.json", credit_ledger=store.credit_ledger)


def test_recent_events_cache_is_bounded(store, tmp_path):
    ledger = _ledger(store, tmp_path / "balance.json", initial_dev_balance=1000)
    for i in range(120):
        ledger.hold(task_id=f"t{i}", amount=1, service_id="svc", provider_peer_id="p")
        ledger.release(task_id=f"t{i}", reason="x")
    assert len(ledger.events()) <= 200
    assert ledger.summary()["available"] == 1000
