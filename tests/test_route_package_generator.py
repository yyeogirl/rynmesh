"""`scripts/new_route_package.py`: the generated skeleton actually works.

See docs/ROUTE_PACKAGES.md for the conventions the generator's templates
encode. `scripts/new_route_package.py` is not importable as a package
module, so it is loaded from its path, the same way `test_llm_e2e_script.py`
loads `scripts/llm_e2e.py`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.background_workers import BackgroundWorkerRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "new_route_package.py"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("_new_route_package_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubStore:
    """Minimal stand-in for `RynmeshStore`: only `home` is read by an installer."""

    def __init__(self, home: Path) -> None:
        self.home = home


def _load_generated(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- 1. the generated package installs and works, imported in-process -----


def test_generated_package_installs_and_works(generator, tmp_path: Path) -> None:
    routes_path, test_path = generator.generate("widget_demo", tmp_path)
    assert routes_path == tmp_path / "rynmesh" / "widget_demo_routes.py"
    assert test_path == tmp_path / "tests" / "test_widget_demo_routes.py"

    module = _load_generated(routes_path, "generated_widget_demo_routes")

    app = FastAPI()
    workers = BackgroundWorkerRegistry()
    home = tmp_path / "home"
    store = _StubStore(home)
    state = module.install_widget_demo(app, store=store, home=home, workers=workers)

    assert app.state.widget_demo is state
    specs = {spec.name: spec for spec in workers.specs()}
    assert module.WORKER_NAME in specs
    assert specs[module.WORKER_NAME].policy == module.TICK_POLICY

    module.install_widget_demo(app, store=store, home=home, workers=workers)  # must not raise
    assert len(workers.specs()) == 1

    client = TestClient(app)
    response = client.get("/api/local/widget_demo/status")
    assert response.status_code == 200
    assert set(response.json()) == {"version", "updated_at", "worker"}

    written = state.touch()
    assert written["version"] == 1
    record_path = home / "widget_demo" / "state.json"
    assert record_path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(record_path.stat().st_mode) == 0o600


def test_status_route_calls_local_control(generator, tmp_path: Path) -> None:
    routes_path, _ = generator.generate("widget_gate", tmp_path)
    module = _load_generated(routes_path, "generated_widget_gate_routes")

    app = FastAPI()
    workers = BackgroundWorkerRegistry()
    home = tmp_path / "home"
    calls: list[object] = []
    module.install_widget_gate(
        app, store=_StubStore(home), home=home, workers=workers,
        local_control=lambda request: calls.append(request),
    )

    TestClient(app).get("/api/local/widget_gate/status")

    assert len(calls) == 1


# ---- 2. the generated *test file* actually passes, run in a subprocess ----


def _fastapi_test_stack_importable() -> bool:
    return (
        importlib.util.find_spec("fastapi") is not None
        and importlib.util.find_spec("fastapi.testclient") is not None
    )


@pytest.mark.skipif(
    not _fastapi_test_stack_importable(),
    reason="fastapi/httpx TestClient stack is not importable in this environment",
)
def test_generated_test_file_passes_in_a_subprocess(generator, tmp_path: Path) -> None:
    """Prove the generated skeleton works, not just that it parses.

    A self-contained "rynmesh" is built next to the generated file: this
    repo's `atomic_io.py`, its Windows ACL helper and `background_workers.py` (dependency-free
    beyond the stdlib) plus an empty `__init__.py`. That lets the subprocess
    resolve `rynmesh.<name>_routes` on its own terms. Simply pointing
    PYTHONPATH at `tmp_path` is not enough: the subprocess's own working
    directory is implicitly on `sys.path` too, and this repo's real,
    editable-installed `rynmesh` package (found there, or anywhere else on
    `sys.path`) wins the name over a same-named namespace directory
    regardless of PYTHONPATH order, since it is a full regular package and
    a bare namespace portion never displaces one. Giving the generated
    package its own `__init__.py` and copies of the support modules it needs
    sidesteps the conflict entirely instead of fighting import precedence.
    """

    routes_path, test_path = generator.generate("widget_probe", tmp_path)
    package_dir = routes_path.parent
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(REPO_ROOT / "rynmesh" / "atomic_io.py", package_dir / "atomic_io.py")
    shutil.copy2(REPO_ROOT / "rynmesh" / "private_permissions.py", package_dir / "private_permissions.py")
    shutil.copy2(
        REPO_ROOT / "rynmesh" / "background_workers.py", package_dir / "background_workers.py",
    )

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(test_path)],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"could not run the generated test file in a subprocess: {exc}")

    assert result.returncode == 0, (
        f"generated test file failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert " passed" in result.stdout


# ---- 3. rejected names and existing files: raise/exit non-zero, write nothing --


@pytest.mark.parametrize(
    "bad_name",
    ["Uppercase", "1leading", "has/slash", "has.dot", "has-dash", "a", "", "_underscore"],
)
def test_rejects_bad_names_and_writes_nothing(generator, tmp_path: Path, bad_name: str) -> None:
    with pytest.raises(ValueError):
        generator.generate(bad_name, tmp_path)
    assert list(tmp_path.rglob("*")) == []


def test_rejects_when_destination_file_already_exists(generator, tmp_path: Path) -> None:
    existing = tmp_path / "rynmesh" / "taken_routes.py"
    existing.parent.mkdir(parents=True)
    existing.write_text("# already here\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        generator.generate("taken", tmp_path)

    assert existing.read_text(encoding="utf-8") == "# already here\n"
    assert not (tmp_path / "tests" / "test_taken_routes.py").exists()


def test_failing_second_write_leaves_destination_clean(
    generator, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure writing the test file must not orphan the routes file.

    Without cleanup, a retry after this failure would hit `FileExistsError`
    on `<name>_routes.py` with no indication it's a leftover from the
    failed attempt rather than someone else's file.
    """
    original_write_text = Path.write_text

    def flaky_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "test_flaky_pkg_routes.py":
            raise OSError("simulated disk failure on the second write")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)

    with pytest.raises(OSError, match="simulated disk failure"):
        generator.generate("flaky_pkg", tmp_path)

    assert not (tmp_path / "rynmesh" / "flaky_pkg_routes.py").exists()
    assert not (tmp_path / "tests" / "test_flaky_pkg_routes.py").exists()


def test_cli_rejects_bad_name_and_exits_nonzero(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "NotLowercase", "--dest", str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert list(tmp_path.rglob("*")) == []


def test_writes_nothing_outside_dest(generator, tmp_path: Path) -> None:
    # `dest` sits a couple of levels below `tmp_path`; `mkdir(parents=True)`
    # legitimately creates the empty ancestor directories in between (here,
    # `tmp_path / "sub"`) to reach it, so only newly created *files* count
    # as "written" for this check, not the scaffolding directories.
    dest = tmp_path / "sub" / "nested"
    before = set(tmp_path.rglob("*"))

    generator.generate("contained_pkg", dest)

    new_files = {path for path in (set(tmp_path.rglob("*")) - before) if path.is_file()}
    outside = {path for path in new_files if dest != path and dest not in path.parents}
    assert outside == set()
    assert new_files  # sanity: the generator did write something


# ---- 4. ruff check on the generated files -----------------------------------


def _ruff_check(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, "-m", "ruff", "check",
            "--config", str(REPO_ROOT / "pyproject.toml"),
            *(str(path) for path in paths),
        ],
        capture_output=True, text=True, timeout=60,
    )


def test_generated_files_pass_ruff(generator, tmp_path: Path) -> None:
    routes_path, test_path = generator.generate("widget_lint", tmp_path)

    result = _ruff_check(routes_path, test_path)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name", ["first_run", "library", "friends", "assistant"])
def test_each_upcoming_package_name_generates_ruff_clean(
    generator, tmp_path: Path, name: str,
) -> None:
    # "assistant" sorts before "background_workers" alphabetically while the
    # other three sort after it; this is what actually exercises the
    # generator's dynamic import-order fix rather than a hardcoded one.
    routes_path, test_path = generator.generate(name, tmp_path / name)

    result = _ruff_check(routes_path, test_path)

    assert result.returncode == 0, result.stdout + result.stderr
