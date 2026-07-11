# hermes-tmux — Agent Notes

## What this is

A Hermes plugin exposing four tmux-related tools (`tmux_list`, `tmux_capture`, `tmux_send`, `tmux_wait`). The plugin is a thin wrapper over the `tmux` CLI, routed through the framework's `terminal` tool so all approval/redaction/interrupt semantics apply. There is no bundled skill — the tool schemas are the documentation.

## Architecture

```
register(ctx) ────► tools.set_ctx(ctx)    # stashed in tools.py module global
                  └► ctx.register_tool    # × 4, gated on _tmux_available

Tool handler ────► _ctx_or_none()         # reads stashed ctx
              ───► _run_tmux(args)       # → ctx.dispatch_tool("terminal", ...)
              ───► parses stdout → JSON
```

The plugin is a proper Python package under `src/hermes_tmux/` (importable
as `hermes_tmux` after `pip install -e .`). The framework's plugin loader
discovers it via the symlink at `~/.hermes/profiles/<profile>/plugins/tmux`
— that path is unchanged. The pip install makes the package importable
in tests and from anywhere on the system Python, not just from the
framework's runtime.

The plugin never touches tmux directly — it always goes through `ctx.dispatch_tool("terminal", ...)`. This is what makes the plugin safe: every tmux call gets the same approval gating and redaction as a normal `terminal()` call.

## Design decisions

**No `tmux_spawn`, no `tmux_kill`.** Lifecycle stays in human hands. A teardown tool would let the agent destroy its own observability mid-session. Spawn via `terminal("tmux new-window -n <name> '<command>'")` and grab the new pane's `%pane_id` from `tmux_list` — the agent can handle that from memory, and pinning it as a tool would lock the lifecycle into the agent's reach.

**`tmux_wait` is a polling substring wait, not `tmux wait-for`.** The wait-for command requires the *command itself* to participate in the sync (`cmd; tmux wait-for -S done`), which couples every command the agent drives to the sync pattern — the reverse shell, the exploit, the server log don't know about tmux. Polling `tmux_capture` is the black-box version that works with anything producing text in a pane. The tool polls at 100ms and returns a 5-line status hint on both match and timeout (so the agent can decide whether to call `tmux_capture` for full output, send more input, or give up). 5 lines is a *status hint*, not a read — the expected follow-up is `tmux_capture(pane, lines=N)` for the full scrollback.

**`tmux_send` returns a 5-line post-send capture on every success.** After the send completes, the handler sleeps 100ms and runs the same `_capture_text` helper `tmux_wait` uses, attaching the result to the response as `post_send_capture`. For instant-return commands (`echo`, `pwd`, `ls`) the snapshot has the result and the agent can skip the explicit `tmux_capture` call. For slow commands (builds, server starts) the snapshot may be empty or partial and the agent falls through to `tmux_capture`. The 100ms tail and the 5-line cap are baked in (not parameters) for the same reason as `tmux_wait`'s hint: same shape, same follow-up. The field is always present on success (empty string if the capture itself failed — pane died mid-send); error envelopes don't include it.

**Default-leave, no teardown by the agent.** Once a pane exists, the agent drives it but does not kill it. The user is watching the session; tearing it down mid-task is destructive without an explicit ask. If the agent needs the window back, ask first.

**Self-pane guard on `tmux_send`.** The plugin captures `$TMUX_PANE` at register time and refuses to send into the agent's own pane — covers the mis-target case (stale `pane_id`, resolved-by-name target, etc.) where keystrokes would land in the agent's own input. Returns `{"error": "refusing to send to own pane (%N); use a different target"}`. Costs one env-var read and one equality check per call. No-op when the agent is outside tmux (the agent has no pane of its own; the tools are still available).

**Each interactive session is a new named window.** Windows are stable; pane indexes shift when panes die. Pick a name that describes the session (`ssh-prod`, `revshell-app1`, `mysql-orders`) so `tmux_list(target="<name>")` finds it later.

**`check_fn` gates on the `tmux` binary only.** Tools are visible whenever `tmux` is on PATH, including when the agent itself is outside any tmux session (driving a session from outside is supported).

**Per-pane socket resolution (internal mechanism).** `_resolve_pane_id` queries the target's tmux server with `display-message -p '#{pane_id} ... #{socket_path}'` and returns the server name. `_run_tmux` adds `-L <name>` only when the resolved server differs from the agent's own (captured from `$TMUX` at register time). The agent never sees the socket; the resolution is automatic and only matters when the agent's `$TMUX` points at a server that doesn't match the default. This is the fix for the original `tmux_list_socket_mismatch` bug — when the agent is in server A and looks at a pane in server A, the tool queries server A.

**All tmux flags baked in.** `capture-pane -p -J -q` (default) and `capture-pane -p -J -a -q` (with `include_normal_scrollback: true`). `send-keys -l` + separate `Enter` key for sends. The schema descriptions are where the agent reads this; the parameters are not exposed.

**Both target formats accepted.** `%pane_id` and `session:window.pane` both work. Internally normalized to `%pane_id` via `tmux display-message`. Response always echoes both so the model can chain calls.

**ANSI always stripped.** The model almost never wants raw escape sequences. If it ever does, `terminal` with a raw `tmux capture-pane -e ...` is one call away.

**`tmux_capture` default = alternate screen.** Confirmed empirically against tmux 3.5a: `capture-pane -p` (no `-a`) returns the TUI surface / visible pane contents, and `-a` returns the normal scrollback. The flag name is the opposite of what you might guess from the manpage's wording. The pytest test `test_capture_alt_screen_vs_normal_scrollback` locks this in — do not flip it without updating both the schema and the test.

**No skill is shipped on purpose.** A traditional skill would carry the same content as the tool schema descriptions, plus reverse-shell / SSH / exploit recipes. The plugin's design is to keep that knowledge baked into the schemas and the design rules above; the model reads the schemas at call time. If a future use case needs a skill (a domain the schemas can't cover), add one — but check first whether the schemas can be extended instead.

## Gotchas for the next agent

- **The `ctx` is captured once at `register()` time** and stashed in a module global in `tools.py`. If you add a new handler, call `_ctx_or_none()` (or the helpers in `tools.py`) — `ctx` is not threaded through `**kwargs`.
- **Don't add `pre_tool_call` or `post_tool_call` hooks** that auto-capture every `terminal()` call. That's the "tmux backend" we explicitly decided against. The model should opt in by calling `tmux_capture`.
- **Don't add a `tmux_kill` tool** without checking with the user. Lifecycle in human hands is the point.
- **The plugin does NOT install tmux.** It assumes tmux is on PATH. Don't try to lazy-install; the system might not be using tmux at all.
- **Long-lived sessions hold stale tool definitions.** After editing `tools.py` or `schemas.py`, the user has to restart the TUI to reload the plugin. The smoke test exercises the on-disk code; live tool calls reflect the registered copy at session start.

## Test plan

The test suite is a pytest run that exercises every public handler
against a real tmux server. The plugin is a Python package under
`src/hermes_tmux/` (src-layout); `pyproject.toml` adds `src/` to
pytest's `pythonpath` so the test run can import the package
without a prior `pip install -e .`.

```bash
# Run the suite from the project root using the system pytest
# (apt: python3-pytest) — no venv or pip install required.
pytest tests/
```

The test layout, one file per tool:

- `tests/test_tmux_list.py` — 3 tests: basic list, `include_dead` + `target` filter, no-match target.
- `tests/test_tmux_capture.py` — 6 tests: default capture, bare session target, nonexistent target, ANSI stripping, `include_normal_scrollback` parameter, alt-screen vs normal-scrollback with vim.
- `tests/test_tmux_send.py` — 6 tests: text mode, keys mode, `submit: false`, `oneOf` validation, self-pane guard, post-send capture.
- `tests/test_tmux_wait.py` — 4 tests: pattern match, timeout, validation, `timeout: 0` clamping.

`tests/conftest.py` provides the per-module tmux-server fixture
(`scope="module"`, so each test file gets its own server on a
dedicated socket `hermes-tmux-test-<module>`) and a `FakeCtx` that
bypasses the framework's terminal pipeline. Nineteen tests total.
The plugin's design rule — no `tmux_kill` tool — is honored by tests:
the dead-pane case uses an `exit` shell command under `remain-on-exit
on`, not `tmux kill-pane`.

If you do want a venv (e.g. for `pip install -e .` to make the
package importable from anywhere on the system Python), the project
still supports that workflow — `[project.optional-dependencies]`
has a `dev` extra that pulls in pytest. But the canonical test
command is just `pytest tests/`.

For manual checks beyond the pytest suite:

1. `python3 -m py_compile src/hermes_tmux/*.py tests/*.py` — syntax check.
2. Run `hermes` and confirm `tmux_list` / `tmux_capture` / `tmux_send` / `tmux_wait` appear in the tool list whenever the `tmux` binary is on PATH (regardless of whether the agent is inside a tmux session).
3. Call each tool and verify the JSON response shape matches `schemas.py`.
4. The plugin is designed for a single local tmux server. If you genuinely need to drive a separate server, the internal per-pane resolution handles the routing automatically as long as `$TMUX` points at the right server. Multi-server driving across `tmux -L` boundaries is not a supported workflow.
