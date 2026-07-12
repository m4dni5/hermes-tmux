"""Tests for ``tmux_capture`` — read pane contents.

Five cases from the original smoke test:
  - default capture returns the visible pane contents
  - target resolution accepts bare session name
  - nonexistent target returns a clean error
  - ANSI escape sequences are stripped
  - alt-screen vs normal-scrollback flag semantics (tmux 3.5a)
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import tools

from .conftest import tmux_send


def test_capture_default_returns_visible(sock: str) -> None:
    """The default capture path reads the visible pane contents (TUI surface)."""
    tmux_send(sock, "%0", "-l", "echo hello-from-tmux-plugin")
    tmux_send(sock, "%0", "Enter")
    time.sleep(0.8)

    result = json.loads(tools.tmux_capture_handler({"pane": "%0", "lines": 50}))
    assert "hello-from-tmux-plugin" in result["text"]
    assert result["pane_id"] == "%0"
    # Fast path: when the input is a %pane_id and the agent is inside
    # tmux, the target is the bare %pane_id (not session:window.pane).
    # The full form is available via tmux_list if needed.
    assert result["target"] == "%0"


def test_capture_accepts_bare_session_name(sock: str) -> None:
    """Target resolution: ``test`` (session name) resolves to the only pane."""
    tmux_send(sock, "%0", "-l", "echo resolution-test")
    tmux_send(sock, "%0", "Enter")
    time.sleep(0.5)

    result = json.loads(tools.tmux_capture_handler({"pane": "test", "lines": 30}))
    assert result["pane_id"] == "%0"
    assert "resolution-test" in result["text"]


def test_capture_nonexistent_target(sock: str) -> None:
    """A target that doesn't resolve returns a clean error envelope."""
    result = json.loads(tools.tmux_capture_handler({"pane": "no-such-session"}))
    assert "error" in result


def test_capture_strips_ansi(sock: str) -> None:
    """Color codes in shell output are stripped; the text content is preserved."""
    subprocess.run(
        ["tmux", "-L", sock, "new-window", "-t", "test", "-n", "ansi"],
        check=True,
    )
    ansi_pane = next(
        p for p in json.loads(tools.tmux_list_handler({}))["panes"]
        if p["window_name"] == "ansi"
    )
    tmux_send(sock, ansi_pane["pane_id"], "-l", "printf '\\033[31mRED\\033[0m\\n'")
    tmux_send(sock, ansi_pane["pane_id"], "Enter")
    time.sleep(0.5)

    captured = json.loads(
        tools.tmux_capture_handler({"pane": ansi_pane["pane_id"], "lines": 30})
    )
    assert "\x1b" not in captured["text"]
    assert "RED" in captured["text"]


def test_capture_include_normal_scrollback_param(sock: str) -> None:
    """The ``include_normal_scrollback: true`` path is accepted and returns cleanly."""
    # Reuse the ansi pane from the prior test, or the bash pane if not present.
    panes = json.loads(tools.tmux_list_handler({}))["panes"]
    target = next(
        (p for p in panes if p["window_name"] == "ansi"),
        panes[0],
    )
    result = json.loads(
        tools.tmux_capture_handler(
            {"pane": target["pane_id"], "lines": 5, "include_normal_scrollback": True}
        )
    )
    assert "pane_id" in result


def test_capture_alt_screen_vs_normal_scrollback(sock: str) -> None:
    """Lock in the tmux 3.5a flag semantics:

    - default (no ``-a``) returns the TUI / alt-screen surface
    - ``include_normal_scrollback: true`` returns the normal scrollback
      (the shell history), not the TUI

    Both paths must give different output for the same pane, and the
    default must show the TUI view (vim's status line / file content).
    """
    subprocess.run(
        ["tmux", "-L", sock, "new-window", "-t", "test", "-n", "vimwin"],
        check=True,
    )
    vim_pane = next(
        p for p in json.loads(tools.tmux_list_handler({}))["panes"]
        if p["window_name"] == "vimwin"
    )
    # Drop into vim on this test file. tmux's send-keys joins its
    # non-``-l`` arguments with a space; ``-l`` on the whole argv
    # also flags the trailing ``Enter`` as literal text. The correct
    # pattern is two invocations: literal text, then a key name.
    test_path = str(Path(__file__).resolve())
    tmux_send(sock, vim_pane["pane_id"], "-l", f"vi {test_path}")
    tmux_send(sock, vim_pane["pane_id"], "Enter")
    time.sleep(2.0)

    # Confirm vim is on the alternate screen.
    status = subprocess.run(
        ["tmux", "-L", sock, "display-message", "-p", "-t", vim_pane["pane_id"],
         "#{alternate_on}"],
        capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "1"

    alt = json.loads(tools.tmux_capture_handler({"pane": vim_pane["pane_id"], "lines": 100}))
    normal = json.loads(
        tools.tmux_capture_handler(
            {"pane": vim_pane["pane_id"], "lines": 100, "include_normal_scrollback": True}
        )
    )
    # TUI surface: contains the test file's contents (the docstring or
    # any distinctive line from the file vim opened). Just confirm the
    # TUI capture is *different* from the normal-scrollback capture —
    # the flag-semantic claim is "they diverge", not "TUI contains a
    # specific string".
    assert alt["text"] != normal["text"], (
        "alt-screen and normal-scrollback captures must differ"
    )
    # TUI surface: contains the file's contents — distinctive line
    # from this test file's docstring is reliable across renames.
    assert "Tests for" in alt["text"] or "tmux_capture" in alt["text"]
    # Normal scrollback: shows the shell history, NOT the TUI. The
    # `vi test_tmux_capture.py` command must be in the history.
    assert "test_tmux_capture.py" in normal["text"] and "vi" in normal["text"]
    assert "Top" not in normal["text"] and "1,1" not in normal["text"]

    # Clean up vim so the server can be killed.
    tmux_send(sock, vim_pane["pane_id"], ":q!", "Enter")
    time.sleep(0.3)
