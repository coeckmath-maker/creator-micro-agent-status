#!/usr/bin/env python3
"""Read and write the Creator Micro 2's on-device config files, safely.

This firmware has two habits that will ruin your day if you trust it:

  * it truncates oversized writes *silently*, reporting success while writing
    fewer bytes than you sent;
  * pushed past that point, it can lock up hard enough to need the internal
    reset button.

So every write here is small (256-byte chunks), paced, and — most importantly —
**read back in full and compared byte for byte**. If the verification fails,
restore your backup before touching anything else.

Keep keymap.json under ~5.5 KB. The ceiling is around 6.1 KB and is documented
nowhere. See docs/PROTOCOL.md.

Three more precautions around every write, taken from 00cyre/claude-code-keypad
(src/remap.js and src/service.js, MIT):

  * the file currently on the device is backed up first, to
    ~/.local/share/wl-agent-status/backups/, and the path is printed;
  * the write is refused while the Input app is running — the device takes a
    single byte stream, and two writers corrupt each other;
  * for the same reason the status daemon is stopped for the duration, and
    restarted afterwards even if the write fails.

Usage:
    python3 write_config.py --read keymap.json > keymap.json
    python3 write_config.py local.json keymap.json
"""
import base64
import contextlib
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wl_device import Device  # noqa: E402

SMALL_CHUNK = 256   # well under the 3072 the vendor code uses
SOFT_LIMIT = 5632   # 5.5 KB — refuse to write more without --force
BACKUP_DIR = os.path.expanduser("~/.local/share/wl-agent-status/backups")
SERVICE = "wl-agent-status.service"


def read_file(dev, filename):
    """Pull a file off the device in 512-byte chunks."""
    got, off = bytearray(), 0
    while True:
        r = dev.call("fs.readbin", {"file": filename, "offset": off, "len": 512},
                     timeout=20.0)
        total = r["total_size"]
        chunk = base64.b64decode(r["data"])
        if not chunk and off < total:
            raise RuntimeError(f"{filename}: empty chunk before EOF ({off}/{total})")
        got.extend(chunk)
        off += len(chunk)
        if off >= total:
            return bytes(got)


def write_verified(dev, filename, data: bytes, quiet=False):
    """Write, then read the whole file back and compare. Returns True if equal."""
    offset, n = 0, len(data)
    while offset < n:
        end = min(offset + SMALL_CHUNK, n)
        result = dev.call("fs.writebin", {
            "file": filename,
            "data": base64.b64encode(data[offset:end]).decode("ascii"),
            "append": offset > 0,
            "completed": end >= n,
            "offset": offset,
        }, timeout=20.0)
        if not quiet:
            print(f"  {offset}/{n} -> {result}", file=sys.stderr)
        offset = end

    got = read_file(dev, filename)
    ok = got == data
    print(f"  verify {filename}: {'OK, identical' if ok else 'MISMATCH'} "
          f"({len(got)}/{len(data)} bytes)", file=sys.stderr)
    return ok


def input_app_running():
    """True if the Work Louder Input app is up (its process is named `input`)."""
    try:
        return subprocess.run(["pgrep", "-x", "input"], capture_output=True).returncode == 0
    except OSError:
        return False


def backup(name, data):
    """Save a timestamped copy of what is on the device and return its path."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(name))
    path = os.path.join(BACKUP_DIR, f"{base}-{time.strftime('%Y%m%d-%H%M%S')}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


@contextlib.contextmanager
def daemon_paused():
    """Stop the status daemon for the duration, then put it back as it was.

    Its heartbeat writes to the same hidraw node every 30 s, and a status frame
    landing between two chunks of a keymap write is exactly the kind of
    interleaving this firmware does not survive. The restart is in a finally:
    a failed write must not also leave the lights off.
    """
    try:
        was_active = _systemctl("is-active", "--quiet", SERVICE).returncode == 0
    except OSError:          # no systemd: nothing to pause
        was_active = False
    if was_active:
        print(f"  stopping {SERVICE} for the write", file=sys.stderr)
        _systemctl("stop", SERVICE)
    try:
        yield
    finally:
        if was_active:
            _systemctl("start", SERVICE)
            print(f"  restarted {SERVICE}", file=sys.stderr)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2

    if args[0] == "--read":
        with daemon_paused():
            dev = Device()
            sys.stdout.buffer.write(read_file(dev, args[1]))
            dev.close()
        return 0

    force = "--force" in args
    args = [a for a in args if a != "--force"]
    local_path, device_name = args[0], args[1]
    data = open(local_path, "rb").read()

    if len(data) > SOFT_LIMIT and not force:
        print(f"refusing: {len(data)} bytes is above the {SOFT_LIMIT}-byte safety "
              f"limit.\nThe firmware truncates silently past ~6.1 KB and can lock "
              f"up. Shrink the file, or pass --force if you know what you're doing.",
              file=sys.stderr)
        return 1

    # Not overridable by --force: that flag is about size, and this is about
    # two programs writing the same stream at once.
    if input_app_running():
        print("refusing: the Input app is running. Quit it first — the device "
              "takes a single byte stream, and two writers corrupt each other.",
              file=sys.stderr)
        return 1

    with daemon_paused():
        dev = Device()
        try:
            current = read_file(dev, device_name)
        except Exception as e:
            print(f"refusing: could not read the current {device_name} to back it "
                  f"up ({e}). Nothing was written.", file=sys.stderr)
            dev.close()
            return 1
        print(f"  backup of the current {device_name}: {backup(device_name, current)}",
              file=sys.stderr)
        ok = write_verified(dev, device_name, data)
        dev.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
