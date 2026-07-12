# hermes-tmux

Tmux pane observability for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — four tools that let the agent see what's running in tmux, send text/keys into panes, and wait for output to appear, with tmux's tricky flag combinations baked in as defaults so the model never has to remember them.

## Tools

| Tool | What it does |
|---|---|
| `tmux_list` | List panes with stable `%pane_id`, session/window, current command, working dir, dead/alive. |
| `tmux_capture` | Read a pane's contents as text (ANSI stripped). Defaults to the TUI surface — what's on screen right now. Pass `include_normal_scrollback: true` to read the history that's scrolled out of view. |
| `tmux_send` | Type text (typing mode) or send a key-name sequence (keystroke mode). Defaults: literal text, press Enter after. Pass `keys: ["C-c", ...]` for keystrokes; include `"Enter"` in the list to submit. Returns a 5-line `post_send_capture` snapshot. |
| `tmux_wait` | Block until a substring appears in the pane, or time out. Returns a 5-line status hint on both paths so the agent can decide whether to call `tmux_capture`, send more input, or give up. |

## Install

```bash
# Symlink into the target profile's plugin directory
ln -s ~/src/hermes-tmux ~/.hermes/profiles/<profile>/plugins/tmux
```

Then enable in `~/.hermes/profiles/<profile>/config.yaml`:

```yaml
plugins:
  enabled:
    - tmux
```

The tools' `check_fn` hides them when the `tmux` binary isn't on PATH. The agent doesn't have to be in a tmux session itself to drive one.

## Why a plugin (not a built-in tool)

* Niche capability — useful for security research and long-running-process workflows, not general users.
* The tool schemas replace what a traditional skill would have carried — no bundled skill, no recipes, no extra context for the model to load. Everything the agent needs is in the schema descriptions.

## Tests

```bash
pytest tests/
```

Nineteen tests across four files (`test_tmux_list.py`, `test_tmux_capture.py`, `test_tmux_send.py`, `test_tmux_wait.py`), all running against a real tmux server on a custom socket. Each test file gets its own server (`scope="module"`). See `AGENTS.md` for the full layout and design rationale.

## Files

```
hermes-tmux/
├── pyproject.toml         # pytest config
├── plugin.yaml            # name, version, provides_tools
├── __init__.py            # register(ctx) — wires the 4 tools
├── schemas.py             # 4 tool schemas (what the model reads)
├── tools.py               # 4 handlers (what runs)
├── README.md
├── AGENTS.md
├── LICENSE
└── tests/                 # pytest suite (one file per tool)
    ├── conftest.py
    ├── test_tmux_list.py
    ├── test_tmux_capture.py
    ├── test_tmux_send.py
    └── test_tmux_wait.py
```

## License

MIT
