"""The E2E launcher's CLI routing — no Docker, no network.

`scripts/llm_e2e.py` is not importable as a package module, so it is loaded from
its path. Only the argument plumbing is exercised here: the flows themselves are
covered by the Docker job in CI.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "llm_e2e.py"


@pytest.fixture(scope="module")
def e2e():
    spec = importlib.util.spec_from_file_location("_llm_e2e_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(e2e, monkeypatch, command: str) -> list[tuple[str, object]]:
    """Dispatch one command with every side effect recorded instead of run."""

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(e2e, "up", lambda mode: calls.append(("up", mode)))
    monkeypatch.setattr(e2e, "verify", lambda mode: calls.append(("verify", mode)))
    monkeypatch.setattr(e2e, "mailbox_verify", lambda: calls.append(("mailbox_verify", None)))
    monkeypatch.setattr(e2e, "_compose", lambda *args, **kwargs: calls.append(("compose", args)))
    monkeypatch.setattr(sys, "argv", ["llm_e2e.py", command])
    assert e2e.main() == 0
    return calls


def test_mailbox_run_brings_the_stack_up_then_verifies(e2e, monkeypatch) -> None:
    assert _run(e2e, monkeypatch, "mailbox-run") == [
        ("up", "mailbox"), ("mailbox_verify", None),
    ]


def test_mailbox_up_and_verify_are_separately_addressable(e2e, monkeypatch) -> None:
    assert _run(e2e, monkeypatch, "mailbox-up") == [("up", "mailbox")]
    assert _run(e2e, monkeypatch, "mailbox-verify") == [("mailbox_verify", None)]


def test_the_mailbox_mode_never_hijacks_the_llm_commands(e2e, monkeypatch) -> None:
    assert _run(e2e, monkeypatch, "run") == [("up", "test"), ("verify", "test")]
    assert _run(e2e, monkeypatch, "relay-run") == [
        ("up", "relay-test"), ("verify", "relay-test"),
    ]


def test_an_unknown_command_is_refused(e2e, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["llm_e2e.py", "mailbox"])
    with pytest.raises(SystemExit):
        e2e.main()


def test_the_mailbox_mode_forces_the_consumer_onto_store_and_forward(
    e2e, monkeypatch
) -> None:
    """`up("mailbox")` is what sets the compose variable the E2E depends on."""

    monkeypatch.delenv("RYNMESH_MESSAGING_FORCE_MAILBOX", raising=False)
    seen: dict[str, str] = {}

    def compose(*args, env=None):
        seen.update({k: v for k, v in (env or {}).items()
                     if k.startswith("RYNMESH_MESSAGING")})

    monkeypatch.setattr(e2e, "_compose", compose)
    monkeypatch.setattr(e2e, "_wait", lambda url, timeout=120: None)
    e2e.up("mailbox")
    assert seen == {"RYNMESH_MESSAGING_FORCE_MAILBOX": "1"}

    seen.clear()
    e2e.up("test")
    assert seen == {}
