# Live agent status on a Work Louder Creator Micro 2 — on Linux

Turn the macropad's keys into live status lights for your **Claude Code**
sessions: blue while an agent works, amber when it wants a decision, green when
it's done. Same idea as the Codex integration, except it runs on Linux, needs no
ChatGPT app, and watches whatever sessions you choose.

Built by reverse-engineering the firmware's RPC service. The device is driven
directly over raw HID — **the vendor app doesn't even need to be running.**

## Why this exists

Work Louder's 0.6 firmware brought "agent supervision" to the Creator Micro,
but it's wired to the ChatGPT desktop app — and it's for *its* agent, not
yours. The vendor app greys the layer out on Linux and tells you the feature
isn't available here.

It turns out the LEDs aren't gated on any of that. A key becomes a status slot
purely because its keycode is `KV_OAI_AG00`…`AG05`, and `v.oai.thstatus` drives
those slots over plain JSON-RPC. Any layer can have them, any program can drive
them. Everything else here follows from that.

The ChatGPT app has since shipped a Linux build, and it drives the very same
six slots. That's fine — this daemon only ever writes the slots it owns, and
fills them from the opposite end, so the two run side by side. See
`docs/PROTOCOL.md` for why per-layer status is impossible.

## What you get

- **Live status** for up to 6 sessions (one slot per project directory), driven
  by Claude Code hooks — no polling of anything, no wrapper around the CLI
- **A background daemon** that only talks to the device when something changes,
  survives unplugging, and reconnects on its own
- **The ring lit with whatever is most urgent**, so a session waiting on you is
  visible from across the room without reading four keys (`--no-ring` to skip)
- **A safe config writer** that reads every write back and compares it byte for
  byte, because this firmware truncates oversized writes without saying so
- **A protocol write-up** ([`docs/PROTOCOL.md`](docs/PROTOCOL.md)) covering the
  RPC methods, the config format, and the traps that cost me a frozen device

It is not limited to Claude Code. Anything that can write a small JSON file can
drive the lights.

## Requirements

- A **Creator Micro 2**, firmware **v0.6.1** or later (VID `303A`, PID `8298` — or `8297`, which the same keypad can also report)
- Linux with `hidraw`, Python 3.9+, no extra packages
- Read/write access to the device node — see [udev](#udev-permissions)

Only tested on the hardware named above, on Ubuntu 26.04 under GNOME/Wayland,
over both USB and Bluetooth LE.

## Install

```bash
git clone https://github.com/coeckmath-maker/creator-micro-agent-status
cd creator-micro-agent-status
./install.sh
```

That copies the scripts to `~/.local/share/wl-agent-status/`, installs a
`--user` systemd unit, and prints the hook configuration to add to
`~/.claude/settings.json`.

Then put `KV_OAI_AG00`…`AG03` on four keys of a layer (see
[Configuring the layer](#configuring-the-layer)) and you're done.

### udev permissions

Bluetooth HID sits on bus `0005`, so match on `KERNELS` rather than the USB
attributes:

```
# /etc/udev/rules.d/99-worklouder.rules
KERNEL=="hidraw*", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="8298", MODE="0660", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="8297", MODE="0660", TAG+="uaccess"
KERNEL=="hidraw*", KERNELS=="0005:303A:*", MODE="0660", TAG+="uaccess"
```

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## How it works

```
Claude Code hooks ──► record_state.py ──► state.json
                                              │
                                              ▼
                                    agent_status_daemon.py
                                              │  JSON-RPC over hidraw
                                              ▼
                                    Creator Micro 2 LEDs
```

Hooks write a one-line state per session: the four basic ones
(`SessionStart`, `UserPromptSubmit`, `Stop`, `Notification`), plus five that keep
the light honest — `PostToolUse` / `PostToolUseFailure` clear amber once an
approved tool has run, `PreToolUse` on `AskUserQuestion` turns amber,
`StopFailure` turns red, and `SessionEnd` frees the slot at once. A compaction
(`SessionStart` with `source: compact`) leaves the state as it was. The daemon groups sessions by working directory,
keeps the worst state per project, assigns each project a stable LED slot, and
pushes a frame only when something actually changed.

Hooks reload without restarting your session, so you can wire this up mid-flight.

| State | Colour | Trigger |
|---|---|---|
| `working` | blue | you sent a prompt |
| `awaiting-approval` | amber | a permission prompt is up |
| `unread` | green | the turn finished |
| `idle` | white | session started, nothing pending |
| `error` | red | an API error ended the turn (`StopFailure`) |

### The ring

The band around the case takes the most urgent state on the board, dimmed to
0.35 — a wide diffuse surface at full brightness dominates a desk instead of
informing it. `idle` deliberately leaves it dark: a light that is always on
tells you nothing, and this one sits on top of whatever ambient lighting you
chose yourself.

That last part is the catch. **The ring is a property of the device, not of a
layer** — the firmware has nowhere to store a per-layer version — so driving it
overrides your own setting on every layer, and it is still doing so while you
are on some other one. If you would rather keep your lighting, run the daemon
with `--no-ring`:

```bash
systemctl --user edit --full wl-agent-status.service   # add --no-ring to ExecStart
```

The daemon hands the ring back when it stops. It does not hand the keys back:
another program may own the slots it never touched.

## Configuring the layer

The device's own flash is the source of truth — the vendor app reads it on
connect and overwrites its local cache. Editing the host-side files does
nothing on its own.

```bash
# read the current config off the device
python3 src/write_config.py --read keymap.json > keymap.json

# edit it, then write it back (verified by read-back)
python3 src/write_config.py keymap.json keymap.json
```

Put `KV_OAI_AG00`…`AG03` on the keys you want as status lights — ideally ones
with transparent keycaps.

> **Keep `keymap.json` under about 5.5 KB.** Past roughly 6.1 KB the firmware
> truncates writes silently, and a write past that point froze my device hard
> enough to need the internal reset button. `write_config.py` always reads back
> and will tell you if it doesn't match. Details in
> [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

### Two things you give up

Using `KV_OAI_` keycodes makes the layer **read-only in the vendor app**, and
**disables its per-layer lighting**. Both are structural, both are documented
in the protocol notes. Configure that layer from scripts instead.

## Debugging

```bash
python3 src/listen.py 60      # dump everything the device emits for 60s
python3 src/wl_device.py working awaiting-approval unread idle   # force colours
python3 src/wl_device.py --ring awaiting-approval                # force the ring
python3 -m unittest discover -s test                             # no hardware needed
journalctl --user -u wl-agent-status -f
```

`listen.py` is how the notification format above was found. If you're exploring
this firmware yourself, start there.

## Notes for other Linux users of the Input app

Things that cost me hours, in case they cost you fewer:

- **The app force-enables your screen reader.** It sets `IsEnabled` and
  `ScreenReaderEnabled` on `org.a11y.Status` whenever it thinks it's on Wayland
  — which starts Orca, which reads every key you press aloud. Launch it with
  `XDG_SESSION_TYPE=x11` and it stops. `--ozone-platform=x11` alone is not
  enough; it doesn't change the variable the app checks.
- **`CMD_STEP` smart actions are refused** until you tick the consent box in the
  app's settings.
- **`TEXT_STEP` smart actions need `xdotool`**, which cannot type into native
  Wayland windows anyway. Use keyboard macros — they're real HID keystrokes and
  work everywhere.
- **Keycodes are physical positions, not letters.** On AZERTY, `KC_Q` types `a`.
  See the table in the protocol notes.
- After killing the app, delete `~/.config/input/Singleton{Lock,Cookie,Socket}`
  or it silently refuses to start again.

## Credits

Protocol work cross-checked against two MIT-licensed projects that emulate or
drive this hardware:

- [mpociot/codex-micro-stream-deck-emulator](https://github.com/mpociot/codex-micro-stream-deck-emulator)
- [maxxspotter/codex-micro-app](https://github.com/maxxspotter/codex-micro-app)

Two more appeared in September 2026, from macOS:
[00cyre/creator-micro-kit](https://github.com/00cyre/creator-micro-kit) (a Node
library for this RPC service) and
[00cyre/claude-code-keypad](https://github.com/00cyre/claude-code-keypad) (the
same idea as this project, reading Claude Code's transcripts instead of its
hooks). They go through a Swift IOKit bridge, so neither runs here — but they
found the same central fact independently, and `v.oai.rgbcfg` is documented
there first. What that method wants on the wire, though, is not what they say:
see [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

### Borrowed from 00cyre/claude-code-keypad

Four ideas in this project come from
[00cyre/claude-code-keypad](https://github.com/00cyre/claude-code-keypad)
(MIT, commit `b92bb2b`), reimplemented here in Python for Linux. Each is marked
at the spot in the code where it lives:

- **A dead session stops looking busy.** Claude Code writes
  `~/.claude/sessions/<pid>.json` for each running session; the hooks record
  that pid, and the daemon turns a `working` or `awaiting-approval` session
  whose process has exited into `idle`. Before this, a session killed mid-turn
  (Ctrl+C, a crash) stayed blue for up to three hours, because no `Stop` hook
  fires in that case. We additionally check the process start time, so a
  recycled pid cannot keep a dead session lit.
  (`record_state.py`, `agent_status_daemon.py`)
- **Both product ids.** The Creator Micro 2 can enumerate as `0x8297` as well
  as `0x8298`. (`wl_device.py`)
- **A backup before every keymap write**, in
  `~/.local/share/wl-agent-status/backups/`, with the path printed.
  (`write_config.py`)
- **One writer at a time.** `write_config.py` refuses to write while the Input
  app is running, and stops the status daemon for the duration of the write.
  (`write_config.py`)

### Inspired by n4ru/cm2-claude

The five extra hook transitions above follow the state table in
[n4ru/cm2-claude](https://github.com/n4ru/cm2-claude) (Windows). That
repository publishes no licence, so no code was taken from it: the transitions
were reimplemented here from its README. (`record_state.py`)

Not affiliated with Work Louder or OpenAI. Hardware is theirs; the mistakes
here are mine.

## License

MIT — see [LICENSE](LICENSE).
