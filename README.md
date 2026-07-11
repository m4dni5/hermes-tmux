# hermes-tmux

Tmux pane observability for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — three tools that let the agent see what's running in tmux and send text/keys into panes, with tmux's tricky flag combinations baked in as defaults so the model never has to remember them.

## Tools

| Tool | What it does |
|---|---|
| `tmux_list` | List panes with stable `%pane_id`, session/window, current command, working dir, dead/alive. |
| `tmux_capture` | Read a pane's contents as text (ANSI stripped, long lines unwrapped). Defaults to the TUI surface — what's on screen right now. Pass `include_normal_scrollback: true` to read the history that's scrolled out of view. |
| `tmux_send` | Type text or send a key name into a pane. Defaults: literal mode, press Enter. Set `literal: false` and `press_enter: false` to send `C-c`, arrow keys, etc. Use this instead of `terminal("tmux send-keys ...")` — the flag choices are handled here. |

## Install

Symlink into the target profile's plugin directory:

```bash
ln -s ~/src/hermes-tmux ~/.hermes/profiles/<profile>/plugins/tmux
```

Then enable in `~/.hermes/profiles/<profile>/config.yaml`:

```yaml
plugins:
  enabled:
    - tmux
```

The tools' `check_fn` hides them when `$TMUX` is not set or the `tmux` binary is missing — the model won't see `tmux_list` etc. in non-tmux contexts, and will fall through to `terminal` naturally.

## Why a plugin (not a built-in tool)

* Niche capability — useful for security research and long-running-process workflows, not general users.
* The tool schemas replace what a traditional skill would have carried — no bundled skill, no recipes, no extra context for the model to load. Everything the agent needs is in the schema descriptions.

## Files

```
hermes-tmux/
├── plugin.yaml          # name, version, provides_tools
├── __init__.py          # register(ctx) — wires the 3 tools
├── schemas.py           # 3 tool schemas (what the model reads)
├── tools.py             # 3 handlers (what runs)
├── smoke_test.py        # end-to-end test against a real tmux server
├── README.md
└── AGENTS.md
```

## License

MIT
