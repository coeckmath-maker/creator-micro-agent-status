#!/usr/bin/env python3
"""Tests that need no macropad.

Everything here is about the decisions the daemon makes before it talks to the
device: which project owns which slot, what the ring should show, and what the
zone call actually puts on the wire. The device half is covered by running it —
see README.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import agent_status_daemon as daemon   # noqa: E402
from wl_device import Device, zone_for  # noqa: E402


def sessions(*items):
    """Build a state.json body. Each item is (id, cwd, state, age_seconds)."""
    now = time.time()
    return {"sessions": {i: {"cwd": cwd, "state": st, "ts": now - age}
                         for i, cwd, st, age in items}}


class ProjectStates(unittest.TestCase):
    def test_worst_state_wins_a_project(self):
        state = sessions(("a", "/p", "unread", 0), ("b", "/p", "awaiting-approval", 0))
        self.assertEqual(daemon.project_states(state), {"/p": "awaiting-approval"})

    def test_order_of_sessions_does_not_matter(self):
        state = sessions(("a", "/p", "awaiting-approval", 0), ("b", "/p", "unread", 0))
        self.assertEqual(daemon.project_states(state), {"/p": "awaiting-approval"})

    def test_stale_sessions_are_dropped(self):
        state = sessions(("a", "/p", "working", daemon.STALE_AFTER_S + 1))
        self.assertEqual(daemon.project_states(state), {})


class DeadSessions(unittest.TestCase):
    """A session whose process is gone falls back to idle — except "unread".
    Approach borrowed from 00cyre/claude-code-keypad."""

    def with_pid(self, st):
        state = sessions(("a", "/p", st, 0))
        state["sessions"]["a"].update(pid=123, proc_start="456")
        return state

    def test_dead_working_session_goes_idle(self):
        got = daemon.project_states(self.with_pid("working"), gone=lambda *_: True)
        self.assertEqual(got, {"/p": "idle"})

    def test_dead_approval_prompt_goes_idle(self):
        got = daemon.project_states(self.with_pid("awaiting-approval"), gone=lambda *_: True)
        self.assertEqual(got, {"/p": "idle"})

    def test_unread_survives_its_process(self):
        got = daemon.project_states(self.with_pid("unread"), gone=lambda *_: True)
        self.assertEqual(got, {"/p": "unread"})

    def test_live_session_is_untouched(self):
        got = daemon.project_states(self.with_pid("working"), gone=lambda *_: False)
        self.assertEqual(got, {"/p": "working"})

    def test_own_process_is_alive(self):
        with open(f"/proc/{os.getpid()}/stat") as f:
            start = f.read().rpartition(")")[2].split()[19]
        self.assertFalse(daemon.process_gone(os.getpid(), start))

    def test_recycled_pid_counts_as_gone(self):
        self.assertTrue(daemon.process_gone(os.getpid(), "1"))

    def test_missing_pid_counts_as_gone(self):
        self.assertTrue(daemon.process_gone(2 ** 22 + 1, "1"))   # above pid_max

    def test_no_pid_recorded_is_never_gone(self):
        self.assertFalse(daemon.process_gone(None, None))


class SlotAssignment(unittest.TestCase):
    def test_fills_from_the_top(self):
        # The ChatGPT app fills from slot 0 up; we come down from the top so
        # both can drive the same six slots without erasing each other.
        states, _ = daemon.assign_slots({"/p": "working"}, [])
        self.assertEqual(states, {daemon.NUM_SLOTS - 1: "working"})

    def test_a_project_keeps_its_slot(self):
        _, slots = daemon.assign_slots({"/a": "working"}, [])
        states, _ = daemon.assign_slots({"/a": "unread", "/b": "working"}, slots)
        self.assertEqual(states[daemon.NUM_SLOTS - 1], "unread")

    def test_finished_projects_free_their_slot(self):
        _, slots = daemon.assign_slots({"/a": "working"}, [])
        states, slots = daemon.assign_slots({}, slots)
        self.assertEqual((states, slots), ({}, []))

    def test_least_recently_active_is_recycled_when_full(self):
        full = {f"/p{i}": "idle" for i in range(daemon.NUM_SLOTS)}
        _, slots = daemon.assign_slots(full, [])
        slots[0]["last_active"] = 0        # /p0 has been quiet the longest
        crowded = dict(full, **{"/new": "working"})
        states, slots = daemon.assign_slots(crowded, slots)
        self.assertEqual(len(slots), daemon.NUM_SLOTS)
        self.assertIn("/new", [s["cwd"] for s in slots])
        self.assertEqual(sorted(states), list(range(daemon.NUM_SLOTS)))


class Ring(unittest.TestCase):
    def test_most_urgent_state_wins(self):
        self.assertEqual(daemon.ring_state({0: "unread", 1: "awaiting-approval"}),
                         "awaiting-approval")

    def test_idle_does_not_light_the_ring(self):
        self.assertIsNone(daemon.ring_state({0: "idle", 1: "idle"}))

    def test_nothing_running_leaves_it_dark(self):
        self.assertIsNone(daemon.ring_state({}))


class ZoneWireFormat(unittest.TestCase):
    """v.oai.rgbcfg takes the SHORT field names, unlike v.oai.thstatus.

    Both answer {"ok":1} whatever you send, so nothing but a test like this
    stops a well-meaning cleanup from spelling the names out and silently
    turning the ring off for good.
    """
    def setUp(self):
        self.dev = object.__new__(Device)      # no hardware, no file descriptor
        self.sent = []
        self.dev.call = lambda method, params: self.sent.append((method, params))

    def test_ring_call_uses_short_names(self):
        self.dev.set_zones(ambient=zone_for("working", 0.35))
        method, params = self.sent[0]
        self.assertEqual(method, "v.oai.rgbcfg")
        self.assertEqual(set(params["ambient"]), {"c", "b", "e", "s", "m"})
        self.assertEqual(params["ambient"]["c"], 0x304FFE)
        self.assertEqual(params["ambient"]["b"], 0.35)

    def test_untouched_zones_are_left_out(self):
        self.dev.set_zones(ambient=zone_for("off"))
        self.assertNotIn("keys", self.sent[0][1])

    def test_nothing_to_say_sends_nothing(self):
        self.assertIsNone(self.dev.set_zones())
        self.assertEqual(self.sent, [])

    def test_status_slots_still_use_the_long_names(self):
        self.dev.set_agent_states({0: "working"})
        method, params = self.sent[0]
        self.assertEqual(method, "v.oai.thstatus")
        self.assertEqual(set(params[0]), {"id", "color", "brightness", "effect", "speed"})


if __name__ == "__main__":
    unittest.main()
