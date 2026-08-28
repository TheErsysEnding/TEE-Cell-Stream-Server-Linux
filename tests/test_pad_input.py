"""Tests for pad_receiver, virtual_gamepad and desktop_input.

Safe on a live desktop: the maths and the mapping are checked against stand-in devices and a fake clock,
and where a real uinput device is created its node is grabbed (EVIOCGRAB) before anything is emitted,
so the compositor never sees a click, a key or a pointer move from these tests. The evdev-backed tests
skip when python3-evdev is missing, /dev/uinput is not writable, or the node cannot be read back
(/dev/input/event* is root:input; joysticks get a uaccess ACL, a virtual mouse or keyboard does not).

Run:  cd <project> && PYTHONPATH=src python3 -m unittest tests.test_pad_input -v
"""

import atexit
import os
import shutil
import tempfile

_SCRATCH = tempfile.mkdtemp(prefix="tee-cst-pad-test-")
atexit.register(shutil.rmtree, _SCRATCH, True)
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_SCRATCH, "settings.json"))   # keep the user's real files out of it
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_SCRATCH, "server.log"))

import select     # noqa: E402
import struct     # noqa: E402
import threading  # noqa: E402
import time      # noqa: E402
import unittest  # noqa: E402
from unittest import mock   # noqa: E402

try:
    import evdev
except ImportError:
    evdev = None

from teecellstream import desktop_input, log, protocol, virtual_gamepad   # noqa: E402
from teecellstream.clock import now_us                                    # noqa: E402
from teecellstream.desktop_input import (                                 # noqa: E402
    BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, DesktopInput, EV_REL, KEY_BACKSPACE, KEY_DOWN, KEY_ENTER, KEY_LEFT,
    KEY_LEFTMETA, KEY_LEFTSHIFT, KEY_RIGHT, KEY_RIGHTALT, KEY_TAB, KEY_UP, KEYBOARD_NAME, MOUSE_NAME,
    REL_WHEEL, REL_WHEEL_HI_RES, REL_X, REL_Y,
)
from teecellstream.pad_receiver import PadReceiver                        # noqa: E402
from teecellstream.virtual_gamepad import (                               # noqa: E402
    ABS_HAT0X, ABS_HAT0Y, ABS_RX, ABS_RY, ABS_X, ABS_Y, ABS_Z, ABS_RZ, BTN_A, BTN_B, BTN_SELECT, BTN_START,
    BTN_THUMBL, BTN_THUMBR, BTN_TL, BTN_TR, BTN_X, BTN_Y, DEVICE_NAME, EV_ABS, EV_KEY, EV_SYN, VirtualGamepad,
)

SENDER = ("10.42.0.151", 38311)
KEY_Y, KEY_Z, KEY_Q, KEY_APOSTROPHE, KEY_SPACE, KEY_2 = 21, 44, 16, 40, 57, 3
FRAME_S = 1 / 60


def bits(*names) -> int:
    return sum(1 << getattr(protocol.PadBits, name.upper()) for name in names)


def cp_packet(packet_id, buttons=0, left_x=0, left_y=0, right_x=0, right_y=0, sent_us=None) -> bytes:
    """A CP packet exactly as stream.c's sendPadState builds it."""
    if sent_us is None:
        sent_us = now_us()
    return struct.pack(">2sIHbbbbQ", b"CP", packet_id, buttons, left_x, left_y, right_x, right_y, sent_us)


# ---------------------------------------------------------------------------- stand-ins

class FakeGamepad:
    def __init__(self, can_open=True):
        self.can_open = can_open
        self.is_open = False
        self.open_attempts = 0
        self.sent = []
        self.closed = False

    def try_open(self):
        self.open_attempts += 1
        self.is_open = self.can_open
        return self.can_open

    def send(self, buttons, left_x, left_y, right_x, right_y):
        self.sent.append((buttons, left_x, left_y, right_x, right_y))

    def close(self):
        self.closed = True


class FakeDesktop:
    def __init__(self):
        self.applied = []
        self.released = 0
        self.typed = []
        self.closed = False

    def apply(self, buttons, left_x, left_y, right_x, right_y):
        self.applied.append((buttons, left_x, left_y, right_x, right_y))

    def release_all(self):
        self.released += 1

    def type_character(self, character):
        self.typed.append(character)

    def close(self):
        self.closed = True


class FakeEmitter:
    """Stands in for an evdev UInput: records every write and marks each syn()."""

    def __init__(self):
        self.events = []
        self.closed = False

    def write(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append("SYN")

    def close(self):
        self.closed = True

    def values(self, etype, code):
        return [event[2] for event in self.events if event != "SYN" and event[0] == etype and event[1] == code]

    def keys(self):
        """(code, value) of every EV_KEY event, in order."""
        return [(event[1], event[2]) for event in self.events if event != "SYN" and event[0] == EV_KEY]

    def clear(self):
        self.events.clear()


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def find_device(name, timeout_s=3.0):
    """The readable /dev/input/event* node with this name. udev grants the ACL a moment after creation."""
    deadline = time.monotonic() + timeout_s
    while True:
        for path in evdev.list_devices():
            try:
                device = evdev.InputDevice(path)
            except OSError:
                continue   # somebody else's, or the ACL is not there yet
            if device.name == name:
                return device
            device.close()
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def open_readable(path, name, timeout_s=1.5):
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            device = evdev.InputDevice(path)
            if device.name == name:
                return device
            device.close()
            return None
        except OSError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)


def drain(node, wait_s=0.5):
    """Everything the node delivers: first event within wait_s, then until it goes quiet for 50 ms."""
    events = []
    quiet = wait_s
    while True:
        ready, _, _ = select.select([node.fd], [], [], quiet)
        if not ready:
            return events
        event = node.read_one()
        while event is not None:
            if event.type != EV_SYN:
                events.append((event.type, event.code, event.value))
            event = node.read_one()
        quiet = 0.05


def last_values(events) -> dict:
    return {(etype, code): value for etype, code, value in events}


def key_events(events):
    return [(code, value) for etype, code, value in events if etype == EV_KEY]


def log_since(marker: str) -> str:
    """The log lines written after `marker` was logged - the in-memory log is shared by every test in the run."""
    return log.get_recent().rpartition(marker)[2]


VIRTUAL_INPUT_DIR = "/sys/devices/virtual/input"


def virtual_input_nodes() -> set:
    """Every virtual input device the kernel has right now - readable or not, so a leak cannot hide."""
    try:
        return set(os.listdir(VIRTUAL_INPUT_DIR))
    except OSError:
        return set()


def wait_for_nodes(expected: set, timeout_s=2.0) -> set:
    """The kernel removes an input device the moment its uinput fd closes; give udev a moment anyway."""
    deadline = time.monotonic() + timeout_s
    while True:
        nodes = virtual_input_nodes()
        if nodes == expected or time.monotonic() >= deadline:
            return nodes
        time.sleep(0.05)


def sysfs_text(node_path: str, name: str) -> str:
    with open("/sys/class/input/%s/device/%s" % (os.path.basename(node_path), name), encoding="utf-8") as handle:
        return handle.read().strip()


def sysfs_bits(node_path: str, name: str) -> set:
    """The set bits of .../capabilities/<name> - a list of 64-bit words, highest word first."""
    value = 0
    for index, word in enumerate(reversed(sysfs_text(node_path, "capabilities/" + name).split())):
        value |= int(word, 16) << (64 * index)
    return {bit for bit in range(value.bit_length()) if value >> bit & 1}


def xbox_axis_reference(value: int) -> int:
    """ToXboxAxis from upstream/server/VirtualGamepad.cs, transcribed, as the yardstick for to_axis()."""
    dead_zone, full_tilt = 12, 115.0
    if value >= dead_zone:
        tilt = (value - dead_zone) / (full_tilt - dead_zone)
    elif value <= -dead_zone:
        tilt = (value + dead_zone) / (full_tilt - dead_zone)
    else:
        tilt = 0.0
    tilt = max(-1.0, min(1.0, tilt))
    return int(tilt * 32767)     # (short) truncates toward zero


# ---------------------------------------------------------------------------- PadReceiver

class PadReceiverTests(unittest.TestCase):
    def make(self, swap=False, gamepad=None, desktop=None):
        self.gamepad = gamepad or FakeGamepad()
        self.desktop = desktop or FakeDesktop()
        return PadReceiver(self.desktop, self.gamepad, lambda: swap)

    def test_parses_every_field_and_drives_the_desktop_by_default(self):
        receiver = self.make()
        receiver.handle(cp_packet(7, bits("cross", "L2"), 127, -128, 5, -5), SENDER)
        self.assertEqual(self.desktop.applied, [(bits("cross", "L2"), 127, -128, 5, -5)])
        self.assertEqual(self.gamepad.sent, [])
        self.assertEqual(receiver.packets_received, 1)
        self.assertEqual(receiver.packets_lost, 0)

    def test_swapped_sticks_hand_the_right_stick_to_the_pointer(self):
        receiver = self.make(swap=True)
        receiver.handle(cp_packet(1, 0, 10, 20, 30, 40), SENDER)
        self.assertEqual(self.desktop.applied, [(0, 30, 40, 10, 20)])

    def test_gamepad_mode_routes_to_the_gamepad_only(self):
        receiver = self.make()
        receiver.set_gamepad_mode(True)
        self.assertTrue(receiver.gamepad_mode)
        self.assertEqual(self.gamepad.sent[-1], (0, 0, 0, 0, 0))        # released on the way in
        self.assertEqual(self.desktop.released, 1)
        receiver.handle(cp_packet(1, bits("square"), 1, 2, 3, 4), SENDER)
        self.assertEqual(self.gamepad.sent[-1], (bits("square"), 1, 2, 3, 4))
        self.assertEqual(self.desktop.applied, [])
        receiver.set_gamepad_mode(False)
        receiver.handle(cp_packet(2, 0, 1, 2, 3, 4), SENDER)
        self.assertEqual(self.desktop.applied, [(0, 1, 2, 3, 4)])
        self.assertIn("pad: steuert jetzt ein virtuelles Xbox-Gamepad", log.get_recent())
        self.assertIn("pad: steuert jetzt Maus und Tastatur", log.get_recent())

    def test_missing_gamepad_keeps_the_mouse_and_asks_only_once(self):
        receiver = self.make(gamepad=FakeGamepad(can_open=False))
        receiver.set_gamepad_mode(True)
        receiver.set_gamepad_mode(True)      # the PS3 repeats PADMODE every second
        self.assertFalse(receiver.gamepad_mode)
        self.assertEqual(self.gamepad.open_attempts, 1)
        self.assertIn("pad: kein virtuelles Gamepad verfügbar", log.get_recent())
        receiver.handle(cp_packet(1, 0, 0, 0, 0, 0), SENDER)
        self.assertEqual(len(self.desktop.applied), 1)

    def test_counts_lost_packets_from_the_id_gaps(self):
        receiver = self.make()
        for packet_id in (0, 1, 2, 5, 6, 3, 10):    # 3,4 missing; 3 arriving late is not a loss; 7,8,9 missing
            receiver.handle(cp_packet(packet_id), SENDER)
        self.assertEqual(receiver.packets_received, 7)
        self.assertEqual(receiver.packets_lost, 2 + 6)

    def test_duplicates_and_late_packets_are_never_counted_as_loss(self):
        receiver = self.make()
        for packet_id in (5, 5, 5, 4, 5, 6):       # UDP may repeat one and hand us an old one afterwards
            receiver.handle(cp_packet(packet_id, bits("cross")), SENDER)
        self.assertEqual(receiver.packets_received, 6)
        self.assertEqual(receiver.packets_lost, 0)
        self.assertEqual(len(self.desktop.applied), 6)   # a repeat still drives the PC: the PS3 resends state

    def test_the_packet_id_wrapping_past_four_billion_is_not_four_billion_lost(self):
        receiver = self.make()
        for packet_id in (0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF, 0, 1, 2):
            receiver.handle(cp_packet(packet_id), SENDER)
        self.assertEqual(receiver.packets_received, 6)
        self.assertEqual(receiver.packets_lost, 0)       # u32 wrap: the next id really is the next packet

    def test_a_gap_at_the_far_end_of_the_id_range_still_counts(self):
        receiver = self.make()
        receiver.handle(cp_packet(0xFFFFFFF0), SENDER)
        receiver.handle(cp_packet(0xFFFFFFF3), SENDER)
        self.assertEqual(receiver.packets_lost, 2)

    def test_switching_mode_mid_stream_leaves_nothing_held(self):
        receiver = self.make()
        held = bits("cross", "start", "up")
        receiver.handle(cp_packet(1, held), SENDER)
        receiver.set_gamepad_mode(True)                          # SELECT + Cross on the PS3
        self.assertEqual(self.desktop.released, 1)               # the mouse button and Super let go ...
        self.assertEqual(self.gamepad.sent, [(0, 0, 0, 0, 0)])   # ... and the pad starts from rest
        receiver.handle(cp_packet(2, held), SENDER)
        self.assertEqual(self.gamepad.sent[-1], (held, 0, 0, 0, 0))
        self.assertEqual(len(self.desktop.applied), 1)           # nothing reaches the mouse while on the pad
        receiver.set_gamepad_mode(False)
        self.assertEqual(self.gamepad.sent[-1], (0, 0, 0, 0, 0))  # nothing left held on the pad either
        self.assertEqual(self.desktop.released, 2)
        receiver.handle(cp_packet(3, held), SENDER)
        self.assertEqual(self.desktop.applied[-1], (held, 0, 0, 0, 0))
        self.assertEqual(self.gamepad.open_attempts, 1)          # the pad stays plugged in for the session

    def test_every_pad_bit_has_its_own_name(self):
        receiver = self.make()
        log.write("test-marker-alle-knoepfe")
        receiver.handle(cp_packet(1, 0xFFFF), SENDER)
        receiver.handle(cp_packet(2, 0), SENDER)
        names = "+".join(protocol.BUTTON_NAMES)                  # bit order as in the PS3's pad.h
        self.assertIn("pad: gedrückt " + names, log_since("test-marker-alle-knoepfe"))
        self.assertIn("pad: losgelassen " + names, log_since("test-marker-alle-knoepfe"))

    def test_the_report_repeats_only_when_something_changed(self):
        receiver = self.make()
        receiver.report_interval_ms = 0            # report on every packet instead of every 2 s
        log.write("test-marker-report")
        for packet_id in range(1, 6):
            receiver.handle(cp_packet(packet_id, 0, 20, 0, 0, 0), SENDER)     # the same sticks every time
        self.assertEqual(log_since("test-marker-report").count("pad: Sticks L(20,0) R(0,0)"), 1)
        receiver.handle(cp_packet(6, 0, 21, 0, 0, 0), SENDER)
        self.assertEqual(log_since("test-marker-report").count("pad: Sticks L(21,0) R(0,0)"), 1)

    def test_release_lets_go_of_everything_and_restarts_the_id_tracking(self):
        receiver = self.make()
        for packet_id in range(4):
            receiver.handle(cp_packet(packet_id, bits("cross")), SENDER)
        receiver.release()
        self.assertEqual(self.desktop.released, 1)
        self.assertEqual(self.gamepad.sent[-1], (0, 0, 0, 0, 0))
        receiver.handle(cp_packet(0), SENDER)     # a new stream starts at 0 - not 4 billion packets lost
        self.assertEqual(receiver.packets_lost, 0)

    def test_reports_the_trip_time_and_the_sticks(self):
        receiver = self.make()
        receiver.report_interval_ms = 0             # report on every packet instead of every 2 s
        receiver.handle(cp_packet(1, 0, 1, 2, 3, 4, sent_us=now_us() - 7_000), SENDER)
        self.assertIn(receiver.last_trip_ms, (7, 8))
        self.assertIn("pad: Sticks L(1,2) R(3,4), 1 Pakete, 0 verloren, %d ms PS3 -> hier" % receiver.last_trip_ms,
                      log.get_recent())
        receiver.handle(cp_packet(2, 0, 0, 0, 0, 0, sent_us=now_us() + 5_000_000), SENDER)   # clock skew: never negative
        self.assertEqual(receiver.last_trip_ms, 0)

    def test_logs_presses_and_releases_by_name(self):
        receiver = self.make()
        receiver.handle(cp_packet(1, bits("cross", "L1")), SENDER)
        receiver.handle(cp_packet(2, bits("L1")), SENDER)
        receiver.handle(cp_packet(3, bits("L1")), SENDER)     # unchanged: nothing new logged
        recent = log.get_recent()
        self.assertIn("pad: gedrückt cross+L1", recent)
        self.assertIn("pad: losgelassen cross", recent)
        self.assertEqual(recent.count("pad: losgelassen cross"), 1)

    def test_short_and_malformed_packets_never_raise(self):
        receiver = self.make()
        for packet in (b"", b"CP", b"CP" + b"\xff" * 17, b"\xff" * 20, b"CP" + b"\xff" * 18, b"CP" + b"\x00" * 40, None, "text"):
            receiver.handle(packet, SENDER)
        self.assertEqual(receiver.packets_received, 3)     # the three 20+ byte ones parse as something
        self.assertEqual(len(self.desktop.applied), 3)

    def test_a_failing_device_is_logged_once_and_never_breaks_the_receive_loop(self):
        class Broken(FakeDesktop):
            def apply(self, *args):
                raise RuntimeError("uinput weg (test-marker-2323)")
        receiver = self.make(desktop=Broken())
        for packet_id in range(5):
            receiver.handle(cp_packet(packet_id, bits("cross")), SENDER)   # handle() runs outside any try in server.py
        self.assertEqual(receiver.packets_received, 5)
        self.assertEqual(log.get_recent().count("test-marker-2323"), 1)     # not 60 times a second

    def test_type_key_reaches_the_desktop(self):
        receiver = self.make()
        receiver.type_key("ä")
        self.assertEqual(self.desktop.typed, ["ä"])

    def test_close_releases_destroys_and_then_ignores_packets(self):
        receiver = self.make()
        receiver.handle(cp_packet(1, bits("cross")), SENDER)
        receiver.close()
        self.assertTrue(self.desktop.closed and self.gamepad.closed)
        self.assertEqual(self.desktop.released, 1)
        receiver.handle(cp_packet(2, bits("cross")), SENDER)
        receiver.set_gamepad_mode(True)
        receiver.type_key("x")
        self.assertEqual(len(self.desktop.applied), 1)
        self.assertEqual(self.desktop.typed, [])
        self.assertFalse(receiver.gamepad_mode)


# ---------------------------------------------------------------------------- VirtualGamepad

class VirtualGamepadMappingTests(unittest.TestCase):
    def test_axis_curve_with_dead_zone_and_full_tilt(self):
        to_axis = virtual_gamepad.to_axis
        for resting in (0, 5, 11, -11):
            self.assertEqual(to_axis(resting), 0)
        self.assertEqual(to_axis(12), 0)
        self.assertEqual(to_axis(127), 32767)
        self.assertEqual(to_axis(115), 32767)
        self.assertEqual(to_axis(-128), -32767)
        self.assertEqual(to_axis(63), int((63 - 12) / (115 - 12) * 32767))
        self.assertEqual(to_axis(-63), -to_axis(63))

    def test_report_layout(self):
        state = last_values(virtual_gamepad.report_for(bits("cross", "L2", "up", "left"), 127, -128, 0, 0))
        self.assertEqual(state[(EV_KEY, BTN_A)], 1)
        self.assertEqual(state[(EV_KEY, BTN_B)], 0)
        self.assertEqual(state[(EV_ABS, ABS_Z)], 255)
        self.assertEqual(state[(EV_ABS, ABS_RZ)], 0)
        self.assertEqual(state[(EV_ABS, ABS_X)], 32767)
        self.assertEqual(state[(EV_ABS, ABS_Y)], -32767)    # PS3 -128 = up, evdev negative = up: no inversion
        self.assertEqual(state[(EV_ABS, ABS_HAT0Y)], -1)
        self.assertEqual(state[(EV_ABS, ABS_HAT0X)], -1)
        state = last_values(virtual_gamepad.report_for(bits("down", "right", "R2", "start", "L1"), 0, 0, -128, 127))
        self.assertEqual(state[(EV_ABS, ABS_HAT0Y)], 1)
        self.assertEqual(state[(EV_ABS, ABS_HAT0X)], 1)
        self.assertEqual(state[(EV_ABS, ABS_RZ)], 255)
        self.assertEqual(state[(EV_KEY, BTN_START)], 1)
        self.assertEqual(state[(EV_KEY, BTN_TL)], 1)
        self.assertEqual(state[(EV_ABS, ABS_RX)], -32767)
        self.assertEqual(state[(EV_ABS, ABS_RY)], 32767)
        state = last_values(virtual_gamepad.report_for(bits("up", "down"), 0, 0, 0, 0))
        self.assertEqual(state[(EV_ABS, ABS_HAT0Y)], 0)      # both at once cancel out

    def test_axis_curve_matches_the_original_over_the_whole_range(self):
        """to_axis must equal ToXboxAxis for every value the packet can carry, sign included."""
        for value in range(-128, 128):
            self.assertEqual(virtual_gamepad.to_axis(value), xbox_axis_reference(value), value)
        self.assertLess(virtual_gamepad.to_axis(-100), 0)     # PS3 up (negative) stays negative on evdev
        self.assertGreater(virtual_gamepad.to_axis(100), 0)   # PS3 down (positive) stays positive
        self.assertEqual(virtual_gamepad.to_axis(11), 0)      # dead zone 12, exclusive below
        self.assertEqual(virtual_gamepad.to_axis(-11), 0)
        self.assertEqual(virtual_gamepad.to_axis(114), int((114 - 12) / 103 * 32767))
        self.assertEqual(virtual_gamepad.to_axis(115), 32767)  # full tilt 115, and everything past it

    def test_each_pad_bit_lands_where_the_ps3_expects_it_and_moves_nothing_else(self):
        expected = {
            "cross": (EV_KEY, BTN_A, 1), "circle": (EV_KEY, BTN_B, 1), "square": (EV_KEY, BTN_X, 1),
            "triangle": (EV_KEY, BTN_Y, 1), "L1": (EV_KEY, BTN_TL, 1), "R1": (EV_KEY, BTN_TR, 1),
            "select": (EV_KEY, BTN_SELECT, 1), "start": (EV_KEY, BTN_START, 1),
            "L3": (EV_KEY, BTN_THUMBL, 1), "R3": (EV_KEY, BTN_THUMBR, 1),
            "L2": (EV_ABS, ABS_Z, 255), "R2": (EV_ABS, ABS_RZ, 255),          # digital triggers, 0 or 255
            "up": (EV_ABS, ABS_HAT0Y, -1), "down": (EV_ABS, ABS_HAT0Y, 1),    # d-pad on the hat, up = -1
            "left": (EV_ABS, ABS_HAT0X, -1), "right": (EV_ABS, ABS_HAT0X, 1),
        }
        self.assertEqual(sorted(expected), sorted(protocol.BUTTON_NAMES))   # all 16, none forgotten
        rest = last_values(virtual_gamepad.report_for(0, 0, 0, 0, 0))
        self.assertEqual(set(rest.values()), {0})            # at rest nothing reads as pressed
        for name, (etype, code, value) in expected.items():
            state = last_values(virtual_gamepad.report_for(bits(name), 0, 0, 0, 0))
            self.assertEqual(state.get((etype, code)), value, name)
            moved = {key for key, held in state.items() if held != rest[key]}
            self.assertEqual(moved, {(etype, code)}, name)

    def test_the_spelled_out_event_codes_are_the_kernels(self):
        """The codes are written out so the mapping reads without evdev installed - check them anyway."""
        if evdev is None:
            self.skipTest("python3-evdev fehlt")
        codes = evdev.ecodes.ecodes
        for name in ("EV_SYN", "EV_KEY", "EV_ABS", "BTN_A", "BTN_B", "BTN_X", "BTN_Y", "BTN_TL", "BTN_TR",
                     "BTN_SELECT", "BTN_START", "BTN_MODE", "BTN_THUMBL", "BTN_THUMBR",
                     "ABS_X", "ABS_Y", "ABS_Z", "ABS_RX", "ABS_RY", "ABS_RZ", "ABS_HAT0X", "ABS_HAT0Y"):
            self.assertEqual(getattr(virtual_gamepad, name), codes[name], name)
        self.assertEqual(virtual_gamepad.SYN_REPORT, codes["SYN_REPORT"])
        for name in ("EV_REL", "REL_X", "REL_Y", "REL_WHEEL", "REL_WHEEL_HI_RES", "BTN_LEFT", "BTN_RIGHT",
                     "BTN_MIDDLE", "KEY_BACKSPACE", "KEY_TAB", "KEY_ENTER", "KEY_SPACE", "KEY_LEFTSHIFT",
                     "KEY_RIGHTALT", "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_LEFTMETA", "KEY_MAX"):
            self.assertEqual(getattr(desktop_input, name), codes[name], name)

    def test_send_without_open_is_a_no_op(self):
        gamepad = VirtualGamepad()
        gamepad.send(bits("cross"), 127, 127, 127, 127)
        gamepad.close()
        self.assertFalse(gamepad.is_open)

    def test_a_broken_device_is_reported_once_and_keeps_taking_packets(self):
        """uinput can go away under us (module unloaded, device revoked); 60 reports a second must not
        turn that into 60 log lines a second, and must not throw on the receive thread either."""
        class Broken:
            def write(self, *_args):
                raise OSError("uinput weg (test-marker-9001)")

            def syn(self):
                raise OSError("uinput weg (test-marker-9001)")

            def close(self):
                pass

        log.write("test-marker-broken-pad")
        gamepad = VirtualGamepad()
        gamepad._device = Broken()
        receiver = PadReceiver(FakeDesktop(), gamepad, lambda: False)
        for packet_id in range(60):
            receiver.handle(cp_packet(packet_id, bits("cross")), SENDER)
        receiver.close()
        recent = log_since("test-marker-broken-pad")
        self.assertEqual(recent.count("test-marker-9001"), 1)
        self.assertEqual(recent.count("pad: Gamepad-Report fehlgeschlagen"), 1)
        self.assertEqual(receiver.packets_received, 60)


class WithoutUinputTests(unittest.TestCase):
    """python3-evdev missing, or /dev/uinput not ours: say why once, then everything is a harmless no-op."""

    def test_missing_evdev_keeps_the_receiver_alive_on_a_silent_mouse(self):
        log.write("test-marker-no-evdev")
        with mock.patch.object(virtual_gamepad, "evdev", None), mock.patch.object(desktop_input, "evdev", None):
            gamepad, desktop = VirtualGamepad(), DesktopInput()
            receiver = PadReceiver(desktop, gamepad, lambda: False)
            receiver.set_gamepad_mode(True)
            receiver.set_gamepad_mode(True)
            self.assertFalse(receiver.gamepad_mode)
            self.assertFalse(gamepad.is_open)
            for packet_id in range(3):
                receiver.handle(cp_packet(packet_id, bits("cross", "start"), -128, 0, 0, 127), SENDER)
            receiver.type_key("z")
            receiver.type_key("\n")
            receiver.release()
            receiver.close()
            self.assertFalse(desktop.is_open)
            self.assertEqual(receiver.packets_received, 3)
        recent = log_since("test-marker-no-evdev")
        self.assertEqual(recent.count("pad: python3-evdev fehlt - kein virtuelles Gamepad möglich"), 1)
        self.assertEqual(recent.count("pad: kein virtuelles Gamepad verfügbar"), 1)
        self.assertEqual(recent.count("pad: uinput nicht verfügbar (python3-evdev fehlt)"), 1)
        self.assertIn("pad: gedrückt cross+start", recent)                 # the packet path itself keeps working

    def test_gamepad_open_failure_names_the_reason_and_stays_closed(self):
        def refused(*_args, **_kwargs):
            raise PermissionError("/dev/uinput: keine Berechtigung (test-marker-0815)")
        with mock.patch.object(virtual_gamepad, "evdev", object()), mock.patch.object(virtual_gamepad, "capabilities", dict), \
                mock.patch.object(virtual_gamepad, "open_uinput", refused):
            gamepad = VirtualGamepad()
            self.assertFalse(gamepad.try_open())
            self.assertFalse(gamepad.is_open)
            self.assertIsNone(gamepad.path)
            gamepad.send(bits("cross"), 127, 127, 127, 127)
            gamepad.close()
        recent = log.get_recent()
        self.assertIn("test-marker-0815", recent)
        self.assertIn("/dev/uinput beschreibbar?", recent)


@unittest.skipIf(evdev is None, "python3-evdev fehlt")
class VirtualGamepadDeviceTests(unittest.TestCase):
    def setUp(self):
        self.gamepad = VirtualGamepad()
        if not self.gamepad.try_open():
            self.skipTest("kein uinput: " + log.get_recent().strip().splitlines()[-1])
        self.addCleanup(self.gamepad.close)
        # our own node first (a real 360 pad may be plugged in too); the name search is the fallback
        # for a kernel without UI_GET_SYSNAME. udev grants the joystick uaccess ACL a moment after creation.
        self.node = open_readable(self.gamepad.path, DEVICE_NAME, 3.0) if self.gamepad.path else find_device(DEVICE_NAME)
        if self.node is None:
            self.skipTest("Gamepad-Node nicht lesbar (uaccess-ACL für Joysticks fehlt?)")
        self.addCleanup(self.node.close)
        os.set_blocking(self.node.fd, False)
        try:
            self.node.grab()          # nobody else (Steam, say) gets to see the test's button presses
            self.addCleanup(self.node.ungrab)
        except OSError:
            pass

    def test_identity_and_capabilities(self):
        self.assertEqual(self.node.name, DEVICE_NAME)
        self.assertEqual((self.node.info.bustype, self.node.info.vendor, self.node.info.product, self.node.info.version),
                         (0x03, 0x045E, 0x028E, 0x0110))
        capabilities = self.node.capabilities(absinfo=False)
        self.assertEqual(sorted(capabilities[EV_ABS]), [ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y])
        self.assertIn(BTN_A, capabilities[EV_KEY])
        self.assertIn(virtual_gamepad.BTN_MODE, capabilities[EV_KEY])
        info = self.node.absinfo(ABS_X)
        self.assertEqual((info.min, info.max, info.fuzz, info.flat), (-32768, 32767, 16, 128))
        self.assertEqual((self.node.absinfo(ABS_Z).min, self.node.absinfo(ABS_Z).max), (0, 255))
        self.assertEqual((self.node.absinfo(ABS_HAT0Y).min, self.node.absinfo(ABS_HAT0Y).max), (-1, 1))
        self.assertEqual(self.gamepad.path, self.node.path)

    def test_cp_packet_reaches_the_device_and_release_zeroes_it(self):
        receiver = PadReceiver(FakeDesktop(), self.gamepad, lambda: False)
        receiver.set_gamepad_mode(True)
        drain(self.node, 0.2)                       # the rest reports

        receiver.handle(cp_packet(1, bits("cross", "L2", "up"), 127, -128, 0, 0), SENDER)
        state = last_values(drain(self.node))
        self.assertEqual(state.get((EV_KEY, BTN_A)), 1)
        self.assertEqual(state.get((EV_ABS, ABS_Z)), 255)
        self.assertEqual(state.get((EV_ABS, ABS_HAT0Y)), -1)
        self.assertAlmostEqual(state.get((EV_ABS, ABS_X), 0), 32767, delta=1)
        self.assertAlmostEqual(state.get((EV_ABS, ABS_Y), 0), -32767, delta=1)
        self.assertEqual(self.node.absinfo(ABS_Y).value, -32767)

        receiver.release()
        state = last_values(drain(self.node))
        self.assertEqual(state.get((EV_KEY, BTN_A)), 0)
        self.assertEqual(state.get((EV_ABS, ABS_Z)), 0)
        self.assertEqual(state.get((EV_ABS, ABS_HAT0Y)), 0)
        self.assertEqual(state.get((EV_ABS, ABS_X)), 0)
        self.assertEqual(state.get((EV_ABS, ABS_Y)), 0)
        self.assertEqual(self.node.active_keys(), [])
        for axis in (ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_Z, ABS_RZ, ABS_HAT0X, ABS_HAT0Y):
            self.assertEqual(self.node.absinfo(axis).value, 0)


    def test_switching_back_to_the_mouse_lets_go_of_the_real_pad(self):
        desktop = FakeDesktop()
        receiver = PadReceiver(desktop, self.gamepad, lambda: False)
        receiver.set_gamepad_mode(True)
        held = bits("cross", "R2", "down", "L3", "L1")
        receiver.handle(cp_packet(1, held, -128, 127, 100, -100), SENDER)
        state = last_values(drain(self.node))
        self.assertEqual(state.get((EV_KEY, BTN_A)), 1)
        self.assertEqual(state.get((EV_ABS, ABS_RZ)), 255)

        receiver.set_gamepad_mode(False)          # SELECT + Cross, mid-stream, with five things held down
        drain(self.node)
        self.assertEqual(self.node.active_keys(), [])
        for axis in (ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_Z, ABS_RZ, ABS_HAT0X, ABS_HAT0Y):
            self.assertEqual(self.node.absinfo(axis).value, 0, axis)

        receiver.handle(cp_packet(2, held, 0, 0, 0, 0), SENDER)   # the pad is the mouse's now
        self.assertEqual(drain(self.node, 0.15), [])
        self.assertEqual(desktop.applied[-1], (held, 0, 0, 0, 0))

    def test_the_sticks_reach_the_real_device_undistorted(self):
        receiver = PadReceiver(FakeDesktop(), self.gamepad, lambda: False)
        receiver.set_gamepad_mode(True)
        for left_x, left_y, right_x, right_y in ((127, 127, -128, -128), (-128, 127, 127, -128), (63, -63, 11, -11)):
            receiver.handle(cp_packet(1, 0, left_x, left_y, right_x, right_y), SENDER)
            drain(self.node, 0.2)
            for axis, value in ((ABS_X, left_x), (ABS_Y, left_y), (ABS_RX, right_x), (ABS_RY, right_y)):
                self.assertEqual(self.node.absinfo(axis).value, xbox_axis_reference(value), (axis, value))
        receiver.release()


@unittest.skipIf(evdev is None, "python3-evdev fehlt")
class UinputLifetimeTests(unittest.TestCase):
    """Nothing may outlive close(): a uinput device stays in the kernel until its fd is closed, and
    python-evdev has no __del__, so a forgotten one would pile up over start/stop cycles."""

    def test_the_three_devices_appear_and_disappear_again_every_cycle(self):
        before = virtual_input_nodes()
        self.assertTrue(before or os.path.isdir(VIRTUAL_INPUT_DIR), VIRTUAL_INPUT_DIR)
        for cycle in range(3):
            gamepad, desktop = VirtualGamepad(), DesktopInput(layout="de")
            if not gamepad.try_open():
                self.skipTest("kein uinput: " + log.get_recent().strip().splitlines()[-1])
            self.addCleanup(gamepad.close)
            desktop.apply(0, 0, 0, 0, 0)       # at rest: this creates the devices and emits nothing at all
            self.addCleanup(desktop.close)
            self.assertTrue(desktop.is_open)
            self.assertEqual(len(virtual_input_nodes() - before), 3, cycle)   # pad, mouse, keyboard
            PadReceiver(desktop, gamepad, lambda: False).close()
            self.assertEqual(wait_for_nodes(before), before, cycle)
        self.assertEqual(wait_for_nodes(before), before)


@unittest.skipIf(evdev is None, "python3-evdev fehlt")
class RealDeviceCapabilityTests(unittest.TestCase):
    """The mouse and the keyboard as the kernel really made them, read from sysfs.

    Their /dev/input/event* nodes cannot be opened here: they are root:input, and systemd's
    70-uaccess.rules hands the logged-in user an ACL for joysticks only (measured: a device carrying
    keyboard or mouse capabilities is classified before udev's joystick test is ever reached, so no
    capability set can buy the ACL). sysfs is world-readable and answers the question that matters -
    the kernel silently DROPS an event whose code the device never declared, so a gap between what we
    declare and what the layout asks for would type nothing at all, with nothing in any log.

    Nothing is emitted here: the devices are created at rest and looked at, never written to."""

    @classmethod
    def setUpClass(cls):
        cls.desktop = DesktopInput(layout="de")
        cls.desktop.apply(0, 0, 0, 0, 0)         # at rest: creates the devices, emits not one event
        if not cls.desktop.is_open:
            cls.desktop.close()
            raise unittest.SkipTest("uinput nicht verfügbar: " + log.get_recent().strip().splitlines()[-1])
        cls.mouse_path, cls.keyboard_path = cls.desktop.device_paths
        if not cls.mouse_path or not cls.keyboard_path:
            cls.desktop.close()
            raise unittest.SkipTest("UI_GET_SYSNAME nicht verfügbar - Gerätepfade unbekannt")

    @classmethod
    def tearDownClass(cls):
        cls.desktop.close()

    def test_identity(self):
        self.assertEqual(sysfs_text(self.mouse_path, "name"), MOUSE_NAME)
        self.assertEqual(sysfs_text(self.keyboard_path, "name"), KEYBOARD_NAME)
        for path, product in ((self.mouse_path, desktop_input.MOUSE_PRODUCT_ID),
                              (self.keyboard_path, desktop_input.KEYBOARD_PRODUCT_ID)):
            self.assertEqual(int(sysfs_text(path, "id/bustype"), 16), desktop_input.BUS_VIRTUAL)
            self.assertEqual(int(sysfs_text(path, "id/vendor"), 16), desktop_input.VENDOR_ID)
            self.assertEqual(int(sysfs_text(path, "id/product"), 16), product)

    def test_the_mouse_declares_exactly_what_it_emits(self):
        self.assertEqual(sysfs_bits(self.mouse_path, "key"), {BTN_LEFT, BTN_RIGHT, BTN_MIDDLE})
        self.assertEqual(sysfs_bits(self.mouse_path, "rel"), {REL_X, REL_Y, REL_WHEEL, REL_WHEEL_HI_RES})
        self.assertEqual(sysfs_bits(self.mouse_path, "abs"), set())

    def test_the_keyboard_declares_every_code_the_layout_can_ask_for(self):
        declared = sysfs_bits(self.keyboard_path, "key")
        self.assertEqual(sysfs_bits(self.keyboard_path, "rel"), set())
        self.assertFalse(declared & {BTN_LEFT, BTN_RIGHT, BTN_MIDDLE})     # or udev would call it a mouse
        self.assertFalse(declared & set(evdev.ecodes.BTN))                 # no BTN_* at all, only KEY_*
        for code in (KEY_BACKSPACE, KEY_TAB, KEY_ENTER, KEY_SPACE, KEY_LEFTSHIFT, KEY_RIGHTALT,
                     KEY_LEFTMETA, KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_Y, KEY_Z, KEY_Q, KEY_APOSTROPHE):
            self.assertIn(code, declared)
        if not desktop_input.have_xkbcommon():
            self.skipTest("libxkbcommon fehlt - kein echtes Layout zum Gegenprüfen")
        for layout, variant in (("de", None), ("de", "nodeadkeys"), ("us", None)):
            table = desktop_input.build_key_table(layout, variant)
            wanted = {code for code, _level in list(table._by_char.values()) + list(table._dead.values())}
            self.assertEqual(wanted - declared, set(), (layout, variant))


# ---------------------------------------------------------------------------- DesktopInput: maths and mapping

class DesktopInputMappingTests(unittest.TestCase):
    def make(self, layout="de", open_devices=None):
        self.mouse, self.keyboard = FakeEmitter(), FakeEmitter()
        self.clock = FakeClock()
        return DesktopInput(layout=layout, clock=self.clock,
                            open_devices=open_devices or (lambda: (self.mouse, self.keyboard)))

    def test_face_buttons_click(self):
        desktop = self.make()
        desktop.apply(bits("cross"), 0, 0, 0, 0)
        self.assertEqual(self.mouse.values(EV_KEY, BTN_LEFT), [1])
        desktop.apply(bits("cross", "circle", "square"), 0, 0, 0, 0)
        desktop.apply(0, 0, 0, 0, 0)
        self.assertEqual(self.mouse.values(EV_KEY, BTN_LEFT), [1, 0])
        self.assertEqual(self.mouse.values(EV_KEY, BTN_RIGHT), [1, 0])
        self.assertEqual(self.mouse.values(EV_KEY, BTN_MIDDLE), [1, 0])
        self.assertEqual(self.keyboard.keys(), [])
        self.assertEqual(self.mouse.events.count("SYN"), 6)     # one frame per press and release

    def test_dpad_and_start_press_keys(self):
        desktop = self.make()
        desktop.apply(bits("start"), 0, 0, 0, 0)
        self.assertEqual(self.keyboard.keys(), [(KEY_LEFTMETA, 1)])
        desktop.apply(bits("up", "down", "left", "right"), 0, 0, 0, 0)
        desktop.apply(0, 0, 0, 0, 0)
        for code in (KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_LEFTMETA):
            self.assertEqual(self.keyboard.values(EV_KEY, code), [1, 0])
        self.assertEqual(self.mouse.events, [])
        before = list(self.keyboard.keys())
        desktop.apply(bits("triangle", "L1", "R1", "L2", "R2", "select", "L3", "R3"), 0, 0, 0, 0)
        desktop.apply(0, 0, 0, 0, 0)
        self.assertEqual(self.keyboard.keys(), before)                     # nothing else is bound
        self.assertEqual(self.mouse.events, [])

    def test_full_tilt_for_one_second_moves_880_pixels(self):
        desktop = self.make()
        desktop.apply(0, 0, 0, 0, 0)                  # sets the time base
        for _ in range(60):
            self.clock.advance(FRAME_S)
            desktop.apply(0, -128, 0, 0, 0)           # -128 - dead zone = the full 112 tilt
        moved = sum(self.mouse.values(EV_REL, REL_X))
        self.assertTrue(-880 <= moved <= -878, moved)   # the sub-pixel carry may still hold < 1 px
        self.assertEqual(self.mouse.values(EV_REL, REL_Y), [])
        self.assertGreaterEqual(len(self.mouse.values(EV_REL, REL_X)), 60)   # every packet moved a little

    def test_squared_response_at_half_tilt(self):
        desktop = self.make()
        desktop.apply(0, 0, 0, 0, 0)
        for _ in range(60):
            self.clock.advance(FRAME_S)
            desktop.apply(0, 0, 72, 0, 0)             # 72 - 16 = 56 = half of 112 -> a quarter of the speed
        moved = sum(self.mouse.values(EV_REL, REL_Y))
        self.assertTrue(219 <= moved <= 220, moved)

    def test_elapsed_time_is_capped_at_50ms(self):
        desktop = self.make()
        desktop.apply(0, 0, 0, 0, 0)
        self.clock.advance(5.0)                        # a long gap (or the very first packet)
        desktop.apply(0, -128, 0, 0, 0)
        self.assertEqual(self.mouse.values(EV_REL, REL_X), [-44])   # 880 px/s * 0.05 s, not 4400 px
        self.clock.advance(-1.0)                       # a clock going backwards counts as no time
        desktop.apply(0, -128, 0, 0, 0)
        self.assertEqual(self.mouse.values(EV_REL, REL_X), [-44])

    def test_sub_pixel_movement_is_carried_until_it_adds_up(self):
        desktop = self.make()
        desktop.apply(0, 0, 0, 0, 0)
        for _ in range(400):                           # 20 s at 0.07 px/s = 1.4 px
            self.clock.advance(0.05)
            desktop.apply(0, -17, 0, 0, 0)             # one count past the dead zone
        self.assertEqual(sum(self.mouse.values(EV_REL, REL_X)), -1)

    def test_dead_zone(self):
        desktop = self.make()
        desktop.apply(0, 0, 0, 0, 0)
        for _ in range(60):
            self.clock.advance(FRAME_S)
            desktop.apply(0, 15, -15, 0, 15)
            desktop.apply(0, 16, -16, 0, -16)          # exactly the dead zone is still nothing
        self.assertEqual(self.mouse.events, [])

    def test_right_stick_down_scrolls_the_page_down(self):
        desktop = self.make()
        desktop.apply(0, 0, 0, 0, 0)
        for _ in range(60):
            self.clock.advance(FRAME_S)
            desktop.apply(0, 0, 0, 0, 127)             # 111 * 42/112 = 41.6 notches/s
        self.assertEqual(sum(self.mouse.values(EV_REL, REL_WHEEL)), -41)
        self.assertEqual(sum(self.mouse.values(EV_REL, REL_WHEEL_HI_RES)), -41 * 120)
        self.assertEqual(set(self.mouse.values(EV_REL, REL_WHEEL)), {-1})
        self.assertEqual(self.mouse.values(EV_REL, REL_X), [])

    def test_right_stick_up_scrolls_up(self):
        desktop = self.make()
        desktop.apply(0, 0, 0, 0, 0)
        for _ in range(60):
            self.clock.advance(FRAME_S)
            desktop.apply(0, 0, 0, 0, -128)
        self.assertIn(sum(self.mouse.values(EV_REL, REL_WHEEL)), (41, 42))
        self.assertGreater(min(self.mouse.values(EV_REL, REL_WHEEL_HI_RES)), 0)
        # each frame carries both flavours of the same notch
        frames = [event for event in self.mouse.events if event != "SYN"]
        self.assertEqual(len(frames), 2 * len(self.mouse.values(EV_REL, REL_WHEEL)))

    def test_scroll_carry_resets_when_the_stick_rests(self):
        desktop = self.make()
        desktop.apply(0, 0, 0, 0, 0)
        for _ in range(3):
            self.clock.advance(FRAME_S)
            desktop.apply(0, 0, 0, 0, 127)             # 0.69 of a notch
            self.clock.advance(FRAME_S)
            desktop.apply(0, 0, 0, 0, 0)               # rest: the fraction is dropped, not kept
        self.assertEqual(self.mouse.values(EV_REL, REL_WHEEL), [])

    def test_release_all_lets_go_of_held_buttons(self):
        desktop = self.make()
        desktop.apply(bits("cross", "start"), 0, 0, 0, 0)
        desktop.release_all()
        self.assertEqual(self.mouse.values(EV_KEY, BTN_LEFT), [1, 0])
        self.assertEqual(self.keyboard.values(EV_KEY, KEY_LEFTMETA), [1, 0])

    def test_release_all_does_not_create_devices(self):
        opened = []
        desktop = self.make(open_devices=lambda: opened.append(1) or (self.mouse, self.keyboard))
        desktop.release_all()
        desktop.close()
        self.assertEqual(opened, [])
        self.assertFalse(desktop.is_open)

    def test_open_failure_is_logged_once_and_everything_becomes_a_no_op(self):
        def broken():
            raise PermissionError("/dev/uinput: keine Berechtigung (test-marker-4711)")
        desktop = self.make(open_devices=broken)
        desktop.apply(bits("cross"), -128, 0, 0, 0)
        desktop.apply(0, 0, 0, 0, 0)
        desktop.type_character("a")
        self.assertFalse(desktop.is_open)
        self.assertEqual(log.get_recent().count("test-marker-4711"), 1)
        self.assertIn("pad: uinput nicht verfügbar", log.get_recent())

    def test_a_broken_device_is_reported_once(self):
        class Broken(FakeEmitter):
            def write(self, etype, code, value):
                raise OSError("uinput weg (test-marker-9002)")

        log.write("test-marker-broken-maus")
        self.mouse, self.keyboard = Broken(), Broken()
        self.clock = FakeClock()
        desktop = DesktopInput(layout="de", clock=self.clock,
                               open_devices=lambda: (self.mouse, self.keyboard))
        for _ in range(60):
            self.clock.advance(FRAME_S)
            desktop.apply(bits("cross"), -128, 0, 0, 0)
            desktop.apply(0, -128, 0, 0, 0)
        desktop.type_character("z")
        desktop.close()
        recent = log_since("test-marker-broken-maus")
        self.assertEqual(recent.count("test-marker-9002"), 1)
        self.assertEqual(recent.count("pad: Eingabe an uinput fehlgeschlagen"), 1)

    def test_close_releases_and_never_reopens(self):
        desktop = self.make()
        desktop.apply(bits("cross"), 0, 0, 0, 0)
        desktop.close()
        self.assertEqual(self.mouse.values(EV_KEY, BTN_LEFT), [1, 0])
        self.assertTrue(self.mouse.closed and self.keyboard.closed)
        self.mouse.clear()
        desktop.apply(bits("cross"), -128, 0, 0, 0)
        desktop.type_character("a")
        self.assertEqual(self.mouse.events, [])
        self.assertFalse(desktop.is_open)

    def test_control_characters(self):
        desktop = self.make()
        desktop.type_character("\b")
        desktop.type_character("\t")
        desktop.type_character("\n")
        self.assertEqual(self.keyboard.keys(),
                         [(KEY_BACKSPACE, 1), (KEY_BACKSPACE, 0), (KEY_TAB, 1), (KEY_TAB, 0), (KEY_ENTER, 1), (KEY_ENTER, 0)])
        self.assertEqual(self.keyboard.events.count("SYN"), 6)

    @unittest.skipIf(not desktop_input.have_xkbcommon(), "libxkbcommon fehlt")
    def test_typing_in_the_german_layout(self):
        desktop = self.make(layout="de")
        desktop.type_character("z")
        self.assertEqual(self.keyboard.keys(), [(KEY_Y, 1), (KEY_Y, 0)])      # de swaps y and z
        self.keyboard.clear()
        desktop.type_character("Z")
        self.assertEqual(self.keyboard.keys(), [(KEY_LEFTSHIFT, 1), (KEY_Y, 1), (KEY_Y, 0), (KEY_LEFTSHIFT, 0)])
        self.keyboard.clear()
        desktop.type_character("@")
        self.assertEqual(self.keyboard.keys(), [(KEY_RIGHTALT, 1), (KEY_Q, 1), (KEY_Q, 0), (KEY_RIGHTALT, 0)])
        self.keyboard.clear()
        desktop.type_character("ä")
        self.assertEqual(self.keyboard.keys(), [(KEY_APOSTROPHE, 1), (KEY_APOSTROPHE, 0)])
        self.keyboard.clear()
        desktop.type_character("y")
        desktop.type_character(" ")
        self.assertEqual(self.keyboard.keys(), [(KEY_Z, 1), (KEY_Z, 0), (KEY_SPACE, 1), (KEY_SPACE, 0)])
        self.assertIn("pad: Tastaturlayout de, libxkbcommon", log.get_recent())
        self.assertEqual(self.mouse.events, [])

    @unittest.skipIf(not desktop_input.have_xkbcommon(), "libxkbcommon fehlt")
    def test_typing_in_the_us_layout(self):
        desktop = self.make(layout="us")
        desktop.type_character("z")
        desktop.type_character("@")
        self.assertEqual(self.keyboard.keys(),
                         [(KEY_Z, 1), (KEY_Z, 0), (KEY_LEFTSHIFT, 1), (KEY_2, 1), (KEY_2, 0), (KEY_LEFTSHIFT, 0)])

    @unittest.skipIf(not desktop_input.have_xkbcommon(), "libxkbcommon fehlt")
    def test_key_table_details(self):
        table = desktop_input.build_key_table("de", "nodeadkeys")
        self.assertEqual(table.lookup("z"), (KEY_Y, 0))
        self.assertEqual(table.lookup("€"), (18, 2))                 # AltGr+E, not the KEY_EURO media key
        self.assertEqual(table.lookup("1"), (2, 0))                 # the number row, not Shift on the keypad
        self.assertIsNone(table.lookup("中"))
        self.assertEqual(table.describe(), "de(nodeadkeys), libxkbcommon")
        self.assertIs(desktop_input.get_key_table("de", "nodeadkeys"), desktop_input.get_key_table("de", "nodeadkeys"))
        with self.assertRaises(OSError):
            desktop_input.build_key_table("kein-solches-layout-4711")
        fallback = desktop_input.get_key_table("kein-solches-layout-4711")
        self.assertEqual(fallback.lookup("z")[0], KEY_Z)               # us instead
        self.assertIn("pad: Tastaturlayout kein-solches-layout-4711 nicht ladbar", log.get_recent())

    @unittest.skipIf(not desktop_input.have_xkbcommon(), "libxkbcommon fehlt")
    def test_characters_stay_on_the_ordinary_keys(self):
        """The stock layouts also put some characters on media keys - taking those would be a trap.

        '(' ')' sit on KEY_KPLEFTPAREN/KPRIGHTPAREN and '$' '€' on KEY_DOLLAR/KEY_EURO (codes 434/435),
        all at level 0, so the fewest-modifiers rule alone would prefer them over Shift+8 or AltGr+E.
        Codes above 247 are xkb keycodes above 255, which X11 cannot express at all: an Xwayland client
        would never be told about them and '$' and '€' would type nothing there."""
        for layout, characters in (("de", "()$€"), ("us", "()$")):
            table = desktop_input.build_key_table(layout, None)
            for character in characters:
                code, level = table.lookup(character)
                self.assertLessEqual(code, desktop_input.MAIN_KEY_CODE_MAX, (layout, character, code))
                self.assertGreater(level, 0, (layout, character))     # they are all Shift or AltGr keys
            self.assertEqual({character: entry for character, entry in table._by_char.items()
                              if entry[0] > desktop_input.MAIN_KEY_CODE_MAX and character in "()$"}, {})
        table = desktop_input.build_key_table("de", None)
        self.assertEqual(table.lookup("("), (9, 1))       # Shift+8
        self.assertEqual(table.lookup(")"), (10, 1))      # Shift+9
        self.assertEqual(table.lookup("$"), (5, 1))       # Shift+4
        self.assertEqual(table.lookup("1"), (2, 0))       # and still the number row, not Shift on the keypad
        self.assertEqual(desktop_input.KeyTable._rank(2, 1), (0, 1))
        self.assertEqual(desktop_input.KeyTable._rank(435, 0), (1, 0))
        self.assertLess(desktop_input.KeyTable._rank(2, 3), desktop_input.KeyTable._rank(435, 0))

    @unittest.skipIf(not desktop_input.have_xkbcommon(), "libxkbcommon fehlt")
    def test_dead_keys_are_typed_as_the_key_then_a_space(self):
        """'^' and '`' exist only as dead keys on the stock 'de' layout; Windows injected them as Unicode."""
        desktop = self.make(layout="de")
        desktop.type_character("^")
        self.assertEqual(self.keyboard.keys(), [(41, 1), (41, 0), (KEY_SPACE, 1), (KEY_SPACE, 0)])
        self.keyboard.clear()
        desktop.type_character("`")               # Shift on the dead key, and the space AFTER Shift is up
        self.assertEqual(self.keyboard.keys(),
                         [(KEY_LEFTSHIFT, 1), (13, 1), (13, 0), (KEY_LEFTSHIFT, 0), (KEY_SPACE, 1), (KEY_SPACE, 0)])
        self.keyboard.clear()
        desktop.type_character("ö")               # a real key: no space anywhere near it
        self.assertEqual(self.keyboard.keys(), [(39, 1), (39, 0)])

    def test_a_broken_layout_is_complained_about_once_even_from_two_threads(self):
        """The warm-up thread and a KEY packet can reach get_key_table at the same moment."""
        name = "kein-solches-layout-%d" % time.monotonic_ns()
        log.write("test-marker-layout-race")
        tables, gate = [], threading.Barrier(4)

        def build():
            gate.wait()
            tables.append(desktop_input.get_key_table(name))

        workers = [threading.Thread(target=build, daemon=True) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(tables), 4)
        self.assertEqual(len({id(table) for table in tables}), 1)     # everyone ends up on the cached one
        self.assertEqual(log_since("test-marker-layout-race").count("pad: Tastaturlayout %s nicht ladbar" % name), 1)

    def test_the_layout_table_is_warmed_up_off_the_packet_path(self):
        """The first KEY packet arrives on the receive thread; the table must already be there."""
        desktop_input._tables.pop(("de", None), None)
        desktop = self.make(layout="de")
        desktop.apply(0, 0, 0, 0, 0)                       # creates the devices -> warm-up starts
        for name in ("keytable",):
            worker = next((t for t in threading.enumerate() if t.name == name), None)
            if worker is not None:
                self.assertTrue(worker.daemon)             # never keeps the process alive
                worker.join(10)
                self.assertFalse(worker.is_alive())
        self.assertIn(("de", None), desktop_input._tables)
        desktop.close()

    def test_the_layout_lookup_cannot_outlast_the_client_watchdog(self):
        """detect_layout shells out twice, worst case on the receive thread; the PS3 counts as gone after
        CLIENT_TIMEOUT_MS without a pad packet, so two hung calls must stay well inside that."""
        self.assertLess(2 * desktop_input.GSETTINGS_TIMEOUT_S * 1000, protocol.CLIENT_TIMEOUT_MS)

    def test_unknown_character_is_logged_once_and_ignored(self):
        desktop = self.make()
        desktop.type_character("中")
        desktop.type_character("中")
        desktop.type_character("")
        self.assertEqual(self.keyboard.events, [])
        self.assertEqual(log.get_recent().count("pad: kein Tastencode für '中'"), 1)

    def test_us_fallback_table(self):
        table = desktop_input.us_fallback_table()
        self.assertEqual(table.lookup("z"), (KEY_Z, 0))
        self.assertEqual(table.lookup("Z"), (KEY_Z, 1))
        self.assertEqual(table.lookup("@"), (KEY_2, 1))
        self.assertEqual(table.lookup(" "), (KEY_SPACE, 0))
        self.assertEqual(table.lookup("'"), (KEY_APOSTROPHE, 0))
        self.assertIsNone(table.lookup("ä"))
        self.assertEqual(table.source, "us-fallback")

    def test_parse_input_sources(self):
        parse = desktop_input.parse_input_sources
        self.assertEqual(parse("[('xkb', 'de')]\n"), ("de", None))
        self.assertEqual(parse("[('xkb', 'de+nodeadkeys'), ('xkb', 'us')]"), ("de", "nodeadkeys"))
        self.assertEqual(parse("[('ibus', 'mozc-jp'), ('xkb', 'us+altgr-intl')]"), ("us", "altgr-intl"))
        self.assertIsNone(parse("@a(ss) []"))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("garbage ("))
        self.assertIsNone(parse("[('ibus', 'x')]"))

    def test_detect_layout_always_answers(self):
        layout, variant = desktop_input.detect_layout()
        self.assertTrue(layout)
        self.assertTrue(variant is None or isinstance(variant, str))


# ---------------------------------------------------------------------------- DesktopInput: the real uinput devices

@unittest.skipIf(evdev is None, "python3-evdev fehlt")
class DesktopInputDeviceTests(unittest.TestCase):
    """Reads back from the virtual mouse and keyboard. Both nodes are grabbed first, so the real
    desktop never sees the click, the Super key or the typed letters; the pointer motion is 1 px."""

    @classmethod
    def setUpClass(cls):
        cls.clock = FakeClock()
        cls.desktop = DesktopInput(layout="de", clock=cls.clock)
        cls.desktop.apply(0, 0, 0, 0, 0)          # creates the devices; at rest nothing is emitted
        if not cls.desktop.is_open:
            cls.desktop.close()
            raise unittest.SkipTest("uinput nicht verfügbar: " + log.get_recent().strip().splitlines()[-1])
        mouse_path, keyboard_path = cls.desktop.device_paths
        cls.mouse = open_readable(mouse_path, MOUSE_NAME) if mouse_path else None
        cls.keyboard = open_readable(keyboard_path, KEYBOARD_NAME) if keyboard_path else None
        if cls.mouse is None or cls.keyboard is None:
            cls.tearDownClass()
            raise unittest.SkipTest(
                "kein Lesezugriff auf %s / %s - /dev/input/event* ist root:input und 70-uaccess.rules gibt die "
                "ACL nur Joysticks (gemessen: udev stuft Tastatur/Maus vorher ein, keine Capability-Kombination "
                "holt sie). Ohne Gruppe 'input' entfaellt das Ruecklesen; RealDeviceCapabilityTests prueft das "
                "echte Geraet stattdessen ueber sysfs." % (mouse_path, keyboard_path))
        for node in (cls.mouse, cls.keyboard):
            os.set_blocking(node.fd, False)
            try:
                node.grab()                        # the compositor must not act on what the test emits
            except OSError as error:
                cls.tearDownClass()
                raise unittest.SkipTest("Node lässt sich nicht exklusiv greifen: %s" % error)

    @classmethod
    def tearDownClass(cls):
        for node in (getattr(cls, "mouse", None), getattr(cls, "keyboard", None)):
            if node is not None:
                try:
                    node.ungrab()
                except OSError:
                    pass
                node.close()
        cls.mouse = cls.keyboard = None
        cls.desktop.close()

    def setUp(self):
        self.clock.advance(0.1)
        self.desktop.apply(0, 0, 0, 0, 0)
        drain(self.mouse, 0.05)
        drain(self.keyboard, 0.05)

    def test_cross_clicks_the_left_button(self):
        self.desktop.apply(bits("cross"), 0, 0, 0, 0)
        self.assertEqual(key_events(drain(self.mouse)), [(BTN_LEFT, 1)])
        self.desktop.apply(0, 0, 0, 0, 0)
        self.assertEqual(key_events(drain(self.mouse)), [(BTN_LEFT, 0)])
        self.assertEqual(drain(self.keyboard, 0.05), [])

    def test_start_presses_super(self):
        self.desktop.apply(bits("start"), 0, 0, 0, 0)
        self.desktop.apply(0, 0, 0, 0, 0)
        self.assertEqual(key_events(drain(self.keyboard)), [(KEY_LEFTMETA, 1), (KEY_LEFTMETA, 0)])

    def test_typed_characters(self):
        self.desktop.type_character("\n")
        self.assertEqual(key_events(drain(self.keyboard)), [(KEY_ENTER, 1), (KEY_ENTER, 0)])
        if not desktop_input.have_xkbcommon():
            self.skipTest("libxkbcommon fehlt - kein deutsches Layout")
        self.desktop.type_character("z")
        self.assertEqual(key_events(drain(self.keyboard)), [(KEY_Y, 1), (KEY_Y, 0)])
        self.desktop.type_character("Z")
        self.assertEqual(key_events(drain(self.keyboard)), [(KEY_LEFTSHIFT, 1), (KEY_Y, 1), (KEY_Y, 0), (KEY_LEFTSHIFT, 0)])
        self.desktop.type_character("@")
        self.assertEqual(key_events(drain(self.keyboard)), [(KEY_RIGHTALT, 1), (KEY_Q, 1), (KEY_Q, 0), (KEY_RIGHTALT, 0)])
        self.desktop.type_character("ä")
        self.assertEqual(key_events(drain(self.keyboard)), [(KEY_APOSTROPHE, 1), (KEY_APOSTROPHE, 0)])
        self.assertEqual(self.keyboard.active_keys(), [])

    def test_pointer_moves_one_pixel(self):
        self.clock.advance(0.002)                    # 880 px/s * 2 ms = 1.76 px -> 1 px and a carry
        self.desktop.apply(0, -128, 0, 0, 0)
        moved = sum(value for etype, code, value in drain(self.mouse) if etype == EV_REL and code == REL_X)
        self.assertIn(moved, (-1, -2))
        self.desktop.apply(0, 0, 0, 0, 0)

    def test_scroll_one_notch_down(self):
        self.clock.advance(0.03)                     # 41.6 notches/s * 30 ms = 1.25 notches
        self.desktop.apply(0, 0, 0, 0, 127)
        state = last_values(drain(self.mouse))
        self.assertEqual(state.get((EV_REL, REL_WHEEL)), -1)
        self.assertEqual(state.get((EV_REL, REL_WHEEL_HI_RES)), -120)
        self.desktop.apply(0, 0, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
