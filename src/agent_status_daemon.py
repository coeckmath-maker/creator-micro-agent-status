#!/usr/bin/env python3
"""Watch state.json and push the matching colours to the macropad.

Reads what the Claude Code hooks wrote (see record_state.py), groups sessions
by working directory, and gives each project a stable LED slot. Only talks to
the device when something actually changed.

The ring around the case carries the single most urgent state, so you can tell
from across the room that something wants you without reading four keys. Pass
--no-ring to leave the ambient lighting alone.

Never dies when the device goes away: it retries until it comes back.
"""
import argparse
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wl_device import ZONE_OFF, Device, DeviceNotFound, zone_for  # noqa: E402

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

# The ring shows the worst state on the board — but only one worth looking up
# for. "idle" is deliberately absent: a wide band of white left on all day is
# not information, it is a lamp, and it would also sit on top of whatever
# lighting you had chosen for the device.
RING_STATES = ["error", "awaiting-approval", "working", "unread"]

# Dimmer than the keys. The ring is a large diffuse surface right under your
# hands; at full brightness it dominates the desk rather than informing it.
RING_BRIGHTNESS = 0.35


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


def process_gone(pid, proc_start):
    """True only when we can tell the session's process has exited.

    The start time is compared, not just the pid: Linux recycles pids, and a
    stranger that inherited one must not keep a dead session lit. Anything we
    cannot read counts as "still there" — wrongly dimming a live session is
    worse than a stale light.
    """
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if proc_start is None:
        return False
    # comm, field 2, is in parentheses and may contain spaces: split after it.
    # starttime is field 22, so index 19 counting from field 3.
    fields = stat.rpartition(")")[2].split()
    return len(fields) > 19 and fields[19] != str(proc_start)


# A session whose process is gone cannot be working, nor waiting on a
# permission prompt — no hook will ever say so, because Stop does not fire on
# Ctrl+C or a crash. Those states fall back to idle. "unread" stays: the
# answer is still there to read.
# From 00cyre/claude-code-keypad (stateFromRecords in src/sessions.js, MIT).
NEEDS_A_PROCESS = {"working", "awaiting-approval"}


def project_states(state, gone=process_gone):
    """Collapse sessions to one state per working directory, ignoring stale ones."""
    now = time.time()
    by_cwd = {}
    for sess in state.get("sessions", {}).values():
        if now - sess.get("ts", 0) > STALE_AFTER_S:
            continue
        cwd = sess.get("cwd", "unknown")
        st = sess.get("state", "idle")
        if st in NEEDS_A_PROCESS and gone(sess.get("pid"), sess.get("proc_start")):
            st = "idle"
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


def ring_state(states):
    """The most urgent state on the board, or None if none is worth showing."""
    for state in RING_STATES:
        if state in states.values():
            return state
    return None


class Stop(BaseException):
    """Raised from the SIGTERM handler so the loop can tidy up on the way out.

    BaseException, not Exception, and for a concrete reason: the signal can
    land in the middle of a device write, which sits inside an `except
    Exception` that treats anything thrown there as "the device went away".
    Deriving from Exception would have that handler swallow the shutdown, log a
    phantom disconnect, and carry on — until systemd lost patience and killed
    us with the ring still lit.
    """


def _stop(*_):
    """SIGTERM handler. Raising is what interrupts the sleep: since PEP 475 a
    handler that returns normally lets time.sleep resume where it left off."""
    raise Stop()


def main():
    parser = argparse.ArgumentParser(description="Push Claude Code session status to a Creator Micro 2")
    parser.add_argument("--no-ring", action="store_true",
                        help="don't touch the ambient ring. It is a device-wide "
                             "setting, not a per-layer one, so driving it overrides "
                             "whatever lighting you configured yourself.")
    args = parser.parse_args()
    ring_enabled = not args.no_ring

    signal.signal(signal.SIGTERM, _stop)

    log(f"wl-agent-status daemon starting (ring {'on' if ring_enabled else 'off'})")
    last_states, dev, last_attempt = None, None, 0.0
    last_push = 0.0
    owned = set()     # slots WE lit: the only ones we are allowed to turn off
    ring, lit_ring = None, None

    try:
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
                        if ring_enabled:
                            # Resent on the heartbeat like the keys are. Whether
                            # the ring is forgotten after a long silence the way
                            # the slots are has not been established either way,
                            # and one more call every 30s is not worth finding out.
                            ring = ring_state(states)
                            dev.set_zones(ambient=zone_for(ring, RING_BRIGHTNESS) if ring else ZONE_OFF)
                            lit_ring = ring
                        if changed:      # keep the heartbeat out of the log
                            log(f"leds -> {dict(sorted(states.items()))}"
                                + (f", ring {ring or 'off'}" if ring_enabled else ""))
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
    except (Stop, KeyboardInterrupt):
        # Give the ring back. The keys we leave alone: another program may own
        # the slots we never touched, and the ones we did are about to be
        # overwritten by whoever starts next.
        if dev is not None and lit_ring:
            try:
                dev.set_zones(ambient=ZONE_OFF)
            except Exception:
                pass
        log("stopping")


if __name__ == "__main__":
    main()
