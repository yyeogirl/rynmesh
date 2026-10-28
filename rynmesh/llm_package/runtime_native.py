"""Native llama.cpp runtime backend: `llama-server` as a loopback child process.

Mirrors the backend surface of `runtime_docker.py` (`RUNTIME_ID`, `available`,
`prepare`, `start`, `stop`, `remove`, `update`, `state`, `install_source`) so
`lifecycle._backend` can dispatch on `manifest.runtime`. This is the default
managed runtime on consumer desktops, which cannot run Docker (issue #34).

This module resolves and runs the server; `runtime_native_install` obtains it.

A spawned server is loopback-only, CORS-restricted, and authenticated with a
per-package bearer token, so a web page the owner visits cannot reach the
inference port; every server this process starts is stopped again on exit.

Privacy rule for everything here: no filesystem path, prompt, model output, or
runtime API key may appear in a raised message, in `state()`, or in anything
the node itself writes to a log.
"""

from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .adapters import AdapterError, adapter_from_manifest
from .errors import LifecycleError
from .manifest import LLMPackageManifest, fingerprint_file
from .runtime_native_install import (
    MARKER_NAME,
    RUNTIME_RELEASE,
    UNAVAILABLE_REASON,
    UNWRITABLE_STATE,
    asset,
    download,
    find_server,
    managed_root,
    report,
    server_filename,
    usable_server,
)

RUNTIME_ID = "native_llama_cpp"

STARTUP_GRACE_SECONDS = 2.0
STOP_GRACE_SECONDS = 10.0

# `llama-server` ships two dangerous defaults for a loopback service: CORS open
# to every origin (`--cors-origins *`) and no authentication, which together let
# any page the owner happens to visit POST to the inference port and read the
# reply. Both are closed on every spawn; the flag spellings are the pinned
# release's own (`--cors-origins localhost` reflects only a localhost Origin,
# `-lv N` is the log-verbosity threshold, `1` = errors only).
CORS_ORIGINS = "localhost"
LOG_VERBOSITY = "1"
API_KEY_BYTES = 32

# Child handles owned by this process, so `stop()`/`state()` can reap an exited
# server instead of reading a zombie pid as "still running", and so shutdown
# can stop every server this node started.
_CHILDREN: dict[int, subprocess.Popen] = {}
_PIDFILES: dict[int, Path] = {}


# --------------------------------------------------------------------------
# Binary resolution
# --------------------------------------------------------------------------

def _default_root() -> Path:
    # Imported lazily: `lifecycle` imports this module at import time.
    from .lifecycle import default_root

    return default_root()


def _marker_server(base: Path) -> Path | None:
    """Server recorded by a completed managed download, if the marker is sane."""
    try:
        marker = json.loads((base / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("release") != RUNTIME_RELEASE:
        return None
    relative = marker.get("server")
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        return None
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts:
        return None
    return usable_server(base / parts)


def resolve_server(root: Path | str | None = None) -> Path | None:
    """First usable `llama-server`, in the documented preference order."""
    explicit = os.environ.get("RYNMESH_LLAMA_SERVER", "").strip()
    if explicit:
        found = usable_server(Path(explicit).expanduser())
        if found is not None:
            return found
    bundled = os.environ.get("RYNMESH_LLAMA_DIR", "").strip()
    if bundled:
        found = find_server(Path(bundled).expanduser())
        if found is not None:
            return found
    if getattr(sys, "frozen", False):
        found = find_server(Path(sys.executable).parent / "llama")
        if found is not None:
            return found
    base = managed_root(root if root is not None else _default_root())
    # Managed extraction may leave an executable before its libraries finish.
    # Only the completion record makes that directory eligible for reuse.
    found = _marker_server(base)
    if found is not None:
        return found
    on_path = shutil.which(server_filename())
    return usable_server(Path(on_path)) if on_path else None


def available(root: Path | str | None = None) -> tuple[bool, str]:
    """(True, "") when a server is resolvable or downloadable; else a safe reason."""
    if resolve_server(root) is not None or asset() is not None:
        return True, ""
    return False, UNAVAILABLE_REASON


def prepare(*, progress: Any = None, cancel_check: Any = None,
            root: Path | str | None = None) -> None:
    """Make a `llama-server` available, downloading the pinned release if needed."""
    base = Path(root).expanduser() if root is not None else _default_root()
    if resolve_server(base) is not None:
        report(progress, cancel_check, 72, "Local inference runtime already present")
        return
    download(base, progress=progress, cancel_check=cancel_check)


# --------------------------------------------------------------------------
# Process control
# --------------------------------------------------------------------------

def _runtime_root(manifest: LLMPackageManifest) -> Path:
    return Path(manifest.runtime_dir).expanduser() if manifest.runtime_dir else _default_root()


def _runtime_dir(root: Path) -> Path:
    """The private per-node runtime directory (pidfiles and server logs)."""
    directory = root / "runtime"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
    except OSError as exc:
        raise LifecycleError(UNWRITABLE_STATE) from exc
    return directory


def _pid_path(root: Path, package_id: str) -> Path:
    return root / "runtime" / f"{package_id}.pid"


def _read_record(path: Path) -> dict[str, Any] | None:
    """The pidfile as `{"pid": int, "server": str, "api_key": str}`, or None.

    A bare-integer pidfile written before the server name was recorded cannot
    be attributed to any process, so it reads as stale rather than as
    something this node may signal.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        pid = int(value.get("pid"))
    except (TypeError, ValueError):
        return None
    return {"pid": pid, "server": str(value.get("server") or ""),
            "api_key": str(value.get("api_key") or "")}


def _write_record(root: Path, package_id: str, pid: int, server_name: str,
                  api_key: str = "") -> None:
    """Record which process, running which server, with which key, this node owns.

    The server basename is what makes a reused pid recognizable as *not* ours
    later, so a stale pidfile can never aim a signal at an unrelated process.
    The key makes the record self-describing: an install that died between
    `start()` and saving the manifest leaves a running server whose token
    exists nowhere else, and a retry has to be able to recover it. The file is
    already opened 0o600, so it is as private as the manifest.
    """
    path = _runtime_dir(root) / f"{package_id}.pid"
    payload = json.dumps({"pid": int(pid), "server": server_name, "api_key": api_key,
                          "started": int(time.time())}, sort_keys=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError as exc:
        raise LifecycleError(UNWRITABLE_STATE) from exc


def _command_matches(pid: int, server_name: str) -> bool:
    """True when the running process still names the recorded server."""
    # Linux exposes the exact argv; read it directly so a long install path
    # is never cut off. `ps` is the portable fallback, and `-ww` stops procps
    # from truncating piped output at 80 columns (which hid the server name
    # on CI runners with long temporary paths).
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        cmdline = b""
    if cmdline:
        return server_name.encode() in cmdline
    try:
        result = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False  # Unverifiable: treat as stale, never signal it.
    return server_name in result.stdout


def _alive(pid: int | None, server_name: str = "") -> bool:
    """True when `pid` is running and (given `server_name`) is still our server.

    Pids are reused, so a pidfile number alone never justifies signalling a
    process: with `server_name` set, a process whose command no longer names
    that server counts as gone.
    """
    if not pid or pid <= 0:
        return False
    child = _CHILDREN.get(pid)
    if child is not None and child.poll() is not None:
        _forget_child(pid)
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return True  # Unknown: assume alive rather than spawn a second server.
        if str(pid) not in result.stdout:
            return False
        return not server_name or server_name.lower() in result.stdout.lower()
    if server_name and not _command_matches(pid, server_name):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _endpoint_serves_alias(manifest: LLMPackageManifest) -> bool:
    """True when something already answers on the port with this model alias."""
    try:
        health = adapter_from_manifest(manifest).health()
    except AdapterError:
        return False
    expected = manifest.model or manifest.public_model_alias
    return bool(health.get("ok")) and str(health.get("model") or "") == expected


def _child_env() -> dict[str, str]:
    """The node environment minus every `llama-server` argument override.

    An inherited `LLAMA_ARG_LOG_VERBOSITY=99` would make the server log whole
    request bodies and `LLAMA_ARG_LOG_FILE` would redirect its log out of the
    owner-only runtime directory, so no such variable is passed through; the
    explicit flags on the command line are the only configuration the child
    gets. `LLAMA_API_KEY` goes too, so nothing can pre-empt the minted key.
    """
    return {key: value for key, value in os.environ.items()
            if not key.startswith("LLAMA_ARG_") and key != "LLAMA_API_KEY"}


def _spawn(server: Path, command: list[str], root: Path, package_id: str,
           port: int) -> subprocess.Popen:
    detach: dict[str, Any] = {"start_new_session": True}
    if os.name == "nt":
        detach = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    log = _runtime_dir(root) / f"{package_id}.log"
    try:
        # Keep only node-generated lifecycle metadata. Even error-level child
        # output may echo a request or credential; it is not safe to copy.
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            if os.name != "nt":
                os.chmod(log, 0o600)
            handle.write(b"runtime_process_starting\n")
            handle.flush()
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(server.parent), env=_child_env(), **detach,
            )
    except OSError as exc:
        raise LifecycleError("unable to start llama-server (see the runtime log)") from exc
    deadline = time.monotonic() + STARTUP_GRACE_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            try:
                with log.open("ab") as handle:
                    handle.write(f"runtime_process_exited code={process.returncode}\n".encode("ascii"))
            except OSError:
                pass  # Reporting the original startup failure must not depend on logging.
            if os.name == "nt" and process.returncode & 0xFFFFFFFF == 0xC0000135:
                raise LifecycleError("local inference runtime dependency is missing; use Update runtime to repair it")
            raise LifecycleError("llama-server exited during startup (see the runtime log)")
        if _port_open(port):
            break
        time.sleep(0.05)
    return process


def start(manifest: LLMPackageManifest) -> None:
    root = _runtime_root(manifest)
    server = resolve_server(root)
    if server is None:
        raise LifecycleError("the local inference runtime is not installed")
    model = Path(manifest.model_path).resolve()
    if not model.is_file():
        raise LifecycleError("configured model file is missing")
    if manifest.checksum and fingerprint_file(model) != manifest.checksum:
        raise LifecycleError("configured model checksum no longer matches; refusing to start")
    port = int(urlparse(manifest.base_url).port or 8080)
    manifest.runtime_dir = str(root)
    pid_path = _pid_path(root, manifest.package_id)
    # Our own pidfile is consulted *before* the port is probed: a server still
    # loading a multi-gigabyte model answers `/health` "not ok" for a while,
    # and probing first would read that as "nothing is here", spawn a second
    # server against the same port, and fail with "exited during startup".
    record = _read_record(pid_path)
    if record is not None and _alive(record["pid"], record["server"]):
        # Already running under this pidfile; never spawn a second server.
        # A retry after an install that failed between `start()` and saving
        # the manifest arrives here with no key, while the live server is
        # still demanding the one it was given: take it from the record, or
        # every later call would 401 until the app quits.
        if not manifest.runtime_api_key and record["api_key"]:
            manifest.runtime_api_key = record["api_key"]
        return
    if record is not None and record["pid"] > 0:
        _forget(pid_path, record["pid"])  # Stale, or a reused pid that is not ours.
    if _endpoint_serves_alias(manifest):
        _write_record(root, manifest.package_id, 0, server.name)  # Owner-managed.
        return
    if not manifest.runtime_api_key:
        manifest.runtime_api_key = secrets.token_urlsafe(API_KEY_BYTES)
    command = [
        str(server), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
        "--alias", manifest.public_model_alias, "-c", str(manifest.context_window),
        "-np", str(manifest.max_concurrent), "--no-webui",
        "--api-key", manifest.runtime_api_key, "--cors-origins", CORS_ORIGINS,
        "-lv", LOG_VERBOSITY,
    ]
    process = _spawn(server, command, root, manifest.package_id, port)
    _CHILDREN[process.pid] = process
    _PIDFILES[process.pid] = pid_path
    _write_record(root, manifest.package_id, process.pid, server.name,
                  manifest.runtime_api_key)
    manifest.runtime_command = command


def _terminate(pid: int, *, force: bool) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", *(["/F"] if force else []), "/PID", str(pid)],
                           capture_output=True, timeout=30)
        else:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except OSError:
        pass


def _forget_child(pid: int) -> None:
    _CHILDREN.pop(pid, None)
    _PIDFILES.pop(pid, None)


def _forget(pid_path: Path | None, pid: int | None) -> None:
    if pid_path is not None:
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
    if pid:
        _forget_child(pid)


def _wait_gone(pid: int) -> bool:
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.1)
    return not _alive(pid)


def _stop_pid(pid: int) -> bool:
    """Terminate, then (after the grace period) kill; True once it is gone."""
    _terminate(pid, force=False)
    if _wait_gone(pid):
        return True
    _terminate(pid, force=True)
    return _wait_gone(pid)


def stop(manifest: LLMPackageManifest) -> bool:
    """True only when this node had a live server and it is now gone."""
    root = _runtime_root(manifest)
    pid_path = _pid_path(root, manifest.package_id)
    record = _read_record(pid_path)
    if record is None:
        # No pidfile, or a legacy/unreadable one that names no server: this
        # node has nothing it can prove it owns, so nothing is signalled.
        _forget(pid_path, None)
        return False
    if record["pid"] == 0:
        return False  # Adopted server: owner-managed, never stopped by Rynmesh.
    if not _alive(record["pid"], record["server"]):
        _forget(pid_path, record["pid"])  # Stale, or a reused pid that is not ours.
        return False
    stopped = _stop_pid(record["pid"])
    if stopped:
        _forget(pid_path, record["pid"])
    return stopped


def stop_owned_children() -> None:
    """Stop every `llama-server` this process started.

    Registered with `atexit` and called from the node's shutdown hook: without
    it, quitting the node orphans an inference server that keeps holding the
    loopback port and the model in memory.
    """
    for pid in list(_CHILDREN):
        # Captured first: reaping the child inside `_stop_pid` drops both
        # bookkeeping entries, and the pidfile still has to be removed.
        pid_path = _PIDFILES.get(pid)
        process = _CHILDREN.get(pid)
        if process is None or process.poll() is None:
            _stop_pid(pid)
        _forget(pid_path, pid)


atexit.register(stop_owned_children)


def remove(manifest: LLMPackageManifest) -> None:
    """Stop this package's server; the runtime dir is shared and stays."""
    stop(manifest)


def update(manifest: LLMPackageManifest) -> None:
    """Repair managed runtime files from the verified pinned release."""
    root = _runtime_root(manifest)
    server = resolve_server(root)
    if server is not None and not server.resolve().is_relative_to(managed_root(root).resolve()):
        # Explicit or bundled installations are outside this package's ownership.
        prepare(root=root)
        return
    stop(manifest)
    for path in (root / "runtime").glob("*.pid"):
        record = _read_record(path)
        if record and record["pid"] > 0 and _alive(record["pid"], record["server"]):
            raise LifecycleError("stop other local model runtimes before repairing shared runtime files")
    download(root)


def state(manifest: LLMPackageManifest) -> dict[str, Any]:
    root = _runtime_root(manifest)
    installed = resolve_server(root) is not None
    record = _read_record(_pid_path(root, manifest.package_id))
    running = _alive(record["pid"], record["server"]) if record else False
    if record is not None and record["pid"] == 0:
        status = "adopted"
    elif running:
        status = "running"
    else:
        status = "stopped" if installed else "not installed"
    return {"installed": installed, "running": running, "status": status,
            "release": RUNTIME_RELEASE}


def install_source(manifest_or_none: LLMPackageManifest | None = None) -> dict[str, str]:
    return {"kind": RUNTIME_ID, "runtime_release": RUNTIME_RELEASE}
