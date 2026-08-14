# hermes-tmux — Design Notes

The project-specific rationale for the hermes-tmux plugin. **This file records
*why*; it is not an instruction file.** Reusable Hermes-plugin and coding
guidance lives in `AGENTS.md`; the tool schemas are the documentation the model
reads at call time.

## What this is

A Hermes plugin exposing four tmux-related tools (`tmux_list`, `tmux_capture`,
`tmux_send`, `tmux_wait`). The plugin is a thin wrapper over the `tmux` CLI,
routed through the framework's `terminal` tool so all
approval/redaction/interrupt semantics apply. There is no bundled skill —
the tool schemas are the documentation.

## Architecture

```
register(ctx) ────► tmux_tools.set_ctx(ctx)    # stashed in tmux_tools.py module global
                  └► ctx.register_tool    # × 4, gated on _tmux_available

Tool handler ────► _ctx_or_none()         # reads stashed ctx
              ───► _run_tmux(args)       # → ctx.dispatch_tool("terminal", ...)
              ───► parses stdout → JSON
```

The plugin is a flat directory plugin (importable from the project root after
symlinking into the target profile's plugin directory). The framework's plugin
loader discovers it via the symlink at
`~/.hermes/profiles/<profile>/plugins/tmux`. The plugin never touches tmux
directly — it always goes through `ctx.dispatch_tool("terminal", ...)`, which
is what makes every tmux call safe.

## Design decisions

**No `tmux_spawn`, no `tmux_kill`.** Lifecycle stays in human hands. A teardown
tool would let the agent destroy its own observability mid-session. Spawn via
`terminal("tmux new-window -n <name> '<command>'")` and grab the new pane's
`%pane_id` from `tmux_list`. Pinning lifecycle as a tool would lock it into the
agent's reach.

**`tmux_wait` is a polling wait, not `tmux wait-for`.** `wait-for` requires the
*command itself* to participate in the sync (`cmd; tmux wait-for -S done`),
coupling every command the agent drives to the sync pattern — the reverse
shell, the exploit, the server log don't know about tmux. Polling
`tmux_capture` is the black-box version that works with anything producing text
in a pane. The tool polls at 100ms and returns the last 30 lines on both match
and timeout (a status hint; the expected follow-up is `tmux_capture` for full
scrollback).

**`tmux_wait` supports regex and async mode.** `regex: true` treats `pattern`
as a Python regex, validated before any polling (inline error, not a cryptic
background failure). `async: true` returns immediately with `status:
"watching"` — the handler spawns a background Python process and the framework
delivers the result as a follow-up message, so the agent can continue other
work during long waits.

**`tmux_send` returns a 5-line post-send capture on every success.** The
handler sleeps 100ms then runs the same `_capture_text` helper `tmux_wait`
uses, attaching the result as `post_send_capture`. For instant-return commands
(`echo`, `pwd`) the snapshot has the result and the agent can skip the explicit
`tmux_capture`; for slow commands the snapshot may be empty and the agent falls
through to `tmux_capture`. The 100ms tail and 5-line cap are baked in, not
parameters — same shape, same follow-up as `tmux_wait`.

**No `/pane` slash command — pane context sharing is a plain user message.**
A `/pane` slash command was tried and removed. It captured a pane and injected
it as a user message via `ctx.inject_message`, which only works in interactive
CLI mode (it needs `_cli_ref`, absent in the TUI/gateway), so in the TUI it
failed or degraded to returning the content to the pager. Two reasons it was
removed rather than fixed:

1. **It didn't match the slash-command contract.** A slash command's return
   value goes to the user's screen (CLI `_cprint`, TUI pager) — it is not a
   way to feed the agent's context. Routing `/pane` through `inject_message`
   was fighting the framework's design, and `pre_llm_call` staging (the
   session-agnostic alternative) would have made `/pane` a partial-trigger
   that only decorates the *next* turn — neither matches how other commands
   behave.
2. **A plain message is strictly cleaner.** The agent already has
   `tmux_capture` as a tool and reads its schema description. The user shares
   pane context by typing a normal message ("pane :1.0 has nmap output") and
   the agent captures the pane itself — no custom command, no injection
   primitive, no TUI/CLI divergence. This is the plugin's "schemas are the
   documentation, the model reads them at call time" philosophy doing the work
   instead of bespoke glue.

**Default-leave, no teardown by the agent.** Once a pane exists, the agent
drives it but does not kill it. The user is watching the session; tearing it
down mid-task is destructive without an explicit ask. If the agent needs the
window back, ask first.

**Self-pane guard on `tmux_send`.** The plugin captures `$TMUX_PANE` at
register time and refuses to send into the agent's own pane — covers the
mis-target case (stale `pane_id`, resolved-by-name target) where keystrokes
would land in the agent's own input. Costs one env-var read and one equality
check per call. No-op when the agent is outside tmux.

**Each interactive session is a new named window.** Windows are stable; pane
indexes shift when panes die. Pick a name that describes the session
(`ssh-prod`, `revshell-app1`, `mysql-orders`) so `tmux_list(target="<name>")`
finds it later.

**`check_fn` gates on the `tmux` binary only.** Tools are visible whenever
`tmux` is on PATH, including when the agent itself is outside any tmux session
(driving a session from outside is supported).

**Per-pane socket resolution (internal mechanism).** `_resolve_pane_id`
queries the target's tmux server with `display-message -p
'#{pane_id} ... #{socket_path}'` and returns the server name. `_run_tmux` adds
`-L <name>` only when the resolved server differs from the agent's own
(captured from `$TMUX` at register time). This is the fix for the original
`tmux_list_socket_mismatch` bug — when the agent is in server A and looks at a
pane in server A, the tool queries server A.

**Fast path for `%pane_id` inputs.** When the input already starts with `%` and
the agent is inside tmux (`_self_socket` is non-None), `_resolve_pane_id`
skips the `display-message` round-trip entirely — tmux accepts `%pane_id` as a
target directly. The trade-off: the `target` field in the response is the bare
`%pane_id` instead of `session:window.pane`. Still a valid target for any
subsequent call; the full form is available via `tmux_list`.

**Flag-injection guard on `tmux_send` keys mode.** The `keys` list is validated
before `send-keys`. Tmux key names are alphanumeric (`Enter`, `C-c`, `BSpace`,
`Up`, `Minus`) — no legitimate key name starts with `-`. A leading `-` would be
interpreted as a tmux flag (`-l` literal mode, `-N` repeat count, `-X`
copy-mode command). The guard rejects these with an error pointing to `Minus`
as the key name for the `-` character.

**All tmux flags baked in.** `capture-pane -p -J -q` (default) and
`capture-pane -p -J -a -q` (with `include_normal_scrollback: true`).
`send-keys -l` + separate `Enter` key for sends. The schema descriptions are
where the agent reads this; the parameters are not exposed.

**Both target formats accepted.** `%pane_id` and `session:window.pane` both
work. Internally normalized to `%pane_id` via `tmux display-message`. Response
always echoes both so the model can chain calls.

**ANSI always stripped.** The model almost never wants raw escape sequences. If
it ever does, `terminal` with a raw `tmux capture-pane -e ...` is one call
away.

**`tmux_capture` default = alternate screen.** Confirmed empirically against
tmux 3.5a: `capture-pane -p` (no `-a`) returns the TUI surface / visible pane
contents, and `-a` returns the normal scrollback. The flag name is the opposite
of what you might guess from the manpage. The test
`test_capture_alt_screen_vs_normal_scrollback` locks this in — do not flip it
without updating both the schema and the test.

**No skill is shipped on purpose.** A traditional skill would carry the same
content as the tool schema descriptions, plus reverse-shell / SSH / exploit
recipes. The design keeps that knowledge baked into the schemas and these
rules; the model reads the schemas at call time.

## Gotchas for the next agent

- **The `ctx` is captured once at `register()` time** and stashed in a module
  global in `tmux_tools.py`. If you add a new handler, call `_ctx_or_none()`
  (or the helpers) — `ctx` is not threaded through `**kwargs`.
- **Don't add `pre_tool_call` or `post_tool_call` hooks** that auto-capture
  every `terminal()` call. That's the "tmux backend" we explicitly decided
  against. The model should opt in by calling `tmux_capture`.
- **Don't add a `tmux_kill` tool** without checking with the user. Lifecycle in
  human hands is the point.
- **The plugin does NOT install tmux.** It assumes tmux is on PATH. Don't try
  to lazy-install; the system might not be using tmux at all.
- **Long-lived sessions hold stale tool definitions.** After editing
  `tmux_tools.py` or `schemas.py`, the user has to restart the TUI to reload
  the plugin. The smoke test exercises the on-disk code; live tool calls
  reflect the registered copy at session start.
- **`__init__.py` uses relative imports with an absolute fallback — keep it
  that way.** The framework loader imports `__init__.py` as a namespaced
  package (`hermes_plugins.tmux`) where relative imports resolve, so the plugin
  never depends on cwd or `sys.path`. Don't "simplify" this back to bare
  `import schemas`: an absolute import only works when the hermes process
  happens to start inside the plugin dir, which silently breaks other profiles
  launched from elsewhere. This exact bug shipped (the ares profile failed with
  `No module named 'schemas'` while rbw loaded). The absolute fallback exists
  only because pytest imports the root `__init__.py` as a bare module (the dir
  name `hermes-tmux` has a hyphen, invalid as a package name). Both contexts
  are load-bearing.
- **All four tools share one toolset (`tmux`).** They used to each register
  under their own toolset name. That made "enable the tmux plugin" a four-entry
  config change in `platform_toolsets.cli` instead of one. If you ever change
  the toolset back, update every profile's `platform_toolsets` config to match.

## Test plan

The test suite is a pytest run that exercises every public handler against a
real tmux server. `pyproject.toml` adds `.` to pytest's `pythonpath` so the
test run imports the package without a prior `pip install -e .`.

```bash
# Run the suite from the project root using the system pytest
# (apt: python3-pytest) — no venv or pip install required.
pytest tests/
```

The test layout, one file per tool:

- `tests/test_tmux_list.py` — basic list, `include_dead` + `target` filter,
  no-match target.
- `tests/test_tmux_capture.py` — default capture, bare session target,
  nonexistent target, ANSI stripping, `include_normal_scrollback`, alt-screen
  vs normal-scrollback with vim.
- `tests/test_tmux_send.py` — text mode, keys mode, `submit: false`,
  mutual-exclusion validation, self-pane guard, post-send capture.
- `tests/test_tmux_wait.py` — pattern match, timeout, validation,
  `timeout: 0` clamping.

`tests/conftest.py` provides the per-module tmux-server fixture
(`scope="module"`, so each test file gets its own server on a dedicated socket
`hermes-tmux-test-<module>`) and a `FakeCtx` that bypasses the framework's
terminal pipeline. The plugin's design rule — no `tmux_kill` tool — is honored
by tests: the dead-pane case uses an `exit` shell command under
`remain-on-exit on`, not `tmux kill-pane`.

For manual checks beyond the pytest suite:

1. `python3 -m py_compile *.py tests/*.py` — syntax check.
2. Run `hermes` and confirm `tmux_list` / `tmux_capture` / `tmux_send` /
   `tmux_wait` appear in the tool list whenever the `tmux` binary is on PATH
   (regardless of whether the agent is inside a tmux session).
3. Call each tool and verify the JSON response shape matches `schemas.py`.
4. The plugin is designed for a single local tmux server. If you genuinely need
   to drive a separate server, the internal per-pane resolution handles the
   routing automatically as long as `$TMUX` points at the right server.
   Multi-server driving across `tmux -L` boundaries is not a supported
   workflow.
