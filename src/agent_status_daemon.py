#!/usr/bin/env python3
"""Watch state.json and push the matching colours to the macropad.

Reads what the Claude Code hooks wrote (see record_state.py), groups sessions
by working directory, and gives each project a stable LED slot. Only talks to
the device when something actually changed.

Never dies when the device goes away: it retries until it comes back.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wl_device import Device, DeviceNotFound  # noqa: E402

STATE_DIR = os.path.expanduser("~/.local/share/wl-agent-status")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SLOTS_FILE = os.path.join(STATE_DIR, "slots.json")

# The firmware exposes six slots, but only keys carrying a KV_OAI_AGxx keycode
# light up. Set this to how many such keys your layer actually has — a session
# assigned to a slot with no key behind it is invisible.
NUM_SLOTS = 4

STALE_AFTER_S = 3 * 3600   # a session untouched for 3h is treated as gone
POLL_S = 1.0
RETRY_DEVICE_S = 10.0

# The firmware eventually forgets the colours if you stop talking to it: after
# six quiet hours the keys were dark while v.oai.thstatus still answered
# {"ok":1}, and a single fresh call brought them straight back. So resend the
# current state on a timer, whether or not anything changed.
HEARTBEAT_S = 30.0

# Worst state wins the slot, so a project needing attention is never masked
# by another of its sessions quietly finishing.
PRIORITY = ["error", "awaiting-approval", "working", "unread", "idle"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def project_states(state):
    """Collapse sessions to one state per working directory, ignoring stale ones."""
    now = time.time()
    by_cwd = {}
    for sess in state.get("sessions", {}).values():
        if now - sess.get("ts", 0) > STALE_AFTER_S:
            continue
        cwd = sess.get("cwd", "unknown")
        st = sess.get("state", "idle")
        cur = by_cwd.get(cwd)
        if cur is None or PRIORITY.index(st) < PRIORITY.index(cur):
            by_cwd[cwd] = st
    return by_cwd


def assign_slots(by_cwd, slots):
    """Keep each project on the same LED across restarts.

    `slots` is the persisted list of {cwd, last_active}. New projects take a
    free slot; if all are taken, the least recently active one is recycled.
    """
    now = time.time()
    slots = [s for s in slots if s.get("cwd") in by_cwd]   # drop finished projects
    known = {s["cwd"] for s in slots}

    for cwd in by_cwd:
        if cwd in known:
            continue
        if len(slots) < NUM_SLOTS:
            slots.append({"cwd": cwd, "last_active": now})
        else:
            slots.sort(key=lambda s: s["last_active"])
            slots[0] = {"cwd": cwd, "last_active": now}
        known.add(cwd)

    for s in slots:
        s["last_active"] = now

    # Fill from the TOP (slot 3, then 2, 1, 0). The ChatGPT desktop app drives
    # the same six slots and fills them from the bottom, so starting at the
    # opposite end lets both run at once without erasing each other.
    states = {}
    for i, s in enumerate(slots[:NUM_SLOTS]):
        st = by_cwd.get(s["cwd"])
        if st:
            states[NUM_SLOTS - 1 - i] = st
    return states, slots


def main():
    log("wl-agent-status daemon starting")
    last_states, dev, last_attempt = None, None, 0.0
    last_push = 0.0
    owned = set()     # slots WE lit: the only ones we are allowed to turn off

    while True:
        state = load_json(STATE_FILE, {"sessions": {}})
        slots = load_json(SLOTS_FILE, [])
        states, slots = assign_slots(project_states(state), slots)
        save_json(SLOTS_FILE, slots)

        now = time.time()
        changed = states != last_states
        if changed or now - last_push >= HEARTBEAT_S:
            if dev is None and now - last_attempt >= RETRY_DEVICE_S:
                last_attempt = now
                try:
                    dev = Device()
                    log("device connected")
                except DeviceNotFound:
                    dev = None
            if dev is not None:
                try:
                    payload = dict(states)
                    for slot in owned - set(states):
                        payload[slot] = "off"    # hand back the slots we drop
                    dev.set_agent_states(payload)
                    owned = set(states)
                    if changed:      # keep the heartbeat out of the log
                        log(f"leds -> {dict(sorted(states.items()))}")
                    last_states = states
                    last_push = now
                except Exception as e:
                    log(f"send failed ({e}); device probably disconnected")
                    try:
                        dev.close()
                    except Exception:
                        pass
                    dev = None

        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
