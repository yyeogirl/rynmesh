"""Versioned private configuration and redacted public LLM service manifest."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from rynmesh.atomic_io import atomic_write_json
from rynmesh.crypto import sha256_file

PROTOCOL_VERSION = "rynmesh.llm.task.v1"
PACKAGE_VERSION = "0.1.0"
MODES = {"managed", "import_gguf", "openai_compatible", "ollama"}


class ManifestError(ValueError):
    pass


@dataclass
class Pricing:
    currency: str = "DEV_TASK_BALANCE"
    input_per_1k: float = 0.001
    output_per_1k: float = 0.002
    minimum: float = 0.001
    maximum_per_task: float = 1.0


@dataclass
class PrivacyPolicy:
    log_bodies: bool = False
    temporary_storage: bool = False
    retention_seconds: int = 0
    compute_node_sees_plaintext: bool = True
    policy_text: str = "No prompt/response logging; plaintext exists at the compute node during inference."


@dataclass
class LLMPackageManifest:
    package_id: str
    mode: str
    public_model_alias: str
    adapter: str = "openai_compatible"
    runtime: str = "external"
    version: str = PACKAGE_VERSION
    protocol_version: str = PROTOCOL_VERSION
    capabilities: list[str] = field(default_factory=lambda: ["text-generation"])
    context_window: int = 4096
    max_output_tokens: int = 512
    max_concurrent: int = 1
    queue_limit: int = 0
    timeout_seconds: float = 120.0
    hardware_requirements: dict[str, Any] = field(default_factory=dict)
    pricing: Pricing = field(default_factory=Pricing)
    privacy: PrivacyPolicy = field(default_factory=PrivacyPolicy)
    healthcheck: dict[str, Any] = field(default_factory=lambda: {"path": "/health", "timeout_seconds": 5})
    lifecycle: dict[str, str] = field(default_factory=dict)
    install_source: dict[str, str] = field(default_factory=dict)
    checksum: str = ""
    license_notice: str = "Operator must review and comply with the model and runtime licenses."
    license_id: str = "unknown"
    risk_labels: list[str] = field(default_factory=list)
    content_rules: str = "Provider-defined content policy applies."
    model_fingerprint: str = ""
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""
    model_path: str = ""
    runtime_command: list[str] = field(default_factory=list)
    runtime_dir: str = ""
    # Loopback bearer token minted for a Rynmesh-owned `llama-server`, so a web
    # page the owner happens to visit cannot reach the local inference port.
    # Node-private: never in `public_dict()`, a log line, or a raised message.
    runtime_api_key: str = ""
    model_owned: bool = False
    allow_non_loopback: bool = False
    debug_log_bodies: bool = False

    def validate(self) -> None:
        if not self.package_id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for ch in self.package_id):
            raise ManifestError("package_id must be a non-empty lowercase slug")
        if self.mode not in MODES:
            raise ManifestError(f"unsupported mode: {self.mode}")
        if not self.public_model_alias.strip():
            raise ManifestError("public_model_alias is required")
        if self.context_window < 256 or self.max_output_tokens < 1:
            raise ManifestError("context/output limits are invalid")
        if self.max_output_tokens > self.context_window:
            raise ManifestError("max_output_tokens exceeds context_window")
        if self.max_concurrent < 1 or self.queue_limit < 0 or self.timeout_seconds <= 0:
            raise ManifestError("capacity/timeout limits are invalid")
        prices = (
            self.pricing.input_per_1k,
            self.pricing.output_per_1k,
            self.pricing.minimum,
            self.pricing.maximum_per_task,
        )
        if self.pricing.currency != "DEV_TASK_BALANCE":
            raise ManifestError("only the development Task Balance is supported")
        if any(not math.isfinite(float(value)) or float(value) < 0 for value in prices):
            raise ManifestError("pricing values must be finite and non-negative")
        if self.pricing.minimum > self.pricing.maximum_per_task:
            raise ManifestError("minimum price exceeds maximum_per_task")
        if self.privacy.retention_seconds < 0:
            raise ManifestError("privacy retention_seconds must be non-negative")
        if self.mode == "import_gguf" and not self.model_path:
            raise ManifestError("GGUF import requires model_path")
        if self.mode in {"openai_compatible", "ollama"} and not self.base_url:
            raise ManifestError("local API mode requires base_url")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """Return only owner-approved discovery information.

        Local paths, filenames, runtime commands, URLs, key references, the
        loopback runtime bearer token, and installation source details are
        intentionally excluded.
        """
        return {
            "package_id": self.package_id,
            "version": self.version,
            "protocol_version": self.protocol_version,
            "adapter": self.adapter,
            "runtime": self.runtime,
            "model_alias": self.public_model_alias,
            "capabilities": list(self.capabilities),
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "max_concurrent": self.max_concurrent,
            "queue_limit": self.queue_limit,
            "timeout_seconds": self.timeout_seconds,
            "hardware_requirements": dict(self.hardware_requirements),
            "pricing": asdict(self.pricing),
            "privacy": asdict(self.privacy),
            "healthcheck": {"enabled": True},
            "license_id": self.license_id,
            "license_notice": self.license_notice,
            "risk_labels": list(self.risk_labels),
            "content_rules": self.content_rules,
            "model_fingerprint": self.model_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMPackageManifest":
        values = dict(data)
        values["pricing"] = Pricing(**dict(values.get("pricing") or {}))
        values["privacy"] = PrivacyPolicy(**dict(values.get("privacy") or {}))
        manifest = cls(**values)
        manifest.validate()
        return manifest


def load_manifest(path: str | Path) -> LLMPackageManifest:
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be an object")
    return LLMPackageManifest.from_dict(data)


def save_manifest(manifest: LLMPackageManifest, path: str | Path) -> Path:
    manifest.validate()
    target = Path(path).expanduser()
    # Owner-only: this file carries the loopback runtime bearer token, local
    # model paths, and the runtime command line. `atomic_write_json` durably
    # writes it via a uniquely named temp file (no cross-write collision),
    # fsyncs before the rename (no crash-truncated manifest), and applies
    # 0600 on every platform (previously skipped on Windows). `AtomicIOError`
    # is an `OSError` subclass, so callers that already catch `OSError`
    # around `save_manifest` (e.g. lifecycle's install rollback) keep working
    # unchanged.
    atomic_write_json(target, manifest.to_dict(), indent=2, sort_keys=True)
    return target


def fingerprint_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    return sha256_file(path, chunk_size=chunk_size)
