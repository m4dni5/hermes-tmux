# Hermes Plugin Development — Agent Notes (Master Playbook)

The distilled, project-agnostic playbook for building and maintaining Hermes
Agent plugins. It is a synthesis of the lessons learned across three real
plugins — `hermes-caido`, `hermes-recall`, and `hermes-tmux` — with their
individual project specifics removed. **This file holds no project-specific
content**; the rationale for *this* project lives in `DESIGN.md` at the repo
root.

## The plugin surface

A Hermes plugin hooks into the agent through the registration capabilities on
the `ctx` handed to `register(ctx)`:

| Capability | API | What it adds |
|---|---|---|
| Tools | `ctx.register_tool(name, toolset, schema, handler, check_fn, emoji)` | Model-callable tools |
| Hooks | `ctx.register_hook("post_tool_call", cb)` | Lifecycle events (pre/post LLM, session start/end, tool filter) |
| Slash commands | `ctx.register_command(name, handler, description, args_hint)` | `/name` in CLI and gateway sessions |
| CLI commands | `ctx.register_cli_command(name, help, setup_fn, handler_fn)` | `hermes <name> <subcommand>` from the terminal |
| Auxiliary tasks | `ctx.register_auxiliary_task(key, defaults)` | Configurable sub-model task (`auxiliary.<key>` in config.yaml) |

There is no subprocess substrate and no per-call execution model. Plugins do
*not* shell out directly — they call the framework's existing tools via
`ctx.dispatch_tool(name, args)`. That is the whole safety model: every
dispatched call inherits the same approval, redaction, and interrupt pipelines
as a direct tool call.

**Slash commands** (`ctx.register_command`) take a `handler: Callable[[str],
str | None]` — sync or async (the gateway awaits async handlers). The handler
receives the raw argument string after the command name. To invoke a tool from
a slash handler, use `ctx.dispatch_tool(...)` (parent-agent context wired up
automatically); don't reach into framework internals. Conflicts with built-in
commands are silently rejected with a warning — built-ins always win.

**Overriding a built-in tool** (`ctx.register_tool(..., override=True)`) is a
privileged capability: it needs the `tools.override` capability declared (and
consented) plus `allow_tool_override: true` in config for non-bundled plugins.
Prefer a non-conflicting tool name unless replacement is the explicit goal.

**Group a plugin's tools under one toolset name.** Enabling/disabling a plugin
is a per-toolset config change (`platform_toolsets.*`); registering each tool
under its own toolset turns "enable the plugin" into an N-entry change that can
drift. One `toolset` per plugin keeps enablement atomic. (This is a learned
lesson — a plugin originally registered each tool under its own toolset and was
refactored to a single one for exactly this reason.)

## Plugin anatomy (flat directory plugin)

```
plugin.yaml      # name, version, description, provides_tools, requires_env
__init__.py      # register(ctx) — wires tools + commands
schemas.py       # tool JSON schemas (what the model reads)
tools.py         # handlers (what runs)  — the docs' canonical name
pyproject.toml   # pytest config (pythonpath = ["."]) so tests import w/o pip install
tests/
```

- The loader imports `__init__.py` as a namespaced package
  (`hermes_plugins.<name>`), so **relative imports** (`from . import schemas`)
  resolve and the plugin never depends on cwd or `sys.path`.
- pytest, by contrast, may import the root `__init__.py` as a bare module
  (especially when the repo dir has a hyphen, which is invalid as a package
  name, so it can't be `tests`' parent). That context has no parent package.
  The standard fix is a `try: from . import schemas / except ImportError:
  import schemas` in `__init__.py`. Both contexts are load-bearing — keep the
  fallback.
- `pyproject.toml` should add `.` to pytest's `pythonpath` so tests import the
  package without a prior `pip install -e .`.

## The ctx capture pattern

Tool handlers are called by the framework with `(args, **kwargs)` — **`ctx` is
not threaded through**. It is only available inside `register(ctx)`. The
standard pattern is to stash it in a module global at registration time and
read it in handlers:

```python
_ctx: Optional[PluginContext] = None
def set_ctx(ctx): global _ctx; _ctx = ctx
def _ctx_or_none():
    global _ctx
    return _ctx if _ctx else None
```

Any new handler must go through `_ctx_or_none()` (or the module helpers) —
never assume `ctx` is in `**kwargs`.

**`session_id` *is* threaded through `kwargs`.** The framework passes the
current session ID as `kwargs["session_id"]` when dispatching tool calls. Use
it when a plugin needs to exclude the current conversation (e.g. a recall
search) or otherwise key off which session is active.

## Handler contract

Every tool handler must follow three rules (the docs' "common mistakes"):

1. **Return a JSON string — ALWAYS, even on error.** Never a bare dict.
2. **Accept `**kwargs`.** The framework may pass extra context (task_id,
   session_id, parent_agent); a handler without `**kwargs` breaks when it does.
3. **Catch exceptions and return error JSON.** A propagating exception fails
   the tool call; return `json.dumps({"error": str(e)})` instead.

Hook callbacks should also accept `**kwargs` — Hermes inspects callback
signatures, so a callback with `**kwargs` receives the complete additive
payload across versions. If a callback crashes, it's logged and skipped;
other hooks and the agent continue.

## Injecting context into the conversation

There are two mechanisms, with different availability:

- **`ctx.inject_message(content, role="user") -> bool` — CLI-only.** It needs
  `ctx._cli_ref`, which is populated only in an interactive CLI session. It is
  `None` in the gateway, in non-interactive `hermes chat -q`, and in
  kanban-spawned worker sessions — there it returns `False`. Design around
  this: check the return value and degrade gracefully (see the Testing section
  for the FakeCtx trap).
- **`pre_llm_call` context injection — session-agnostic.** A `pre_llm_call`
  hook callback may return a dict with a `"context"` key (or a plain string);
  Hermes appends it to the current turn's *user message* (never the system
  prompt, preserving the prompt-cache prefix) at API call time, ephemeral and
  unpersisted. This is the stable mechanism for memory/RAG/guardrail plugins
  that need to feed the model context every turn, and it works in every
  process.

For the stable session-agnostic surface, use `ctx.profile_name` (resolves the
active profile from `HERMES_HOME`, no `_cli_ref`) and `ctx.dispatch_tool(...)`
rather than reaching into `ctx._cli_ref.agent` or similar private state.

## Import strategy

- **Prefer package-relative imports (`from .lib import ...`, `from . import
  schemas`) over `sys.path` mutation.** The loader imports the plugin as a
  namespaced package where relative imports resolve, so no path trickery is
  needed in the framework path.
- **Do NOT `sys.path.insert(0, ...)` at import time to reach vendored code.**
  This mutates the shared process's `sys.path` (a persistent process-wide side
  effect) and lets *generic top-level names* — `graphql`, `output`, `http`,
  etc. — silently collide with stdlib/third-party modules loaded later. This
  exact antipattern shipped in one plugin and now carries a no-sys.path-
  mutation guard in its test suite.
- **Two legitimate exceptions** (document them at the site):
  1. A standalone entrypoint (`python3 auth_helper.py`) that inserts the
     plugin *root* — not a nested `lib/` — and imports the qualified path
     (`lib.graphql.client`), never the generic leaf name.
  2. Skill `execute_code` snippets that insert `PLUGIN_DIR` and import the
     qualified package (`from lib import http_requests`).

## Routing through the framework

Route every external command through `ctx.dispatch_tool(name, args, *,
parent_agent=None) -> str`, not a direct `subprocess` call. The return envelope
to parse is `{"output", "exit_code", "error"}`. This keeps approval gating,
redaction, and interrupts on every invocation. `parent_agent` resolves from the
active CLI agent (or degrades gracefully in gateway mode); pass it explicitly
only when you must override.

Where a plugin drives a *sub-model* rather than a shell, use native function
calling through `call_llm(tools=[...])` from `agent.auxiliary_client.py` —
**never regex-parse tool calls out of plain text.** Structured `tool_calls`
from the API are the contract: the loop dispatches them, feeds results back as
`tool` role messages, and termination is "no `tool_calls` in the response."
Regex/fence parsers break on nested braces, assume key ordering, and turn every
model formatting quirk into a silent stall.

## Plugin config and state

User-visible behavior goes in plugin-relative config; runtime bookkeeping goes
in `ctx.state`:

- **`ctx.get_config(key, default=...)` / `ctx.set_config(key, value)`** —
  resolves under `plugins.entries.<plugin-id>.settings` in `config.yaml`.
  Global, cross-plugin, and traversal paths are rejected.
- **`ctx.state`** — plugin-owned runtime data (cursors, caches, dedup),
  profile-scoped, atomically replaced, ~10 MiB per plugin, stored under
  `<HERMES_HOME>/plugin-data/`.

Neither API exposes another plugin's namespace. Settings live in `config.yaml`;
state lives under the profile home — keep the two concerns separate.

## Two-layer design (async core + sync wrappers)

Plugins doing network I/O (GraphQL, HTTP, WebSocket) can benefit from the
two-layer pattern:

1. `lib/graphql/<domain>.py` — async functions using raw protocol + aiohttp.
2. `lib/<domain>.py` — sync wrappers via `sync_run()` for skill consumption.

Tool handlers call the async layer directly; skill `execute_code` blocks call
the sync wrappers. The sync wrapper must `close()` the session after each call
— `asyncio.run()` creates a fresh event loop, so a singleton session is always
stale.

**Auth isolation.** If the plugin authenticates against a server (OAuth2,
device flow), the agent's async context can interfere with connection
handshakes (inherited SSL state, nested event loops). Run the auth flow in a
standalone subprocess (`auth_helper.py`), never in the agent's event loop.

## Gating tools on availability

Use `check_fn` to hide tools when their dependency isn't present, rather than
erroring at runtime. Gate on the *real condition*:

- `shutil.which(binary) is not None` for a CLI dependency
- DB/file existence for a state dependency (respect the active profile via
  `get_hermes_home()` / `HERMES_HOME`)

Don't gate on ambient session state (e.g. "inside tmux") — the agent should be
able to drive a tool from outside the context it normally runs in. And
`manifest.provides_tools` must agree with `check_fn`: hide tools from the list
when they can't work, don't leave a stub that errors at call time.

## Profile-aware paths

Resolve Hermes's data dir via `HERMES_HOME` (if set) falling back to
`~/.hermes/` — never hardcode a path. This is what makes the plugin work under
any profile (`rbw`, `ares`, `default`). Use the helper from
`hermes_constants.py` rather than re-deriving it.

## Progressive tool disclosure: register, don't hide in skills

Hermes uses progressive tool disclosure — non-core tools sit behind
`tool_search` / `tool_describe` / `tool_call` and schemas load on demand. The
old context-cost argument for burying operations inside a skill is gone.
**Register every operation an agent performs as a tool** and put the decisions
in the schema descriptions. A skill is warranted only for a *judgment layer*
that doesn't fit a schema — shared-workspace conventions, when to use which
tool, pitfalls, recipes — not as a hiding place for callable operations.

## Schemas: keep the framework sanitizer in mind

The framework sanitizes tool schemas. Known sharp edges:
- **A top-level `oneOf` / `anyOf` combinator can be stripped.** Flatten the
  schema so the sanitizer can't discard the intended shape. Add a dedicated
  test that runs `sanitize_tool_schemas` over every schema and asserts
  `properties`/`required` survive with no top-level combinators.
- Descriptions are the documentation the model reads at call time. Bake the
  tricky behavior (flag defaults, failure modes, follow-up expectations) into
  the schema text rather than a separate skill.

## Security principles for plugin tools

- **Lifecycle stays in human hands.** Don't ship a teardown tool that lets the
  agent destroy its own observability mid-session. If teardown is needed, the
  agent can do it via `terminal` — don't pin it as a tool.
- **Guard against injection.** Validate free-form input before it reaches the
  underlying CLI. A leading `-` on a value that should be a bare token can be
  interpreted as a flag (tmux key names, shell args). Reject it with a message
  that names the correct form.
- **Protect the agent's own input.** If the tool targets a pane/stream the
  agent itself drives, refuse to send into the agent's own one — a stale or
  resolved-by-name target could land keystrokes in the agent's own input.
- **No automatic capture hooks.** Don't add `pre_tool_call`/`post_tool_call`
  hooks that observe every terminal call unless the user explicitly asks. The
  model should opt in by calling the tool, not have observability forced on it.
- **Make destructive operations opt-in.** If a tool modifies state the user is
  watching, bias the default toward the non-destructive path and expose the
  destructive one explicitly.

## Testing plugins

- **Prefer a real pytest suite against the actual dependency** (a live server,
  a real tmux session on an isolated socket) over mocks of the CLI. Mocks
  verify the happy path; they silently pass when the *contract* with the
  framework changes.
- **`FakeCtx` must model the real framework's return *envelope* exactly, and
  must not unconditionally succeed where the real framework can fail.** If a
  method returns `False` in some real mode (e.g. `inject_message` in the
  TUI/gateway), add a test that exercises that failure path — otherwise the
  suite masks it. This is the single most common blind spot: a FakeCtx that
  always returns `True` passes while the real path is broken.
- **Test the framework contract, not just the happy path.** Include a loader
  test (registrations, no sys.path mutation, sync-wrapper imports) and a schema
  sanitizer test (no top-level combinators, `properties`/`required` intact).
  These are the two failure classes that have bitten real plugins.
- Stand up an isolated server per test module on a dedicated socket so tests
  never touch a real session, and tear it down in a fixture `finally`.
- If a handler runs a sub-model loop, unit-test the loop's *control flow* with
  a mocked `call_llm` (tool-then-answer, immediate answer, max-iterations
  synthesis, unknown-tool rejection, malformed args, multiple calls per turn),
  and have a separate integration harness for the real end-to-end path.
- **Validate loading before writing tests**: `hermes plugins doctor . --ci`
  runs the same discovery, manifest parse, namespaced import, `register()`,
  hook registry, and tool registry Hermes uses, and exits non-zero on error
  (reports invalid hook names, callbacks without `**kwargs`, and drift between
  declared and registered tools/hooks). For a plugin that isn't appearing at
  all, set `HERMES_PLUGINS_DEBUG=1` for verbose discovery logs, or tail
  `~/.hermes/logs/agent.log`.

## General coding best practices

- **Compose, don't construct.** Small functions that do one thing well,
  connected cleanly. Every handler should be understandable on its own.
- **Read before you write.** Understand the full context — the file, its
  callers, its tests, the commit that introduced it — before changing anything.
  Trace a symbol to its definition and usages rather than guessing its shape.
- **Make the smallest change that solves the problem.** Don't refactor what you
  don't need, don't add what isn't asked for, don't decorate.
- **Test before you assume.** If you're not certain how something behaves, write
  a test and find out. Assumptions are expensive; verification is cheap.
- **Think in systems.** A change ripples to callers, tests, deployments, and the
  next reader. Follow the thread before you pull it.
- **Respect the machine.** Understand what the code actually does at the level
  it runs. Profile before optimizing; measure before claiming.
- **Fix root causes, not symptoms.** When you find a bug, check sibling call
  paths for the same flaw and fix the class, not just the reported site.
- **Be honest about what you don't know.** Say "I don't know" rather than guess
  confidently. A wrong answer wastes more time than an honest gap.

## Distinction this file depends on

**AGENTS.md dictates behavior; DESIGN.md records the project's rationale.**
If a rule gates what the agent must or must not do, it belongs in this file (or
in the tool schemas). If it explains *why* a decision was made — a measured
behavior against a live system, a reverted alternative, a historical bug — it
belongs in `DESIGN.md`. Rationale in AGENTS.md makes every rationale edit an
edit to a protected instruction file; keeping the two separate is the point.
