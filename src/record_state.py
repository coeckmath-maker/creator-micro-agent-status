#!/usr/bin/env python3
"""Claude Code hook -> record the current session's state in state.json.

Called from settings.json with the event name in argv[1] and the hook's JSON
payload on stdin. This must **never** make a hook fail: every error is
swallowed and the script always exits 0. A status light is not worth breaking
someone's session over.

    record_state.py SessionStart      < hook JSON on stdin
    record_state.py UserPromptSubmit  < hook JSON on stdin
    record_state.py Stop              < hook JSON on stdin
    record_state.py Notification      < hook JSON on stdin
"""
import json
import os
import sys
import tempfile
import time

STATE_DIR = os.path.expanduser("~/.local/share/wl-agent-status")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Any Notification reaching this hook means something wants attention, so it
# maps to awaiting-approval regardless of its subtype. Narrow it with the
# `matcher` field in settings.json if you only care about permission prompts.
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
    """Write atomically, so a daemon reading concurrently never sees a partial file."""
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


def main():
    try:
        event = sys.argv[1] if len(sys.argv) > 1 else ""
        try:
            payload = json.load(sys.stdin)
        except Exception:
            payload = {}

        new_state = EVENT_STATE.get(event)
        if new_state is None:
            return  # event we don't map; ignore quietly

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
