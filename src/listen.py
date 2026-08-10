#!/usr/bin/env python3
"""Dump everything the Creator Micro 2 emits on its own.

Read-only: nothing is ever sent to the device. Decodes channel 1 (firmware
debug log) and channel 2 (JSON-RPC); anything else is printed as raw hex.

This is how the notification format was found, e.g. pressing a status key:
    {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}
"""
import os
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wl_device import REPORT_ID, find_device, split_json  # noqa: E402

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

fd = os.open(find_device(), os.O_RDWR | os.O_NONBLOCK)
buf = {1: "", 2: ""}
end = time.time() + DURATION
print(f"listening for {DURATION:.0f}s - press some keys", flush=True)

while time.time() < end:
    if not select.select([fd], [], [], 0.3)[0]:
        continue
    try:
        r = os.read(fd, 64)
    except BlockingIOError:
        continue
    v = r[1:] if r and r[0] == REPORT_ID else r
    if not v:
        continue
    ch, ln = v[0], v[1]
    ts = time.strftime("%H:%M:%S")
    if ch in (1, 2):
        buf[ch] += v[2:2 + ln].decode("utf8", "replace")
        if ch == 1:   # firmware logs are newline-delimited
            while "\n" in buf[1]:
                line, buf[1] = buf[1].split("\n", 1)
                if line.strip():
                    print(f"[{ts}] DEBUG  {line.strip()}", flush=True)
        else:         # RPC has no delimiter at all; balance braces
            objs, buf[2] = split_json(buf[2])
            for o in objs:
                print(f"[{ts}] RPC    {o}", flush=True)
    else:
        print(f"[{ts}] BRUT   canal={ch} {v[:16].hex()}", flush=True)

os.close(fd)
print("done", flush=True)
