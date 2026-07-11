# hermes-tmux — Agent Notes

## What this is

A Hermes plugin exposing three tmux-related tools (`tmux_list`, `tmux_capture`, `tmux_send`). The plugin is a thin wrapper over the `tmux` CLI, routed through the framework's `terminal` tool so all approval/redaction/interrupt semantics apply. There is no bundled skill — the tool schemas are the documentation.

## Architecture

```
register(ctx) ────► tools.set_ctx(ctx)    # stashed in tools.py module global
                  └► ctx.register_tool    # × 3, gated on _tmux_available

Tool handler ────► _ctx_or_none()         # reads stashed ctx
              ───► _run_tmux(args)       # → ctx.dispatch_tool("terminal", ...)
              ───► parses stdout → JSON
```

The plugin never touches tmux directly — it always goes through `ctx.dispatch_tool("terminal", ...)`. This is what makes the plugin safe: every tmux call gets the same approval gating and redaction as a normal `terminal()` call.

## Design decisions

**No `tmux_spawn`, no `tmux_kill`.** Lifecycle stays in human hands. A teardown tool would let the agent destroy its own observability mid-session. Spawn via `terminal("tmux new-window -n <name> '<command>'")` and grab the new pane's `%pane_id` from `tmux_list` — the agent can handle that from memory, and pinning it as a tool would lock the lifecycle into the agent's reach.

**`tmux_wait` is a polling substring wait, not `tmux wait-for`.** The wait-for command requires the *command itself* to participate in the sync (`cmd; tmux wait-for -S done`), which couples every command the agent drives to the sync pattern — the reverse shell, the exploit, the server log don't know about tmux. Polling `tmux_capture` is the black-box version that works with anything producing text in a pane. The tool polls at 100ms and returns a 5-line status hint on both match and timeout (so the agent can decide whether to call `tmux_capture` for full output, send more input, or give up). 5 lines is a *status hint*, not a read — the expected follow-up is `tmux_capture(pane, lines=N)` for the full scrollback.

**Default-leave, no teardown by the agent.** Once a pane exists, the agent drives it but does not kill it. The user is watching the session; tearing it down mid-task is destructive without an explicit ask. If the agent needs the window back, ask first.

**Self-pane guard on `tmux_send`.** The plugin captures `$TMUX_PANE` at register time and refuses to send into the agent's own pane — covers the mis-target case (stale `pane_id`, resolved-by-name target, etc.) where keystrokes would land in the agent's own input. Returns `{"error": "refusing to send to own pane (%N); use a different target"}`. Costs one env-var read and one equality check per call. No-op when the agent is outside tmux (the agent has no pane of its own; the tools are still available).

**Each interactive session is a new named window.** Windows are stable; pane indexes shift when panes die. Pick a name that describes the session (`ssh-prod`, `revshell-app1`, `mysql-orders`) so `tmux_list(target="<name>")` finds it later.

**`check_fn` gates on the `tmux` binary only.** The agent doesn't have to be inside a tmux session to drive one — driving a session from outside (e.g. a subagent spawned in a non-tmux context) is the realistic case. Tools are visible whenever `tmux` is on PATH.

**Per-pane socket resolution (internal mechanism).** `_resolve_pane_id` queries the target's tmux server with `display-message -p '#{pane_id} ... #{socket_path}'` and returns the server name. `_run_tmux` adds `-L <name>` only when the resolved server differs from the agent's own (captured from `$TMUX` at register time). The agent never sees the socket; the resolution is automatic and only matters when the agent's `$TMUX` points at a server that doesn't match the default. This is the fix for the original `tmux_list_socket_mismatch` bug — when the agent is in server A and looks at a pane in server A, the tool queries server A.

**All tmux flags baked in.** `capture-pane -p -J -q` (default) and `capture-pane -p -J -a -q` (with `include_normal_scrollback: true`). `send-keys -l` + separate `Enter` key for sends. The schema descriptions are where the agent reads this; the parameters are not exposed.

**Both target formats accepted.** `%pane_id` and `session:window.pane` both work. Internally normalized to `%pane_id` via `tmux display-message`. Response always echoes both so the model can chain calls.

**ANSI always stripped.** The model almost never wants raw escape sequences. If it ever does, `terminal` with a raw `tmux capture-pane -e ...` is one call away.

**`tmux_capture` default = alternate screen.** Confirmed empirically against tmux 3.5a: `capture-pane -p` (no `-a`) returns the TUI surface / visible pane contents, and `-a` returns the normal scrollback. The flag name is the opposite of what you might guess from the manpage's wording. The smoke test (test 11) locks this in — do not flip it without updating both the schema and the test.

**No skill is shipped on purpose.** A traditional skill would carry the same content as the tool schema descriptions, plus reverse-shell / SSH / exploit recipes. The plugin's design is to keep that knowledge baked into the schemas and the design rules above; the model reads the schemas at call time. If a future use case needs a skill (a domain the schemas can't cover), add one — but check first whether the schemas can be extended instead.

## Gotchas for the next agent

- **The `ctx` is captured once at `register()` time** and stashed in a module global in `tools.py`. If you add a new handler, call `_ctx_or_none()` (or the helpers in `tools.py`) — `ctx` is not threaded through `**kwargs`.
- **Don't add `pre_tool_call` or `post_tool_call` hooks** that auto-capture every `terminal()` call. That's the "tmux backend" we explicitly decided against. The model should opt in by calling `tmux_capture`.
- **Don't add a `tmux_kill` tool** without checking with the user. Lifecycle in human hands is the point.
- **The plugin does NOT install tmux.** It assumes tmux is on PATH. Don't try to lazy-install; the system might not be using tmux at all.
- **Long-lived sessions hold stale tool definitions.** After editing `tools.py` or `schemas.py`, the user has to restart the TUI to reload the plugin. The smoke test exercises the on-disk code; live tool calls reflect the registered copy at session start.

## Test plan

`smoke_test.py` is the canonical verification. It spins up an isolated tmux server on a custom socket, exercises every public handler against it, and tears down. Run it from the plugin root:

```bash
python3 smoke_test.py
```

It covers: list/capture/send round-trips, target resolution, error envelopes, ANSI stripping, the text/keys split for `tmux_send`, the dead-pane filter, the self-pane guard, the alternate-screen vs normal-scrollback flag semantics with a real vim pane, and `tmux_wait` substring-match and timeout cases. Fourteen cases. Exits non-zero on any failure.

For manual checks beyond the smoke test:

1. `python3 -m py_compile tools.py schemas.py __init__.py smoke_test.py` — syntax check.
2. Run `hermes` and confirm `tmux_list` / `tmux_capture` / `tmux_send` appear in the tool list whenever the `tmux` binary is on PATH (regardless of whether the agent is inside a tmux session).
3. Call each tool and verify the JSON response shape matches `schemas.py`.
4. The plugin is designed for a single local tmux server. If you genuinely need to drive a separate server, the internal per-pane resolution handles the routing automatically as long as `$TMUX` points at the right server. Multi-server driving across `tmux -L` boundaries is not a supported workflow.
