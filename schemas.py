"""Tool schemas for the tmux plugin.

The descriptions here are what the model reads to decide how to call each
tool. They are intentionally short: one or two sentences on function,
one or two on each parameter. Defaults are stated in the parameter
descriptions, not in prose. Anything that doesn't change a call site
(flag trivia, manpage gotchas, chaining recipes) lives in tools.py
comments, not here. Per the plugin's design (see AGENTS.md), this is
where the documentation lives — the tool schemas replace what a
traditional skill would have carried.
"""

TMUX_LIST_SCHEMA = {
    "name": "tmux_list",
    "description": (
        "List tmux panes. Returns a stable `%pane_id` per pane, plus "
        "session/window info, current command, working directory, and "
        "dead/alive status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Substring filter on session name, window name, or "
                    "`session:window.pane`. Omit to list all panes."
                ),
            },
            "include_dead": {
                "type": "boolean",
                "description": "Include panes whose process has exited. Default false.",
                "default": False,
            },
        },
    },
}

TMUX_CAPTURE_SCHEMA = {
    "name": "tmux_capture",
    "description": (
        "Read the contents of a tmux pane as text — useful for shell "
        "prompts and password prompts — with ANSI escape sequences "
        "stripped."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pane": {
                "type": "string",
                "description": (
                    "Pane to capture. Accepts `%pane_id` (`%12`) or any "
                    "tmux target format (`session:window.pane`, window "
                    "name, etc.)."
                ),
            },
            "lines": {
                "type": "integer",
                "description": "Number of lines to return. Default 200, max 5000.",
                "minimum": 1,
                "maximum": 5000,
                "default": 200,
            },
            "include_normal_scrollback": {
                "type": "boolean",
                "description": (
                    "Read the normal scrollback (history that's scrolled "
                    "out of view) instead of the TUI surface. Default false."
                ),
                "default": False,
            },
        },
        "required": ["pane"],
    },
}

TMUX_SEND_SCHEMA = {
    "name": "tmux_send",
    "description": (
        "Send text or keystrokes to a tmux pane. Use this instead of "
        "calling `terminal('tmux send-keys ...')` directly — the flag "
        "choices are handled here. Returns a 5-line post-send snapshot "
        "of the pane; call `tmux_capture` for the full scrollback."
    ),
    "parameters": {
        "oneOf": [
            {
                "type": "object",
                "description": "Typing mode: send `text` as literal characters.",
                "properties": {
                    "pane": {
                        "type": "string",
                        "description": (
                            "Pane to send to. Accepts `%pane_id` or any "
                            "tmux target format."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "Text to type character-by-character. By "
                            "default Enter is pressed after — set "
                            "`submit: false` to build up a command "
                            "interactively."
                        ),
                    },
                    "submit": {
                        "type": "boolean",
                        "description": (
                            "Press Enter after typing. Default true. "
                            "Set false to leave the text in the input "
                            "buffer without submitting it."
                        ),
                        "default": True,
                    },
                },
                "required": ["pane", "text"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "description": "Keystroke mode: send a sequence of tmux key names.",
                "properties": {
                    "pane": {
                        "type": "string",
                        "description": (
                            "Pane to send to. Accepts `%pane_id` or any "
                            "tmux target format."
                        ),
                    },
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": (
                            "Tmux key names as a list, e.g. `\"C-c\"` for "
                            "Ctrl+C, `\"BSpace\"` for backspace, `\"Enter\"` "
                            "to submit. Trailing Enter is not implied — "
                            "include it explicitly."
                        ),
                    },
                },
                "required": ["pane", "keys"],
                "additionalProperties": False,
            },
        ],
    },
}

TMUX_WAIT_SCHEMA = {
    "name": "tmux_wait",
    "description": (
        "Wait for a substring to appear in a tmux pane, or time out. "
        "Returns a 5-line status hint on both paths so the agent can "
        "decide whether to call `tmux_capture`, send more input, or "
        "give up."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pane": {
                "type": "string",
                "description": (
                    "Pane to watch. Accepts `%pane_id` or any tmux "
                    "target format."
                ),
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Substring to look for in the captured scrollback. "
                    "Matched against the last 5 lines of the pane on "
                    "each poll."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Seconds to wait before giving up. Default 10, "
                    "max 60."
                ),
                "minimum": 1,
                "maximum": 60,
                "default": 10,
            },
        },
        "required": ["pane", "pattern"],
    },
}

ALL_SCHEMAS = [
    TMUX_LIST_SCHEMA,
    TMUX_CAPTURE_SCHEMA,
    TMUX_SEND_SCHEMA,
    TMUX_WAIT_SCHEMA,
]
