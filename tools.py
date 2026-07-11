"""Tool handlers for the tmux plugin.

Each handler is a thin wrapper that:

1. Resolves the user-facing pane reference to a stable ``%pane_id`` via
   ``tmux display-message`` (so callers can pass ``%12``, ``session:window.pane``,
   ``window.pane``, or a bare window/session name).
2. Runs the actual tmux command via ``ctx.dispatch_tool("terminal", ...)``
   so the work flows through Hermes's framework pipelines (approval,
   redaction, interrupt).
3. Parses tmux's text output into structured JSON the model can act on.

All tmux flag choices are baked in. The schema exposes only the
one tmux flag the agent might legitimately want to flip —
``include_normal_scrollback`` on ``tmux_capture`` — plus the two-mode
shape of ``tmux_send`` (``text`` for typing, ``keys`` for keystrokes).
"""


from __future__ import annotations

import json
import logging
import re
import shlex
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Cap on lines any single capture can return (schema-enforced; this is the
# belt to the schema's suspenders). Default for callers who don't specify.
_MAX_CAPTURE_LINES = 5000
_DEFAULT_CAPTURE_LINES = 200
# tmux list/capture/send are all fast. 10s leaves headroom for slow shells.
_TMUX_TIMEOUT = 10

# tmux_wait: bake-in constants. 5 lines is a status hint, not a full read.
# The agent's expected follow-up is tmux_capture(pane, lines=N) for the
# full scrollback; tmux_wait exists to *decide* whether to call that.
_WAIT_LINES = 5
_WAIT_POLL_INTERVAL_S = 0.1
# Per-poll capture timeout. Should be small (the poll is bounded by the
# outer timeout) but enough for a slow shell to respond.
_WAIT_CAPTURE_TIMEOUT = 3

# --------------------------------------------------------------------
# PluginContext handle
#
# ``register()`` in __init__.py calls ``set_ctx()`` once with the live
# PluginContext. The handlers below read it via ``_ctx_or_none()`` so
# they can route every tmux call through the framework's pipelines.
# --------------------------------------------------------------------

_ctx: Optional[Any] = None


def set_ctx(ctx: Any) -> None:
    """Called once from __init__.register() with the live PluginContext."""
    global _ctx
    _ctx = ctx


def _ctx_or_none() -> Optional[Any]:
    return _ctx

# Agent's own ``%pane_id``, captured from ``$TMUX_PANE`` at register time.
# When set, ``tmux_send`` refuses to target it (prevents the mis-target
# case where a stale or resolved target lands on the agent's own pane).
_self_pane: Optional[str] = None


def set_self_pane(pane: Optional[str]) -> None:
    """Called once from __init__.register() with the agent's own pane_id."""
    global _self_pane
    _self_pane = pane


def _self_pane_or_none() -> Optional[str]:
    return _self_pane

# Agent's own tmux server socket name (basename of $TMUX path, e.g.
# "default" from "/tmp/tmux-1000/default,781089,0"). Used as the
# comparison baseline in _run_tmux — if a target pane is on this same
# socket, no -L flag is needed (the agent's $TMUX env points there).
# None when the agent is outside tmux; in that case every call needs
# an explicit -L (or the default server) since there's no "same as me"
# fallback.
_self_socket: Optional[str] = None


def set_self_socket(name: Optional[str]) -> None:
    """Called once from __init__.register() with the agent's own socket name."""
    global _self_socket
    _self_socket = name


def _self_socket_or_none() -> Optional[str]:
    return _self_socket

# ANSI escape sequence stripper. Covers the common CSI sequences (colors,
# cursor movement) and OSC sequences (titles, etc.). Not a full ECMA-48
# implementation — anything weirder than this can wait.
_ANSI_RE = re.compile(
    r"""
    \x1B\] [^\x07\x1B]* (?:\x07|\x1B\\)
  | \x1B \[ [0-?]* [ -/]* [@-~]
  | \x1B [P^_].*?\x1B\\
  | \x1B [@-Z\\-_]
    """,
    re.VERBOSE | re.DOTALL,
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from a captured scrollback."""
    if not text:
        return text
    return _ANSI_RE.sub("", text)


def _run_tmux(args: list[str], timeout: int = _TMUX_TIMEOUT, socket: Optional[str] = None) -> str:
    """Run a tmux command via the framework's terminal tool.

    Returns the raw stdout as a string. The framework handles approval,
    redaction, and interrupts; we just get the text back.

    The framework's terminal tool takes ``command`` as a string, not a
    list, so we shlex-join the argv. shlex-join handles quoting correctly
    so values containing spaces (e.g. ``-F '#{pane_current_command}'``)
    survive the round-trip.

    The framework wraps the result in a JSON envelope
    ``{"output": "<stdout>", "exit_code": <int>, "error": ...}``. We parse
    that envelope here so callers get the bare stdout. On non-zero exit
    code (or missing ``output`` field), we raise so the handler can
    surface a clean error to the model.

    The optional ``socket`` argument selects which tmux server to talk
    to. If it matches the agent's own socket (captured at register
    time), no -L flag is added — tmux uses $TMUX by default. If it
    differs, ``-L <name>`` is prepended to the argv. If the agent is
    outside tmux (``_self_socket`` is None) and no ``socket`` is passed,
    tmux's default server is used (the ``default`` socket).
    """
    if socket and socket != _self_socket_or_none():
        # Pane is on a different local server. The socket name is the
        # basename of the socket path (tmux's convention: the path is
        # /tmp/tmux-UID/<name>, the -L flag takes <name>).
        args = ["-L", socket, *args]
    ctx = _ctx_or_none()
    if ctx is None:
        # Plugin registered but ctx never captured (shouldn't happen in
        # normal operation; defensive guard).
        raise RuntimeError("tmux plugin: PluginContext not initialized")
    raw = ctx.dispatch_tool("terminal", {
        "command": shlex.join(["tmux", *args]),
        "timeout": timeout,
    })
    # The terminal tool returns a JSON string envelope.
    envelope = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(envelope, dict):
        # Defensive: if the framework ever returns plain text again.
        return str(raw)
    output = envelope.get("output", "")
    exit_code = envelope.get("exit_code", 0)
    error = envelope.get("error")
    if exit_code != 0 or error:
        raise RuntimeError(
            error if error else f"tmux exit {exit_code}: {(output or '').strip()}"
        )
    return output or ""


def _resolve_pane_id(pane: str) -> Dict[str, Any]:
    """Resolve any pane reference to ``%pane_id`` and its tmux server socket.

    Accepts ``%12``, ``session:window.pane``, ``window.pane``, or a bare
    name. Returns ``{"pane_id": "%12", "target": "...", "socket": "<name>"}``
    on success, or ``{"error": ...}`` if the pane doesn't exist. The
    ``socket`` field is the basename of the tmux server's socket path
    (the value tmux's ``-L`` flag takes), so callers can route subsequent
    ``_run_tmux`` calls to the right server.
    """
    if not pane or not pane.strip():
        return {"error": "pane reference is required"}

    pane = pane.strip()

    # Single display-message call: pane_id, target, and socket_path.
    # tmux's #{socket_path} format variable returns the absolute path of
    # the server's socket; we take the basename for the -L flag.
    fmt = "#{pane_id} #{session_name}:#{window_name}.#{pane_index} #{socket_path}"
    try:
        raw = _run_tmux(["display-message", "-p", "-t", pane, fmt])
    except Exception as exc:
        return {"error": f"failed to resolve pane {pane!r}: {exc}"}

    parts = raw.strip().split(maxsplit=2)
    if len(parts) < 3 or not parts[0].startswith("%"):
        return {"error": f"pane {pane!r} not found"}

    pane_id, target, socket_path = parts
    # socket_path is e.g. "/tmp/tmux-1000/default"; take the basename.
    socket = socket_path.rsplit("/", 1)[-1] if "/" in socket_path else socket_path
    return {"pane_id": pane_id, "target": target, "socket": socket}


# ---------------------------------------------------------------------------
# tmux_list
# ---------------------------------------------------------------------------

def tmux_list_handler(args: Dict[str, Any], **kwargs) -> str:
    """List active tmux panes as structured JSON."""
    target_filter = (args.get("target") or "").strip()
    include_dead = bool(args.get("include_dead", False))

    # The format string produces one line per pane, tab-separated, with all
    # the fields we want. tab is the separator because none of the format
    # variables contain tabs in normal use.
    fmt = (
        "#{pane_id}\t"
        "#{session_name}\t"
        "#{window_index}\t"
        "#{window_name}\t"
        "#{pane_index}\t"
        "#{pane_current_command}\t"
        "#{pane_current_path}\t"
        "#{pane_dead}\t"
        "#{pane_width}x#{pane_height}"
    )
    try:
        # ``tmux_list`` has no target pane, so the per-pane socket
        # resolution doesn't apply. tmux picks the right server via
        # ``$TMUX`` (the agent is inside tmux) or the default. No
        # explicit ``-L`` is added.
        raw = _run_tmux(["list-panes", "-a", "-F", fmt], timeout=10)
    except Exception as exc:
        return json.dumps({"error": f"tmux list-panes failed: {exc}"})

    panes: list[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 9:
            # tmux output can contain noise; skip malformed lines.
            continue
        pane_id, session, win_idx, win_name, pane_idx, cmd, path, dead, size = parts[:9]
        is_dead = dead == "1"
        if is_dead and not include_dead:
            continue
        target = f"{session}:{win_name}.{pane_idx}"
        if target_filter and target_filter.lower() not in target.lower():
            continue
        panes.append({
            "pane_id": pane_id,
            "session_name": session,
            "window_index": int(win_idx) if win_idx.isdigit() else win_idx,
            "window_name": win_name,
            "pane_index": int(pane_idx) if pane_idx.isdigit() else pane_idx,
            "target": target,
            "current_command": cmd or "",
            "current_path": path or "",
            "is_dead": is_dead,
            "size": size,
        })

    return json.dumps({
        "pane_count": len(panes),
        "include_dead": include_dead,
        "panes": panes,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# tmux_capture
# ---------------------------------------------------------------------------

def tmux_capture_handler(args: Dict[str, Any], **kwargs) -> str:
    """Capture scrollback from a tmux pane, ANSI stripped, joined lines."""
    pane_ref = (args.get("pane") or "").strip()
    if not pane_ref:
        return json.dumps({"error": "pane is required"})

    lines = int(args.get("lines") or _DEFAULT_CAPTURE_LINES)
    if lines < 1:
        lines = 1
    if lines > _MAX_CAPTURE_LINES:
        lines = _MAX_CAPTURE_LINES

    include_normal = bool(args.get("include_normal_scrollback", False))

    resolved = _resolve_pane_id(pane_ref)
    if "error" in resolved:
        return json.dumps(resolved)
    pane_id = resolved["pane_id"]
    target = resolved["target"]
    socket = resolved.get("socket")

    text = _capture_text(pane_id, socket, lines, include_normal=include_normal)
    if isinstance(text, dict) and "error" in text:
        return json.dumps(text)

    line_count = text.count("\n") + 1 if text else 0

    return json.dumps({
        "pane_id": pane_id,
        "target": target,
        "lines_requested": lines,
        "lines_returned": line_count,
        "text": text,
    }, ensure_ascii=False)


def _capture_text(
    pane_id: str,
    socket: Optional[str],
    lines: int,
    include_normal: bool = False,
    timeout: int = 15,
) -> Any:
    """Run tmux capture-pane + ANSI strip on a resolved pane.

    Shared by ``tmux_capture_handler`` (which exposes lines + include_normal
    to the agent) and ``tmux_wait_handler`` (which polls the same flow with
    a fixed 5-line window). Returns the captured text on success, or
    ``{"error": ...}`` on failure — the caller decides how to surface it.
    """
    # tmux capture-pane flag semantics (tmux 3.5a, confirmed empirically):
    #   -p print, -J join wrapped lines, -q suppress "no alt screen" error
    #   -a select the NORMAL scrollback (the history a TUI is covering).
    #     Without -a, capture returns the alternate screen / TUI surface —
    #     the opposite of what the manpage wording suggests. The smoke
    #     test (test 11) locks this in; do not flip without updating it.
    tmux_args = ["capture-pane", "-p", "-J", "-q", "-t", pane_id, "-S", f"-{lines}"]
    if include_normal:
        tmux_args.insert(3, "-a")
    try:
        raw = _run_tmux(tmux_args, timeout=timeout, socket=socket)
    except Exception as exc:
        return {"error": f"tmux capture-pane failed: {exc}"}
    return _strip_ansi(raw).rstrip("\n")


# ---------------------------------------------------------------------------
# tmux_send
#
# The schema is a ``oneOf`` with two branches:
#   - typing mode:    ``text="..."``        (optionally ``submit: false``)
#   - keystroke mode: ``keys=["C-c", ...]`` (no submit; include "Enter" in list)
#
# The branches are disjoint by parameter name, so an agent that wants
# a keystroke can't accidentally leave a submit flag set: ``keys``
# simply doesn't have one. An agent that wants to type a command
# can't accidentally send a key-name list: ``text`` is a string.
# ---------------------------------------------------------------------------

def tmux_send_handler(args: Dict[str, Any], **kwargs) -> str:
    """Send text (typing mode) or keystrokes (keystroke mode) to a tmux pane."""
    pane_ref = (args.get("pane") or "").strip()
    if not pane_ref:
        return json.dumps({"error": "pane is required"})

    has_text = "text" in args
    has_keys = "keys" in args
    if has_text and has_keys:
        return json.dumps({"error": "pass either `text` (typing mode) or `keys` (keystroke mode), not both"})
    if not has_text and not has_keys:
        return json.dumps({"error": "pass either `text` (typing mode) or `keys` (keystroke mode)"})

    resolved = _resolve_pane_id(pane_ref)
    if "error" in resolved:
        return json.dumps(resolved)
    pane_id = resolved["pane_id"]
    target = resolved["target"]
    socket = resolved.get("socket")

    # Self-pane guard: refuse to send into the agent's own pane. The
    # check is post-resolution so any target format (``%12``,
    # ``session:window.pane``, bare window name, etc.) that resolves
    # to the agent's own pane is caught — including a stale pane_id
    # that happens to point there. The guard only fires when the
    # agent is inside tmux (``$TMUX_PANE`` was set at register time);
    # otherwise the agent has no pane of its own, and the guard is a
    # no-op so the tools can drive sessions from outside.
    self_pane = _self_pane_or_none()
    if self_pane and pane_id == self_pane:
        return json.dumps({
            "error": f"refusing to send to own pane ({pane_id}); use a different target"
        })

    try:
        if has_text:
            text = args["text"]
            if not isinstance(text, str):
                return json.dumps({"error": "text must be a string"})
            submit = bool(args.get("submit", True))

            # Typing mode: send ``-l`` (literal) for the text, then a
            # separate ``send-keys Enter`` if submit is true. The two
            # calls are necessary because applying ``-l`` to a whole
            # argv makes ``Enter`` the five literal characters.
            if text:
                _run_tmux(["send-keys", "-t", pane_id, "-l", text], timeout=5, socket=socket)
            if submit:
                _run_tmux(["send-keys", "-t", pane_id, "Enter"], timeout=5, socket=socket)

            return json.dumps({
                "pane_id": pane_id,
                "target": target,
                "mode": "text",
                "sent": text,
                "submit": submit,
                "status": "ok",
            }, ensure_ascii=False)

        # Keystroke mode. ``keys`` is the list of tmux key names.
        # No submit flag: the agent includes ``"Enter"`` in the list
        # if they want it.
        keys = args["keys"]
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            return json.dumps({"error": "keys must be a list of strings"})
        if not keys:
            return json.dumps({"error": "keys must be a non-empty list"})

        # One ``send-keys`` call with the key names as trailing
        # arguments (no -l, so tmux interprets them as key names).
        _run_tmux(["send-keys", "-t", pane_id] + keys, timeout=5, socket=socket)

        return json.dumps({
            "pane_id": pane_id,
            "target": target,
            "mode": "keys",
            "sent": keys,
            "status": "ok",
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"tmux send-keys failed: {exc}"})


# ---------------------------------------------------------------------------
# tmux_wait
#
# Replaces the agent's implicit ``sleep N`` between ``tmux_send`` and
# ``tmux_capture`` with a deterministic wait. Polls the captured text
# for a substring (not regex — keeps the schema simple and the model
# honest about what it's matching) and returns when the pattern appears
# or the timeout fires.
#
# Why polling instead of ``tmux wait-for``:
#   - ``wait-for`` requires the *command itself* to participate in the
#     sync (``cmd; tmux wait-for -S done``), which couples every
#     command the agent drives to the sync pattern. The reverse shell,
#     the exploit, the server log — none of them know about tmux.
#   - Polling is the black-box version: works with anything that
#     produces text in a pane, no command-side cooperation needed.
#
# Why a 5-line status hint (not the full capture):
#   - The agent's follow-up to a match is usually "now do the next
#     thing" (send the next command, read more, give up). Five lines
#     is enough to see "the prompt is back" or "an error appeared."
#   - The agent's follow-up to a timeout is "what's the pane doing?"
#     Same five lines answer that.
#   - If the agent wants more, ``tmux_capture(pane, lines=200)`` is
#     one call. ``tmux_wait`` is a *decision tool*, not a read tool.
# ---------------------------------------------------------------------------

def tmux_wait_handler(args: Dict[str, Any], **kwargs) -> str:
    """Wait for a substring to appear in a tmux pane, or time out."""
    pane_ref = (args.get("pane") or "").strip()
    if not pane_ref:
        return json.dumps({"error": "pane is required"})

    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return json.dumps({"error": "pattern is required and must be a non-empty string"})

    # Resolve timeout. ``args.get("timeout")`` can be: missing (None →
    # use default 10), an int/float, or a string. The explicit-or-10
    # pattern would silently rewrite a user-supplied 0 to 10 (because
    # ``0 or 10`` is ``10`` in Python), so we check for None separately.
    raw_timeout = args.get("timeout")
    if raw_timeout is None:
        timeout_s = 10
    else:
        try:
            timeout_s = int(raw_timeout)
        except (TypeError, ValueError):
            return json.dumps({"error": f"timeout must be an integer (got {raw_timeout!r})"})
    if timeout_s < 1:
        timeout_s = 1
    if timeout_s > 60:
        timeout_s = 60

    resolved = _resolve_pane_id(pane_ref)
    if "error" in resolved:
        return json.dumps(resolved)
    pane_id = resolved["pane_id"]
    target = resolved["target"]
    socket = resolved.get("socket")

    # Polling loop. We start with a poll at t=0 (no point waiting first),
    # then sleep _WAIT_POLL_INTERVAL_S between polls. ``time.monotonic``
    # is wall-clock-immune and monotonic, so the timeout is correct
    # under NTP corrections and DST.
    started = time.monotonic()
    deadline = started + timeout_s
    last_text = ""

    while True:
        text = _capture_text(
            pane_id, socket, _WAIT_LINES,
            include_normal=False, timeout=_WAIT_CAPTURE_TIMEOUT,
        )
        if isinstance(text, dict) and "error" in text:
            # Capture itself failed (pane gone, server down). Surface
            # the error rather than silently returning a stale hint.
            return json.dumps({
                **text,
                "pane_id": pane_id,
                "target": target,
                "pattern": pattern,
            })
        last_text = text

        if pattern in text:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return json.dumps({
                "pane_id": pane_id,
                "target": target,
                "pattern": pattern,
                "matched": True,
                "elapsed_ms": elapsed_ms,
                "text": text,
            }, ensure_ascii=False)

        if time.monotonic() >= deadline:
            return json.dumps({
                "pane_id": pane_id,
                "target": target,
                "pattern": pattern,
                "matched": False,
                "elapsed_ms": timeout_s * 1000,
                "text": last_text,
            }, ensure_ascii=False)

        time.sleep(_WAIT_POLL_INTERVAL_S)
