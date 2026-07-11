"""Smoke test for the tmux plugin handlers.

Exercises every public handler against a real tmux server, with a
fake PluginContext that dispatches to ``tmux -L <custom-socket>``.

Run from the plugin root:
    python3 smoke_test.py

Cleans up the test tmux server on exit. Exits non-zero on any failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Plugin root
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import tools  # noqa: E402

SOCK = "hermes-tmux-smoke"


class FakeCtx:
    """Minimal PluginContext for the smoke test.

    In production, ctx.dispatch_tool("terminal", ...) runs the command
    through the framework's approval/redaction/interrupt pipelines. For
    the smoke test we bypass that and run tmux directly with a custom
    socket name so we don't pollute the user's real tmux server.

    The framework's terminal tool takes ``command`` as a string, so the
    plugin's _run_tmux shlex-joins the argv before calling dispatch_tool.
    The fake shell-splits back into argv.
    """

    def __init__(self) -> None:
        pass

    def dispatch_tool(self, name: str, args: dict) -> str:
        # In production, ctx.dispatch_tool("terminal", ...) runs the
        # command through the framework's approval/redaction/interrupt
        # pipelines and returns a JSON envelope:
        #     {"output": "<stdout>", "exit_code": <int>, "error": <str|None>}
        # tools.py:_run_tmux parses that envelope, so the fake must
        # produce the same shape. The framework's terminal tool takes
        # ``command`` as a string, so the plugin's _run_tmux shlex-joins
        # the argv before calling dispatch_tool. We shell-split back
        # into argv and run it as-is — the plugin's per-pane resolution
        # already adds the right -L flag for the smoke server's socket.
        import shlex
        full = shlex.split(args["command"])
        r = subprocess.run(full, capture_output=True, text=True, timeout=10)
        return json.dumps({
            "output": r.stdout,
            "exit_code": r.returncode,
            "error": r.stderr if r.returncode != 0 else None,
        })


def _new_server() -> None:
    """Start a fresh detached tmux server for the test.

    Sets ``$TMUX`` and ``$TMUX_PANE`` in the test process to point at
    the smoke server's session, so the plugin's ``_self_socket``
    baseline matches the smoke server. Without this, ``tmux_list()``
    would route to whatever tmux server the test runner is attached
    to (typically the agent's own session), and the per-pane
    resolution would force a ``-L`` flag.
    """
    subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)
    r = subprocess.run(
        ["tmux", "-L", SOCK, "new-session", "-d", "-s", "smoke", "-x", "200", "-y", "50"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"new-session failed: {r.stderr}"
    # Set remain-on-exit so we can test the include_dead filter.
    subprocess.run(["tmux", "-L", SOCK, "set-option", "-g", "remain-on-exit", "on"], check=True)
    # Simulate the agent being attached to the smoke server's session.
    # The plugin reads these at register time, so we set them before
    # tools.set_ctx / set_self_* are called.
    pane_id = subprocess.run(
        ["tmux", "-L", SOCK, "list-panes", "-t", "smoke:0", "-F", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    socket_path = subprocess.run(
        ["tmux", "-L", SOCK, "display-message", "-p", "-t", pane_id, "#{socket_path}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    os.environ["TMUX"] = socket_path
    os.environ["TMUX_PANE"] = pane_id


def _kill_server() -> None:
    subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)


def _check(condition: bool, label: str, extra: str = "") -> None:
    mark = "OK" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({extra})" if extra else ""))
    if not condition:
        _kill_server()
        sys.exit(1)


def main() -> int:
    _new_server()
    tools.set_ctx(FakeCtx())
    # Mirror what __init__.register() does: capture the agent's own
    # pane and socket from the (now-smoke-server-pointing) env so
    # per-pane resolution's "is this the same socket" check works.
    import importlib
    importlib.reload(tools)
    tools.set_ctx(FakeCtx())
    tmux_env = os.environ.get("TMUX", "")
    tmux_pane = os.environ.get("TMUX_PANE", "")
    if tmux_env:
        first = tmux_env.split(",", 1)[0]
        socket = first.rsplit("/", 1)[-1] if "/" in first else first
        tools.set_self_socket(socket)
        if tmux_pane:
            tools.set_self_pane(tmux_pane)

    try:
        # --- 1: tmux_list ---
        print("1. tmux_list")
        result = tools.tmux_list_handler({})
        parsed = json.loads(result)
        _check("panes" in parsed, "returns panes key")
        _check(parsed["pane_count"] == 1, "finds 1 initial pane", str(parsed["pane_count"]))
        _check(parsed["panes"][0]["target"] == "smoke:bash.0", "target is session:window.pane",
               parsed["panes"][0]["target"])

        # --- 2: tmux_capture default (TUI/alt screen or visible pane) ---
        print("\n2. tmux_capture (default capture)")
        subprocess.run(
            ["tmux", "-L", SOCK, "send-keys", "-t", "%0", "-l", "echo hello-from-tmux-plugin"],
            check=True,
        )
        subprocess.run(["tmux", "-L", SOCK, "send-keys", "-t", "%0", "Enter"], check=True)
        time.sleep(0.8)
        result = tools.tmux_capture_handler({"pane": "%0", "lines": 50})
        parsed = json.loads(result)
        _check("hello-from-tmux-plugin" in parsed["text"], "captured output present",
               parsed["text"][-60:])
        _check(parsed["pane_id"] == "%0", "pane_id echoed correctly")
        _check(parsed["target"] == "smoke:bash.0", "target echoed correctly")

        # --- 3: target resolution (bare session name) ---
        print("\n3. tmux_capture with bare session name")
        result = tools.tmux_capture_handler({"pane": "smoke"})
        parsed = json.loads(result)
        _check(parsed["pane_id"] == "%0", "bare session name resolved to %0",
               parsed.get("pane_id", "missing"))

        # --- 4: nonexistent pane ---
        print("\n4. tmux_capture(nonexistent)")
        result = tools.tmux_capture_handler({"pane": "%9999"})
        parsed = json.loads(result)
        _check("error" in parsed, "returns error dict", result)

        # --- 5: send text in typing mode (default submit=true) ---
        print("\n5. tmux_send(text, default submit)")
        subprocess.run(["tmux", "-L", SOCK, "new-window", "-t", "smoke", "-n", "sender"], check=True)
        sender_pane = next(
            p for p in json.loads(tools.tmux_list_handler({}))["panes"]
            if p["window_name"] == "sender"
        )
        result = tools.tmux_send_handler({"pane": sender_pane["pane_id"], "text": "echo Pa$$w0rd"})
        parsed = json.loads(result)
        _check(parsed.get("status") == "ok", "send text ok", result)
        _check(parsed.get("mode") == "text", "mode == 'text'", str(parsed.get("mode")))
        _check(parsed.get("submit") is True, "submit defaults to True", str(parsed.get("submit")))
        time.sleep(0.5)
        captured = json.loads(tools.tmux_capture_handler({"pane": sender_pane["pane_id"], "lines": 30}))
        _check("Pa$$w0rd" in captured["text"], "literal $ preserved in scrollback",
               "Pa$$w0rd" if "Pa$$w0rd" in captured["text"] else captured["text"][-80:])

        # --- 6: send keystroke in keys mode (Ctrl+C) ---
        print("\n6. tmux_send(keys=['C-c'])")
        result = tools.tmux_send_handler(
            {"pane": sender_pane["pane_id"], "keys": ["C-c"]}
        )
        parsed = json.loads(result)
        _check(parsed.get("status") == "ok", "send keys ok", result)
        _check(parsed.get("mode") == "keys", "mode == 'keys'", str(parsed.get("mode")))
        _check(parsed.get("sent") == ["C-c"], "sent reflects the list",
               str(parsed.get("sent")))

        # --- 7: typing mode with submit=false leaves text in buffer ---
        print("\n7. tmux_send(text, submit=false)")
        result = tools.tmux_send_handler(
            {"pane": sender_pane["pane_id"], "text": "echo unfinished", "submit": False}
        )
        parsed = json.loads(result)
        _check(parsed.get("status") == "ok", "submit=false ok", result)
        _check(parsed.get("submit") is False, "submit reflected in response",
               str(parsed.get("submit")))
        time.sleep(0.3)
        captured = json.loads(tools.tmux_capture_handler({"pane": sender_pane["pane_id"], "lines": 20}))
        _check("unfinished" in captured["text"], "text in input buffer, not executed")

        # --- 7b: validation rejects the bad-shape calls ---
        print("\n7b. tmux_send validation (oneOf enforcement)")
        for bad_call, expected_substr in [
            ({"pane": sender_pane["pane_id"]}, "pass either"),
            ({"pane": sender_pane["pane_id"], "text": "x", "keys": ["C-c"]}, "not both"),
            ({"pane": sender_pane["pane_id"], "keys": []}, "non-empty"),
            ({"pane": sender_pane["pane_id"], "keys": "C-c"}, "list of strings"),
        ]:
            r = json.loads(tools.tmux_send_handler(bad_call))
            _check("error" in r and expected_substr in r["error"],
                   f"rejected: {bad_call}", r.get("error", "no error"))

        # --- 8: include_dead + target filter (with remain-on-exit) ---
        print("\n8. tmux_list(include_dead + target filter)")
        # Flush any pending input first (we left 'echo unfinished' in the
        # buffer with submit=false), then C-d to exit the shell.
        subprocess.run(["tmux", "-L", SOCK, "send-keys", "-t", sender_pane["pane_id"], "Enter"], check=True)
        subprocess.run(["tmux", "-L", SOCK, "send-keys", "-t", sender_pane["pane_id"], "Enter"], check=True)
        time.sleep(0.3)
        subprocess.run(["tmux", "-L", SOCK, "send-keys", "-t", sender_pane["pane_id"], "C-d"], check=True)
        time.sleep(0.5)
        default = json.loads(tools.tmux_list_handler({}))
        explicit = json.loads(tools.tmux_list_handler({"include_dead": True}))
        _check(default["pane_count"] == 1, "default hides dead pane", str(default["pane_count"]))
        _check(explicit["pane_count"] == 2, "include_dead shows dead pane", str(explicit["pane_count"]))
        dead = next((p for p in explicit["panes"] if p["pane_id"] == sender_pane["pane_id"]), None)
        _check(dead is not None and dead["is_dead"], "dead pane flagged is_dead=true")
        no_match = json.loads(tools.tmux_list_handler({"target": "no-such-session", "include_dead": True}))
        _check(no_match["pane_count"] == 0, "no-match target returns 0 panes")

        # --- 9: ANSI escape stripping (use a fresh pane) ---
        print("\n9. ANSI escape stripping")
        subprocess.run(["tmux", "-L", SOCK, "new-window", "-t", "smoke", "-n", "ansi"], check=True)
        ansi_pane = next(
            p for p in json.loads(tools.tmux_list_handler({}))["panes"]
            if p["window_name"] == "ansi"
        )
        subprocess.run(
            ["tmux", "-L", SOCK, "send-keys", "-t", ansi_pane["pane_id"],
             "-l", "printf '\\033[31mRED\\033[0m\\n'"],
            check=True,
        )
        subprocess.run(["tmux", "-L", SOCK, "send-keys", "-t", ansi_pane["pane_id"], "Enter"], check=True)
        time.sleep(0.5)
        captured = json.loads(tools.tmux_capture_handler({"pane": ansi_pane["pane_id"], "lines": 30}))
        _check("\x1b" not in captured["text"], "ANSI escapes stripped from captured text")
        _check("RED" in captured["text"], "RED text content preserved",
               captured["text"][-80:])

        # --- 10: include_normal_scrollback parameter (no TUI app, but path is exercised) ---
        print("\n10. tmux_capture(include_normal_scrollback=true)")
        result = tools.tmux_capture_handler(
            {"pane": ansi_pane["pane_id"], "lines": 5, "include_normal_scrollback": True}
        )
        parsed = json.loads(result)
        _check("pane_id" in parsed, "include_normal_scrollback parameter accepted", result[:120])

        # --- 11: alt-screen vs normal-scrollback with a TUI (vim) active ---
        # This pins down the flag semantics in tmux 3.5a, which are
        # counter-intuitive: `capture-pane -p` (no -a) returns the
        # TUI/alt-screen surface, and `-a` returns the NORMAL scrollback
        # (the history the TUI is covering). The default and the
        # include_normal_scrollback=true path must give different
        # output for the same pane, and the default must show the TUI
        # view.
        print("\n11. alt-screen vs normal-scrollback with vim active")
        subprocess.run(["tmux", "-L", SOCK, "new-window", "-t", "smoke", "-n", "vimwin"], check=True)
        vim_pane = next(
            p for p in json.loads(tools.tmux_list_handler({}))["panes"]
            if p["window_name"] == "vimwin"
        )
        # Drop into vim on the smoke_test.py file. tmux's send-keys joins
        # its non-`-l` arguments with a space, but `-l` applied to a whole
        # argv also flags the trailing `Enter` argument as literal text
        # (you end up typing "vi /pathEnter" as one word). The correct
        # pattern is TWO invocations: literal text, then a key name.
        smoke_path = str(HERE / "smoke_test.py")
        subprocess.run(
            ["tmux", "-L", SOCK, "send-keys", "-t", vim_pane["pane_id"], "-l", f"vi {smoke_path}"],
            check=True,
        )
        subprocess.run(
            ["tmux", "-L", SOCK, "send-keys", "-t", vim_pane["pane_id"], "Enter"],
            check=True,
        )
        time.sleep(2.0)
        # Confirm vim is actually on the alternate screen. The format
        # variable is `alternate_on` (no `pane_` prefix in this tmux
        # build); `pane_alternate_on` exists as a name but expands empty.
        status = subprocess.run(
            ["tmux", "-L", SOCK, "display-message", "-p", "-t", vim_pane["pane_id"], "#{alternate_on}"],
            capture_output=True, text=True, check=True,
        )
        _check(status.stdout.strip() == "1", "vim is on the alternate screen",
               status.stdout.strip())

        alt = json.loads(tools.tmux_capture_handler({"pane": vim_pane["pane_id"], "lines": 100}))
        normal = json.loads(tools.tmux_capture_handler(
            {"pane": vim_pane["pane_id"], "lines": 100, "include_normal_scrollback": True}
        ))
        # The alt-screen capture should show the file's contents (smoke_test.py
        # starts with a docstring line) — that's the TUI view.
        _check("smoke_test" in alt["text"] or "tmux plugin" in alt["text"] or "Smoke test" in alt["text"],
               "default capture shows the TUI/file content", alt["text"][-80:].replace("\n", " ⏎ "))
        # The normal-scroll back capture should NOT show the TUI surface — it
        # should show the shell history (the `vi smoke_test.py` command at minimum).
        _check("smoke_test.py" in normal["text"] and "vi" in normal["text"],
               "include_normal_scrollback shows the shell history, not the TUI",
               normal["text"][-80:].replace("\n", " ⏎ "))
        _check("Top" not in normal["text"] and "1,1" not in normal["text"],
               "normal scrollback does NOT contain the vim status line",
               "contains 'Top'!" if "Top" in normal["text"] else "ok")

        # Clean up vim so the server can be killed.
        subprocess.run(["tmux", "-L", SOCK, "send-keys", "-t", vim_pane["pane_id"], ":q!", "Enter"], check=True)
        time.sleep(0.3)

        # --- 12: self-pane guard ---
        # Simulate the agent's own pane by setting it on the tools
        # module, then send to that pane. Both modes (text and keys)
        # must be rejected. Other panes must still work.
        print("\n12. self-pane guard")
        # The original pane %0 is the smoke server's only pane; that
        # is also the "self" pane for this simulation.
        tools.set_self_pane("%0")
        try:
            r1 = json.loads(tools.tmux_send_handler({"pane": "%0", "text": "should-not-run"}))
            _check("error" in r1 and "refusing to send to own pane" in r1["error"],
                   "text mode rejected when targeting self pane", r1)
            r2 = json.loads(tools.tmux_send_handler({"pane": "%0", "keys": ["C-c"]}))
            _check("error" in r2 and "refusing to send to own pane" in r2["error"],
                   "keys mode rejected when targeting self pane", r2)
            # Clearing the guard re-enables sends to %0.
            tools.set_self_pane(None)
            r3 = json.loads(tools.tmux_send_handler({"pane": "%0", "text": "echo ok", "submit": False}))
            _check(r3.get("status") == "ok",
                   "clearing self_pane re-enables send", r3)
            # Flush the buffer.
            subprocess.run(["tmux", "-L", SOCK, "send-keys", "-t", "%0", "Enter"], check=True)
            time.sleep(0.2)
        finally:
            tools.set_self_pane(None)

        # --- 13: tmux_wait ---
        # The substring-matching wait. Test both paths: a pattern that
        # appears (we send a command, wait for "ok" to land in the
        # scrollback) and a pattern that never appears (we wait for a
        # string we know isn't in the pane, expect timeout).
        print("\n13. tmux_wait (substring match + timeout)")
        # Use a fresh pane so we control its scrollback cleanly.
        subprocess.run(
            ["tmux", "-L", SOCK, "new-window", "-t", "smoke", "-n", "waitwin"],
            check=True,
        )
        wait_pane = next(
            p for p in json.loads(tools.tmux_list_handler({}))["panes"]
            if p["window_name"] == "waitwin"
        )
        # 13a: pattern appears. Send a command that prints a known
        # marker, then wait for the marker. The marker text is unique
        # to this test so we don't false-match on shell prompt noise.
        marker = "WAIT-MARKER-13a"
        subprocess.run(
            ["tmux", "-L", SOCK, "send-keys", "-t", wait_pane["pane_id"], "-l",
             f"echo {marker}"],
            check=True,
        )
        subprocess.run(
            ["tmux", "-L", SOCK, "send-keys", "-t", wait_pane["pane_id"], "Enter"],
            check=True,
        )
        r = json.loads(tools.tmux_wait_handler({
            "pane": wait_pane["pane_id"],
            "pattern": marker,
            "timeout": 5,
        }))
        _check(r.get("matched") is True, "13a: pattern matched", str({k: r.get(k) for k in ("matched", "elapsed_ms")}))
        _check(marker in r.get("text", ""), "13a: text contains matched marker",
               r.get("text", "")[-80:].replace("\n", " ⏎ "))
        _check(isinstance(r.get("elapsed_ms"), int) and r["elapsed_ms"] < 5000,
               "13a: elapsed_ms within timeout", str(r.get("elapsed_ms")))
        # 13b: pattern never appears. Use a string the shell definitely
        # won't print. The expected return is matched=false, full
        # timeout elapsed, text is a non-empty 5-line hint.
        t0 = time.monotonic()
        r = json.loads(tools.tmux_wait_handler({
            "pane": wait_pane["pane_id"],
            "pattern": "DEFINITELY-NOT-IN-PANE-13b",
            "timeout": 2,
        }))
        wall = time.monotonic() - t0
        _check(r.get("matched") is False, "13b: timeout returned matched=false",
               str(r.get("matched")))
        _check(r.get("elapsed_ms") == 2000, "13b: elapsed_ms == timeout * 1000",
               str(r.get("elapsed_ms")))
        _check(isinstance(r.get("text"), str) and len(r["text"]) > 0,
               "13b: text is non-empty status hint", r.get("text", "")[-80:].replace("\n", " ⏎ "))
        _check(wall >= 1.8 and wall < 4.0, "13b: wall-clock waited ~timeout",
               f"{wall:.2f}s")
        # 13c: validation. Empty pattern and missing pane must reject.
        bad_pane = json.loads(tools.tmux_wait_handler({"pattern": "x", "pane": ""}))
        _check("error" in bad_pane and "pane is required" in bad_pane["error"],
               "13c: missing pane rejected", bad_pane.get("error", ""))
        bad_pattern = json.loads(tools.tmux_wait_handler({"pane": "%0", "pattern": ""}))
        _check("error" in bad_pattern and "pattern" in bad_pattern["error"],
               "13c: empty pattern rejected", bad_pattern.get("error", ""))
        # 13d: timeout clamping. timeout=0 should clamp to 1; timeout=999
        # should clamp to 60. Verified by behavior, not by the schema.
        t0 = time.monotonic()
        r = json.loads(tools.tmux_wait_handler({
            "pane": wait_pane["pane_id"],
            "pattern": "DEFINITELY-NOT-IN-PANE-13d",
            "timeout": 0,
        }))
        wall = time.monotonic() - t0
        _check(r.get("elapsed_ms") == 1000 and 0.8 < wall < 3.0,
               "13d: timeout=0 clamps to 1s", f"elapsed_ms={r.get('elapsed_ms')} wall={wall:.2f}s")

        print("\n=== All 14 smoke tests passed ===")
        return 0
    finally:
        _kill_server()


if __name__ == "__main__":
    sys.exit(main())
