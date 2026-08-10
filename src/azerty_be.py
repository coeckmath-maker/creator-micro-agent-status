#!/usr/bin/env python3
"""Type real text from the macropad on a non-US keyboard layout.

HID keycodes name a **physical position on a US QWERTY board**, not a letter.
Send `KC_Q` to a Belgian AZERTY desktop and you get `a`, not `q`. This bites
anyone outside the US, and it is the reason a "type /effort high" macro can
come out as gibberish.

The table below was checked line by line against
/usr/share/X11/xkb/symbols/be — not from memory. Do the same for your own
layout: find the `key <XX99>` lines and read off which character each physical
position produces.

    key <AC01> {[ q, Q, ... ]}   ->  QWERTY's A position types 'q'
    key <AD01> {[ a, A, ... ]}   ->  QWERTY's Q position types 'a'
    key <AB09> {[ colon, slash ]} -> QWERTY's . position, shifted, types '/'

Keycode names use QMK's short forms (KC_SCLN, KC_DOT, KC_EQL, KC_SPC, KC_ENT).
The long forms do not exist in this firmware.
"""

# character -> (keycode, needs shift)
AZERTY_BE = {
    # top letter row: AZERTY a z e r t y u i o p
    "a": ("KC_Q", False), "z": ("KC_W", False), "e": ("KC_E", False),
    "r": ("KC_R", False), "t": ("KC_T", False), "y": ("KC_Y", False),
    "u": ("KC_U", False), "i": ("KC_I", False), "o": ("KC_O", False),
    "p": ("KC_P", False),
    # home row: AZERTY q s d f g h j k l m
    "q": ("KC_A", False), "s": ("KC_S", False), "d": ("KC_D", False),
    "f": ("KC_F", False), "g": ("KC_G", False), "h": ("KC_H", False),
    "j": ("KC_J", False), "k": ("KC_K", False), "l": ("KC_L", False),
    "m": ("KC_SCLN", False),
    # bottom row: AZERTY w x c v b n
    "w": ("KC_Z", False), "x": ("KC_X", False), "c": ("KC_C", False),
    "v": ("KC_V", False), "b": ("KC_B", False), "n": ("KC_N", False),
    # punctuation
    " ": ("KC_SPC", False),
    "/": ("KC_DOT", True),    # <AB09>: colon unshifted, slash shifted
    "-": ("KC_EQL", False),   # <AE12>: minus
}

KEY_DELAY = 8  # ms between keystrokes; the `delay` field is milliseconds


def text_macro(macro_id, name, text, layout=AZERTY_BE, enter=False):
    """Build a macro that types `text`.

    `enter` defaults to False on purpose. If the prompt already contains text,
    a leading "/" is no longer a command, and an automatic Enter sends a
    half-written message. Let the human press Enter.
    """
    actions = []
    for ch in text:
        keycode, shifted = layout[ch]
        if shifted:
            actions.append({"kc": "KC_LSFT", "delay": KEY_DELAY, "act": 1})
        actions.append({"kc": keycode, "delay": KEY_DELAY, "act": 2})
        if shifted:
            actions.append({"kc": "KC_LSFT", "delay": KEY_DELAY, "act": 0})
    if enter:
        actions.append({"kc": "KC_ENT", "delay": KEY_DELAY, "act": 2})
    return {"id": macro_id, "name": name, "color": None, "actions": actions}


def chord(macro_id, name, mods, key, delay=20):
    """Build a modifier chord, e.g. chord(1, 'Dictate', ['KC_LGUI','KC_LSFT'], 'KC_A').

    Modifiers get a slightly longer delay: desktop environments want them
    firmly held before the key arrives, or the global shortcut misfires.
    """
    actions = [{"kc": m, "delay": delay, "act": 1} for m in mods]
    actions.append({"kc": key, "delay": delay + 10, "act": 2})
    actions += [{"kc": m, "delay": delay, "act": 0} for m in reversed(mods)]
    return {"id": macro_id, "name": name, "color": None, "actions": actions}


if __name__ == "__main__":
    import json
    print(json.dumps(text_macro(12, "Effort low", "/effort low"), indent=2))
    print(json.dumps(chord(11, "Dictate", ["KC_LGUI", "KC_LSFT"], "KC_A"), indent=2))
