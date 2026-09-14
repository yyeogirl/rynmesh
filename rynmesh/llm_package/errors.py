"""Shared lifecycle error type.

Split out so `lifecycle.py` and runtime backend modules (e.g. `runtime_docker.py`)
can each raise `LifecycleError` without either importing the other.
"""

from __future__ import annotations


class LifecycleError(RuntimeError):
    pass
