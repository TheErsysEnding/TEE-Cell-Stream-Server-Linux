"""USB HID usage codes -> Linux evdev key codes.

The PS3 reads its USB keyboard in RAW mode (CELL_KB_CODETYPE_RAW), which hands out the usage codes
from the HID Keyboard/Keypad page exactly as the keyboard's firmware sends them. Those describe a key
by its POSITION, not by the character on it - usage 0x1C is "the key where a US keyboard has Y", which
on a German keyboard is Z.

That is precisely what we want. The server types through a virtual uinput keyboard, and a uinput
keyboard emits positions too: whatever layout the PC is set to then decides which character comes out.
So the console must not translate anything - it sends positions, the PC applies its own layout, and
umlauts, Y/Z and AltGr all land where the person at the PC expects them.

Translating on the console instead was the alternative, and it cannot work: the PS3 would apply ITS
keyboard layout (the one set in the XMB) and send finished characters, and the PC would then have to
undo that mapping to find a key to press. Two layouts, one of them wrong.

The table is written with evdev's own names rather than numbers. A typo in a name raises at start-up;
a typo in a number would silently press a different key.
"""

# HID usage (Keyboard/Keypad page 0x07) -> the evdev name of the key in that position
HID_TO_KEY_NAME: dict[int, str] = {}

# letters: usage 0x04..0x1D are a..z, in alphabetical order
for _index, _letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    HID_TO_KEY_NAME[0x04 + _index] = "KEY_" + _letter

# digits: 0x1E..0x26 are 1..9, and 0x27 is 0 - the row order, not the numeric order
for _index in range(9):
    HID_TO_KEY_NAME[0x1E + _index] = "KEY_%d" % (_index + 1)
HID_TO_KEY_NAME[0x27] = "KEY_0"

# F1..F12: 0x3A..0x45
for _index in range(12):
    HID_TO_KEY_NAME[0x3A + _index] = "KEY_F%d" % (_index + 1)

HID_TO_KEY_NAME.update({
    0x28: "KEY_ENTER",      0x29: "KEY_ESC",         0x2A: "KEY_BACKSPACE",  0x2B: "KEY_TAB",
    0x2C: "KEY_SPACE",      0x2D: "KEY_MINUS",       0x2E: "KEY_EQUAL",      0x2F: "KEY_LEFTBRACE",
    0x30: "KEY_RIGHTBRACE", 0x31: "KEY_BACKSLASH",   0x32: "KEY_BACKSLASH",  0x33: "KEY_SEMICOLON",
    0x34: "KEY_APOSTROPHE", 0x35: "KEY_GRAVE",       0x36: "KEY_COMMA",      0x37: "KEY_DOT",
    0x38: "KEY_SLASH",      0x39: "KEY_CAPSLOCK",

    0x46: "KEY_SYSRQ",      0x47: "KEY_SCROLLLOCK",  0x48: "KEY_PAUSE",      0x49: "KEY_INSERT",
    0x4A: "KEY_HOME",       0x4B: "KEY_PAGEUP",      0x4C: "KEY_DELETE",     0x4D: "KEY_END",
    0x4E: "KEY_PAGEDOWN",   0x4F: "KEY_RIGHT",       0x50: "KEY_LEFT",       0x51: "KEY_DOWN",
    0x52: "KEY_UP",

    0x53: "KEY_NUMLOCK",    0x54: "KEY_KPSLASH",     0x55: "KEY_KPASTERISK", 0x56: "KEY_KPMINUS",
    0x57: "KEY_KPPLUS",     0x58: "KEY_KPENTER",
    0x59: "KEY_KP1", 0x5A: "KEY_KP2", 0x5B: "KEY_KP3", 0x5C: "KEY_KP4", 0x5D: "KEY_KP5",
    0x5E: "KEY_KP6", 0x5F: "KEY_KP7", 0x60: "KEY_KP8", 0x61: "KEY_KP9", 0x62: "KEY_KP0",
    0x63: "KEY_KPDOT",

    # the extra key European keyboards have and US ones do not - "<>|" on a German board. Without this
    # one entry a German keyboard is missing a key and nobody can tell why
    0x64: "KEY_102ND",
    0x65: "KEY_COMPOSE",    0x66: "KEY_POWER",       0x67: "KEY_KPEQUAL",

    0xE0: "KEY_LEFTCTRL",   0xE1: "KEY_LEFTSHIFT",   0xE2: "KEY_LEFTALT",    0xE3: "KEY_LEFTMETA",
    0xE4: "KEY_RIGHTCTRL",  0xE5: "KEY_RIGHTSHIFT",  0xE6: "KEY_RIGHTALT",   0xE7: "KEY_RIGHTMETA",
})

# The modifier bits the PS3 sends alongside (CELL_KB_MKEY_*), in the order of the HID modifier byte.
# The console reports these separately from the key list, exactly as a HID report does.
MODIFIER_BITS: tuple[tuple[int, str], ...] = (
    (1 << 0, "KEY_LEFTCTRL"),  (1 << 1, "KEY_LEFTSHIFT"),  (1 << 2, "KEY_LEFTALT"),  (1 << 3, "KEY_LEFTMETA"),
    (1 << 4, "KEY_RIGHTCTRL"), (1 << 5, "KEY_RIGHTSHIFT"), (1 << 6, "KEY_RIGHTALT"), (1 << 7, "KEY_RIGHTMETA"),
)

del _index, _letter


def resolve(names) -> dict:
    """{hid usage or bit -> evdev code} for one of the tables above, using evdev's own names.

    Kept separate from the tables so the module imports on a PC without python3-evdev; the input path
    is a no-op there anyway.
    """
    from evdev import ecodes
    return {key: ecodes.ecodes[name] for key, name in names}
