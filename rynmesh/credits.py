"""Rynmesh Credits: signed contribution events and distribution reputation.

Credits are not a transferable token in this phase. They are a verifiable,
append-only reputation layer that lets peers and registries weight discovery by
useful work: publishing safe content, serving bytes, operating registries, and
respecting protocol rules.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .crypto import SignedPayload, canonical_json, sha256_bytes, sign_payload, verify_signed_payload
from .identity import IdentityTier, tier_can_issue, tier_distribution_cap
from .types import now_iso

CREDIT_LEDGER_VERSION = "rynmesh-credits-v0.1"
GLOBAL_CATEGORY = "global"
GENERAL_CATEGORY = "general"
# Categories under this prefix are accounting views (escrow holds, dev
# balances), not contribution evidence. They are never folded into the
# global/general reputation score; a scoreboard must ask for them by name.
DEV_CATEGORY_PREFIX = "dev:"
TASK_BALANCE_CATEGORY = DEV_CATEGORY_PREFIX + "task_balance"


EVENT_WEIGHTS: dict[str, float] = {
    "node_joined": 1.0,
    "content_published": 8.0,
    "clip_published": 8.0,
    "preview_served": 0.5,
    "full_served": 2.0,
    "registry_operated": 10.0,
    "availability_attested": 1.0,
    "job_capacity_advertised": 0.25,
    "work_order_completed": 0.0,
    # Escrow accounting for peer services (category dev:task_balance). Amounts
    # are custom, non-negative units of the development Task Balance; the
    # kind says which direction the fold applies them in.
    "task_balance_opening": 0.0,
    "task_hold": 0.0,
    "task_release": 0.0,
    "task_settle": 0.0,
    "task_earning": 0.0,
    "safety_blocked": -5.0,
    "spam_reported": -10.0,
    "protocol_violation": -25.0,
    "illegal_content": -100.0,
}
CUSTOM_AMOUNT_EVENT_KINDS = {
    "work_order_completed",
    "task_balance_opening",
    "task_hold",
    "task_release",
    "task_settle",
    "task_earning",
}


class CreditLedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CreditPolicy:
    positive_half_life_days: float = 180.0
    min_distribution_weight: float = 0.05
    max_distribution_weight: float = 5.0
    exploration_fraction: float = 0.15
    # Sublinear score -> distribution_weight transform (vision OQ-14).
    # weight scales as score**beta; beta=0.5 is the existing sqrt behavior
    # (kept as the default for backward compatibility); beta<0.5 compresses
    # ratios more aggressively (e.g., 0.33 ≈ cube root). 1.0 disables.
    weight_transform_beta: float = 0.5
    trusted_penalty_issuers: tuple[str, ...] = ()
    enforce_issuer_tier: bool = False
    enforce_distribution_tier_cap: bool = False


@dataclass(frozen=True)
class CreditEvent:
    subject_peer_id: str
    issuer_peer_id: str
    kind: str
    amount: float
    role: str = "node"
    category: str = GENERAL_CATEGORY
    subject_id: str = ""
    evidence_hash: str = ""
    reason: str = ""
    dedupe_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    credit_version: str = CREDIT_LEDGER_VERSION

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "credit_version": self.credit_version,
            "created_at": self.created_at,
            "subject_peer_id": self.subject_peer_id,
            "issuer_peer_id": self.issuer_peer_id,
            "kind": self.kind,
            "amount": self.amount,
            "role": self.role,
            "category": self.category or GENERAL_CATEGORY,
            "subject_id": self.subject_id,
            "evidence_hash": self.evidence_hash,
            "reason": self.reason,
            "dedupe_key": self.dedupe_key,
            "metadata": dict(self.metadata),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.unsigned_payload()
        payload["event_id"] = credit_event_id(payload)
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CreditEvent":
        expected_id = credit_event_id({key: value for key, value in payload.items() if key != "event_id"})
        if payload.get("event_id") and payload["event_id"] != expected_id:
            raise CreditLedgerError("credit_event_id_mismatch")
        return cls(
            credit_version=str(payload.get("credit_version", CREDIT_LEDGER_VERSION)),
            created_at=str(payload["created_at"]),
            subject_peer_id=str(payload["subject_peer_id"]),
            issuer_peer_id=str(payload["issuer_peer_id"]),
            kind=str(payload["kind"]),
            amount=float(payload["amount"]),
            role=str(payload.get("role", "node")),
            category=str(payload.get("category", GENERAL_CATEGORY) or GENERAL_CATEGORY),
            subject_id=str(payload.get("subject_id", "")),
            evidence_hash=str(payload.get("evidence_hash", "")),
            reason=str(payload.get("reason", "")),
            dedupe_key=str(payload.get("dedupe_key", "")),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True)
class CreditAccount:
    peer_id: str
    score: float
    raw_positive: float
    raw_negative: float
    distribution_weight: float
    event_count: int
    categories: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "score": round(self.score, 6),
            "raw_positive": round(self.raw_positive, 6),
            "raw_negative": round(self.raw_negative, 6),
            "distribution_weight": round(self.distribution_weight, 6),
            "event_count": self.event_count,
            "categories": {key: round(value, 6) for key, value in sorted(self.categories.items())},
        }


def credit_event_id(unsigned_payload: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(dict(unsigned_payload)))


def default_credit_amount(kind: str) -> float:
    if kind not in EVENT_WEIGHTS:
        raise CreditLedgerError(f"unknown_credit_event_kind: {kind}")
    return EVENT_WEIGHTS[kind]


def sign_credit_event(event: CreditEvent, *, private_key_bytes: bytes) -> SignedPayload:
    return sign_payload(event.to_payload(), private_key_bytes=private_key_bytes)


def verify_credit_event(signed_event: SignedPayload) -> CreditEvent:
    return verify_credit_event_with_policy(signed_event, policy=CreditPolicy())


def verify_credit_event_with_policy(
    signed_event: SignedPayload,
    *,
    policy: CreditPolicy,
    issuer_tier: IdentityTier | None = None,
) -> CreditEvent:
    try:
        verify_signed_payload(signed_event)
        event = CreditEvent.from_payload(signed_event.payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise CreditLedgerError(f"invalid_credit_event: {exc}") from exc
    if event.issuer_peer_id != signed_event.public_key:
        raise CreditLedgerError("credit_event_issuer_key_mismatch")
    if not _amount_allowed(event.kind, event.amount):
        raise CreditLedgerError("credit_event_amount_mismatch")
    _parse_time(event.created_at)
    if (
        event.amount < 0
        and event.issuer_peer_id != event.subject_peer_id
        and event.issuer_peer_id not in policy.trusted_penalty_issuers
    ):
        raise CreditLedgerError("credit_penalty_issuer_untrusted")
    if policy.enforce_issuer_tier:
        if issuer_tier is None:
            # Fail closed: the policy demands tier evidence and we have none.
            raise CreditLedgerError("credit_issuer_tier_required")
        if not tier_can_issue(issuer_tier, event.kind):
            raise CreditLedgerError(f"credit_issuer_tier_too_low: {issuer_tier.value}")
    return event


TierResolver = Callable[[str], Optional[IdentityTier]]


class FileCreditLedger:
    """Append-only file ledger for signed credit events."""

    def __init__(
        self,
        root: str | Path,
        *,
        policy: CreditPolicy | None = None,
        tier_resolver: TierResolver | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.policy = policy or CreditPolicy()
        self.tier_resolver = tier_resolver
        if self.policy.enforce_issuer_tier and self.tier_resolver is None:
            raise CreditLedgerError("credit_issuer_tier_resolver_required")
        if self.policy.enforce_distribution_tier_cap and self.tier_resolver is None:
            raise CreditLedgerError("credit_distribution_tier_cap_resolver_required")
        self.events_dir.mkdir(parents=True, exist_ok=True)

    def _issuer_tier(self, issuer_peer_id: str) -> IdentityTier | None:
        if self.tier_resolver is None:
            return None
        return self.tier_resolver(issuer_peer_id)

    @property
    def events_dir(self) -> Path:
        return self.root / "events"

    def append(self, signed_event: SignedPayload) -> dict[str, Any]:
        # Peek at the issuer so we can look up its tier before full verification.
        peek_event = CreditEvent.from_payload(signed_event.payload)
        issuer_tier = self._issuer_tier(peek_event.issuer_peer_id)
        event = verify_credit_event_with_policy(
            signed_event,
            policy=self.policy,
            issuer_tier=issuer_tier,
        )
        if event.dedupe_key:
            existing = self.find_by_dedupe(
                issuer_peer_id=event.issuer_peer_id,
                subject_peer_id=event.subject_peer_id,
                kind=event.kind,
                dedupe_key=event.dedupe_key,
            )
            if existing is not None:
                existing_event = verify_credit_event_with_policy(
                    existing,
                    policy=self.policy,
                    issuer_tier=self._issuer_tier(event.issuer_peer_id),
                )
                return {
                    "status": "already_recorded",
                    "event_id": existing_event.to_payload()["event_id"],
                    "subject_peer_id": existing_event.subject_peer_id,
                    "kind": existing_event.kind,
                    "amount": existing_event.amount,
                }
        path = self._event_path(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(signed_event.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return {
            "status": "recorded",
            "event_id": event.to_payload()["event_id"],
            "subject_peer_id": event.subject_peer_id,
            "kind": event.kind,
            "amount": event.amount,
            "path": str(path),
        }

    def record(
        self,
        *,
        subject_peer_id: str,
        issuer_peer_id: str,
        private_key_bytes: bytes,
        kind: str,
        amount: float | None = None,
        role: str = "node",
        category: str = GENERAL_CATEGORY,
        subject_id: str = "",
        evidence_hash: str = "",
        reason: str = "",
        dedupe_key: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = CreditEvent(
            subject_peer_id=subject_peer_id,
            issuer_peer_id=issuer_peer_id,
            kind=kind,
            amount=_resolve_amount(kind, amount),
            role=role,
            category=category or GENERAL_CATEGORY,
            subject_id=subject_id,
            evidence_hash=evidence_hash,
            reason=reason,
            dedupe_key=dedupe_key,
            metadata=dict(metadata or {}),
        )
        signed = sign_credit_event(event, private_key_bytes=private_key_bytes)
        return self.append(signed)

    def list_events(
        self,
        *,
        subject_peer_id: str = "",
        category: str = GLOBAL_CATEGORY,
    ) -> list[SignedPayload]:
        events: list[SignedPayload] = []
        for path in sorted(self.events_dir.glob("*/*.json")):
            try:
                signed = SignedPayload.from_dict(json.loads(path.read_text(encoding="utf-8")))
                # Re-verify with the current policy + resolver so events that
                # passed under an old policy but would fail now are quietly
                # excluded from rankings (write-time gate is the same code path).
                peek = CreditEvent.from_payload(signed.payload)
                event = verify_credit_event_with_policy(
                    signed,
                    policy=self.policy,
                    issuer_tier=self._issuer_tier(peek.issuer_peer_id),
                )
            except (OSError, json.JSONDecodeError, KeyError, ValueError, CreditLedgerError):
                continue
            if subject_peer_id and event.subject_peer_id != subject_peer_id:
                continue
            if not _category_matches(event.category, category):
                continue
            events.append(signed)
        return events

    def find_by_dedupe(
        self,
        *,
        issuer_peer_id: str,
        subject_peer_id: str,
        kind: str,
        dedupe_key: str,
    ) -> SignedPayload | None:
        for signed in self.list_events(subject_peer_id=subject_peer_id):
            event = CreditEvent.from_payload(signed.payload)
            if (
                event.issuer_peer_id == issuer_peer_id
                and event.kind == kind
                and event.dedupe_key == dedupe_key
            ):
                return signed
        return None

    def account(self, peer_id: str, *, category: str = GLOBAL_CATEGORY) -> CreditAccount:
        accounts = self.scoreboard(category=category)
        return accounts.get(peer_id, _empty_account(peer_id, self.policy))

    def scoreboard(
        self,
        *,
        category: str = GLOBAL_CATEGORY,
        tier_resolver: TierResolver | None = None,
    ) -> dict[str, CreditAccount]:
        # Tier cap on distribution_weight is a policy-level decision. When the
        # policy enables it we MUST have a resolver; refuse to silently fall
        # back to the uncapped path because a kwarg was omitted.
        active_resolver = tier_resolver if tier_resolver is not None else self.tier_resolver
        if self.policy.enforce_distribution_tier_cap and active_resolver is None:
            raise CreditLedgerError("credit_distribution_tier_cap_missing_resolver")
        # When the cap is disabled, intentionally drop the resolver so weights
        # are not silently affected by an attached resolver.
        if not self.policy.enforce_distribution_tier_cap:
            active_resolver = None
        now = datetime.now(timezone.utc)
        buckets: dict[str, dict[str, Any]] = {}
        for signed in self.list_events(category=category):
            peek = CreditEvent.from_payload(signed.payload)
            event = verify_credit_event_with_policy(
                signed,
                policy=self.policy,
                issuer_tier=self._issuer_tier(peek.issuer_peer_id),
            )
            bucket = buckets.setdefault(
                event.subject_peer_id,
                {
                    "score": 0.0,
                    "raw_positive": 0.0,
                    "raw_negative": 0.0,
                    "event_count": 0,
                    "categories": {},
                },
            )
            amount = float(event.amount)
            weighted = _weighted_amount(event, now=now, policy=self.policy)
            bucket["score"] += weighted
            bucket["event_count"] += 1
            bucket["categories"][event.category] = bucket["categories"].get(event.category, 0.0) + weighted
            if amount >= 0:
                bucket["raw_positive"] += amount
            else:
                bucket["raw_negative"] += amount
        return {
            peer_id: CreditAccount(
                peer_id=peer_id,
                score=payload["score"],
                raw_positive=payload["raw_positive"],
                raw_negative=payload["raw_negative"],
                distribution_weight=distribution_weight(
                    payload["score"],
                    self.policy,
                    identity_tier=active_resolver(peer_id) if active_resolver else None,
                ),
                event_count=payload["event_count"],
                categories=payload["categories"],
            )
            for peer_id, payload in buckets.items()
        }

    def _event_path(self, event: CreditEvent) -> Path:
        event_id = event.to_payload()["event_id"].split(":", 1)[1]
        peer_slug = sha256_bytes(event.subject_peer_id.encode("utf-8")).split(":", 1)[1][:16]
        return self.events_dir / peer_slug / f"{event_id}.json"


def distribution_weight(
    score: float,
    policy: CreditPolicy | None = None,
    *,
    identity_tier: IdentityTier | None = None,
) -> float:
    active_policy = policy or CreditPolicy()
    if score <= 0:
        base = max(active_policy.min_distribution_weight, 1.0 + (score / 50.0))
    else:
        # Sublinear / saturating transform; β=0.5 (sqrt) is the historical
        # default and is the empirically-validated F4 setting.
        shaped = score ** active_policy.weight_transform_beta
        base = min(active_policy.max_distribution_weight, 1.0 + shaped / 4.0)
    if identity_tier is not None:
        return min(base, tier_distribution_cap(identity_tier))
    return base


def _weighted_amount(event: CreditEvent, *, now: datetime, policy: CreditPolicy) -> float:
    amount = float(event.amount)
    if amount <= 0:
        return amount
    age_days = max(0.0, (now - _parse_time(event.created_at)).total_seconds() / 86400.0)
    half_life = max(1.0, policy.positive_half_life_days)
    return amount * (0.5 ** (age_days / half_life))


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CreditLedgerError("credit_event_time_invalid") from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_amount(kind: str, amount: float | None) -> float:
    expected = default_credit_amount(kind)
    if amount is None:
        return expected
    active_amount = float(amount)
    if not _amount_allowed(kind, active_amount):
        raise CreditLedgerError("credit_event_amount_mismatch")
    return active_amount


def _amount_allowed(kind: str, amount: float) -> bool:
    default_credit_amount(kind)
    if not math.isfinite(amount):
        return False
    if kind in CUSTOM_AMOUNT_EVENT_KINDS:
        return amount >= 0
    return amount == default_credit_amount(kind)


def _category_matches(event_category: str, requested: str) -> bool:
    if event_category.startswith(DEV_CATEGORY_PREFIX):
        # Accounting views are only visible when asked for explicitly.
        return event_category == requested
    if not requested or requested == GLOBAL_CATEGORY:
        return True
    return event_category in {requested, GENERAL_CATEGORY}


def _empty_account(peer_id: str, policy: CreditPolicy) -> CreditAccount:
    return CreditAccount(
        peer_id=peer_id,
        score=0.0,
        raw_positive=0.0,
        raw_negative=0.0,
        distribution_weight=distribution_weight(0.0, policy),
        event_count=0,
        categories={},
    )
