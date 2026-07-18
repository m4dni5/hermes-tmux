"""Tests for ``tmux_wait`` — substring and regex polling wait.

Six cases:
  - pattern appears within timeout (matched: true)
  - pattern never appears (matched: false, full timeout elapsed)
  - validation rejects missing pane / empty pattern / invalid regex
  - ``timeout: 0`` clamps to 1 second
  - regex matching works
  - async mode returns immediately with ``status: watching``
"""

from __future__ import annotations

import json
import time

import tmux_tools

from .conftest import tmux_send


def test_wait_pattern_appears(sock: str) -> None:
    """A pattern that appears in the captured scrollback returns matched: true."""
    marker = "WAIT-MARKER-13a"
    tmux_send(sock, "%0", "-l", f"echo {marker}")
    tmux_send(sock, "%0", "Enter")
    r = json.loads(
        tmux_tools.tmux_wait_handler(
            {"pane": "%0", "pattern": marker, "timeout": 5}
        )
    )
    assert r["matched"] is True
    assert marker in r["text"]
    assert isinstance(r["elapsed_ms"], int)
    assert r["elapsed_ms"] < 5000


def test_wait_pattern_times_out(sock: str) -> None:
    """A pattern that never appears returns matched: false with the timeout elapsed."""
    t0 = time.monotonic()
    r = json.loads(
        tmux_tools.tmux_wait_handler(
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
    bad_pane = json.loads(tmux_tools.tmux_wait_handler({"pattern": "x", "pane": ""}))
    assert "error" in bad_pane and "pane is required" in bad_pane["error"]
    bad_pattern = json.loads(
        tmux_tools.tmux_wait_handler({"pane": "%0", "pattern": ""})
    )
    assert "error" in bad_pattern and "pattern" in bad_pattern["error"]


def test_wait_timeout_clamping(sock: str) -> None:
    """``timeout: 0`` clamps to 1 second (avoids the Python ``0 or 10`` gotcha)."""
    t0 = time.monotonic()
    r = json.loads(
        tmux_tools.tmux_wait_handler(
            {"pane": "%0", "pattern": "DEFINITELY-NOT-IN-PANE-13d", "timeout": 0}
        )
    )
    wall = time.monotonic() - t0
    assert r["elapsed_ms"] == 1000
    assert 0.8 < wall < 3.0, f"elapsed_ms=1000 but wall={wall:.2f}s"


def test_wait_regex_matces(sock: str) -> None:
    """Regex pattern matching works (``regex: true``)."""
    marker = "REGEX-WAIT-987"
    tmux_send(sock, "%0", "-l", f"echo {marker}")
    tmux_send(sock, "%0", "Enter")
    time.sleep(0.3)
    # Use a regex that matches a prefix of the marker.
    r = json.loads(
        tmux_tools.tmux_wait_handler(
            {
                "pane": "%0",
                "pattern": r"REGEX.*987",
                "regex": True,
                "timeout": 3,
            }
        )
    )
    assert r["matched"] is True
    assert r["regex"] is True
    assert marker in r["text"]


def test_wait_regex_invalid(sock: str) -> None:
    """An invalid regex pattern returns an error before any polling."""
    r = json.loads(
        tmux_tools.tmux_wait_handler(
            {"pane": "%0", "pattern": r"foo[", "regex": True}
        )
    )
    assert "error" in r
    assert "invalid regex" in r["error"].lower()


def test_wait_regex_no_match(sock: str) -> None:
    """A regex that doesn't match any text returns matched: false."""
    r = json.loads(
        tmux_tools.tmux_wait_handler(
            {
                "pane": "%0",
                "pattern": r"XYZZY-\d{10}",
                "regex": True,
                "timeout": 1,
            }
        )
    )
    assert r["matched"] is False
    assert r["regex"] is True


def test_wait_async_returns_watching(fake_ctx, sock: str) -> None:
    """``async: true`` returns immediately with status: watching."""
    # Spawn an async wait. The background process will finish on its
    # own; the test just checks the immediate response.
    r = json.loads(
        tmux_tools.tmux_wait_handler(
            {
                "pane": "%0",
                "pattern": "SOMETHING-THAT-EXISTS",
                "async": True,
                "timeout": 5,
            }
        )
    )
    assert r["status"] == "watching"
    assert r["pane_id"] == "%0"
    assert r["pattern"] == "SOMETHING-THAT-EXISTS"
    assert r["timeout_s"] == 5
    assert r["regex"] is False


def test_wait_async_with_regex(fake_ctx, sock: str) -> None:
    """``async: true`` with ``regex: true`` returns watching with regex: true."""
    r = json.loads(
        tmux_tools.tmux_wait_handler(
            {
                "pane": "%0",
                "pattern": r"prompt\$",
                "regex": True,
                "async": True,
                "timeout": 3,
            }
        )
    )
    assert r["status"] == "watching"
    assert r["regex"] is True