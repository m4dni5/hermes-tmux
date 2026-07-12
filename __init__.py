"""tmux plugin — pane observability for Hermes Agent.

Four tools: ``tmux_list`` (pane inventory), ``tmux_capture`` (read pane
contents), ``tmux_send`` (send text or a key press), ``tmux_wait``
(block on a substring). Lifecycle (spawning and tearing down panes) is
deliberately out of scope — see AGENTS.md for the design rules.

The plugin is a thin wrapper over ``tmux`` shell commands dispatched
through the framework's ``terminal`` tool, so the usual approval,
redaction, and interrupt pipelines apply to every tmux invocation.
"""

from __future__ import annotations

import logging
import os
import shutil

import schemas
import tools

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------
# PluginContext capture
#
# Plugin tool handlers are called by the framework with ``(args, **kw)``
# but ``ctx`` is NOT threaded through. It's only available inside
# ``register(ctx)``. We stash it in tools.py at registration time so
# the handlers can route every tmux call through the framework's
# pipelines (approval, redaction, interrupt).
# --------------------------------------------------------------------


# --------------------------------------------------------------------
# Availability check — hides all three tools when tmux isn't usable.
# The check is on the binary alone: the agent doesn't have to be
# inside a tmux session to drive one. Driving a session from outside
# (e.g. a subagent spawned in a non-tmux context) is the realistic case.
# --------------------------------------------------------------------

def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


# --------------------------------------------------------------------
# Self-pane and self-socket capture
#
# ``$TMUX_PANE`` is the agent's own ``%pane_id`` (set by tmux in every
# attached pane). Captured at register time so ``tmux_send`` can
# refuse to target it — covers the mis-target case (stale pane_id,
# resolved-by-name target) where keystrokes would land in the
# agent's own input. No-op when the agent is outside tmux.
#
# ``$TMUX`` is the agent's own tmux server socket path (e.g.
# ``/tmp/tmux-1000/default,781089,0``). The basename ("default") is
# the value tmux's ``-L`` flag takes. ``_resolve_pane_id`` returns
# the socket for any target pane, and ``_run_tmux`` adds ``-L`` only
# when the target's socket differs from the agent's. No-op when the
# agent is outside tmux — every call then needs an explicit socket.
# --------------------------------------------------------------------

def _capture_self_pane() -> str | None:
    pane = (os.environ.get("TMUX_PANE") or "").strip()
    return pane or None


def _capture_self_socket() -> str | None:
    tmux_env = (os.environ.get("TMUX") or "").strip()
    if not tmux_env:
        return None
    # ``$TMUX`` format: ``/tmp/tmux-UID/<name>,<session_id>,<idx>`` —
    # the basename of the first comma-separated segment is the
    # socket name. The session-id and window-idx parts are the
    # attached session and window; we don't need them.
    first = tmux_env.split(",", 1)[0]
    return first.rsplit("/", 1)[-1] if "/" in first else first


_TOOLS = (
    ("tmux_list",    "tmux_list",    schemas.TMUX_LIST_SCHEMA,    tools.tmux_list_handler,    "📋"),
    ("tmux_capture", "tmux_capture", schemas.TMUX_CAPTURE_SCHEMA, tools.tmux_capture_handler, "📜"),
    ("tmux_send",    "tmux_send",    schemas.TMUX_SEND_SCHEMA,    tools.tmux_send_handler,    "⌨️"),
    ("tmux_wait",    "tmux_wait",    schemas.TMUX_WAIT_SCHEMA,    tools.tmux_wait_handler,    "⏳"),
)


def register(ctx) -> None:
    """Register all tmux tools.

    Called once by the plugin loader. The tools' check_fn
    (``_tmux_available``) gates the model's tool list on tmux presence
    at request time — when tmux isn't running, the model just doesn't
    see these tools.
    """
    tools.set_ctx(ctx)
    tools.set_self_pane(_capture_self_pane())
    tools.set_self_socket(_capture_self_socket())

    for name, toolset, schema, handler, emoji in _TOOLS:
        try:
            ctx.register_tool(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=_tmux_available,
                emoji=emoji,
            )
        except Exception as exc:
            # Don't take down the whole plugin on a single registration
            # failure. Log and move on so the other tools can still load.
            logger.warning("tmux plugin: failed to register %s: %s", name, exc)
