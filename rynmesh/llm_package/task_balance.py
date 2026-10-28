"""Development-only Task Balance with idempotent holds and settlements.

Two persistence backends behind one interface:

- **standalone** (default): one mutable JSON file, as before. Used by tests,
  the CLI, and any embedder without a node identity.
- **ledger-backed**: every transition is also a signed ``CreditEvent`` in the
  ``dev:task_balance`` category of the node's ``FileCreditLedger`` — one
  auditable, append-only history for every service's escrow. Reputation
  scoring never sees that category (see ``credits._category_matches``), so
  Credits stay non-monetary while sharing the ledger.

In ledger-backed mode the JSON file becomes a materialized *snapshot*: the
balances, the open-hold map (which doubles as the idempotency index), and a
bounded recent-events cache. Hold/settle/release stay O(1) — the ledger's own
``list_events`` re-reads and re-verifies every file per call, which is right
for reputation and wrong for a per-order path. A missing or inconsistent
snapshot is rebuilt by replaying the category.
"""

from __future__ import annotations

import json
import math
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rynmesh.atomic_io import atomic_write_json, migration_backup

LEDGER_VERSION = "rynmesh-dev-task-balance-v1"
SNAPSHOT_VERSION = "rynmesh-dev-task-balance-view-v2"
TERMINAL_HOLD_STATES = {"settled", "released"}
CURRENCY = "DEV_TASK_BALANCE"
_RECENT_EVENTS = 200

# Local transition kind -> signed credit event kind.
_CREDIT_KINDS = {
    "opening": "task_balance_opening",
    "hold": "task_hold",
    "rehold": "task_hold",
    "settle": "task_settle",
    "release": "task_release",
    "earning": "task_earning",
}


class TaskBalanceError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _amount(value: float) -> float:
    number = round(float(value), 8)
    if not math.isfinite(number) or number < 0:
        raise TaskBalanceError("amount must be a finite non-negative number")
    return number


class TaskBalanceLedger:
    """A local simulated balance. Not money, a deposit, or a payment system."""

    def __init__(
        self,
        path: str | Path,
        *,
        initial_dev_balance: float = 100.0,
        credit_ledger: Any | None = None,
        peer_id: str = "",
        private_key_bytes: bytes | None = None,
    ) -> None:
        self.path = Path(path)
        self.initial_dev_balance = _amount(initial_dev_balance)
        self._lock = threading.RLock()
        self._ledger = credit_ledger
        self._peer_id = peer_id
        self._private_key = private_key_bytes
        if credit_ledger is not None and not (peer_id and private_key_bytes):
            raise TaskBalanceError("ledger-backed Task Balance needs peer_id and private_key_bytes")
        with self._lock:
            if self._ledger is None:
                if not self.path.exists():
                    self._write(self._fresh_state(LEDGER_VERSION))
                return
            self._initialize_ledger_backed()

    # ---- public API (identical in both modes) ---------------------------
    def summary(self) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            return {
                "version": state["version"],
                "development_only": True,
                "available": state["available"],
                "held": state["held"],
                "earned": state["earned"],
                "currency": CURRENCY,
                "ledger_backed": self._ledger is not None,
            }

    def hold(self, *, task_id: str, amount: float, service_id: str, provider_peer_id: str,
             idempotency_key: str = "", request_fingerprint: str = "") -> dict[str, Any]:
        value = _amount(amount)
        if not task_id:
            raise TaskBalanceError("task_id is required")
        with self._lock:
            state = self._read()
            record = self._apply_hold(
                state, task_id=task_id, amount=value, service_id=service_id,
                provider_peer_id=provider_peer_id, idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint, replay=False,
            )
            self._write(state)
            return record

    def settle(self, *, task_id: str, amount: float, input_tokens: int, output_tokens: int,
               duration_ms: int, service_id: str, provider_peer_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            record = self._apply_settle(
                state, task_id=task_id, amount=_amount(amount), input_tokens=int(input_tokens),
                output_tokens=int(output_tokens), duration_ms=int(duration_ms),
                service_id=service_id, provider_peer_id=provider_peer_id, replay=False,
            )
            self._write(state)
            return record

    def release(self, *, task_id: str, reason: str) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            record = self._apply_release(state, task_id=task_id, reason=reason, replay=False)
            self._write(state)
            return record

    def earn(self, *, task_id: str, amount: float, input_tokens: int, output_tokens: int,
             duration_ms: int, service_id: str, consumer_peer_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            event = self._apply_earning(
                state, task_id=task_id, amount=_amount(amount), input_tokens=int(input_tokens),
                output_tokens=int(output_tokens), duration_ms=int(duration_ms),
                service_id=service_id, consumer_peer_id=consumer_peer_id, replay=False,
            )
            self._write(state)
            return event

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._read()["events"]]

    def rebuild(self) -> dict[str, Any]:
        """Replay the signed category into a fresh snapshot (ledger-backed only)."""
        if self._ledger is None:
            raise TaskBalanceError("rebuild requires a ledger-backed Task Balance")
        with self._lock:
            state = self._replay()
            self._write(state)
            return self.summary()

    # ---- transitions (shared by live calls and replay) ------------------
    def _apply_hold(self, state, *, task_id, amount, service_id, provider_peer_id,
                    idempotency_key, request_fingerprint, replay, created_at=None):
        if task_id in state["holds"]:
            previous = state["holds"][task_id]
            expected = {
                "service_id": str(service_id),
                "provider_peer_id": str(provider_peer_id),
                "idempotency_key": str(idempotency_key or task_id),
                "request_fingerprint": str(request_fingerprint),
            }
            actual = {key: str(previous.get(key) or "") for key in expected}
            if actual != expected or _amount(previous["amount"]) != amount:
                raise TaskBalanceError("task idempotency conflict")
            if previous["state"] != "released":
                return dict(previous)
            if amount > float(state["available"]):
                raise TaskBalanceError("insufficient development Task Balance")
            state["available"] = _amount(float(state["available"]) - amount)
            state["held"] = _amount(float(state["held"]) + amount)
            previous.update({"state": "held", "retried_at": created_at or _now()})
            previous.pop("reason", None)
            previous.pop("released_at", None)
            previous["rehold_count"] = int(previous.get("rehold_count") or 0) + 1
            self._event(state, "rehold", {"task_id": task_id, "amount": amount}, replay=replay,
                        dedupe=f"hold:{task_id}:{previous['rehold_count']}",
                        metadata=expected)
            return dict(previous)
        if amount > float(state["available"]):
            raise TaskBalanceError("insufficient development Task Balance")
        record = {"task_id": task_id, "amount": amount, "state": "held",
                  "service_id": service_id, "provider_peer_id": provider_peer_id,
                  "idempotency_key": str(idempotency_key or task_id),
                  "request_fingerprint": str(request_fingerprint),
                  "created_at": created_at or _now()}
        state["available"] = _amount(float(state["available"]) - amount)
        state["held"] = _amount(float(state["held"]) + amount)
        state["holds"][task_id] = record
        self._event(state, "hold", record, replay=replay, dedupe=f"hold:{task_id}",
                    metadata={k: record[k] for k in ("service_id", "provider_peer_id",
                                                     "idempotency_key", "request_fingerprint")})
        return dict(record)

    def _apply_settle(self, state, *, task_id, amount, input_tokens, output_tokens,
                      duration_ms, service_id, provider_peer_id, replay, created_at=None):
        hold = state["holds"].get(task_id)
        if not hold:
            raise TaskBalanceError("task hold not found")
        if hold["state"] == "settled":
            return dict(hold)
        if hold["state"] == "released":
            raise TaskBalanceError("released task cannot be settled")
        reserved = _amount(hold["amount"])
        if amount > reserved:
            raise TaskBalanceError("settlement exceeds frozen amount")
        refund = _amount(reserved - amount)
        state["held"] = _amount(float(state["held"]) - reserved)
        state["available"] = _amount(float(state["available"]) + refund)
        hold.update({"state": "settled", "settled_amount": amount, "refund": refund,
                     "input_tokens": input_tokens, "output_tokens": output_tokens,
                     "duration_ms": duration_ms, "settled_at": created_at or _now()})
        billing = {"task_id": task_id, "service_id": service_id,
                   "provider_peer_id": provider_peer_id, "input_tokens": input_tokens,
                   "output_tokens": output_tokens, "duration_ms": duration_ms,
                   "amount": amount, "currency": CURRENCY}
        self._event(state, "settle", billing, replay=replay, dedupe=f"settle:{task_id}",
                    metadata={k: billing[k] for k in ("service_id", "provider_peer_id",
                                                      "input_tokens", "output_tokens",
                                                      "duration_ms")})
        return dict(hold)

    def _apply_release(self, state, *, task_id, reason, replay, created_at=None):
        hold = state["holds"].get(task_id)
        if not hold:
            raise TaskBalanceError("task hold not found")
        if hold["state"] in TERMINAL_HOLD_STATES:
            return dict(hold)
        reserved = _amount(hold["amount"])
        state["held"] = _amount(float(state["held"]) - reserved)
        state["available"] = _amount(float(state["available"]) + reserved)
        hold.update({"state": "released", "reason": str(reason)[:120],
                     "released_at": created_at or _now()})
        self._event(state, "release", {"task_id": task_id, "amount": reserved,
                                        "reason": str(reason)[:120]}, replay=replay,
                    dedupe=f"release:{task_id}:{int(hold.get('rehold_count') or 0)}",
                    metadata={"reason": str(reason)[:120]})
        return dict(hold)

    def _apply_earning(self, state, *, task_id, amount, input_tokens, output_tokens,
                       duration_ms, service_id, consumer_peer_id, replay, created_at=None):
        key = "earning:" + task_id
        if key in state.setdefault("earnings", {}):
            return dict(state["earnings"][key])
        state["earned"] = _amount(float(state["earned"]) + amount)
        event = {"event_id": key, "kind": "earning", "task_id": task_id,
                 "service_id": service_id, "consumer_peer_id": consumer_peer_id,
                 "input_tokens": input_tokens, "output_tokens": output_tokens,
                 "duration_ms": duration_ms, "amount": amount,
                 "currency": CURRENCY, "created_at": created_at or _now()}
        state["earnings"][key] = event
        state["events"].append(dict(event))
        self._trim(state)
        if not replay:
            self._append_credit("earning", task_id, amount, key, {
                "service_id": service_id, "consumer_peer_id": consumer_peer_id,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "duration_ms": duration_ms,
            })
        return dict(event)

    # ---- event plumbing ------------------------------------------------
    def _event(self, state, kind, fields, *, replay, dedupe, metadata):
        state["events"].append({"event_id": uuid.uuid4().hex, "kind": kind,
                                "created_at": _now(), **fields})
        self._trim(state)
        if not replay:
            self._append_credit(kind, str(fields.get("task_id") or ""),
                                _amount(fields.get("amount", 0.0)), dedupe, metadata)

    def _trim(self, state) -> None:
        if len(state["events"]) > _RECENT_EVENTS:
            del state["events"][: len(state["events"]) - _RECENT_EVENTS]

    def _append_credit(self, kind, task_id, amount, dedupe, metadata) -> None:
        if self._ledger is None:
            return
        result = self._ledger.record(
            subject_peer_id=self._peer_id, issuer_peer_id=self._peer_id,
            private_key_bytes=self._private_key, kind=_CREDIT_KINDS[kind], amount=amount,
            role="consumer" if kind in {"hold", "rehold", "settle", "release"} else "provider",
            category=_task_balance_category(), subject_id=task_id, dedupe_key=dedupe,
            reason=kind, metadata={"phase": kind, **dict(metadata)},
        )
        if result.get("status") == "recorded":
            self._folded += 1

    # ---- ledger-backed initialization / replay ---------------------------
    def _initialize_ledger_backed(self) -> None:
        self._folded = 0
        existing = None
        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
        category_count = self._category_event_count()
        if existing and existing.get("version") == SNAPSHOT_VERSION:
            if int(existing.get("folded_events") or 0) == category_count:
                self._folded = category_count
                return
            self._write(self._replay())
            return
        if existing and existing.get("version") == LEDGER_VERSION and category_count == 0:
            self._migrate_legacy(existing)
            return
        if category_count:
            self._write(self._replay())
            return
        state = self._fresh_state(SNAPSHOT_VERSION)
        # The opening grant must land in the snapshot as well as the ledger;
        # replay applies it from the event, the live path applies it here.
        state["available"] = self.initial_dev_balance
        self._append_credit("opening", "", self.initial_dev_balance, "opening:initial",
                            {"earned": 0.0, "source": "initial_dev_grant"})
        self._write(state)

    def _migrate_legacy(self, legacy: dict[str, Any]) -> None:
        """Turn a v1 file into signed events + a v2 snapshot, once."""
        migration_backup(self.path, suffix=".migrated")
        state = self._fresh_state(SNAPSHOT_VERSION)
        opening = _amount(float(legacy.get("available") or 0) + float(legacy.get("held") or 0))
        state["available"] = opening
        state["earned"] = _amount(float(legacy.get("earned") or 0))
        self._append_credit("opening", "", opening, "opening:migrated",
                            {"earned": state["earned"], "source": "legacy_task_balance_v1"})
        for task_id, hold in sorted(dict(legacy.get("holds") or {}).items()):
            if hold.get("state") != "held":
                continue
            self._apply_hold(
                state, task_id=task_id, amount=_amount(hold["amount"]),
                service_id=str(hold.get("service_id") or ""),
                provider_peer_id=str(hold.get("provider_peer_id") or ""),
                idempotency_key=str(hold.get("idempotency_key") or task_id),
                request_fingerprint=str(hold.get("request_fingerprint") or ""),
                replay=False, created_at=str(hold.get("created_at") or _now()),
            )
        self._write(state)

    def _category_events(self) -> list[dict[str, Any]]:
        from rynmesh.credits import CreditEvent

        signed = self._ledger.list_events(
            subject_peer_id=self._peer_id, category=_task_balance_category(),
        )
        events = [CreditEvent.from_payload(item.payload) for item in signed]
        events.sort(key=lambda event: (event.created_at, event.dedupe_key))
        return [{"kind": e.kind, "amount": float(e.amount), "task_id": e.subject_id,
                 "created_at": e.created_at, "metadata": dict(e.metadata),
                 "dedupe_key": e.dedupe_key} for e in events]

    def _category_event_count(self) -> int:
        return len(self._category_events())

    def _replay(self) -> dict[str, Any]:
        state = self._fresh_state(SNAPSHOT_VERSION)
        events = self._category_events()
        for item in events:
            meta = item["metadata"]
            kind, task_id, amount = item["kind"], item["task_id"], _amount(item["amount"])
            try:
                if kind == "task_balance_opening":
                    state["available"] = _amount(float(state["available"]) + amount)
                    state["earned"] = _amount(float(meta.get("earned") or 0))
                elif kind == "task_hold":
                    self._apply_hold(
                        state, task_id=task_id, amount=amount,
                        service_id=str(meta.get("service_id") or ""),
                        provider_peer_id=str(meta.get("provider_peer_id") or ""),
                        idempotency_key=str(meta.get("idempotency_key") or task_id),
                        request_fingerprint=str(meta.get("request_fingerprint") or ""),
                        replay=True, created_at=item["created_at"],
                    )
                elif kind == "task_settle":
                    self._apply_settle(
                        state, task_id=task_id, amount=amount,
                        input_tokens=int(meta.get("input_tokens") or 0),
                        output_tokens=int(meta.get("output_tokens") or 0),
                        duration_ms=int(meta.get("duration_ms") or 0),
                        service_id=str(meta.get("service_id") or ""),
                        provider_peer_id=str(meta.get("provider_peer_id") or ""),
                        replay=True, created_at=item["created_at"],
                    )
                elif kind == "task_release":
                    self._apply_release(state, task_id=task_id,
                                        reason=str(meta.get("reason") or ""),
                                        replay=True, created_at=item["created_at"])
                elif kind == "task_earning":
                    self._apply_earning(
                        state, task_id=task_id, amount=amount,
                        input_tokens=int(meta.get("input_tokens") or 0),
                        output_tokens=int(meta.get("output_tokens") or 0),
                        duration_ms=int(meta.get("duration_ms") or 0),
                        service_id=str(meta.get("service_id") or ""),
                        consumer_peer_id=str(meta.get("consumer_peer_id") or ""),
                        replay=True, created_at=item["created_at"],
                    )
            except TaskBalanceError:
                # A transition that was invalid at write time cannot exist in
                # the ledger; one that is invalid only under replay ordering
                # (equal timestamps) is skipped rather than aborting the view.
                continue
        self._folded = len(events)
        return state

    # ---- persistence -----------------------------------------------------
    def _fresh_state(self, version: str) -> dict[str, Any]:
        return {
            "version": version, "development_only": True,
            "available": self.initial_dev_balance if version == LEDGER_VERSION else 0.0,
            "held": 0.0, "earned": 0.0, "holds": {}, "earnings": {}, "events": [],
        }

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskBalanceError(f"cannot read Task Balance ledger: {exc}") from exc
        expected = LEDGER_VERSION if self._ledger is None else SNAPSHOT_VERSION
        if value.get("version") != expected or not value.get("development_only"):
            raise TaskBalanceError("not a development Task Balance ledger")
        value.setdefault("earnings", {})
        return value

    def _write(self, value: dict[str, Any]) -> None:
        if self._ledger is not None:
            value["folded_events"] = self._folded
        atomic_write_json(self.path, value, indent=2, sort_keys=True)


def _task_balance_category() -> str:
    from rynmesh.credits import TASK_BALANCE_CATEGORY

    return TASK_BALANCE_CATEGORY
