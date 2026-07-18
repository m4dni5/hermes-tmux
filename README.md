# hermes-tmux

Tmux pane observability for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — four tools and a slash command that let the agent see what's running in tmux, send text/keys into panes, wait for output to appear, and share pane context between the user and the agent. Tmux's tricky flag combinations and failure modes are baked into the tool defaults so the model never has to remember them.

## Tools

| Tool | What it does |
|---|---|
| `tmux_list` | List panes with stable `%pane_id`, session/window, current command, working dir, dead/alive. |
| `tmux_capture` | Read a pane's contents as text (ANSI stripped). Defaults to the TUI surface — what's on screen right now. Pass `include_normal_scrollback: true` to read the history that's scrolled out of view. |
| `tmux_send` | Type text (typing mode) or send a key-name sequence (keystroke mode). Defaults: literal text, press Enter after. Pass `keys: ["C-c", ...]` for keystrokes; include `"Enter"` in the list to submit. Returns a 5-line `post_send_capture` snapshot. |
| `tmux_wait` | Block until a substring (or regex, with `regex: true`) appears in the pane, or time out. Returns the last ~30 lines on both paths. Pass `async: true` to return immediately — the result arrives as a follow-up message. |

## Slash command

| Command | What it does |
|---|---|
| `/pane [target] [hint]` | Capture a pane's contents and inject them as a user message so the agent incorporates that context before responding. Any tmux target format works (`/pane 2.0`, `/pane .1`, `/pane nc`, `/pane %42`). The optional hint tells the agent what to look at (`/pane 2.0 nmap scan output`). No argument defaults to the most recent pane the agent interacted with. |

## Why this over terminal commands?

The agent *could* write raw `tmux capture-pane -t %12` and `tmux send-keys`. The plugin isn't saving keystrokes — it's removing failure modes the model hits with raw tmux:

- **Flag gotchas baked in.** `capture-pane` needs `-J` (joined lines), `-q` (suppress spurious alternate-screen errors), and the `-a` flag has semantics opposite of the manpage wording. The model gets these wrong. The plugin gets them right by default.
- **Injection guards.** The agent's own pane is off-limits to `tmux_send` — a stale `%pane_id` can't inject keystrokes into the agent's input. Key names starting with `-` (use `Minus`, not `-`) are caught before tmux interprets them as flags.
- **ANSI stripped.** The model almost never wants raw escape sequences in its context window. The plugin strips them automatically.
- **Polling wait and async.** No raw-tmux equivalent for `tmux_wait` — the model would need 3+ chained terminal calls and a hand-rolled polling loop. The async mode lets the agent continue other work during long waits.
- **`/pane` for shared context.** When the user does something in another pane, the agent has no awareness of it. `/pane` gives the user a one-keystroke way to share pane context — no copy-paste, no manual capture.

The plugin doesn't replace the terminal. The agent can still run raw tmux when it needs something the tools don't cover (spawning windows, resizing, anything lifecycle-related). The tools handle the common patterns where getting tmux's flags right matters.

## Install

```bash
hermes plugins install git@github.com:m4dni5/hermes-tmux.git
hermes plugins enable hermes-tmux
```

The tools appear whenever the `tmux` binary is on PATH — the agent doesn't have to be inside a tmux session to drive one.

## Files

```
hermes-tmux/
├── pyproject.toml            # pytest config
├── plugin.yaml               # name, version, provides_tools
├── __init__.py               # register(ctx) — wires the 4 tools + /pane command
├── schemas.py                # 4 tool schemas (what the model reads)
├── tmux_tools.py             # 4 handlers + /pane handler (what runs)
├── README.md
├── AGENTS.md
├── LICENSE
└── tests/                    # pytest suite (one file per tool + /pane)
    ├── conftest.py
    ├── test_tmux_list.py
    ├── test_tmux_capture.py
    ├── test_tmux_send.py
    ├── test_tmux_wait.py
    └── test_pane_command.py
```

## License

MIT