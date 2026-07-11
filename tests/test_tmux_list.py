"""Tests for ``tmux_list`` — pane inventory and filtering.

Two cases from the original smoke test:
  - basic list returns the initial pane with stable %pane_id
  - ``include_dead`` + ``target`` filter work together
"""

from __future__ import annotations

import json
import subprocess

from hermes_tmux import tools

from .conftest import tmux_send


def test_list_returns_initial_pane(sock: str) -> None:
    """``tmux_list()`` finds the smoke server's initial bash pane."""
    result = json.loads(tools.tmux_list_handler({}))
    assert "panes" in result
    assert result["pane_count"] == 1
    assert result["panes"][0]["target"] == "test:bash.0"
    assert result["panes"][0]["pane_id"].startswith("%")


def test_list_include_dead_shows_dead_pane(sock: str) -> None:
    """Dead panes are filtered by default; ``include_dead: true`` reveals them."""
    subprocess.run(
        ["tmux", "-L", sock, "new-window", "-t", "test", "-n", "sender"],
        check=True,
    )
    sender_pane = next(p for p in json.loads(tools.tmux_list_handler({}))["panes"]
                       if p["window_name"] == "sender")
    # Drive the shell to a clean exit. ``tmux kill-pane`` removes the
    # pane entirely; we want it to stay around as a *dead* pane so
    # the plugin's ``include_dead`` filter has something to filter.
    # The original smoke test sent ``Enter Enter C-d``; that races the
    # shell's EOF handling. ``exit`` is a deterministic shell-exit
    # command and works reliably across shells. With ``remain-on-exit
    # on`` (set in conftest) the pane persists as dead.
    tmux_send(sock, sender_pane["pane_id"], "-l", "exit")
    tmux_send(sock, sender_pane["pane_id"], "Enter")
    import time; time.sleep(0.3)

    default = json.loads(tools.tmux_list_handler({}))
    explicit = json.loads(tools.tmux_list_handler({"include_dead": True}))
    # Default: sender is gone, only live panes remain.
    default_sender = next(
        (p for p in default["panes"] if p["pane_id"] == sender_pane["pane_id"]),
        None,
    )
    assert default_sender is None, "default omits dead panes"
    # include_dead: sender is visible again, marked is_dead.
    dead = next(
        p for p in explicit["panes"] if p["pane_id"] == sender_pane["pane_id"]
    )
    assert dead["is_dead"] is True


def test_list_target_filter_no_match(sock: str) -> None:
    """A target that matches no pane returns 0 panes."""
    no_match = json.loads(
        tools.tmux_list_handler({"target": "no-such-session", "include_dead": True})
    )
    assert no_match["pane_count"] == 0
