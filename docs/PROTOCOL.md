# Creator Micro 2 — reverse-engineered protocol notes

Everything here was established against a real device (firmware **v0.6.1**,
then **v0.6.2**, VID `0x303A`, PID `0x8298`) by reading its traffic, its config
files, and the Linux build of the vendor app. Where a claim was only inferred
and not proven, it says so — and this firmware makes that distinction matter
more than most, because it answers `{"ok":1}` to a great deal it then ignores.

## Transport

The firmware speaks JSON-RPC 2.0 over raw HID, on report ID `0x06`.

| byte  | meaning                                    |
|-------|--------------------------------------------|
| 0     | report ID — always `0x06`                  |
| 1     | channel: `1` = firmware debug log, `2` = RPC |
| 2     | payload length in this frame (max **61**)  |
| 3..63 | UTF-8 payload                              |

Longer messages are split across several frames. **Device→host output carries
no delimiter of any kind** — not a newline, not a length prefix. The only way
to recover object boundaries is to balance braces while ignoring those inside
string literals (`split_json()` in `src/wl_device.py`).

### Frame pacing matters

Writing multi-frame payloads in a tight loop makes the device drop bytes
silently. A **4 ms sleep between frames** made multi-kilobyte writes reliable.
Without it, `fs.writebin` reports fewer `bytes_written` than were sent.

## Methods confirmed working

| Method | Params | Notes |
|---|---|---|
| `sys.version` | — | `{"version": "v0.6.1"}` |
| `device.status` | — | `{version, profile_index, layer_index, battery, is_charging}` |
| `fs.list` | `{checksum: true}` | name, size, SHA-1 per file |
| `fs.readbin` | `{file, offset, len}` | `{data: base64, total_size}` |
| `fs.writebin` | `{file, data, append, completed, offset}` | base64 chunks |
| `v.oai.thstatus` | array of `{id, color, brightness, effect, speed}` | the agent-status LEDs |
| `v.oai.rgbcfg` | `{ambient, keys}`, each `{c, b, e, s, m}` | the lighting zones — note the *short* field names |
| `lights.preview` | `{backlight, underglow}` | accepted, but see caveats |
| `host.focused_app` | app info | host tells device what's in focus |
| `ui.home_accent_color` | colour | |

`layer_index` in `device.status` **does move when you switch layers**, but
what it counts is unclear: it read `2` on the Codex layer, which sits at array
position 2 and carries id 3 (so: position), yet it also read `3` at a time when
only three layers existed, positions 0-2 (so: not a position). Use a *change*
in the value as a reliable "the user switched layers" signal; don't trust the
value itself to name the layer.

### Not implemented on v0.6.1

`fs.txbegin` and `fs.txcommit` — the atomic multi-file write transaction that
the vendor's own TypeScript uses — return `{"code": 404, "message": "Method not
found"}`. Plain `fs.writebin` works, but you lose atomicity, so **verify every
write by reading the file back** and comparing byte for byte.

## Notifications sent by the device

Unsolicited messages use `{"m": <method>, "p": <params>}`:

```json
{"m": "v.oai.hid",  "p": {"k": "AG00", "act": 1}}   // status key pressed
{"m": "v.oai.hid",  "p": {"k": "AG00", "act": 0}}   // released
{"m": "kb.cs.show", "p": {"layer": 2, "profile": 0}}
{"m": "kb.cs.hide", "p": null}
```

`v.oai.hid` is what makes "press a status key to jump to that session"
possible: the device *does* report the press. Acting on it is the hard part on
Wayland, where a third-party process cannot raise another app's window.

Here `layer` is the **array position**, unlike `device.status.layer_index`.
Both exist, and they disagree. Watch out.

## The agent-status LEDs

A key lights up as a status slot only if its keycode is `KV_OAI_AG00` …
`KV_OAI_AG05`. These are ordinary entries in `keymap.json`, so **any layer can
have them** — the feature is not tied to the vendor's "Codex" layer, and works
with or without the ChatGPT app running. That is the core finding this project
is built on.

`v.oai.thstatus` takes an array, one entry per slot:

```json
[{"id": 0, "color": 3166206, "brightness": 1, "effect": 1, "speed": 0}]
```

`id` = slot 0-5 (required), `color` packed as `0xRRGGBB`, `brightness` 0-1,
`effect` (0 off, 1 solid, 2 snake, 3 rainbow, 4 breath, 5 gradient,
6 shallow-breath), `speed` 0-1. Two more optional fields exist:
`syncKeysLighting` and `syncAmbientLighting` make the key backlight or the
outer ring follow that slot's colour.

**Spell the field names out in full.** Reverse-engineering notes (this one
included, until 0.6.2 landed) use the short forms `c`, `b`, `e`, `s`. Firmware
v0.6.1 accepted them; **0.6.2 ignores them silently** — it still answers
`{"ok":1}` and leaves the keys dark, with nothing in the debug log. If your
LEDs stop working after a firmware update, this is the first thing to check.
The long names come from the vendor's own SDK (`@worklouder/device-kit-oai`,
bundled in the ChatGPT desktop app) and work on both versions.

**The six slots are global, and you may not be the only one writing them.**
Slot 0 is the same physical LED whichever layer is showing — the firmware has
no notion of per-layer status. The device never announces a layer change
either; its only notifications are `v.oai.hid` (key press) and `v.oai.rad`
(joystick). You can still detect a switch by polling `device.status` and
watching `layer_index` change, at the cost of a poll on a battery device.

What you *can* do is share. The ChatGPT desktop app — which does exist on
Linux, at `/opt/codex-desktop/ChatGPT` — drives these same slots. Send only the
slots you own (see `set_agent_states`) and fill from the opposite end it uses,
and both programs coexist: it takes 0, 1, 2… and you take 5, 4, 3…

**The colours are not persistent — resend them.** Left alone for hours the
device drops back to its normal lighting, and it gives you no hint that it has:
`v.oai.thstatus` keeps answering `{"ok":1}` on dark keys, `device.status` looks
healthy, and the keymap on flash is untouched. One fresh call lights everything
back up. So a daemon that only writes on state *changes* will appear to break
after a quiet stretch. Ours resends every 30 s (`HEARTBEAT_S`), which also
covers the device sleeping or dropping its Bluetooth link and coming back.

## The ring, and a trap between two vendor methods

`v.oai.rgbcfg` drives the lighting *zones* rather than individual keys, and
unlike the status slots it works on any layer:

```json
{"ambient": {"c": 16739584, "b": 0.35, "e": 1, "s": 0, "m": 1},
 "keys":    {"c": 0, "b": 0, "e": 0, "s": 0, "m": 1}}
```

`ambient` is the band around the case. `keys` is the backlight of the keys no
status slot has claimed; on a layer carrying `KV_OAI_AG` keycodes — the only
kind this project runs on — it was never observed to do anything, which fits
the per-layer lighting being ignored there anyway. Either key may be left out
of the payload.

**And here is the trap: this method wants its field names ABBREVIATED**
(`c`, `b`, `e`, `s`, `m`) — the exact opposite of `v.oai.thstatus` on the very
same firmware, which wants them spelled out. Given the long names, `rgbcfg`
answers `{"ok":1}` and leaves the ring dark.

Measured on 0.6.2, alternating red-with-short-names and green-with-long-names
every five seconds: only red ever appeared. Two vendor methods, two
conventions, no error either way. There is no rule to infer here — check each
method against the hardware, and pin what you find with a test, because both
spellings look equally correct to a reader six months later.

Like the status slots, the ring is a property of the **device**: there is no
layer field to put it in, so it stays as you left it while you work on some
other layer. Whether it is forgotten after a long silence the way the slots are
has not been established; this daemon resends it on the same 30-second
heartbeat and does not find out.

### `{"ok":1}` means "I received something"

Not "I understood it". `v.oai.rgbcfg` answers `{"ok":1}` to an empty object,
and to a JSON array where it expects an object. `v.oai.thstatus` answers it for
`id` values of 6, 19 and 20 — well past the six slots the firmware is said to
have. None of that is evidence of anything.

The one thing the firmware does refuse is an unknown *method*:

```json
{"code": 404, "message": "Method not found"}
```

So a 404 tells you a method does not exist, and its absence tells you the
method does. Past that, the LEDs are the only test that means anything — which
is worth remembering before concluding a device is broken.

The [`creator-micro-kit`](https://github.com/00cyre/creator-micro-kit) project,
which documented `rgbcfg` first, lists twenty keycodes `KV_OAI_AG00`…`AG19`.
Whether this firmware paints past the sixth is untested here: finding out means
rewriting `keymap.json`, the one operation on this device that can cost you an
afternoon with a screwdriver.

## Two consequences of using `KV_OAI_` keycodes

Both are structural. You accept them or you go without live status.

1. **The layer becomes read-only in the vendor app.** Its check is literally
   `keycode.startsWith("KV_OAI_")`; on Linux it then shows "the ChatGPT app is
   not yet available on Linux, so this layer can't be edited". Write the layer
   from a script instead — the device is the source of truth anyway.
2. **Per-layer lighting is ignored.** A `lights` block on such a layer has no
   effect, and `lights.preview` doesn't override it either. The vendor's own
   Codex layer carries no `lights` block at all, which fits. Verified on
   hardware, not merely assumed.

## Config file format

The device stores `keymap.json` and `smart_actions.json` in its own flash, and
**the vendor app treats the device as the source of truth**: on connect it
reads them and overwrites its local cache. Editing the host-side cache alone
achieves nothing.

A key references either a plain keycode (`KC_A`), a macro (`KA_A<id>`), a smart
action (`SA_<n>`), or an internal function (`KI_CS_SHOW_TMP` = hold to show the
cheat sheet).

**Keep your own copy of `keymap.json`.** A firmware update can wipe a layer:
going from `v0.6.1` to `0.6.2` left the vendor's Codex layer in place by id and
name but emptied every key to `KC_NONE` and renamed it "Layer", while the two
user layers survived untouched. Nothing warns you, and the vendor app will not
put it back — it refuses to edit any layer holding `KV_OAI_` keycodes. Pull the
file with `--read` after every firmware bump, and keep a factory-state copy:
restoring is then a matter of pasting the old layer object back in.

```jsonc
{
  "id": 1, "name": "My layer", "color": 16753920,
  "layout": {
    "keymap": [["KV_OAI_AG00", "KV_OAI_AG01"], ...],
    "encoders": [["<CCW>", "<CW>", "<click>"]],
    "joystick": {"type": "JOYSTICK", "sectors": []}
  },
  "lights": {"backlight": {...}, "underglow": {...}}   // both keys or neither
}
```

Macro actions are `{"kc": "KC_A", "delay": 0, "act": 2}` where `act` is
`1` press, `2` tap, `0` release, and `delay` is **milliseconds**.

### Two ways to brick your config

**A `lights` block must contain both `backlight` and `underglow`.** With only
one, the app throws `Cannot read properties of undefined (reading 'effect')`,
fails to load, and **deletes its local cache**. The device file stays fine.

**There is a size ceiling of roughly 6.1 KB for `keymap.json`.** It is
documented nowhere. Past it, `fs.writebin` truncates *silently* — it reports
success while writing fewer bytes. On this hardware, a 6362-byte write got
truncated at 6106, and a follow-up write **froze the firmware entirely**:
Bluetooth still connected and the battery still reported, but the RPC service
was dead and the layer key stopped responding. Recovery required the hardware
reset button under the four corner screws.

**So: keep the file well under 5.5 KB, and read back every write.** Text macros
are what blows the budget — roughly 550 bytes each for a dozen keystrokes.

## Smart actions, and why they mostly don't work on Linux

`smart_actions.json` holds `TEXT_STEP`, `CMD_STEP`, `URL_STEP` and `APP_STEP`.
Two independent walls on Linux:

- `CMD_STEP` is refused unless the user ticks a consent box in the app. The log
  says `user has not given consent to run cmd from device`. It is a deliberate
  safety gate: without it, the keypad could run any shell command.
- `TEXT_STEP` calls `xdotool`. Even installed, xdotool cannot type into a
  native Wayland window. The log says
  `insertText requires xdotool, which is not installed`.

**Use keyboard macros instead.** They are injected as real HID keystrokes by
the device itself, so they are indifferent to Wayland, X11, or which app has
focus — and they need no vendor app running.

## Keycodes are physical positions, not letters

HID keycodes name a **position on a US QWERTY board**. On any other layout the
letter that comes out is different. On a Belgian/French AZERTY keyboard:

| you want | send | because |
|---|---|---|
| `q` | `KC_A` | QWERTY's A position |
| `a` | `KC_Q` | QWERTY's Q position |
| `w` | `KC_Z` | |
| `z` | `KC_W` | |
| `m` | `KC_SCLN` | |
| `/` | `Shift` + `KC_DOT` | |
| `-` | `KC_EQL` | |

Check `/usr/share/X11/xkb/symbols/<layout>` rather than trusting memory.
`src/azerty_be.py` has the full table used here.

Names follow QMK's short form: `KC_SCLN`, `KC_DOT`, `KC_EQL`, `KC_SPC`,
`KC_ENT`. The long forms (`KC_SEMICOLON`, …) do not exist in this firmware.
