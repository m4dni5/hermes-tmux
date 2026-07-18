"""Tests for the ``/pane`` slash command handler.

Each test calls ``_pane_command_handler(raw_args)`` and inspects the
return value and/or the ``FakeCtx._injected`` list to verify the
handler's behaviour.  The test server's initial pane is ``%0`` —
same convention as every other test file.
"""

import json

import tmux_tools


# -----------------------------------------------------------------------
# No target, no _last_pane
# -----------------------------------------------------------------------

def test_pane_no_target_no_last_pane(fake_ctx):
    """``/pane`` with no args and no prior interaction returns error."""
    result = tmux_tools._pane_command_handler("")
    assert isinstance(result, str)
    assert "No pane target" in result
    assert len(fake_ctx._injected) == 0


def test_pane_whitespace_only_no_last_pane(fake_ctx):
    """``/pane  `` (whitespace only) same as no target."""
    result = tmux_tools._pane_command_handler("   ")
    assert isinstance(result, str)
    assert "No pane target" in result
    assert len(fake_ctx._injected) == 0


# -----------------------------------------------------------------------
# Explicit target — no hint
# -----------------------------------------------------------------------

def test_pane_target_no_hint(fake_ctx):
    """``/pane %0`` captures the pane and injects content."""
    result = tmux_tools._pane_command_handler("%0")
    assert result is None

    assert len(fake_ctx._injected) == 1
    msg = fake_ctx._injected[0]
    assert msg["role"] == "user"

    content = msg["content"]
    # Header should name the resolved target and pane_id, no hint.
    assert content.startswith("[pane ")
    # Content is in a code block.
    assert "```" in content


def test_pane_target_window_dot_pane(fake_ctx):
    """``/pane test:0.0`` resolves correctly."""
    result = tmux_tools._pane_command_handler("test:0.0")
    assert result is None
    assert len(fake_ctx._injected) == 1


# -----------------------------------------------------------------------
# Explicit target with hint
# -----------------------------------------------------------------------

def test_pane_target_with_hint(fake_ctx):
    """``/pane %0 nmap scan output`` includes hint in header."""
    result = tmux_tools._pane_command_handler("%0 nmap scan output")
    assert result is None

    assert len(fake_ctx._injected) == 1
    content = fake_ctx._injected[0]["content"]
    assert ": nmap scan output]" in content


def test_pane_target_with_hint_preserves_spaces(fake_ctx):
    """Hint preserves internal spaces (split on first whitespace only)."""
    result = tmux_tools._pane_command_handler("%0 scan for port 443")
    assert result is None

    assert len(fake_ctx._injected) == 1
    content = fake_ctx._injected[0]["content"]
    assert ": scan for port 443]" in content


# -----------------------------------------------------------------------
# Default target via _last_pane
# -----------------------------------------------------------------------

def test_pane_defaults_to_last_pane(fake_ctx):
    """``/pane`` with no target uses the most recent pane the agent touched."""
    # Simulate the agent having captured this pane earlier.
    tmux_tools._last_pane = "%0"

    result = tmux_tools._pane_command_handler("")
    assert result is None

    assert len(fake_ctx._injected) == 1
    content = fake_ctx._injected[0]["content"]
    assert "%0" in content


# -----------------------------------------------------------------------
# Errors
# -----------------------------------------------------------------------

def test_pane_nonexistent_target(fake_ctx):
    """``/pane nonexistent-window`` returns error from _resolve_pane_id."""
    result = tmux_tools._pane_command_handler("nonexistent-window")
    assert isinstance(result, str)
    assert "not found" in result
    assert len(fake_ctx._injected) == 0


# -----------------------------------------------------------------------
# _last_pane tracking through tool use
# -----------------------------------------------------------------------

def test_last_pane_set_by_capture(fake_ctx):
    """Calling tmux_capture sets _last_pane."""
    tmux_tools._last_pane = None  # reset

    result = json.loads(
        tmux_tools.tmux_capture_handler({"pane": "%0"})
    )
    assert "error" not in result
    assert tmux_tools._last_pane == result["pane_id"]


def test_last_pane_set_by_send(fake_ctx):
    """Calling tmux_send sets _last_pane."""
    tmux_tools._last_pane = None  # reset

    result = json.loads(
        tmux_tools.tmux_send_handler({"pane": "%0", "text": "echo set-by-send"})
    )
    assert result.get("status") == "ok"
    assert tmux_tools._last_pane == result["pane_id"]