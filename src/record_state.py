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

Beyond the four basic events, a few more keep the light honest. The
transitions are an idea from n4ru/cm2-claude's state table (no licence
published, so nothing was copied — reimplemented here from its README):

  * PostToolUse / PostToolUseFailure clear amber back to blue. Without them a
    session stayed amber after you approved its tool, for the rest of the
    turn, because nothing else fires until Stop.
  * PreToolUse on AskUserQuestion turns amber: the session is waiting on you,
    even though it is technically mid-turn.
  * SessionStart after a compaction keeps the state it had, instead of
    flashing a working session back to white.
  * StopFailure (an API error ended the turn) is red — the first thing ever to
    trigger the `error` state.
  * SessionEnd frees the slot at once instead of three hours later.
"""
import fcntl
import json
import os
import re
import sys
import tempfile
import time

STATE_DIR = os.path.expanduser("~/.local/share/wl-agent-status")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Claude Code keeps one <pid>.json per running session here, naming its
# sessionId. Recording the pid lets the daemon notice a session that died
# mid-turn — Ctrl+C, a crash, a closed window — after which no Stop hook ever
# fires and the key would otherwise stay "working" for hours.
# Idea borrowed from 00cyre/claude-code-keypad (src/sessions.js, MIT).
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")

LOCK_FILE = os.path.join(STATE_DIR, "state.lock")

EVENT_STATE = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "Stop": "unread",
    "Notification": "awaiting-approval",
    "PreToolUse": "awaiting-approval",   # wired to AskUserQuestion only
    "StopFailure": "error",
}

# Events that mean "the tool ran, so whatever we were waiting for is over".
# They fire after every tool call, so they only ever clear amber: anything
# else is left alone and nothing is written.
TOOL_DONE = {"PostToolUse", "PostToolUseFailure"}

KEEP, DROP = object(), object()


def decide(event, payload, current):
    """What this hook means for a session: a new state, KEEP or DROP.

    `current` is the session's present state, or None if it has none yet.
    Pure, so the whole table can be tested without touching a file.
    """
    if event == "SessionEnd":
        return DROP
    if event in TOOL_DONE:
        return "working" if current == "awaiting-approval" else KEEP
    if event == "SessionStart" and payload.get("source") == "compact" and current:
        return KEEP
    if event == "PreToolUse" and payload.get("tool_name") != "AskUserQuestion":
        return KEEP        # belt and braces: settings.json already filters on it
    new_state = EVENT_STATE.get(event)
    if new_state is None:
        return KEEP
    if event == "Stop" and ends_with_question(as_text(payload.get("last_assistant_message"))):
        return "awaiting-approval"
    return new_state


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


def find_process(session_id):
    """(pid, procStart) of the process running `session_id`, or (None, None).

    procStart is the process start time as /proc/<pid>/stat reports it, which
    is what makes the pid safe to check later: a recycled pid has a different
    start time.
    """
    try:
        entries = os.listdir(SESSIONS_DIR)
    except OSError:
        return None, None
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, entry)) as f:
                record = json.load(f)
        except Exception:
            continue
        if record.get("sessionId") == session_id and record.get("pid"):
            return record["pid"], record.get("procStart")
    return None, None


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

        session_id = payload.get("session_id") or "unknown"
        cwd = payload.get("cwd") or os.environ.get("PWD") or "unknown"

        os.makedirs(STATE_DIR, exist_ok=True)
        # Several sessions' hooks can fire at the same instant, and each one
        # reads, changes and rewrites the whole file. Without a lock the last
        # writer silently erases what the others just recorded.
        with open(LOCK_FILE, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = load_state()
            sessions = state.setdefault("sessions", {})
            current = (sessions.get(session_id) or {}).get("state")

            verdict = decide(event, payload, current)
            if verdict is KEEP:
                return
            if verdict is DROP:
                if sessions.pop(session_id, None) is not None:
                    save_state(state)
                return

            entry = {"cwd": cwd, "state": verdict, "ts": time.time()}
            pid, proc_start = find_process(session_id)
            if pid:
                entry["pid"], entry["proc_start"] = pid, proc_start
            sessions[session_id] = entry
            save_state(state)
    except Exception:
        # Never fail a Claude Code hook because of us.
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
