"""Drives the PC's mouse and keyboard from the PS3 pad (port of DesktopInput.cs).

Windows let any program synthesize input (SendInput); on Linux the equivalent is a virtual mouse and a
virtual keyboard on /dev/uinput, which the compositor treats like real hardware. It is NOT a gamepad -
a game that wants a controller sees nothing here; that is what VirtualGamepad is for.

typing is the on-screen keyboard's job - its characters arrive separately as KEY packets
(type_character). Windows injected them as Unicode, layout be damned; uinput only knows key codes, so
each character is looked up in the user's keyboard layout with libxkbcommon (see KeyTable).

mapping, deliberately plain; change it here:
  left stick   mouse pointer          right stick   scroll
  cross        left click             circle        right click        square   middle click
  d-pad        arrow keys             start         Super key
  triangle     shows/hides the PS3's on-screen keyboard, so it never reaches the PC
every other button does nothing here.
"""

import ast
import ctypes
import ctypes.util
import os
import subprocess
import threading
import time

try:
    import evdev
except ImportError:
    evdev = None

from . import log
from .protocol import PadBits
from .virtual_gamepad import EV_KEY, node_path, open_uinput

# the sticks rest slightly off centre once a pad has some age on it (the PS3's reads -6 at rest), and
# without a dead zone that drifts the pointer across the screen on its own
STICK_DEAD_ZONE = 16

# pointer and scroll speeds are per SECOND, not per packet: the pad's send rate can vary, so moving by
# elapsed time keeps the speed steady and the motion smooth however the packets are spaced. the stick
# reads up to ~112 once the dead zone is off it. full tilt crosses the screen in ~1.8s; tune these two.
STICK_FULL_TILT = 112.0
POINTER_PIXELS_PER_SECOND_AT_FULL_TILT = 880.0
SCROLL_NOTCHES_PER_SECOND_AT_FULL_TILT = 42.0

POINTER_SPEED = POINTER_PIXELS_PER_SECOND_AT_FULL_TILT / (STICK_FULL_TILT * STICK_FULL_TILT)
SCROLL_SPEED = SCROLL_NOTCHES_PER_SECOND_AT_FULL_TILT / STICK_FULL_TILT
WHEEL_NOTCH_HI_RES = 120     # one detent, as REL_WHEEL_HI_RES counts them (same unit Windows used)
MAX_ELAPSED_S = 0.05         # a gap between packets (or the first one) must not lurch the pointer

# event codes from linux/input-event-codes.h (EV_KEY comes from virtual_gamepad)
EV_REL = 0x02
REL_X, REL_Y, REL_WHEEL, REL_WHEEL_HI_RES = 0x00, 0x01, 0x08, 0x0B
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
KEY_BACKSPACE, KEY_TAB, KEY_ENTER, KEY_SPACE = 14, 15, 28, 57
KEY_LEFTSHIFT, KEY_RIGHTALT = 42, 100      # Shift, and AltGr (ISO_Level3_Shift on every stock layout)
KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_DOWN = 103, 105, 106, 108
KEY_LEFTMETA = 125                          # Super - the Windows key's seat
KEY_MAX = 0x2FF
MAIN_KEY_CODE_MAX = 127                     # everything a keyboard layout puts a character on lives below this
XKB_KEYCODE_OFFSET = 8                      # xkb keycode = evdev code + 8
XKB_LOG_LEVEL_CRITICAL = 10                 # enum xkb_log_level: only messages at or above this level are printed
GSETTINGS_TIMEOUT_S = 1.0                   # see _gsettings_get: this can end up on the receive thread
LEVELS_SUPPORTED = 4                        # 0 plain, 1 Shift, 2 AltGr, 3 Shift+AltGr

# a dead key types no character of its own, and on the stock 'de' layout ^ and ` exist ONLY as dead keys
# (Windows typed them fine: it injected Unicode, no key needed). Every toolkit's compose table turns
# <dead_X> <space> into the plain accent (X11 Compose: dead_acute + space = apostrophe ...), so those
# characters are typed as the dead key followed by a space when the layout has no direct key for them.
DEAD_KEY_PLAIN_CHARS = {0xFE50: "`", 0xFE51: "'", 0xFE52: "^", 0xFE53: "~", 0xFE57: '"'}   # dead_grave, dead_acute, dead_circumflex, dead_tilde, dead_diaeresis

BUS_VIRTUAL = 0x06
MOUSE_NAME = "TEE Cell Stream Mouse"
KEYBOARD_NAME = "TEE Cell Stream Keyboard"
VENDOR_ID = 0x5445                          # 'TE'; nothing real
MOUSE_PRODUCT_ID, KEYBOARD_PRODUCT_ID = 0x0001, 0x0002

# one row per pad button that presses a key
KEY_BINDINGS = (
    (PadBits.UP, KEY_UP), (PadBits.DOWN, KEY_DOWN), (PadBits.LEFT, KEY_LEFT), (PadBits.RIGHT, KEY_RIGHT),
    (PadBits.START, KEY_LEFTMETA),
)

# one row per pad button that clicks a mouse button
CLICK_BINDINGS = ((PadBits.CROSS, BTN_LEFT), (PadBits.CIRCLE, BTN_RIGHT), (PadBits.SQUARE, BTN_MIDDLE))


# ---------------------------------------------------------------------------- character -> key code

class KeyTable:
    """character -> (evdev key code, level) for one keyboard layout.

    level 0 is the plain key, 1 needs Shift, 2 needs AltGr, 3 both. Built once and kept.
    """

    def __init__(self, layout: str, variant: str | None, source: str):
        self.layout = layout
        self.variant = variant
        self.source = source                 # "libxkbcommon" or "us-fallback"
        self._by_keysym: dict[int, tuple[int, int]] = {}
        self._by_char: dict[str, tuple[int, int]] = {}
        self._dead: dict[str, tuple[int, int]] = {}      # plain accent -> the dead key that composes it with a space
        self._utf32_to_keysym = None

    def describe(self) -> str:
        return self.layout + ("(%s)" % self.variant if self.variant else "") + ", " + self.source

    @staticmethod
    def _rank(code: int, level: int) -> tuple[int, int]:
        """Smaller is better. An ordinary key beats an extra key, then the fewest modifiers wins.

        Both halves are needed. Fewest modifiers alone would take the keypad's '1' over the number row;
        ordinary keys alone would take Shift+7 over the plain '/'. And the extra keys must lose even at
        level 0: the stock layouts put '(' ')' on KEY_KPLEFTPAREN/KPRIGHTPAREN and '$' '€' on
        KEY_DOLLAR/KEY_EURO (codes 434/435 = xkb keycodes 442/443), and X11 keycodes stop at 255 - an
        Xwayland client can never be told about those two, so '$' and '€' would type nothing there.
        """
        return (1 if code > MAIN_KEY_CODE_MAX else 0, level)

    def _add(self, keysym: int, codepoint: int, code: int, level: int) -> None:
        rank = self._rank(code, level)
        if keysym and (keysym not in self._by_keysym or self._rank(*self._by_keysym[keysym]) > rank):
            self._by_keysym[keysym] = (code, level)
        character = chr(codepoint) if codepoint else ""
        if character and (character not in self._by_char or self._rank(*self._by_char[character]) > rank):
            self._by_char[character] = (code, level)
        plain = DEAD_KEY_PLAIN_CHARS.get(keysym)
        if plain is not None and (plain not in self._dead or self._rank(*self._dead[plain]) > rank):
            self._dead[plain] = (code, level)

    def lookup(self, character: str) -> tuple[int, int] | None:
        entry = None
        if self._utf32_to_keysym is not None:
            entry = self._by_keysym.get(self._utf32_to_keysym(ord(character)))
        if entry is None:
            entry = self._by_char.get(character)
        return entry

    def lookup_dead(self, character: str) -> tuple[int, int] | None:
        """The dead key that, followed by a space, composes this character. Only for what lookup() lacks."""
        return self._dead.get(character)


class _RuleNames(ctypes.Structure):
    _fields_ = [("rules", ctypes.c_char_p), ("model", ctypes.c_char_p), ("layout", ctypes.c_char_p),
                ("variant", ctypes.c_char_p), ("options", ctypes.c_char_p)]


_xkb_lib = None
_xkb_tried = False
_tables: dict[tuple[str, str | None], KeyTable] = {}
_tables_gate = threading.Lock()


def _xkbcommon():
    """libxkbcommon with its signatures declared, or None when it is not installed."""
    global _xkb_lib, _xkb_tried
    with _tables_gate:
        if _xkb_tried:
            return _xkb_lib
        _xkb_tried = True
        try:
            try:
                lib = ctypes.CDLL("libxkbcommon.so.0")
            except OSError:
                lib = ctypes.CDLL(ctypes.util.find_library("xkbcommon") or "libxkbcommon.so")
            u32, ptr = ctypes.c_uint32, ctypes.c_void_p
            lib.xkb_context_new.restype, lib.xkb_context_new.argtypes = ptr, [ctypes.c_int]
            lib.xkb_context_unref.restype, lib.xkb_context_unref.argtypes = None, [ptr]
            lib.xkb_keymap_new_from_names.restype = ptr
            lib.xkb_keymap_new_from_names.argtypes = [ptr, ctypes.POINTER(_RuleNames), ctypes.c_int]
            lib.xkb_keymap_unref.restype, lib.xkb_keymap_unref.argtypes = None, [ptr]
            lib.xkb_keymap_min_keycode.restype, lib.xkb_keymap_min_keycode.argtypes = u32, [ptr]
            lib.xkb_keymap_max_keycode.restype, lib.xkb_keymap_max_keycode.argtypes = u32, [ptr]
            lib.xkb_keymap_num_layouts_for_key.restype, lib.xkb_keymap_num_layouts_for_key.argtypes = u32, [ptr, u32]
            lib.xkb_keymap_num_levels_for_key.restype = u32
            lib.xkb_keymap_num_levels_for_key.argtypes = [ptr, u32, u32]
            lib.xkb_keymap_key_get_syms_by_level.restype = ctypes.c_int
            lib.xkb_keymap_key_get_syms_by_level.argtypes = [ptr, u32, u32, u32, ctypes.POINTER(ctypes.POINTER(u32))]
            lib.xkb_utf32_to_keysym.restype, lib.xkb_utf32_to_keysym.argtypes = u32, [u32]
            lib.xkb_keysym_to_utf32.restype, lib.xkb_keysym_to_utf32.argtypes = u32, [u32]
            try:
                lib.xkb_context_set_log_level.restype = None
                lib.xkb_context_set_log_level.argtypes = [ptr, ctypes.c_int]
            except AttributeError:
                pass   # ancient library: it will just be chattier on stderr
            _xkb_lib = lib
        except (OSError, AttributeError):
            _xkb_lib = None
        return _xkb_lib


def have_xkbcommon() -> bool:
    return _xkbcommon() is not None


def parse_input_source_list(text: str) -> list[tuple[str, str | None]]:
    """The xkb entries of `gsettings get org.gnome.desktop.input-sources sources`, as (layout, variant), in order.

    gsettings prints GVariant text, e.g. "[('xkb', 'de'), ('xkb', 'us+altgr-intl'), ('ibus', 'mozc-jp')]",
    which happens to be Python literal syntax. An empty list prints typed: "@a(ss) []".
    """
    text = text.strip()
    if text.startswith("@"):
        text = text.partition(" ")[2]
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return []
    if not isinstance(value, (list, tuple)):
        return []
    layouts = []
    for entry in value:
        if isinstance(entry, tuple) and len(entry) == 2 and entry[0] == "xkb" and isinstance(entry[1], str) and entry[1]:
            layout, _plus, variant = entry[1].partition("+")
            layouts.append((layout, variant or None))
    return layouts


def parse_input_sources(text: str) -> tuple[str, str | None] | None:
    """The first xkb entry of a gsettings input-sources list, or None."""
    layouts = parse_input_source_list(text)
    return layouts[0] if layouts else None


def _gsettings_get(key: str) -> str:
    """`gsettings get org.gnome.desktop.input-sources <key>`; "" when gsettings is missing or unhappy.

    The timeout is short on purpose: worst case this runs on the server's receive thread (a KEY packet
    with no table built yet), and detect_layout asks twice. The watchdog calls the PS3 gone after
    CLIENT_TIMEOUT_MS = 3000 ms of no pad, so two hung calls must stay well under that. Measured here:
    3 ms per call.
    """
    try:
        result = subprocess.run(["gsettings", "get", "org.gnome.desktop.input-sources", key],
                                capture_output=True, text=True, timeout=GSETTINGS_TIMEOUT_S,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return result.stdout


def detect_layout() -> tuple[str, str | None]:
    """GNOME keeps the layouts in gsettings, not in the X-style environment; try that first.

    `sources` lists them in the user's order; a user with several switches between them with Super+Space,
    and gnome-shell then rewrites `mru-sources` with the active one first. Typing in the wrong one of two
    layouts swaps letters silently, so the active one wins - as long as it is still configured.
    """
    configured = parse_input_source_list(_gsettings_get("sources"))
    if configured:
        for recent in parse_input_source_list(_gsettings_get("mru-sources")):
            if recent in configured:
                return recent
        return configured[0]
    layout = (os.environ.get("XKB_DEFAULT_LAYOUT") or "").split(",")[0].strip()
    if layout:
        variant = (os.environ.get("XKB_DEFAULT_VARIANT") or "").split(",")[0].strip()
        return layout, (variant or None)
    return "us", None


def build_key_table(layout: str, variant: str | None = None) -> KeyTable:
    """Walks the whole keymap once: every key code, levels 0..3 of layout 0, and notes which character
    each produces. Raises OSError when libxkbcommon is missing or the layout does not compile."""
    lib = _xkbcommon()
    if lib is None:
        raise OSError("libxkbcommon fehlt")
    context = lib.xkb_context_new(0)
    if not context:
        raise OSError("xkb_context_new fehlgeschlagen")
    if hasattr(lib, "xkb_context_set_log_level"):
        # a layout that does not compile is reported through our own log; the library's own stderr
        # commentary (a dozen lines per attempt) would only land in the journal
        lib.xkb_context_set_log_level(context, XKB_LOG_LEVEL_CRITICAL)
    try:
        names = _RuleNames(None, None, layout.encode("utf-8"), variant.encode("utf-8") if variant else None, None)
        keymap = lib.xkb_keymap_new_from_names(context, ctypes.byref(names), 0)
        if not keymap:
            raise OSError("Layout %r%s lässt sich nicht übersetzen" % (layout, "(%s)" % variant if variant else ""))
        try:
            table = KeyTable(layout, variant, "libxkbcommon")
            syms = ctypes.POINTER(ctypes.c_uint32)()
            first = max(lib.xkb_keymap_min_keycode(keymap), XKB_KEYCODE_OFFSET)
            last = min(lib.xkb_keymap_max_keycode(keymap), KEY_MAX + XKB_KEYCODE_OFFSET)
            for keycode in range(first, last + 1):
                if lib.xkb_keymap_num_layouts_for_key(keymap, keycode) == 0:
                    continue
                levels = min(lib.xkb_keymap_num_levels_for_key(keymap, keycode, 0), LEVELS_SUPPORTED)
                for level in range(levels):
                    if lib.xkb_keymap_key_get_syms_by_level(keymap, keycode, 0, level, ctypes.byref(syms)) < 1:
                        continue
                    keysym = syms[0]
                    table._add(keysym, lib.xkb_keysym_to_utf32(keysym), keycode - XKB_KEYCODE_OFFSET, level)
            table._utf32_to_keysym = lib.xkb_utf32_to_keysym
            return table
        finally:
            lib.xkb_keymap_unref(keymap)
    finally:
        lib.xkb_context_unref(context)


def us_fallback_table() -> KeyTable:
    """Without libxkbcommon: a plain US layout, ASCII only. Better than typing nothing."""
    table = KeyTable("us", None, "us-fallback")
    plain = {
        "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11, "-": 12, "=": 13,
        "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25, "[": 26, "]": 27,
        "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38, ";": 39, "'": 40, "`": 41,
        "\\": 43, "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50, ",": 51, ".": 52, "/": 53, " ": 57,
    }
    shifted = {
        "!": 2, "@": 3, "#": 4, "$": 5, "%": 6, "^": 7, "&": 8, "*": 9, "(": 10, ")": 11, "_": 12, "+": 13,
        "{": 26, "}": 27, ":": 39, '"': 40, "~": 41, "|": 43, "<": 51, ">": 52, "?": 53,
    }
    for character, code in plain.items():
        table._add(0, ord(character), code, 0)
        if character.isalpha():
            table._add(0, ord(character.upper()), code, 1)
    for character, code in shifted.items():
        table._add(0, ord(character), code, 1)
    return table


def get_key_table(layout: str | None = None, variant: str | None = None) -> KeyTable:
    """The (cached) table for a layout; None = the user's current one. Never raises: falls back to US."""
    if layout is None:
        layout, variant = detect_layout()
    key = (layout, variant)
    with _tables_gate:
        cached = _tables.get(key)
    if cached is not None:
        return cached
    complaint = None
    try:
        table = build_key_table(layout, variant)
    except OSError as error:
        if have_xkbcommon() and layout != "us":
            try:
                table = build_key_table("us", None)   # the layout name was the problem, not the library
            except OSError:
                table = us_fallback_table()
        else:
            table = us_fallback_table()
        complaint = "pad: Tastaturlayout %s nicht ladbar (%s) - nehme %s" % (layout, error, table.describe())
    with _tables_gate:
        # the warm-up thread and the first KEY packet can both land here; only whoever fills the cache says so
        first = key not in _tables
        _tables.setdefault(key, table)
        table = _tables[key]
    if complaint is not None and first:
        log.write(complaint)
    return table


# ---------------------------------------------------------------------------- the devices

def _keyboard_codes() -> list[int]:
    """Every KEY_* code (not the BTN_* ones - those would make udev call the keyboard a mouse).

    Up to and including KEY_MAX, which is exactly the range build_key_table can hand back: the kernel
    silently drops an event whose code the device never declared, so a gap here would type nothing.
    """
    return sorted(code for code in evdev.ecodes.KEY if 0 < code <= KEY_MAX)


def open_uinput_devices():
    """The virtual mouse and keyboard. Raises with a readable reason when uinput is not available."""
    if evdev is None:
        raise OSError("python3-evdev fehlt")
    mouse = open_uinput({EV_KEY: [BTN_LEFT, BTN_RIGHT, BTN_MIDDLE], EV_REL: [REL_X, REL_Y, REL_WHEEL, REL_WHEEL_HI_RES]},
                        MOUSE_NAME, VENDOR_ID, MOUSE_PRODUCT_ID, 1, BUS_VIRTUAL)
    try:
        keyboard = open_uinput({EV_KEY: _keyboard_codes()}, KEYBOARD_NAME, VENDOR_ID, KEYBOARD_PRODUCT_ID, 1, BUS_VIRTUAL)
    except Exception:
        mouse.close()
        raise
    return mouse, keyboard


class DesktopInput:
    """apply() the pad state 60 times a second; type_character() for the on-screen keyboard.

    The devices are created on first use, so a PC that only ever plays games never grows a virtual
    mouse. If uinput is not available that is logged once and everything here becomes a no-op.
    `clock` and `open_devices` exist for the tests (a fake clock makes the elapsed-time maths exact,
    fake devices record what would have been emitted).
    """

    def __init__(self, *, layout: str | None = None, variant: str | None = None,
                 clock=time.monotonic, open_devices=open_uinput_devices):
        self._gate = threading.RLock()
        self._clock = clock
        self._started = clock()
        self._last_apply_s = 0.0
        self._open_devices = open_devices
        self._mouse = None
        self._keyboard = None
        self._open_failed = False
        self._closed = False
        self._write_failed = False
        self._layout = layout
        self._variant = variant
        self._table: KeyTable | None = None
        self._last_buttons = 0
        self._pointer_carry_x = self._pointer_carry_y = self._scroll_carry = 0.0   # sub-pixel remainders, so slow moves still move
        self._unknown_logged: set[str] = set()

    @property
    def is_open(self) -> bool:
        return self._mouse is not None

    @property
    def device_paths(self) -> tuple[str | None, str | None]:
        """(/dev/input/event* of the mouse, of the keyboard) - for the log and the tests."""
        if self._mouse is None:
            return None, None
        return node_path(self._mouse), node_path(self._keyboard)

    def _ensure_open(self) -> bool:
        if self._mouse is not None:
            return True
        if self._closed or self._open_failed:
            return False
        try:
            self._mouse, self._keyboard = self._open_devices()
        except Exception as error:   # noqa: BLE001 - whatever uinput's complaint is, say it once and carry on
            self._open_failed = True
            log.write("pad: uinput nicht verfügbar (%s) - Maus/Tastatur-Steuerung aus" % error)
            return False
        self._write_failed = False
        paths = self.device_paths
        log.write("pad: Maus und Tastatur (uinput) angelegt" +
                  (" (%s, %s)" % paths if paths[0] and paths[1] else ""))
        # the layout table costs a gsettings call and a keymap compile (measured 5 ms + 4 ms). Build it
        # now, beside the packet path, so the first KEY packet - which arrives on the receive thread,
        # holding two locks - finds it in the cache instead of shelling out there.
        threading.Thread(target=self._prewarm_key_table, name="keytable", daemon=True).start()
        return True

    def _prewarm_key_table(self) -> None:
        try:
            get_key_table(self._layout, self._variant)
        except Exception:   # noqa: BLE001 - a warm-up must never take a thread down with it
            pass

    def _emit(self, device, etype: int, code: int, value: int, more=()) -> None:
        """One event frame: the event (plus any companions) and a SYN_REPORT."""
        try:
            device.write(etype, code, value)
            for extra_type, extra_code, extra_value in more:
                device.write(extra_type, extra_code, extra_value)
            device.syn()
        except OSError as error:
            if not self._write_failed:
                self._write_failed = True
                log.write("pad: Eingabe an uinput fehlgeschlagen: %s" % error)

    def apply(self, buttons: int, left_x: int, left_y: int, right_x: int, right_y: int) -> None:
        with self._gate:
            if not self._ensure_open():
                return
            self._apply_locked(buttons, left_x, left_y, right_x, right_y)

    def _apply_locked(self, buttons, left_x, left_y, right_x, right_y) -> None:
        # seconds since the previous packet, capped so a gap (or the first packet) can't lurch the pointer
        now = self._clock() - self._started
        elapsed = now - self._last_apply_s
        self._last_apply_s = now
        elapsed = max(0.0, min(MAX_ELAPSED_S, elapsed))

        self._move_pointer(left_x, left_y, elapsed)
        self._scroll(right_y, elapsed)

        pressed = buttons & ~self._last_buttons
        released = self._last_buttons & ~buttons
        for bit, code in CLICK_BINDINGS:
            if pressed & (1 << bit):
                self._emit(self._mouse, EV_KEY, code, 1)
            if released & (1 << bit):
                self._emit(self._mouse, EV_KEY, code, 0)
        for bit, code in KEY_BINDINGS:
            if pressed & (1 << bit):
                self._emit(self._keyboard, EV_KEY, code, 1)
            if released & (1 << bit):
                self._emit(self._keyboard, EV_KEY, code, 0)
        self._last_buttons = buttons

    # releases anything still held, so nothing is left stuck down when the stream ends
    def release_all(self) -> None:
        with self._gate:
            if self._mouse is None:
                # nothing can be held on devices that were never created - and do not create them for this
                self._last_buttons = 0
                self._pointer_carry_x = self._pointer_carry_y = self._scroll_carry = 0.0
                return
            self._apply_locked(0, 0, 0, 0, 0)

    # types one character from the PS3's on-screen keyboard. control keys map to real keys; every
    # other character goes through the layout table.
    def type_character(self, character: str) -> None:
        with self._gate:
            if not character or not self._ensure_open():
                return
            character = character[0]
            if character == "\b":
                self._tap(KEY_BACKSPACE)
            elif character == "\t":
                self._tap(KEY_TAB)
            elif character == "\n":
                self._tap(KEY_ENTER)
            else:
                self._type_via_layout(character)

    def _type_via_layout(self, character: str) -> None:
        if self._table is None:
            self._table = get_key_table(self._layout, self._variant)
            log.write("pad: Tastaturlayout " + self._table.describe())
        entry = self._table.lookup(character)
        via_dead_key = entry is None
        if via_dead_key:
            entry = self._table.lookup_dead(character)
        if entry is None:
            if character not in self._unknown_logged:
                self._unknown_logged.add(character)
                log.write("pad: kein Tastencode für %r im Layout %s - ignoriert" % (character, self._table.layout))
            return
        code, level = entry
        modifiers = []
        if level & 1:
            modifiers.append(KEY_LEFTSHIFT)
        if level & 2:
            modifiers.append(KEY_RIGHTALT)
        for modifier in modifiers:
            self._emit(self._keyboard, EV_KEY, modifier, 1)
        self._tap(code)
        for modifier in reversed(modifiers):
            self._emit(self._keyboard, EV_KEY, modifier, 0)
        if via_dead_key:
            # the space that completes the compose sequence - after the modifiers are up: AltGr+space is a
            # no-break space on 'de', and dead key + no-break space composes to the accent, not the character
            self._tap(KEY_SPACE)

    def _tap(self, code: int) -> None:
        self._emit(self._keyboard, EV_KEY, code, 1)
        self._emit(self._keyboard, EV_KEY, code, 0)

    # squared response: small stick movements stay slow and precise, big ones move fast. a linear
    # pointer is either too slow to cross the screen or too twitchy to hit anything.
    def _move_pointer(self, stick_x: int, stick_y: int, elapsed: float) -> None:
        x, y = apply_dead_zone(stick_x), apply_dead_zone(stick_y)
        if x == 0 and y == 0:
            return
        self._pointer_carry_x += x * abs(x) * POINTER_SPEED * elapsed
        self._pointer_carry_y += y * abs(y) * POINTER_SPEED * elapsed
        move_x, move_y = int(self._pointer_carry_x), int(self._pointer_carry_y)   # toward zero, like the (int) cast
        self._pointer_carry_x -= move_x
        self._pointer_carry_y -= move_y
        if move_x != 0 or move_y != 0:
            events = [(EV_REL, REL_X, move_x)] if move_x else []
            if move_y:
                events.append((EV_REL, REL_Y, move_y))
            self._emit(self._mouse, *events[0], more=events[1:])

    def _scroll(self, stick_y: int, elapsed: float) -> None:
        y = apply_dead_zone(stick_y)
        if y == 0:
            self._scroll_carry = 0.0
            return
        self._scroll_carry -= y * SCROLL_SPEED * elapsed   # stick down (positive) scrolls the page down = negative wheel
        notches = int(self._scroll_carry)
        if notches == 0:
            return
        self._scroll_carry -= notches
        # both flavours in one frame: libinput takes the hi-res one when a device offers it, older
        # consumers the plain one
        self._emit(self._mouse, EV_REL, REL_WHEEL, notches, more=((EV_REL, REL_WHEEL_HI_RES, notches * WHEEL_NOTCH_HI_RES),))

    def close(self) -> None:
        with self._gate:
            self._closed = True
            if self._mouse is None:
                return
            self._apply_locked(0, 0, 0, 0, 0)     # let go of anything still held before the devices vanish
            for device in (self._mouse, self._keyboard):
                try:
                    device.close()
                except OSError:
                    pass
            self._mouse = self._keyboard = None
            log.write("pad: Maus und Tastatur (uinput) entfernt")


def apply_dead_zone(value: int) -> int:
    if -STICK_DEAD_ZONE < value < STICK_DEAD_ZONE:
        return 0
    return value - STICK_DEAD_ZONE if value > 0 else value + STICK_DEAD_ZONE
