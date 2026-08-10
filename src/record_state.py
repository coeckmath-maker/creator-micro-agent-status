#!/usr/bin/env python3
"""Claude Code hook -> record the current session's state in state.json.

Called from settings.json with the event name in argv[1] and the hook's JSON
payload on stdin. This must **never** make a hook fail: every error is
swallowed and the script always exits 0. A status light is not worth breaking
someone's session over.

One subtlety worth knowing: the Stop hook fires at the end of every turn,
whether the assistant delivered a result or asked you a question — nothing in
the payload tells them apart. So we look at `last_assistant_message`: if it
ends on a question, the session gets amber (it wants something from you)
rather than green (there is merely something to read).

That is a heuristic, not a certainty. A closing rhetorical question turns
amber wrongly; a real request phrased without a question mark stays green.
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
    """Does the closing paragraph contain a question aimed at the human?

    Code blocks are stripped first — a '?' inside code is not a question — and
    only the tail of the message is considered, where a question would land.
    """
    if not text:
        return False
    text = re.sub(r"```.*?```", " ", text, flags=re.S)   # fenced code
    text = re.sub(r"`[^`]*`", " ", text)                  # inline code
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return False
    tail = " ".join(lines[-3:])          # last few non-empty lines
    return "?" in tail


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
