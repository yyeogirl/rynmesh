"""The desktop packaging script imports the package with a bare interpreter.

`webapp/src-tauri/scripts/fetch-llama-runtime.sh` reads the pinned llama.cpp
release from `rynmesh.llm_package.runtime_native_install` on CI runners that
have no project dependencies installed. Importing that module must therefore
never pull in `cryptography` (or any other optional dependency) at import
time, no matter what `rynmesh/__init__` drags along.
"""

from __future__ import annotations

import subprocess
import sys


def test_runtime_pin_module_imports_without_cryptography() -> None:
    probe = (
        "import sys\n"
        "import rynmesh.llm_package.runtime_native_install as m\n"
        "assert m.RUNTIME_RELEASE\n"
        "loaded = sorted(name for name in sys.modules if name.startswith('cryptography'))\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
