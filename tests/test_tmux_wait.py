"""Tests for ``tmux_wait`` — substring polling wait.

Four cases from the original smoke test:
  - pattern appears within timeout (matched: true)
  - pattern never appears (matched: false, full timeout elapsed)
  - validation rejects missing pane / empty pattern
  - ``timeout: 0`` clamps to 1 second
"""

from __future__ import annotations

import json
import time

from hermes_tmux import tools

from .conftest import tmux_send


def test_wait_pattern_appears(sock: str) -> None:
    """A pattern that appears in the captured scrollback returns matched: true."""
    marker = "WAIT-MARKER-13a"
    tmux_send(sock, "%0", "-l", f"echo {marker}")
    tmux_send(sock, "%0", "Enter")
    r = json.loads(
        tools.tmux_wait_handler({"pane": "%0", "pattern": marker, "timeout": 5})
    )
    assert r["matched"] is True
    assert marker in r["text"]
    assert isinstance(r["elapsed_ms"], int)
    assert r["elapsed_ms"] < 5000


def test_wait_pattern_times_out(sock: str) -> None:
    """A pattern that never appears returns matched: false with the timeout elapsed."""
    t0 = time.monotonic()
    r = json.loads(
        tools.tmux_wait_handler(
            {"pane": "%0", "pattern": "DEFINITELY-NOT-IN-PANE-13b", "timeout": 2}
        )
    )
    wall = time.monotonic() - t0
    assert r["matched"] is False
    assert r["elapsed_ms"] == 2000
    assert isinstance(r["text"], str) and len(r["text"]) > 0
    # Wall-clock waited ~timeout (not way more, not way less).
    assert 1.8 <= wall < 4.0, f"wall-clock: {wall:.2f}s"


def test_wait_validation(sock: str) -> None:
    """Missing pane and empty pattern return error envelopes."""
    bad_pane = json.loads(tools.tmux_wait_handler({"pattern": "x", "pane": ""}))
    assert "error" in bad_pane and "pane is required" in bad_pane["error"]
    bad_pattern = json.loads(tools.tmux_wait_handler({"pane": "%0", "pattern": ""}))
    assert "error" in bad_pattern and "pattern" in bad_pattern["error"]


def test_wait_timeout_clamping(sock: str) -> None:
    """``timeout: 0`` clamps to 1 second (avoids the Python ``0 or 10`` gotcha)."""
    t0 = time.monotonic()
    r = json.loads(
        tools.tmux_wait_handler(
            {"pane": "%0", "pattern": "DEFINITELY-NOT-IN-PANE-13d", "timeout": 0}
        )
    )
    wall = time.monotonic() - t0
    assert r["elapsed_ms"] == 1000
    assert 0.8 < wall < 3.0, f"elapsed_ms=1000 but wall={wall:.2f}s"
