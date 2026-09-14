# Issue #34 — bundled desktop inference runtime (work plan)

Status: delivered 2026-09-03 via
[#42](https://github.com/yeogirlyun/rynmesh/pull/42) (system track), closing
[#34](https://github.com/yeogirlyun/rynmesh/issues/34). Unblocks the
[Local AI Setup](product/user/USER_LOCAL_AI_SETUP_WORK_PLAN.md) user plan.
Follow-ups and the manual acceptance still owed are recorded on #34.

## Problem

`rynmesh/llm_package/lifecycle.py` implements the "managed" Local AI mode as a
Docker container (`ghcr.io/ggml-org/llama.cpp:server@sha256:…`). Consumer
desktops do not have Docker, so the one-click flow is impossible there. Two
smaller defects compound it:

- `hardware._memory()` only reads `/proc/meminfo` (and the Windows API), so on
  macOS RAM reports as 0 and every managed install is refused with
  "Could not determine total RAM".
- `_download()` deletes the `.part` file on any interruption, so a 0.5–2 GB
  model restarts from zero after a dropped connection or app quit.

## Design

### Runtime capability (one interface, two backends)

```
rynmesh/llm_package/
  runtime_native.py   llama-server child process (new, default on desktops)
  runtime_docker.py   existing container code moved verbatim (server nodes)
  lifecycle.py        chooses a backend by manifest.runtime; no Docker calls
```

`manifest.runtime` values: `native_llama_cpp` (new), `docker_llama_cpp`,
`external`. `select_runtime(preference)` picks `native` when a `llama-server`
is resolvable or downloadable for this platform, else `docker` when the engine
is running, else raises a `LifecycleError` with a stable safe code
(`runtime_unavailable`). Preference may be forced with `runtime: "docker"` in
the setup body for server operators.

### Native backend

- **Pinned release** `b10774` (2026-09-03) from `ggml-org/llama.cpp`, per
  platform asset name + SHA-256 + size recorded in `runtime_native.py`
  (macOS arm64/x64, Ubuntu x64/arm64, Windows CPU x64/arm64). Digests come
  from the GitHub release API and are checked before extraction.
- **Resolution order** for the server binary: `RYNMESH_LLAMA_SERVER` (file) →
  `RYNMESH_LLAMA_DIR/llama-server[.exe]` (set by the desktop shell to the
  bundled resource dir) → `<frozen exe dir>/llama/llama-server` → managed
  download `<llm root>/runtime/llama-b10774/` → `llama-server` on PATH.
- **Managed runtime download**: HTTPS only, SHA-256 verified before unpacking,
  archive members validated (no absolute paths, no `..`, no symlinks, bounded
  total size), executables `chmod 0755`, `runtime.json` marker written last.
- **Process control**: `Popen([server, "-m", model, "--host", "127.0.0.1",
  "--port", port, "--alias", alias, "-c", ctx, "-np", concurrency,
  "--no-webui"])`, stdin closed, stdout/stderr to a size-capped
  `<root>/runtime/<package_id>.log` (llama-server does not log request bodies
  at default verbosity; `-v` is never passed). Pidfile per package; `stop()` is
  terminate → 10 s → kill; stale pidfiles are detected with `kill(pid, 0)`.
  If the port already answers `/health` for the same alias, adopt it instead
  of spawning a second server.
- Manifest records `runtime_command` and `runtime_dir` (already fields) so
  `status()` can show which binary is in use without exposing it publicly
  (`public_dict()` already excludes both).

### Model catalog

`rynmesh/llm_package/catalog.py` — three pinned profiles replacing the tuples
inside `hardware.recommend()`:

| profile | model | size | sha256 |
|---|---|---|---|
| light | Qwen2.5-0.5B-Instruct Q4_K_M @ `872f8a96…` | 491,400,032 | `74a4da8c…d7a9db` |
| balanced | Qwen2.5-1.5B-Instruct Q4_K_M @ `91cad511…` | 1,117,320,736 | `6a1a2eb6…9407e` |
| quality | Qwen2.5-3B-Instruct Q4_K_M @ `7dabda4d…` | 2,104,932,768 | `626b4a66…5c62d` |

All Apache-2.0. `install_managed(profile=…)` selects from the catalog;
`model_url`/`expected_sha256` remain as an override for reviewed custom
models. A remotely hosted, signed catalog is a follow-up, not part of #34.

### Resumable download

`_download()` keeps `.part` on cancel and network errors, resumes with
`Range: bytes=<size>-` (206 appends, 200 restarts), refuses to exceed the
catalog `size_bytes`, and hashes the completed file before `replace()`.
Checksum mismatch quarantines the file (`.corrupt`) and raises.

### Hardware

`_memory()` gains a Darwin path (`sysctl -n hw.memsize`; available from
`vm_stat` free + inactive + speculative pages). `detect_hardware()` reports
`native_runtime_available` next to `container_available`, and the warning
text no longer implies Docker is required.

### Packaging (desktop)

- `webapp/src-tauri/scripts/fetch-llama-runtime.sh` downloads and verifies the
  pinned asset for the host triple into `webapp/src-tauri/resources/llama/`
  (git-ignored).
- `tauri.conf.json` bundles that directory as a resource; `node.rs` passes
  `RYNMESH_LLAMA_DIR` (the resolved resource directory) to the node.
- CI `desktop-compile` and `release.yml` run the fetch before building; the
  verify step checks `llama-server --version` inside the bundle.

### Out of scope for #34

GPU-specific builds (CUDA/Vulkan), multiple resident models, the setup wizard
UI (user track), remote signed catalog updates.

## Tasks

1. `runtime_docker.py` extraction + `lifecycle.py` dispatch (no behavior
   change for Docker; tests move their monkeypatch target).
2. `runtime_native.py`: resolution, pinned download/extract, process control,
   adoption; tests with a fake `llama-server` script.
3. `catalog.py` + `install_managed(profile=)` + resumable `_download` +
   Darwin memory; tests for resume (206/200), size cap, corrupt quarantine,
   Darwin parsing.
4. Desktop packaging: fetch script, Tauri resource, `RYNMESH_LLAMA_DIR`, CI.
5. Webapp copy + error mapping in `Services.tsx`; docs (README, runbook, MVP,
   `SERVICE_PLATFORM_NEXT`); close #34 with evidence.

## Acceptance

- [ ] On macOS (arm64 + x64) and Windows x64 with no Docker, `setup` in
      managed mode completes download → verify → start → health → self-test
      (automated with a fake server; real-model runs still owed, see #34).
- [x] Interrupting a download and rerunning resumes from the `.part` size
      (tests in `tests/test_llm_package.py`).
- [x] A corrupt or truncated archive/model never starts; the error is safe.
- [x] Docker mode still works on a server with the engine running (flags
      unchanged; `tests/test_llm_runtime_docker.py`).
- [x] No prompt, response, model path, or owner filename in logs or public
      manifest views (asserted in `tests/test_llm_runtime_native.py`; a
      real-model log grep at `-lv 1` is still owed).
- [x] `python -m ruff check rynmesh/ tests/` and the full pytest suite pass
      (CI on #42).
