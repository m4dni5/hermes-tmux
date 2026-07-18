"""Shared fixtures and helpers for the hermes-tmux pytest test suite.

The smoke test stands up an isolated tmux server on a custom socket
(``hermes-tmux-test-<module>``) so it never touches the user's real
tmux server, and tears it down on exit. Each test file in this
directory gets its own socket to avoid test-to-test state bleed.

The ``FakeCtx`` class mocks the framework's ``PluginContext`` —
``tmux_tools._run_tmux`` calls ``ctx.dispatch_tool("terminal", ...)`` to
run tmux, and in production that goes through the framework's
approval/redaction/interrupt pipelines. For testing we bypass that
and run tmux directly. The fake's return shape matches what
``tmux_tools._run_tmux`` parses: ``{"output", "exit_code", "error"}``.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

import tmux_tools


SOCKET_BASE = "hermes-tmux-test"


def socket_for_module(module_name: str) -> str:
    """Per-module tmux socket name. Avoids test-to-test state bleed."""
    return f"{SOCKET_BASE}-{module_name}"


class FakeCtx:
    """Minimal PluginContext for the test suite.

    Bypasses the framework's terminal pipeline and runs tmux directly.
    Same return envelope as the real ctx.dispatch_tool("terminal", ...).

    Tracks injected messages (from ctx.inject_message) in
    ``_injected`` so tests can assert on what ``/pane`` injected.
    """

    def __init__(self) -> None:
        self._injected: list[dict[str, str]] = []

    def dispatch_tool(self, name: str, args: dict) -> str:
        full = shlex.split(args["command"])
        r = subprocess.run(full, capture_output=True, text=True, timeout=10)
        return json.dumps({
            "output": r.stdout,
            "exit_code": r.returncode,
            "error": r.stderr if r.returncode != 0 else None,
        })

    def inject_message(self, content: str, role: str = "user") -> bool:
        self._injected.append({"content": content, "role": role})
        return True


@pytest.fixture(scope="module")
def sock(request: pytest.FixtureRequest) -> Iterator[str]:
    """The tmux socket name for this test module.

    Tests reference it as ``sock`` (the fixture name) so they can
    use the right ``-L`` flag when shelling out to tmux directly.
    Per-module scope: the server is started once for the file and
    torn down at the end.
    """
    name = socket_for_module(request.module.__name__.split(".")[-1])
    yield name


@pytest.fixture(scope="module", autouse=True)
def tmux_server(request: pytest.FixtureRequest, sock: str) -> Iterator[None]:
    """Stand up an isolated tmux server for this test module.

    Sets ``$TMUX`` and ``$TMUX_PANE`` in the test process to point
    at the test server's session. Without this, ``tmux_list()``
    would route to whatever tmux server the test runner is attached
    to (typically the agent's own session), and the per-pane
    resolution would force a ``-L`` flag.
    """
    subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
    r = subprocess.run(
        ["tmux", "-L", sock, "new-session", "-d", "-s", "test", "-x", "200", "-y", "50"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"new-session failed: {r.stderr}"
    subprocess.run(["tmux", "-L", sock, "set-option", "-g", "remain-on-exit", "on"], check=True)

    pane_id = subprocess.run(
        ["tmux", "-L", sock, "list-panes", "-t", "test:0", "-F", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    socket_path = subprocess.run(
        ["tmux", "-L", sock, "display-message", "-p", "-t", pane_id, "#{socket_path}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    os.environ["TMUX"] = socket_path
    os.environ["TMUX_PANE"] = pane_id

    # Wire the tools module the way __init__.register() does in
    # production: capture the agent's own pane and socket from the
    # (now-test-server-pointing) env so per-pane resolution's
    # "is this the same socket" check works.
    import importlib
    importlib.reload(tmux_tools)
    tmux_tools.set_ctx(FakeCtx())
    # Capture the agent's own socket so per-pane resolution's
    # "is this the same socket" check works. We deliberately do NOT
    # set the self-pane guard here: most tests send to the test
    # server's initial pane (``%0``) and don't want every call
    # rejected as a self-target. Tests that exercise the guard
    # (``test_send_self_pane_guard``) set it explicitly with
    # ``tmux_tools.set_self_pane(...)`` and clean up in a ``finally``.
    tmux_env = os.environ.get("TMUX", "")
    if tmux_env:
        first = tmux_env.split(",", 1)[0]
        socket_name = first.rsplit("/", 1)[-1] if "/" in first else first
        tmux_tools.set_self_socket(socket_name)

    try:
        yield
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)


def tmux_send(sock: str, pane: str, *args: str) -> None:
    """Thin wrapper around ``tmux send-keys`` for tests that need to
    drive the shell directly. Mirrors how the plugin sends keys but
    stays out of the plugin's handlers (so the tests can verify the
    post-send state, not just the send call)."""
    subprocess.run(["tmux", "-L", sock, "send-keys", "-t", pane, *args], check=True)


@pytest.fixture
def fake_ctx() -> Iterator[FakeCtx]:
    """Return the FakeCtx wired into tmux_tools, with _injected cleared."""
    ctx = tmux_tools._ctx
    assert isinstance(ctx, FakeCtx), (
        "fake_ctx fixture requires FakeCtx — "
        "did a test overwrite set_ctx?"
    )
    ctx._injected.clear()
    # Clear _last_pane so tests start from a known state.
    tmux_tools._last_pane = None
    yield ctx