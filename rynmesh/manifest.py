"""Rynmesh content manifest creation and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .crypto import (
    SignatureError,
    SignedPayload,
    sha256_bytes,
    sha256_file,
    sign_payload,
    verify_signed_payload,
)
from .provenance import (
    LINK_GENESIS,
    LINK_SAFETY_SCAN,
    ProvenanceError,
    chain_to_payload,
    verify_chain,
)
from .safety import SafetyPolicy
from .types import ClipManifest, MediaAsset, RunReceipt, SafetyOutcome, SafetyReceipt


class ManifestError(ValueError):
    """Raised when a Rynmesh manifest cannot be used for propagation."""


def _file_hash(path: Path) -> str:
    return sha256_file(path)


def build_media_asset(
    media_path: str | Path,
    *,
    media_type: str = "video/mp4",
    content_kind: str = "",
    preview_path: str | Path | None = None,
    transcript_text: str = "",
    frame_paths: Iterable[str | Path] = (),
    duration_sec: float = 0.0,
    width: int = 0,
    height: int = 0,
) -> MediaAsset:
    media = Path(media_path).expanduser().resolve()
    media_hash = _file_hash(media)
    preview_hash = _file_hash(Path(preview_path).expanduser().resolve()) if preview_path else ""
    transcript_hash = sha256_bytes(transcript_text.encode("utf-8")) if transcript_text else ""
    frame_hashes = tuple(_file_hash(Path(path).expanduser().resolve()) for path in frame_paths)
    return MediaAsset(
        clip_id=media_hash,
        media_hash=media_hash,
        size_bytes=media.stat().st_size,
        media_type=media_type,
        content_kind=content_kind,
        duration_sec=duration_sec,
        width=width,
        height=height,
        preview_hash=preview_hash,
        transcript_hash=transcript_hash,
        frame_hashes=frame_hashes,
    )


def make_manifest(
    *,
    asset: MediaAsset,
    run_receipt: RunReceipt,
    safety_receipts: Iterable[SignedPayload],
    publisher: str,
    private_key_bytes: bytes,
    title: str = "",
    description: str = "",
    tags: Iterable[str] = (),
    provenance_chain: Iterable[SignedPayload] = (),
    attestations: Iterable[dict] = (),
) -> SignedPayload:
    chain_payload: tuple[dict, ...] = ()
    head_hash = ""
    chain_list = list(provenance_chain)
    if chain_list:
        verification = verify_chain(chain_list, expected_content_id=asset.clip_id)
        chain_payload = tuple(chain_to_payload(chain_list))
        head_hash = verification.head_hash
    manifest = ClipManifest(
        asset=asset,
        run_receipt=run_receipt,
        safety_receipts=tuple(receipt.to_dict() for receipt in safety_receipts),
        publisher=publisher,
        title=title,
        description=description,
        tags=tuple(tags),
        provenance_chain=chain_payload,
        provenance_head_hash=head_hash,
        attestations=tuple(dict(item) for item in attestations),
    )
    return sign_payload(manifest.unsigned_payload(), private_key_bytes=private_key_bytes)


@dataclass
class ManifestValidation:
    manifest: ClipManifest | None = None
    errors: list[str] = field(default_factory=list)
    pass_receipts: int = 0
    warn_receipts: int = 0
    block_receipts: int = 0
    provenance_head_hash: str = ""
    provenance_link_count: int = 0
    provenance_link_types: tuple[str, ...] = ()
    provenance_issuers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def has_chain(self) -> bool:
        return self.provenance_link_count > 0

    @property
    def propagates(self) -> bool:
        return self.ok and self.block_receipts == 0 and self.pass_receipts > 0


def validate_manifest(
    signed_manifest: SignedPayload,
    *,
    policy: SafetyPolicy | None = None,
) -> ManifestValidation:
    active_policy = policy or SafetyPolicy()
    validation = ManifestValidation()
    try:
        verify_signed_payload(signed_manifest)
    except SignatureError as exc:
        validation.errors.append(f"manifest_signature_invalid: {exc}")
        return validation

    try:
        manifest = ClipManifest.from_payload(signed_manifest.payload)
    except (KeyError, TypeError, ValueError) as exc:
        validation.errors.append(f"manifest_payload_invalid: {exc}")
        return validation

    validation.manifest = manifest
    if manifest.publisher != signed_manifest.public_key:
        validation.errors.append("manifest_publisher_key_mismatch")
    if manifest.asset.clip_id != manifest.asset.media_hash:
        validation.errors.append("asset_clip_id_must_match_media_hash")
    if not manifest.run_receipt.run_id:
        validation.errors.append("missing_run_id")
    if not manifest.run_receipt.work_id:
        validation.errors.append("missing_work_id")
    if not manifest.run_receipt.envelope_hash.startswith("sha256:"):
        validation.errors.append("missing_envelope_hash")

    seen_scanners: set[str] = set()
    for raw in manifest.safety_receipts:
        try:
            signed_receipt = SignedPayload.from_dict(raw)
            verify_signed_payload(signed_receipt)
            receipt = SafetyReceipt.from_dict(signed_receipt.payload)
        except (KeyError, TypeError, ValueError, SignatureError) as exc:
            validation.errors.append(f"safety_receipt_invalid: {exc}")
            continue
        if receipt.subject_clip_id != manifest.asset.clip_id:
            validation.errors.append("safety_receipt_subject_mismatch")
            continue
        if not active_policy.receipt_trusted(receipt):
            validation.errors.append(f"safety_receipt_untrusted_scanner: {receipt.scanner_id}")
            continue
        scanner_key = f"{receipt.scanner_id}:{signed_receipt.public_key}"
        if scanner_key in seen_scanners:
            validation.errors.append("duplicate_safety_receipt_scanner")
            continue
        seen_scanners.add(scanner_key)
        if receipt.outcome is SafetyOutcome.PASS:
            validation.pass_receipts += 1
        elif receipt.outcome is SafetyOutcome.WARN:
            validation.warn_receipts += 1
            if not active_policy.allow_warnings:
                validation.errors.append("safety_warning_not_allowed")
        elif receipt.outcome is SafetyOutcome.BLOCK:
            validation.block_receipts += 1

    if validation.pass_receipts < active_policy.min_pass_receipts:
        validation.errors.append("not_enough_passing_safety_receipts")
    if validation.block_receipts:
        validation.errors.append("blocked_by_safety_receipt")

    if manifest.provenance_chain or manifest.provenance_head_hash:
        try:
            verification = verify_chain(
                list(manifest.provenance_chain),
                expected_content_id=manifest.asset.clip_id,
                expected_head_hash=manifest.provenance_head_hash or None,
            )
        except ProvenanceError as exc:
            validation.errors.append(f"provenance_chain_invalid: {exc}")
        else:
            validation.provenance_head_hash = verification.head_hash
            validation.provenance_link_count = verification.link_count
            validation.provenance_link_types = verification.link_types
            validation.provenance_issuers = verification.issuers
            if verification.link_types[0] != LINK_GENESIS:
                validation.errors.append("provenance_chain_missing_genesis")
            if manifest.publisher != verification.issuers[0]:
                validation.errors.append("provenance_genesis_publisher_mismatch")
            chain_scanner_keys = {
                issuer
                for issuer, link_type in zip(
                    verification.issuers, verification.link_types, strict=True
                )
                if link_type == LINK_SAFETY_SCAN
            }
            if manifest.safety_receipts and not chain_scanner_keys:
                validation.errors.append("provenance_chain_missing_safety_scan_link")
            for raw in manifest.safety_receipts:
                receipt_key = str(raw.get("public_key", ""))
                if not receipt_key:
                    validation.errors.append("safety_receipt_missing_public_key")
                    break
                if chain_scanner_keys and receipt_key not in chain_scanner_keys:
                    validation.errors.append("safety_receipt_not_in_provenance_chain")
                    break

    return validation


def require_propagation_allowed(
    signed_manifest: SignedPayload,
    *,
    policy: SafetyPolicy | None = None,
) -> ClipManifest:
    validation = validate_manifest(signed_manifest, policy=policy)
    if not validation.propagates or validation.manifest is None:
        raise ManifestError("; ".join(validation.errors) or "manifest_not_allowed")
    return validation.manifest
