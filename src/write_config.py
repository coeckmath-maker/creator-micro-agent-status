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

Usage:
    python3 write_config.py --read keymap.json > keymap.json
    python3 write_config.py local.json keymap.json
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wl_device import Device  # noqa: E402

SMALL_CHUNK = 256   # well under the 3072 the vendor code uses
SOFT_LIMIT = 5632   # 5.5 KB — refuse to write more without --force


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


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2

    if args[0] == "--read":
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

    dev = Device()
    ok = write_verified(dev, device_name, data)
    dev.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
