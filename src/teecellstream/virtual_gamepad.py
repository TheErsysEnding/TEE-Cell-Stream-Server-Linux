"""A virtual Xbox 360 controller (port of VirtualGamepad.cs).

Windows had no way for a program to fake a gamepad without the ViGEmBus driver; Linux has uinput built
in, so there is nothing to install. The device carries the real wired 360 pad's name and USB ids, so
SDL, Steam and Proton pick their built-in mapping for it and games see an ordinary controller plugged in.

Axis directions: the PS3 reads y positive downwards, and so does evdev (xpad reports a real 360 pad
that way too), so unlike the XInput report on Windows nothing is inverted here.

open_uinput() lives here because desktop_input.py needs the same guarded UInput (see there).
"""

import fcntl
import os
import threading

try:
    import evdev
    from evdev import AbsInfo, UInput
except ImportError:                 # python3-evdev is a package dependency; without it the pad simply does nothing
    evdev = None

from . import log
from .protocol import PadBits
from .i18n import _

DEVICE_NAME = "Microsoft X-Box 360 pad"
VENDOR_ID = 0x045E
PRODUCT_ID = 0x028E
VERSION = 0x0110
BUS_USB = 0x03

# event codes from linux/input-event-codes.h, spelled out so the mapping reads without evdev installed
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
BTN_A, BTN_B, BTN_X, BTN_Y = 0x130, 0x131, 0x133, 0x134
BTN_TL, BTN_TR = 0x136, 0x137
BTN_SELECT, BTN_START, BTN_MODE = 0x13A, 0x13B, 0x13C
BTN_THUMBL, BTN_THUMBR = 0x13D, 0x13E
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11

STICK_DEAD_ZONE = 12          # the PS3's sticks rest a few counts off centre
STICK_FULL_TILT = 115.0
AXIS_MAX = 32767              # sticks -32768..32767, like xpad
STICK_FUZZ, STICK_FLAT = 16, 128
TRIGGER_MAX = 255             # L2/R2 are digital in the PS3 packet: released or fully pulled

# PS3 bit -> Xbox button, as xpad reports a real 360 pad. BTN_MODE (guide) is declared for completeness;
# the PS3 keeps its PS button to itself, so it is never pressed.
BUTTON_MAP = (
    (PadBits.CROSS, BTN_A), (PadBits.CIRCLE, BTN_B), (PadBits.SQUARE, BTN_X), (PadBits.TRIANGLE, BTN_Y),
    (PadBits.L1, BTN_TL), (PadBits.R1, BTN_TR), (PadBits.SELECT, BTN_SELECT), (PadBits.START, BTN_START),
    (PadBits.L3, BTN_THUMBL), (PadBits.R3, BTN_THUMBR),
)

UI_GET_SYSNAME_64 = 0x8040552C    # _IOC(_IOC_READ, 'U', 44, 64): the sysfs name of a uinput device


def to_axis(value: int) -> int:
    """The PS3 reads -128..127 and rests a little off centre; evdev wants -32768..32767 centred."""
    tilt = 0.0
    if value >= STICK_DEAD_ZONE:
        tilt = (value - STICK_DEAD_ZONE) / (STICK_FULL_TILT - STICK_DEAD_ZONE)
    elif value <= -STICK_DEAD_ZONE:
        tilt = (value + STICK_DEAD_ZONE) / (STICK_FULL_TILT - STICK_DEAD_ZONE)
    tilt = max(-1.0, min(1.0, tilt))
    return int(tilt * AXIS_MAX)     # truncates toward zero, like the (short) cast in the original


def report_for(buttons: int, left_x: int, left_y: int, right_x: int, right_y: int) -> list[tuple[int, int, int]]:
    """One full controller state as (type, code, value) events, in the order they are written.

    buttons is the PS3's bitmask (PadBits); sticks are -128..127 with y positive downwards.
    """
    events = [(EV_KEY, code, 1 if buttons & (1 << bit) else 0) for bit, code in BUTTON_MAP]
    events.append((EV_ABS, ABS_Z, TRIGGER_MAX if buttons & (1 << PadBits.L2) else 0))
    events.append((EV_ABS, ABS_RZ, TRIGGER_MAX if buttons & (1 << PadBits.R2) else 0))
    events.append((EV_ABS, ABS_X, to_axis(left_x)))
    events.append((EV_ABS, ABS_Y, to_axis(left_y)))        # y down on the PS3 = y down on evdev: no inversion
    events.append((EV_ABS, ABS_RX, to_axis(right_x)))
    events.append((EV_ABS, ABS_RY, to_axis(right_y)))
    hat_x = (1 if buttons & (1 << PadBits.RIGHT) else 0) - (1 if buttons & (1 << PadBits.LEFT) else 0)
    hat_y = (1 if buttons & (1 << PadBits.DOWN) else 0) - (1 if buttons & (1 << PadBits.UP) else 0)   # up = -1
    events.append((EV_ABS, ABS_HAT0X, hat_x))
    events.append((EV_ABS, ABS_HAT0Y, hat_y))
    return events


def capabilities() -> dict:
    """The uinput capability table of a wired Xbox 360 pad, as xpad presents one."""
    stick = AbsInfo(value=0, min=-AXIS_MAX - 1, max=AXIS_MAX, fuzz=STICK_FUZZ, flat=STICK_FLAT, resolution=0)
    trigger = AbsInfo(value=0, min=0, max=TRIGGER_MAX, fuzz=0, flat=0, resolution=0)
    hat = AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)
    return {
        EV_KEY: [code for _bit, code in BUTTON_MAP] + [BTN_MODE],
        EV_ABS: [(ABS_X, stick), (ABS_Y, stick), (ABS_RX, stick), (ABS_RY, stick),
                 (ABS_Z, trigger), (ABS_RZ, trigger), (ABS_HAT0X, hat), (ABS_HAT0Y, hat)],
    }


if evdev is not None:
    class _WriteOnlyUInput(UInput):
        """python-evdev's UInput also opens the node it just created for READING, retries that for two
        seconds and then raises - and on Ubuntu /dev/input/event* is root:input, which a desktop user is
        not in (only joysticks get a uaccess ACL). We only ever write, and everything we write goes
        through the uinput fd, so a node we cannot read is no failure at all: skip the open."""

        def _find_device(self, fd):
            return None


def open_uinput(events: dict, name: str, vendor: int, product: int, version: int, bustype: int):
    """Creates a uinput device. Raises OSError/UInputError with a readable reason when it cannot."""
    if evdev is None:
        raise OSError("python3-evdev is missing")
    return _WriteOnlyUInput(events, name=name, vendor=vendor, product=product, version=version, bustype=bustype)


def node_path(device) -> str | None:
    """/dev/input/eventN of a uinput device, for the log. None when it cannot be told (never an error)."""
    try:
        sysname = bytearray(64)
        fcntl.ioctl(device.fd, UI_GET_SYSNAME_64, sysname)
        sysname = sysname.split(b"\0", 1)[0].decode("ascii")
        for entry in os.listdir("/sys/devices/virtual/input/" + sysname):
            if entry.startswith("event"):
                return "/dev/input/" + entry
    except (OSError, ValueError, AttributeError):
        pass
    return None


class VirtualGamepad:
    """Plug in with try_open(), feed it the PS3's state with send(), unplug with close()."""

    def __init__(self):
        self._gate = threading.RLock()
        self._device = None
        self._write_failed = False
        self.path: str | None = None

    @property
    def is_open(self) -> bool:
        return self._device is not None

    # false means we have no gamepad: /dev/uinput is missing or not ours to write (the udev rule grants
    # it to the logged-in user). the caller falls back to mouse + keyboard.
    def try_open(self) -> bool:
        with self._gate:
            if self._device is not None:
                return True
            if evdev is None:
                log.write(_("pad: python3-evdev is missing - no virtual gamepad possible"))
                return False
            try:
                device = open_uinput(capabilities(), DEVICE_NAME, VENDOR_ID, PRODUCT_ID, VERSION, BUS_USB)
            except Exception as error:   # noqa: BLE001 - UInputError, PermissionError, FileNotFoundError ...
                log.write("pad: could not create the virtual gamepad: %s "
                          "(is the uinput module loaded and /dev/uinput writable?)" % error)
                return False
            self._device = device
            self._write_failed = False
            self.path = node_path(device)
            log.write("pad: virtual Xbox 360 gamepad created" + (" (%s)" % self.path if self.path else ""))
            # a first report at rest, so nothing reads as held. no settle time needed: unlike Windows, an
            # event that arrives before anyone has the device open is simply dropped, and the next of the
            # PS3's 60 reports a second repeats the state anyway.
            self._send_locked(0, 0, 0, 0, 0)
            return True

    # buttons is the PS3's bitmask (see PadBits); sticks are -128..127 with y positive downwards
    def send(self, buttons: int, left_x: int, left_y: int, right_x: int, right_y: int) -> None:
        with self._gate:
            if self._device is None:
                return
            self._send_locked(buttons, left_x, left_y, right_x, right_y)

    def _send_locked(self, buttons, left_x, left_y, right_x, right_y) -> None:
        # the whole state every time; the kernel drops what has not changed, so this costs nothing
        try:
            for etype, code, value in report_for(buttons, left_x, left_y, right_x, right_y):
                self._device.write(etype, code, value)
            self._device.syn()
        except OSError as error:
            if not self._write_failed:
                self._write_failed = True     # say it once, not 60 times a second
                log.write(_("pad: gamepad report failed: %s") % error)

    def close(self) -> None:
        with self._gate:
            if self._device is None:
                return
            self._send_locked(0, 0, 0, 0, 0)   # let go of everything before the device vanishes
            try:
                self._device.close()
            except OSError:
                pass
            self._device = None
            self.path = None
            log.write(_("pad: virtual gamepad removed"))
