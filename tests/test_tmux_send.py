"""Tests for ``tmux_send`` — text and keystroke modes, validation, self-pane guard.

Six cases from the original smoke test:
  - text mode (default submit=true) types characters and presses Enter
  - keys mode sends tmux key names like ``C-c``
  - text mode with ``submit: false`` types without submitting
  - oneOf validation rejects bad-shape calls
  - self-pane guard refuses to target the agent's own pane
  - post-send capture returns a 5-line snapshot on success
"""

from __future__ import annotations

import json
import subprocess
import time

import tmux_tools

from .conftest import tmux_send


def test_send_text_default_submit(sock: str) -> None:
    """``text="echo ok"`` types the characters and presses Enter."""
    tmux_send(sock, "%0", "Enter")  # flush any prior prompt noise
    time.sleep(0.2)
    r = json.loads(
        tmux_tools.tmux_send_handler({"pane": "%0", "text": "echo ok"})
    )
    assert r["pane_id"] == "%0"
    assert r["mode"] == "text"
    assert r["sent"] == "echo ok"
    assert r["submit"] is True
    assert r["status"] == "ok"
    time.sleep(0.3)
    captured = json.loads(tmux_tools.tmux_capture_handler({"pane": "%0", "lines": 30}))
    assert "ok" in captured["text"]


def test_send_keys_mode(sock: str) -> None:
    """``keys=["C-c"]`` sends the named key without literal interpretation."""
    r = json.loads(
        tmux_tools.tmux_send_handler({"pane": "%0", "keys": ["C-c"]})
    )
    assert r["pane_id"] == "%0"
    assert r["mode"] == "keys"
    assert r["sent"] == ["C-c"]
    assert r["status"] == "ok"
    # No submit field on the keys branch.
    assert "submit" not in r


def test_send_text_submit_false(sock: str) -> None:
    """``submit: false`` types the text without pressing Enter."""
    tmux_send(sock, "%0", "Enter")
    time.sleep(0.2)
    r = json.loads(
        tmux_tools.tmux_send_handler(
            {"pane": "%0", "text": "echo unfinished", "submit": False}
        )
    )
    assert r["submit"] is False
    time.sleep(0.3)
    captured = json.loads(tmux_tools.tmux_capture_handler({"pane": "%0", "lines": 20}))
    assert "unfinished" in captured["text"]


def test_send_validation_rejects_bad_shape(sock: str) -> None:
    """The ``oneOf`` schema rejects: missing body, both text+keys, empty keys, non-list keys."""
    cases = [
        ({"pane": "%0"}, "pass either"),
        ({"pane": "%0", "text": "x", "keys": ["C-c"]}, "not both"),
        ({"pane": "%0", "keys": []}, "non-empty"),
        ({"pane": "%0", "keys": "C-c"}, "list of strings"),
        ({"pane": "%0", "keys": ["-l", "echo hi"]}, "must not start with '-'"),
        ({"pane": "%0", "keys": ["Enter", "-N"]}, "must not start with '-'"),
    ]
    for args, expected_substr in cases:
        r = json.loads(tmux_tools.tmux_send_handler(args))
        assert "error" in r and expected_substr in r["error"], (
            f"missing/error mismatch for {args}: {r}"
        )


def test_send_self_pane_guard(sock: str) -> None:
    """Sending to the agent's own pane (the smoke server's only pane) is rejected."""
    # Simulate the agent being in the smoke server's pane.
    tmux_tools.set_self_pane("%0")
    try:
        r_text = json.loads(
            tmux_tools.tmux_send_handler({"pane": "%0", "text": "should-not-run"})
        )
        assert "refusing to send to own pane" in r_text["error"]
        r_keys = json.loads(
            tmux_tools.tmux_send_handler({"pane": "%0", "keys": ["C-c"]})
        )
        assert "refusing to send to own pane" in r_keys["error"]
        # Clearing the guard re-enables sends.
        tmux_tools.set_self_pane(None)
        r_ok = json.loads(
            tmux_tools.tmux_send_handler({"pane": "%0", "text": "echo ok", "submit": False})
        )
        assert r_ok["status"] == "ok"
        tmux_send(sock, "%0", "Enter")
        time.sleep(0.2)
    finally:
        tmux_tools.set_self_pane(None)


def test_send_post_send_capture(sock: str) -> None:
    """Every successful ``tmux_send`` includes a 5-line ``post_send_capture`` field.

    For instant-return commands the snapshot has the result. The
    field is always present on success (empty string if the capture
    itself failed); error envelopes don't include it.
    """
    # 14a: text mode.
    marker = "POST-SEND-MARKER-14a"
    tmux_send(sock, "%0", "-l", f"echo {marker}")
    tmux_send(sock, "%0", "Enter")
    r = json.loads(tmux_tools.tmux_send_handler({"pane": "%0", "text": f"echo {marker}"}))
    assert "post_send_capture" in r
    assert isinstance(r["post_send_capture"], str)
    assert marker in r["post_send_capture"]

    # 14b: keys mode — send a literal key-name sequence. Use Up to
    # recall the last command, then Enter to submit it. This exercises
    # real tmux key names (not flag-like strings) and verifies the
    # post-send capture works for the keys branch.
    marker_b = "POST-SEND-MARKER-14b"
    # First put a command in history via text mode.
    tmux_tools.tmux_send_handler({"pane": "%0", "text": f"echo {marker_b}"})
    time.sleep(0.3)
    # Now send Up + Enter via keys mode to re-run it.
    r = json.loads(
        tmux_tools.tmux_send_handler({"pane": "%0", "keys": ["Up", "Enter"]})
    )
    assert "post_send_capture" in r
    assert marker_b in r["post_send_capture"]

    # 14c: error envelope has no field.
    bad = json.loads(tmux_tools.tmux_send_handler({"pane": "%0"}))
    assert "error" in bad and "post_send_capture" not in bad

    # 14d: self-pane rejection is fast (no 100ms tail).
    tmux_tools.set_self_pane("%0")
    try:
        t0 = time.monotonic()
        rejected = json.loads(
            tmux_tools.tmux_send_handler({"pane": "%0", "text": "should-not-run"})
        )
        wall = time.monotonic() - t0
        assert "error" in rejected and "post_send_capture" not in rejected
        assert wall < 0.05, f"self-pane rejection took {wall*1000:.0f}ms"
    finally:
        tmux_tools.set_self_pane(None)
