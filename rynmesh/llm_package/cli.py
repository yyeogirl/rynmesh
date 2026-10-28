"""Single entry point for the local LLM package MVP."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .adapters import AdapterError
from .hardware import report_json
from .lifecycle import (
    LifecycleError,
    connect_local_api,
    import_gguf,
    install_managed,
    manifest_path,
    restart,
    self_test,
    start,
    status,
    stop,
    uninstall,
    update,
)
from .manifest import ManifestError


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _path(args: argparse.Namespace) -> Path:
    return Path(args.manifest) if getattr(args, "manifest", "") else manifest_path(args.package_id, args.root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rynmesh-llm",
        description="Install, import, connect, publish, and operate a private local LLM without hand-written runtime commands.",
    )
    parser.add_argument("--root", default=os.environ.get("RYNMESH_LLM_HOME", ""))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("detect", help="detect hardware and print safe recommendations")
    setup = sub.add_parser("setup", help="run the one-entry setup wizard")
    setup.add_argument("--mode", choices=["managed", "import-gguf", "openai-compatible", "ollama"], required=True)
    setup.add_argument("--package-id", default="local-small")
    setup.add_argument("--alias", default="rynmesh-local")
    setup.add_argument("--port", type=int, default=18080)
    setup.add_argument("--model-path", default="")
    setup.add_argument("--base-url", default="http://127.0.0.1:8080")
    setup.add_argument("--model", default="")
    setup.add_argument("--api-key-env", default="")
    setup.add_argument("--allow-non-loopback", action="store_true")
    setup.add_argument("--accept-risk", action="store_true")
    setup.add_argument("--yes", action="store_true", help="confirm downloads/runtime preparation")
    setup.add_argument(
        "--runtime", choices=["auto", "native", "docker"], default="auto",
        help="inference runtime backend for managed/import-gguf modes (default: auto)",
    )
    setup.add_argument(
        "--profile", choices=["auto", "light", "balanced", "quality"], default="auto",
        help="catalog model profile for managed mode (default: auto)",
    )
    for name in ("start", "stop", "restart", "status", "update", "self-test", "uninstall"):
        command = sub.add_parser(name)
        command.add_argument("--package-id", default="local-small")
        command.add_argument("--manifest", default="")
        if name == "uninstall":
            command.add_argument("--delete-model", action="store_true")
            command.add_argument("--confirm-model-delete", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "detect":
            print(report_json(args.root or None))
        elif args.command == "setup":
            if args.mode in {"managed", "import-gguf"} and not args.yes:
                raise LifecycleError("runtime/model preparation requires --yes confirmation")
            if args.mode == "managed":
                _emit(install_managed(package_id=args.package_id, root=args.root or None,
                                      port=args.port, accept_risk=args.accept_risk,
                                      runtime=args.runtime, profile=args.profile))
            elif args.mode == "import-gguf":
                if not args.model_path:
                    raise LifecycleError("--model-path is required for GGUF import")
                _emit(import_gguf(source=args.model_path, package_id=args.package_id,
                                  alias=args.alias, root=args.root or None, port=args.port,
                                  accept_risk=args.accept_risk, runtime=args.runtime))
            else:
                _emit(connect_local_api(
                    base_url=args.base_url, package_id=args.package_id, alias=args.alias,
                    model=args.model, api_key_env=args.api_key_env,
                    adapter="ollama" if args.mode == "ollama" else "openai_compatible",
                    root=args.root or None, allow_non_loopback=args.allow_non_loopback,
                ))
        elif args.command == "start":
            _emit(start(_path(args)))
        elif args.command == "stop":
            _emit(stop(_path(args)))
        elif args.command == "restart":
            _emit(restart(_path(args)))
        elif args.command == "status":
            _emit(status(_path(args)))
        elif args.command == "update":
            _emit(update(_path(args)))
        elif args.command == "self-test":
            from .manifest import load_manifest
            _emit(self_test(load_manifest(_path(args))))
        elif args.command == "uninstall":
            _emit(uninstall(_path(args), delete_model=args.delete_model,
                            confirm_model_delete=args.confirm_model_delete))
        return 0
    except (LifecycleError, AdapterError, ManifestError, OSError, ValueError) as exc:
        print(f"rynmesh-llm: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
