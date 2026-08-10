#!/usr/bin/env python3
"""Claude Code hook -> record the current session's state in state.json.

Called from settings.json with the event name in argv[1] and the hook's JSON
payload on stdin. This must **never** make a hook fail: every error is
swallowed and the script always exits 0. A status light is not worth breaking
someone's session over.

One subtlety: the Stop hook fires at the end of every turn, whether the
assistant delivered a result or asked you a question, and nothing in the
payload tells them apart. So we read `last_assistant_message` and check
whether it *ends* on a question — if so the session gets amber (it wants
something from you) instead of green (there is merely something to read).
"""
import json
import os
import re
import sys
import tempfile
import time

STATE_DIR = os.path.expanduser("~/.local/share/wl-agent-status")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

EVENT_STATE = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "Stop": "unread",
    "Notification": "awaiting-approval",
}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"sessions": {}}


def save_state(state):
    """Write atomically, so a reader never sees a half-written file."""
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def as_text(msg):
    """`last_assistant_message` may be a string or a structured block."""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        for key in ("text", "content", "message"):
            v = msg.get(key)
            if isinstance(v, str):
                return v
            if isinstance(v, list):
                return " ".join(as_text(x) for x in v)
    if isinstance(msg, list):
        return " ".join(as_text(x) for x in msg)
    return ""


def ends_with_question(text):
    """Does the message *end* on a question aimed at the human?

    Only the LAST PARAGRAPH counts. An earlier version looked at the last 3
    lines, which reached back into bullet lists: in markdown a paragraph
    occupies a single line, so "3 lines" covers a wildly variable amount of
    text depending on formatting. The verdict tracked the message's layout
    rather than its intent.

    The rule is deliberately strict and predictable: the question has to come
    last. That is also how you should write when you actually want an answer.

    Trailing lists and tables don't count - enumerating open questions is not
    the same as asking for a decision.
    """
    if not text:
        return False
    text = re.sub(r"```.*?```", " ", text, flags=re.S)   # fenced code
    text = re.sub(r"`[^`]*`", " ", text)                  # inline code
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if not paragraphs:
        return False
    last = paragraphs[-1]
    if re.match(r"^\s*([-*+]|\d+[.)]|\|)", last):        # list or table
        return False
    return "?" in last


def main():
    try:
        event = sys.argv[1] if len(sys.argv) > 1 else ""
        try:
            payload = json.load(sys.stdin)
        except Exception:
            payload = {}

        new_state = EVENT_STATE.get(event)
        if new_state is None:
            return

        if event == "Stop" and ends_with_question(as_text(payload.get("last_assistant_message"))):
            new_state = "awaiting-approval"

        session_id = payload.get("session_id") or "unknown"
        cwd = payload.get("cwd") or os.environ.get("PWD") or "unknown"

        state = load_state()
        state.setdefault("sessions", {})[session_id] = {
            "cwd": cwd,
            "state": new_state,
            "ts": time.time(),
        }
        save_state(state)
    except Exception:
        # Never fail a Claude Code hook because of us.
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
