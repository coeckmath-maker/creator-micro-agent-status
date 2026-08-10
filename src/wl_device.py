#!/usr/bin/env python3
"""Talk to a Work Louder Creator Micro 2 over raw HID, from Linux, with no
vendor software running.

The firmware exposes a JSON-RPC 2.0 service over HID report 0x06. This module
implements just enough of it to drive the six "agent status" LEDs and to read
and write the device's configuration files.

Protocol summary (see docs/PROTOCOL.md for the full write-up):

    byte 0   report id, always 0x06
    byte 1   channel: 1 = firmware debug log, 2 = JSON-RPC
    byte 2   payload length in this frame (max 61)
    byte 3.. UTF-8 payload

Requests are split across as many 64-byte frames as needed. Responses carry
**no delimiter at all**, so the stream is reassembled by balancing braces.

Tested against firmware v0.6.1 on a Creator Micro 2 (VID 0x303A, PID 0x8298),
over both USB and Bluetooth LE.

CLI usage:
    python3 wl_device.py working idle unread awaiting-approval
"""
import glob
import json
import os
import select
import sys
import time

VID, PID = 0x303A, 0x8298
REPORT_ID, CH_DEBUG, CH_RPC = 0x06, 1, 2
MAX_PAYLOAD = 61

# Colours lifted from the ChatGPT desktop app's own status palette, so the
# device looks the same as it does under Codex. Packed 0xRRGGBB.
STATE_COLOR = {
    "idle": 0xFFFFFF,               # white  - nothing to report
    "working": 0x304FFE,            # blue   - busy
    "unread": 0x00FF4C,             # green  - finished, waiting to be read
    "awaiting-approval": 0xFF6D00,  # amber  - needs a human decision
    "error": 0xFF0033,              # red    - something broke
    "off": 0x000000,
}
EFFECT_SOLID, EFFECT_OFF = 1, 0


class DeviceNotFound(RuntimeError):
    pass


def find_device():
    """Locate the Creator Micro 2's hidraw node, USB or Bluetooth.

    The /dev/hidrawN number changes on every reconnection, so never cache it.
    """
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = open(f"{node}/device/uevent").read().upper()
        except OSError:
            continue
        if f"{VID:04X}" in uevent and f"{PID:04X}" in uevent:
            return f"/dev/{os.path.basename(node)}"
    raise DeviceNotFound("Creator Micro 2 not found (unplugged, or Bluetooth down?)")


def frames(msg, channel=CH_RPC):
    """Split a message into 64-byte HID output reports."""
    data, out, off = msg.encode(), [], 0
    while True:
        n = min(MAX_PAYLOAD, len(data) - off)
        f = bytearray(64)
        f[0], f[1], f[2] = REPORT_ID, channel, n
        f[3:3 + n] = data[off:off + n]
        out.append(bytes(f))
        off += n
        if off >= len(data):
            return out


def split_json(buf):
    """Cut a delimiter-free JSON stream into objects by balancing braces.

    Braces inside string literals are ignored. Returns (objects, remainder).
    """
    objs, depth, instr, esc, start = [], 0, False, False, -1
    for i, c in enumerate(buf):
        if instr:
            esc = (c == "\\") and not esc
            if c == '"' and not esc:
                instr = False
            continue
        if c == '"':
            instr = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth:
            depth -= 1
            if depth == 0:
                objs.append(buf[start:i + 1])
                start = -1
    rest = buf[start:] if depth and start >= 0 else ""
    return objs, rest


class Device:
    def __init__(self, path=None):
        self.fd = os.open(path or find_device(), os.O_RDWR | os.O_NONBLOCK)
        self.buf = ""

    def call(self, method, params=None, timeout=2.0, frame_pause=0.004):
        """Send a JSON-RPC call and wait for the matching response.

        `frame_pause` is not cosmetic: writing multi-frame payloads in a tight
        loop overruns the device's receive buffer and it silently drops bytes.
        4 ms between frames was enough to make multi-kilobyte writes reliable.
        """
        rid = int(time.time() * 1000) % 999
        for f in frames(json.dumps({"method": method, "params": params, "id": rid})):
            os.write(self.fd, f)
            if frame_pause:
                time.sleep(frame_pause)
        end = time.time() + timeout
        while time.time() < end:
            if not select.select([self.fd], [], [], 0.2)[0]:
                continue
            try:
                r = os.read(self.fd, 64)
            except BlockingIOError:
                continue
            v = r[1:] if r and r[0] == REPORT_ID else r
            if not v or v[0] != CH_RPC:
                continue
            self.buf += v[2:2 + v[1]].decode("utf8", "replace")
            objs, self.buf = split_json(self.buf)
            for o in objs:
                msg = json.loads(o)
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg.get("result")
        raise TimeoutError(method)

    def set_agent_states(self, states, brightness=1):
        """Colour the agent-status LEDs. `states` is a list of STATE_COLOR keys.

        Only keys mapped to a KV_OAI_AGxx keycode on the active layer light up;
        the firmware exposes six slots regardless.
        """
        params = []
        for slot, st in enumerate((list(states) + ["off"] * 6)[:6]):
            color = STATE_COLOR.get(st, 0)
            params.append({
                "id": slot,
                "c": color,
                "b": brightness,
                "e": EFFECT_SOLID if color else EFFECT_OFF,
                "s": 0,
            })
        return self.call("v.oai.thstatus", params)

    def close(self):
        os.close(self.fd)


if __name__ == "__main__":
    states = sys.argv[1:] or ["idle"] * 6
    d = Device()
    print("firmware:", d.call("sys.version"))
    print("status  :", d.call("device.status"))
    print("leds    :", d.set_agent_states(states), "->", states)
    d.close()
