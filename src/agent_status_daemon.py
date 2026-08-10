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

    states = ["off"] * NUM_SLOTS
    for i, s in enumerate(slots[:NUM_SLOTS]):
        states[i] = by_cwd.get(s["cwd"], "off")
    return states, slots


def main():
    log("wl-agent-status daemon starting")
    last_states, dev, last_attempt = None, None, 0.0

    while True:
        state = load_json(STATE_FILE, {"sessions": {}})
        slots = load_json(SLOTS_FILE, [])
        states, slots = assign_slots(project_states(state), slots)
        save_json(SLOTS_FILE, slots)

        if states != last_states:
            now = time.time()
            if dev is None and now - last_attempt >= RETRY_DEVICE_S:
                last_attempt = now
                try:
                    dev = Device()
                    log("device connected")
                except DeviceNotFound:
                    dev = None
            if dev is not None:
                try:
                    dev.set_agent_states(states)
                    log(f"leds -> {states}")
                    last_states = states
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
