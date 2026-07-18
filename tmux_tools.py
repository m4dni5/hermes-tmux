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
import textwrap
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Cap on lines any single capture can return (schema-enforced; this is the
# belt to the schema's suspenders). Default for callers who don't specify.
_MAX_CAPTURE_LINES = 5000
_DEFAULT_CAPTURE_LINES = 200
# tmux list/capture/send are all fast. 10s leaves headroom for slow shells.
_TMUX_TIMEOUT = 10

# tmux_wait: bake-in constants. 30 lines is a status hint — enough to catch
# patterns that scrolled past a tighter window between polls, and enough
# context on timeout for the agent to diagnose why the pattern didn't
# appear. Not a full read — the expected follow-up is still
# tmux_capture(pane, lines=N) for the full scrollback.
_WAIT_LINES = 30
_WAIT_POLL_INTERVAL_S = 0.1
# Per-poll capture timeout. Should be small (the poll is bounded by the
# outer timeout) but enough for a slow shell to respond.
_WAIT_CAPTURE_TIMEOUT = 3

# tmux_send post-send capture: 100ms tail wait + 5-line snapshot of the
# pane. Same 5-line contract as tmux_wait — this is a hint, not the
# answer. For instant-return commands (echo, pwd, ls) the snapshot has
# the result and the agent can skip tmux_capture. For slow commands
# (build, server start) the snapshot may be empty or partial and the
# agent calls tmux_capture. The 100ms is a small tax on every send that
# buys the win case; not exposed as a parameter.
_POST_SEND_TAIL_S = 0.1
_POST_SEND_LINES = 5

# Track the most recent pane the agent captured from or sent to, so
# ``/pane`` (no argument) defaults to the pane the agent was last
# interacting with. Resolved ``%pane_id``, not user-facing target.
_last_pane: Optional[str] = None

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

    # Fast path: when the input is already a ``%pane_id`` and the agent
    # is inside tmux (``$TMUX`` was set at register time, so
    # ``_self_socket`` is non-None), skip the ``display-message``
    # round-trip. tmux accepts ``%pane_id`` directly as a target, and
    # the agent's own socket is the default server — no ``-L`` needed.
    # The trade-off: ``target`` in the response is the bare ``%pane_id``
    # instead of ``session:window.pane``. That's still a valid target
    # format for any subsequent call; the full form is available via
    # ``tmux_list`` if the agent needs it.
    self_sock = _self_socket_or_none()
    if pane.startswith("%") and self_sock is not None:
        return {"pane_id": pane, "target": pane, "socket": self_sock}

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

    global _last_pane
    _last_pane = pane_id

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
    a fixed window). Returns the captured text on success, or
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


def _envelope_with_post_send_capture(
    response: Dict[str, Any], pane_id: str, socket: Optional[str],
) -> str:
    """Add a 5-line post-send capture to a successful tmux_send response.

    Sleeps ``_POST_SEND_TAIL_S`` so the command has a chance to produce
    output, then captures the last ``_POST_SEND_LINES`` lines of the
    pane. The result is added to ``response`` as the
    ``post_send_capture`` field, then the response is returned as a JSON
    string.

    If the capture itself fails (pane gone, server died), the field is
    set to an empty string rather than omitting it — the agent's logic
    is "if post_send_capture has the answer, use it; if empty, call
    tmux_capture." A missing field forces a "is the key there?" branch
    the agent doesn't need.

    No schema change: the response shape is opaque to the schema
    (oneOf describes the *args* shape, not the response). Documented
    in AGENTS.md alongside the tmux_wait 5-line hint.
    """
    time.sleep(_POST_SEND_TAIL_S)
    text = _capture_text(pane_id, socket, _POST_SEND_LINES,
                         include_normal=False, timeout=_WAIT_CAPTURE_TIMEOUT)
    if isinstance(text, dict) and "error" in text:
        # Capture failed (pane gone mid-send, server died). Empty
        # string is the documented contract for "no snapshot available";
        # the agent's fall-through is to call tmux_capture.
        text = ""
    response["post_send_capture"] = text
    return json.dumps(response, ensure_ascii=False)


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

    global _last_pane
    _last_pane = pane_id

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

            response = {
                "pane_id": pane_id,
                "target": target,
                "mode": "text",
                "sent": text,
                "submit": submit,
                "status": "ok",
            }
            return _envelope_with_post_send_capture(response, pane_id, socket)

        # Keystroke mode. ``keys`` is the list of tmux key names.
        # No submit flag: the agent includes ``"Enter"`` in the list
        # if they want it.
        keys = args["keys"]
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            return json.dumps({"error": "keys must be a list of strings"})
        if not keys:
            return json.dumps({"error": "keys must be a non-empty list"})

        # Flag-injection guard: tmux key names are alphanumeric (Enter,
        # C-c, BSpace, Up, etc.). A leading ``-`` would be interpreted
        # as a tmux flag (``-l`` for literal, ``-N`` for repeat count,
        # ``-X`` for copy-mode commands), not as a key name. The tmux
        # key name for ``-`` is ``Minus`` — no legitimate key name
        # starts with ``-``.
        if any(k.startswith("-") for k in keys):
            return json.dumps({
                "error": "key names must not start with '-' (use 'Minus' for the - key)",
            })

        # One ``send-keys`` call with the key names as trailing
        # arguments (no -l, so tmux interprets them as key names).
        _run_tmux(["send-keys", "-t", pane_id] + keys, timeout=5, socket=socket)

        response = {
            "pane_id": pane_id,
            "target": target,
            "mode": "keys",
            "sent": keys,
            "status": "ok",
        }
        return _envelope_with_post_send_capture(response, pane_id, socket)
    except Exception as exc:
        return json.dumps({"error": f"tmux send-keys failed: {exc}"})


# ---------------------------------------------------------------------------
# tmux_wait
#
# Polls the captured text for a substring or regex and returns when the
# pattern appears or the timeout fires. Two modes:
#
#   async=false (default) — blocking.  The agent waits and gets the
#       result inline, same as a regular tool call.
#   async=true — non-blocking.  The handler spawns a background
#       Python process that polls the pane and prints a JSON result
#       when done.  The framework delivers it as a follow-up message.
#       The agent can continue other work in the meantime.
#
# Why polling instead of ``tmux wait-for``:
#   - ``wait-for`` requires the *command itself* to participate in the
#     sync (``cmd; tmux wait-for -S done``), which couples every
#     command the agent drives to the sync pattern.
#   - Polling is the black-box version: works with anything that
#     produces text in a pane, no command-side cooperation needed.
#
# The response always includes the last ``_WAIT_LINES`` lines so the
# agent can decide what to do next — ``tmux_capture`` for full output,
# send more input, or give up.  ``tmux_wait`` is a *decision tool*, not
# a read tool.
# ---------------------------------------------------------------------------


_ASYNC_POLL_SCRIPT = textwrap.dedent("""\
import json, re, subprocess, sys, time
pane_id = sys.argv[1]
pattern = sys.argv[2]
timeout_s = int(sys.argv[3])
use_regex = sys.argv[4] == '1'
lines = int(sys.argv[5])

# Build the tmux command.  Extra args (socket -L flag) may trail the
# positional arguments — they are passed as raw tmux argv fragments.
tmux_cmd = ['tmux']
if len(sys.argv) > 6 and sys.argv[6]:
    tmux_cmd.extend(sys.argv[6:])
tmux_cmd.extend(['capture-pane', '-p', '-J', '-q', '-t', pane_id,
                  '-S', f'-{lines}'])

started = time.monotonic()
deadline = started + timeout_s
while True:
    r = subprocess.run(tmux_cmd, capture_output=True, text=True, timeout=3)
    text = r.stdout.rstrip('\\n')
    if use_regex:
        try:
            matched = bool(re.search(pattern, text, re.DOTALL))
        except re.error as exc:
            print(json.dumps({'error': f'invalid regex: {exc}'}))
            sys.exit(1)
    else:
        matched = pattern in text
    if matched:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(json.dumps({
            'pane_id': pane_id, 'pattern': pattern,
            'regex': use_regex, 'matched': True,
            'elapsed_ms': elapsed_ms, 'text': text,
        }))
        sys.exit(0)
    if time.monotonic() >= deadline:
        elapsed_ms = timeout_s * 1000
        print(json.dumps({
            'pane_id': pane_id, 'pattern': pattern,
            'regex': use_regex, 'matched': False,
            'elapsed_ms': elapsed_ms, 'text': text,
        }))
        sys.exit(0)
    time.sleep(0.1)
""")


def tmux_wait_handler(args: Dict[str, Any], **kwargs) -> str:
    """Wait for a substring or regex to appear in a tmux pane, or time out."""
    pane_ref = (args.get("pane") or "").strip()
    if not pane_ref:
        return json.dumps({"error": "pane is required"})

    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return json.dumps(
            {"error": "pattern is required and must be a non-empty string"}
        )

    use_regex = bool(args.get("regex", False))
    use_async = bool(args.get("async", False))

    # Validate regex early (before spawning) so the agent gets a clean
    # error inline rather than a cryptic background failure.
    if use_regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            return json.dumps(
                {"error": f"invalid regex pattern: {exc}"}
            )

    # Resolve timeout.
    raw_timeout = args.get("timeout")
    if raw_timeout is None:
        timeout_s = 10
    else:
        try:
            timeout_s = int(raw_timeout)
        except (TypeError, ValueError):
            return json.dumps(
                {"error": f"timeout must be an integer (got {raw_timeout!r})"}
            )
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

    if use_async:
        # Build a background command: python3 -c '<script>' <args...>
        # shlex-quote every user-provided value so shell metacharacters
        # in the pattern don't escape the quoting.
        socket_args: list[str] = []
        if socket and socket != _self_socket_or_none():
            socket_args = ["-L", socket]
        background_cmd = (
            "python3 -c "
            + shlex.quote(_ASYNC_POLL_SCRIPT)
            + " "
            + shlex.quote(pane_id)
            + " "
            + shlex.quote(pattern)
            + " "
            + str(timeout_s)
            + " "
            + ("1" if use_regex else "0")
            + " "
            + str(_WAIT_LINES)
            + " "
            + shlex.join(socket_args)
        )
        ctx = _ctx_or_none()
        if ctx is None:
            return json.dumps({"error": "PluginContext not initialized"})
        # Fire-and-forget.  The framework delivers the process stdout
        # as a follow-up message when it exits.
        ctx.dispatch_tool(
            "terminal",
            {
                "command": background_cmd,
                "timeout": timeout_s + 15,  # headroom over the poll deadline
                "background": True,
                "notify_on_complete": True,
            },
        )
        return json.dumps(
            {
                "status": "watching",
                "pane_id": pane_id,
                "target": target,
                "pattern": pattern,
                "regex": use_regex,
                "timeout_s": timeout_s,
            }
        )

    # --- blocking path (async=false) -----------------------------------------

    # Polling loop.  We start with a poll at t=0 (no point waiting
    # first), then sleep _WAIT_POLL_INTERVAL_S between polls.
    started = time.monotonic()
    deadline = started + timeout_s
    last_text = ""

    while True:
        text = _capture_text(
            pane_id,
            socket,
            _WAIT_LINES,
            include_normal=False,
            timeout=_WAIT_CAPTURE_TIMEOUT,
        )
        if isinstance(text, dict) and "error" in text:
            return json.dumps(
                {
                    **text,
                    "pane_id": pane_id,
                    "target": target,
                    "pattern": pattern,
                    "regex": use_regex,
                }
            )
        last_text = text

        if use_regex:
            matched = bool(re.search(pattern, text, re.DOTALL))
        else:
            matched = pattern in text

        if matched:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return json.dumps(
                {
                    "pane_id": pane_id,
                    "target": target,
                    "pattern": pattern,
                    "regex": use_regex,
                    "matched": True,
                    "elapsed_ms": elapsed_ms,
                    "text": text,
                },
                ensure_ascii=False,
            )

        if time.monotonic() >= deadline:
            return json.dumps(
                {
                    "pane_id": pane_id,
                    "target": target,
                    "pattern": pattern,
                    "regex": use_regex,
                    "matched": False,
                    "elapsed_ms": timeout_s * 1000,
                    "text": last_text,
                },
                ensure_ascii=False,
            )

        time.sleep(_WAIT_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# /pane slash command — user-driven pane context injection
# ---------------------------------------------------------------------------


def _pane_command_handler(raw_args: str) -> str | None:
    """Handle ``/pane [target] [hint]`` — capture pane content and inject
    it as a user message so the agent incorporates it before responding.

    The user drives this; it's not automatic.  ``target`` is any format
    tmux accepts (``%pane_id``, ``window.pane``, window name, etc.).
    ``hint`` is an optional string telling the agent what to pay
    attention to (e.g. ``/pane 2.0 nmap scan output``).  With no
    arguments, defaults to the most recent pane the agent interacted
    with — the one it last captured from or sent to.
    """
    raw_args = raw_args.strip()

    # Parse: first whitespace-delimited token is the target; everything
    # after it (including additional spaces) is the hint.
    if raw_args:
        parts = raw_args.split(maxsplit=1)
        target = parts[0]
        hint = parts[1] if len(parts) > 1 else ""
    else:
        target = ""
        hint = ""

    if not target:
        global _last_pane
        if _last_pane:
            target = _last_pane
        else:
            return (
                "No pane target.  Use /pane <window.pane> (e.g. /pane 2.0)"
                " or /pane <window> (e.g. /pane nc)."
            )

    resolved = _resolve_pane_id(target)
    if "error" in resolved:
        return resolved["error"]

    pane_id = resolved["pane_id"]
    resolved_target = resolved["target"]
    socket = resolved.get("socket")

    text = _capture_text(pane_id, socket, _DEFAULT_CAPTURE_LINES)
    if isinstance(text, dict) and "error" in text:
        return text["error"]

    # Frame the content so the model knows this is observational data
    # the user is sharing — not a command to execute.
    header = f"[pane {resolved_target} ({pane_id})"
    if hint:
        header += f": {hint}"
    header += "]"
    message = f"{header}\n\n```\n{text}\n```"

    ctx = _ctx_or_none()
    if ctx is None:
        return "Plugin context not initialized."
    ok = ctx.inject_message(message, role="user")
    if not ok:
        return "Failed to inject pane content (no CLI reference)."

    # None = handled silently — the injected message starts the agent's turn.
    return None
