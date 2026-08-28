"""Switches the desktop to the resolution we stream at, and puts it back afterwards (port of DisplayMode.cs).

This is the difference between sharp text and mush. Shrinking a 1440p or 1080p desktop into a 720p stream
is a 2x / 1.5x resize, and text does not survive it - no bitrate or encoder setting brought it back (all
tried upstream). Matching the desktop to the stream means no resize at all: every pixel of the desktop is
one pixel of video. It is also what Steam Link and Moonlight do, for the same reason.

Restoring is the part that must not fail, so it is defended three ways:
  - the mode is applied as TEMPORARY (never written to monitors.xml), so a re-login puts it back,
  - we restore explicitly when the stream stops,
  - and again on exit, SIGTERM/SIGINT/SIGHUP and atexit (server.install_exit_hooks calls restore()).

Backends: GNOME's mutter (Wayland and GNOME-on-X11) over org.gnome.Mutter.DisplayConfig, xrandr for other
X11 sessions, and a null backend that only logs when there is neither. Windows switched with
ChangeDisplaySettingsEx(CDS_FULLSCREEN); mutter's ApplyMonitorsConfig(method=1) is the same idea.
"""

import math
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field

from gi.repository import Gio, GLib

from . import log

MUTTER_BUS_NAME = "org.gnome.Mutter.DisplayConfig"
MUTTER_OBJECT_PATH = "/org/gnome/Mutter/DisplayConfig"
MUTTER_INTERFACE = "org.gnome.Mutter.DisplayConfig"
CURRENT_STATE_TYPE = GLib.VariantType("(ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv})")
APPLY_CONFIG_TYPE = "(uua(iiduba(ssa{sv}))a{sv})"

# ApplyMonitorsConfig methods: 0 = only verify, 1 = temporary (until logout / hotplug), 2 = persistent (monitors.xml)
METHOD_VERIFY = 0
METHOD_TEMPORARY = 1
METHOD_PERSISTENT = 2

# mutter 'layout-mode': 1 = logical monitors are positioned in scaled (logical) pixels, 2 = in physical pixels
LAYOUT_MODE_LOGICAL = 1
LAYOUT_MODE_PHYSICAL = 2

REFRESH_TOLERANCE_HZ = 0.2      # '1280x720@59.943' or '@60.000' both count as the 60 we stream at
STATE_TIMEOUT_MS = 5000
APPLY_TIMEOUT_MS = 20000        # a mode switch takes a moment (monitor re-sync); mutter answers after it
XRANDR_TIMEOUT_S = 20


class DisplayError(Exception):
    """The compositor would not do (or tell) what we asked; the message is the reason for the log."""


# ---------------------------------------------------------------------------------------------- mutter state

@dataclass
class Mode:
    id: str
    width: int
    height: int
    refresh: float
    preferred_scale: float = 1.0
    supported_scales: list = field(default_factory=list)
    properties: dict = field(default_factory=dict)

    @property
    def is_current(self) -> bool:
        return bool(self.properties.get("is-current", False))

    @property
    def is_variable_rate(self) -> bool:   # the '+vrr' twin of a mode: same size and rate, adaptive sync on
        return self.properties.get("refresh-rate-mode") == "variable"


@dataclass
class Monitor:
    connector: str
    vendor: str
    product: str
    serial: str
    modes: list
    properties: dict = field(default_factory=dict)

    def current_mode(self) -> Mode | None:
        for mode in self.modes:
            if mode.is_current:
                return mode
        return None


@dataclass
class LogicalMonitor:
    x: int
    y: int
    scale: float
    transform: int
    primary: bool
    connectors: list          # connector names of the physical monitors shown here (several = mirrored)
    properties: dict = field(default_factory=dict)

    @property
    def is_rotated(self) -> bool:   # transforms 1,3 (90/270) and their flipped twins 5,7 swap width and height
        return self.transform % 2 == 1


@dataclass
class DisplayState:
    serial: int
    monitors: list
    logical_monitors: list
    properties: dict = field(default_factory=dict)

    @property
    def layout_mode(self) -> int:
        return int(self.properties.get("layout-mode", LAYOUT_MODE_LOGICAL))

    @property
    def supports_changing_layout_mode(self) -> bool:   # Wayland: yes; X11: no (and it refuses being told one)
        return bool(self.properties.get("supports-changing-layout-mode", False))

    @property
    def global_scale_required(self) -> bool:   # GNOME on X11: every logical monitor must have the same scale
        return bool(self.properties.get("global-scale-required", False))

    def apply_properties(self) -> dict:
        """ApplyMonitorsConfig's top-level properties: the layout mode our positions are expressed in, wherever
        mutter lets a client say so. Left out, mutter assumes its DEFAULT layout mode, not the current one - and a
        monitors.xml written under the other setting keeps the current one different - so every position would be
        read in the wrong unit. gnome-control-center sends it the same way. X11 refuses the key ("Can't set
        layout mode"), and there it is left out."""
        if self.supports_changing_layout_mode:
            return {"layout-mode": GLib.Variant("u", self.layout_mode)}
        return {}

    def find_monitor(self, connector: str) -> Monitor | None:
        for monitor in self.monitors:
            if monitor.connector == connector:
                return monitor
        return None

    def primary_logical_monitor(self) -> LogicalMonitor | None:
        """The primary one; failing that (mutter always marks one, but be safe) the first with a monitor."""
        for logical in self.logical_monitors:
            if logical.primary and logical.connectors:
                return logical
        for logical in self.logical_monitors:
            if logical.connectors:
                return logical
        return None


def parse_current_state(reply) -> DisplayState:
    """The unpacked GetCurrentState tuple (serial, monitors, logical_monitors, properties) as objects."""
    serial, monitors, logical_monitors, properties = reply
    parsed_monitors = []
    for (connector, vendor, product, monitor_serial), modes, monitor_properties in monitors:
        parsed_modes = [Mode(mode_id, int(width), int(height), float(refresh), float(preferred_scale),
                             list(scales), dict(mode_properties))
                        for mode_id, width, height, refresh, preferred_scale, scales, mode_properties in modes]
        parsed_monitors.append(Monitor(connector, vendor, product, monitor_serial, parsed_modes, dict(monitor_properties)))
    parsed_logical = [LogicalMonitor(int(x), int(y), float(scale), int(transform), bool(primary),
                                     [spec[0] for spec in specs], dict(logical_properties))
                      for x, y, scale, transform, primary, specs, logical_properties in logical_monitors]
    return DisplayState(int(serial), parsed_monitors, parsed_logical, dict(properties))


def rank_modes(modes: list, width: int, height: int, refresh_hz: float) -> list:
    """Every WxH mode, best first: the wanted rate (±REFRESH_TOLERANCE_HZ) before any other, then one at or above
    the rate before one below it, the closest first, and a fixed-rate mode before its '+vrr' twin."""
    candidates = [mode for mode in modes if mode.width == width and mode.height == height]

    def rank(mode: Mode):
        distance = abs(mode.refresh - refresh_hz)
        return (distance > REFRESH_TOLERANCE_HZ, mode.is_variable_rate, mode.refresh < refresh_hz, distance)

    return sorted(candidates, key=rank)


# GNOME's ScreenCast hands out only about two thirds of the desktop's refresh rate. Measured against the
# real console, same software and same content both times: 40.1 of 60 pictures a second with the desktop
# switched to 1280x720@60, and 60.1 with it left at 2560x1440@320 - even though the second case makes us
# scale 1440p down to 720p and the first was pixel for pixel. So the Windows original's trick of putting
# the desktop at the stream's own size and frame rate, which costs nothing there, throws away a third of
# the frames here, because 1280x720 tops out at 60 Hz on most monitors.
#
# Among the modes that DO leave the compositor room, take the smallest: every pixel is read back out of
# graphics memory once per frame, and that read is what the input delay is made of (2560x1440 is 14.7 MB
# a frame against 8.3 MB for 1920x1080).
CAPTURE_REFRESH_FACTOR = 1.5   # 60 fps wants a 90 Hz desktop before the compositor keeps up


def choose_capture_mode(modes: list, stream_width: int, stream_height: int, fps: int) -> tuple:
    """(width, height, refresh) the desktop should run at while streaming `stream_width`x`stream_height`@fps."""
    roomy = [mode for mode in modes
             if mode.width >= stream_width and mode.height >= stream_height
             and mode.refresh >= fps * CAPTURE_REFRESH_FACTOR and not mode.is_variable_rate]
    if not roomy:
        # no mode is fast enough (a plain 60 Hz monitor): the third is lost whatever we do, so fall back to
        # the original's pixel-for-pixel switch, which at least keeps the capture small and the picture sharp
        return stream_width, stream_height, float(fps)
    best = min(roomy, key=lambda mode: (mode.width * mode.height, -mode.refresh))
    return best.width, best.height, best.refresh



def find_mode(modes: list, width: int, height: int, refresh_hz: float) -> Mode | None:
    """The mode to switch to, or None when the monitor has no WxH at all."""
    ranked = rank_modes(modes, width, height, refresh_hz)
    return ranked[0] if ranked else None


def monitor_apply_properties(monitor: Monitor) -> dict:
    """The per-monitor settings ApplyMonitorsConfig resets to their defaults when a config leaves them out, carried
    over from GetCurrentState so the switch (and the restore) changes nothing but the mode: underscanning (sent only
    while on - mutter refuses the key on a monitor that cannot do it, checked in VERIFY mode), the colour mode and
    the RGB range (mutter 47+; reported and accepted by the same versions, and an unknown key is ignored). The
    values are GLib.Variants, as the a{sv} builder wants them."""
    properties = {}
    if monitor.properties.get("is-underscanning") is True:
        properties["underscanning"] = GLib.Variant("b", True)
    for key in ("color-mode", "rgb-range"):
        value = monitor.properties.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            properties[key] = GLib.Variant("u", value)
    return properties


def logical_size(mode: Mode, scale: float, rotated: bool, layout_mode: int) -> tuple[int, int]:
    """How much desktop a mode covers at this scale, in the units logical monitors are positioned in."""
    width, height = (mode.height, mode.width) if rotated else (mode.width, mode.height)
    if layout_mode == LAYOUT_MODE_LOGICAL and scale > 0:
        # roundf() like mutter (half away from zero), not Python's round() (half to even)
        return int(math.floor(width / scale + 0.5)), int(math.floor(height / scale + 0.5))
    return width, height


def current_config(state: DisplayState) -> list:
    """The layout exactly as it is (modes, positions, scales, per-monitor settings), in ApplyMonitorsConfig's
    a(iiduba(ssa{sv})) shape - what restore() re-applies."""
    config = []
    for logical in state.logical_monitors:
        monitors = []
        for connector in logical.connectors:
            monitor = state.find_monitor(connector)
            mode = monitor.current_mode() if monitor is not None else None
            if mode is not None:
                monitors.append((connector, mode.id, monitor_apply_properties(monitor)))
        if monitors:
            config.append((logical.x, logical.y, logical.scale, logical.transform, logical.primary, monitors))
    return config


def build_config(state: DisplayState, primary: LogicalMonitor, new_mode: Mode, new_scale: float = 1.0) -> list:
    """The layout with the primary logical monitor put into new_mode at new_scale and everything else unchanged -
    except that monitors sitting right of (or below) the primary follow its shrinking edge, because mutter
    refuses a layout with a gap in it ("Logical monitors not adjacent"), and that on a backend with one global
    scale (GNOME on X11) every monitor takes new_scale, because mutter refuses mixed ones there ("Logical monitor
    scales must be identical")."""
    primary_monitor = state.find_monitor(primary.connectors[0]) if primary.connectors else None
    old_mode = primary_monitor.current_mode() if primary_monitor is not None else None
    if old_mode is None:
        raise DisplayError("kein aktiver Modus auf dem primären Monitor")
    old_width, old_height = logical_size(old_mode, primary.scale, primary.is_rotated, state.layout_mode)
    new_width, new_height = logical_size(new_mode, new_scale, primary.is_rotated, state.layout_mode)
    shift_x = old_width - new_width
    shift_y = old_height - new_height
    right_edge, bottom_edge = primary.x + old_width, primary.y + old_height

    config = []
    for logical in state.logical_monitors:
        monitors = []
        first_mode = None
        for connector in logical.connectors:
            monitor = state.find_monitor(connector)
            if monitor is None:
                continue
            if logical is primary:
                # a mirrored primary shows the same picture on every monitor in it, so each needs the size
                mode = new_mode if connector == primary.connectors[0] else find_mode(monitor.modes, new_mode.width, new_mode.height, new_mode.refresh)
                if mode is None:
                    raise DisplayError("%s (gespiegelt) hat keinen Modus %dx%d" % (connector, new_mode.width, new_mode.height))
            else:
                mode = monitor.current_mode()
            if mode is not None:
                monitors.append((connector, mode.id, monitor_apply_properties(monitor)))
                first_mode = first_mode or mode
        if not monitors:
            continue
        if logical is primary:
            config.append((logical.x, logical.y, new_scale, logical.transform, True, monitors))
            continue
        scale = new_scale if state.global_scale_required else logical.scale
        x, y = logical.x, logical.y
        if x >= right_edge:
            x -= shift_x
        if y >= bottom_edge:
            y -= shift_y
        # one that stood against the primary's right (bottom) edge must still overlap it in y (x) afterwards: mutter
        # wants every monitor touching a neighbour along an edge, a corner is not enough. a 1080p monitor bottom-
        # aligned to a 4K desktop sits lower than 720 and would end up beside nothing - so it is aligned to the new
        # bottom (right) edge, or to the top (left) when it is now the taller (wider) one. restore() undoes all of it
        width, height = logical_size(first_mode, scale, logical.is_rotated, state.layout_mode)
        if logical.x == right_edge and logical.y < bottom_edge and y >= primary.y + new_height:
            y = max(primary.y, primary.y + new_height - height)
        elif logical.y == bottom_edge and logical.x < right_edge and x >= primary.x + new_width:
            x = max(primary.x, primary.x + new_width - width)
        config.append((x, y, scale, logical.transform, logical.primary, monitors))
    return config


# ---------------------------------------------------------------------------------------------- backends

@dataclass
class _Snapshot:
    """What read() found: the primary's current size, plus whatever the backend needs to switch it."""
    width: int
    height: int
    detail: str = ""
    payload: object = None


def _dbus_error_text(error: GLib.Error) -> str:
    # "GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: The requested configuration ..." -> the reason
    return re.sub(r"^GDBus\.Error:[^:]+:\s*", "", error.message or "").strip() or str(error)


_bus = None   # GLib holds its shared connection only weakly; keep ours so each call does not reconnect to the bus


def session_bus():
    global _bus
    if _bus is None or _bus.is_closed():
        _bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return _bus


def mutter_available(bus=None) -> bool:
    try:
        bus = bus or session_bus()
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", (MUTTER_BUS_NAME,)), GLib.VariantType("(b)"),
                              Gio.DBusCallFlags.NONE, STATE_TIMEOUT_MS, None)
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


def read_current_state(bus=None) -> DisplayState:
    """GetCurrentState, parsed. Read-only; safe to call any time."""
    try:
        bus = bus or session_bus()
        reply = bus.call_sync(MUTTER_BUS_NAME, MUTTER_OBJECT_PATH, MUTTER_INTERFACE, "GetCurrentState", None,
                              CURRENT_STATE_TYPE, Gio.DBusCallFlags.NONE, STATE_TIMEOUT_MS, None)
    except GLib.Error as error:
        raise DisplayError(_dbus_error_text(error)) from None
    return parse_current_state(reply.unpack())


def apply_config(serial: int, config: list, method: int = METHOD_TEMPORARY, bus=None, properties: dict | None = None) -> None:
    """ApplyMonitorsConfig with the given layout (and DisplayState.apply_properties()). Raises DisplayError with
    mutter's reason."""
    parameters = GLib.Variant(APPLY_CONFIG_TYPE, (serial, method, config, properties or {}))
    try:
        bus = bus or session_bus()
        bus.call_sync(MUTTER_BUS_NAME, MUTTER_OBJECT_PATH, MUTTER_INTERFACE, "ApplyMonitorsConfig", parameters, None,
                      Gio.DBusCallFlags.NONE, APPLY_TIMEOUT_MS, None)
    except GLib.Error as error:
        raise DisplayError(_dbus_error_text(error)) from None


@dataclass
class _MutterOriginal:
    config: list
    properties: dict          # the layout mode the config's positions are in (see DisplayState.apply_properties)
    width: int
    height: int
    mode_id: str
    scale: float
    connector: str


class _MutterBackend:
    name = "mutter"

    def __init__(self, bus=None):
        self._bus = bus
        self._original: _MutterOriginal | None = None

    def read(self) -> _Snapshot:
        state = read_current_state(self._bus)
        primary = state.primary_logical_monitor()
        if primary is None:
            raise DisplayError("kein logischer Monitor")
        monitor = state.find_monitor(primary.connectors[0])
        mode = monitor.current_mode() if monitor is not None else None
        if monitor is None or mode is None:
            raise DisplayError("primärer Monitor %s hat keinen aktiven Modus" % primary.connectors[0])
        return _Snapshot(mode.width, mode.height, "%s %s" % (monitor.connector, mode.id), (state, primary, monitor, mode))

    def capture_modes(self) -> list:
        """Every mode the primary monitor offers, for choose_capture_mode."""
        state = read_current_state(self._bus)
        primary = state.primary_logical_monitor()
        monitor = state.find_monitor(primary.connectors[0]) if primary is not None else None
        return list(monitor.modes) if monitor is not None else []

    def switch(self, snapshot: _Snapshot, width: int, height: int, refresh_hz: float) -> str:
        state, primary, monitor, current = snapshot.payload
        candidates = rank_modes(monitor.modes, width, height, refresh_hz)
        if not candidates:
            raise DisplayError("kein Modus %dx%d auf %s" % (width, height, monitor.connector))
        # remembered before applying: if mutter accepts the switch, this is what restore() puts back
        self._original = _MutterOriginal(current_config(state), state.apply_properties(), current.width, current.height,
                                         current.id, primary.scale, monitor.connector)
        # the monitor may not accept that size at that rate (the original then let Windows pick the rate): a
        # refusal of the best mode gets one more go with the next-best one. a refused config changes nothing.
        refusals = []
        for mode in candidates[:2]:
            try:
                apply_config(state.serial, build_config(state, primary, mode, 1.0), METHOD_TEMPORARY, self._bus,
                             state.apply_properties())
                return "%s Modus %s, Skalierung 1" % (monitor.connector, mode.id)
            except DisplayError as error:
                refusals.append("%s: %s" % (mode.id, error))
        self._original = None
        raise DisplayError("; ".join(refusals))

    def restore(self) -> str:
        original = self._original
        if original is None:
            raise DisplayError("kein gemerkter Modus")
        # the serial we switched with is stale by now; each attempt reads a fresh one. the second attempt covers a
        # MonitorsChanged that lands between our read and our apply.
        last_error: DisplayError | None = None
        for _attempt in range(2):
            try:
                state = read_current_state(self._bus)
                apply_config(state.serial, original.config, METHOD_TEMPORARY, self._bus, original.properties)
                return "%dx%d (%s Modus %s, Skalierung %g)" % (original.width, original.height, original.connector,
                                                               original.mode_id, original.scale)
            except DisplayError as error:
                last_error = error
        raise last_error


# --- xrandr (X11 sessions without mutter) ---------------------------------------------------------------------

@dataclass
class XrandrScreen:
    output: str
    width: int
    height: int
    rate: float
    modes: list          # (width, height, rate, rate_text) as xrandr prints them, rate_text is what --rate wants


_XRANDR_OUTPUT = re.compile(r"^(\S+) connected( primary)? (\d+)x(\d+)\+(-?\d+)\+(-?\d+)")
_XRANDR_MODE = re.compile(r"^\s+(\d+)x(\d+)i?\s+(.*)$")
_XRANDR_RATE = re.compile(r"(\d+(?:\.\d+)?)([*+]*)")


def parse_xrandr(output: str) -> XrandrScreen | None:
    """The primary connected output (or the first connected one) with its mode list and current mode."""
    screens: list[XrandrScreen] = []
    primaries: list[bool] = []
    current: XrandrScreen | None = None
    for line in output.splitlines():
        match = _XRANDR_OUTPUT.match(line)
        if match:
            current = XrandrScreen(match.group(1), int(match.group(3)), int(match.group(4)), 0.0, [])
            screens.append(current)
            primaries.append(bool(match.group(2)))
            continue
        if line.startswith(" ") is False:
            current = None     # a disconnected output or the Screen line: its modes (if any) are not ours
            continue
        mode = _XRANDR_MODE.match(line)
        if mode is None or current is None:
            continue
        width, height = int(mode.group(1)), int(mode.group(2))
        for rate_text, flags in _XRANDR_RATE.findall(mode.group(3)):
            current.modes.append((width, height, float(rate_text), rate_text))
            if "*" in flags and current.rate == 0.0:
                current.rate = float(rate_text)
    if not screens:
        return None
    for screen, primary in zip(screens, primaries):
        if primary:
            return screen
    return screens[0]


class _XrandrBackend:
    name = "xrandr"

    def __init__(self):
        self._original: tuple[str, int, int, str] | None = None

    @staticmethod
    def _run(arguments: list[str]) -> str:
        try:
            result = subprocess.run(["xrandr"] + arguments, capture_output=True, text=True, timeout=XRANDR_TIMEOUT_S,
                                    check=False, env=dict(os.environ))
        except (OSError, subprocess.SubprocessError) as error:
            raise DisplayError("xrandr: %s" % error) from None
        if result.returncode != 0:
            raise DisplayError("xrandr: " + (result.stderr.strip().splitlines() or ["Exit %d" % result.returncode])[0])
        return result.stdout

    def read(self) -> _Snapshot:
        screen = parse_xrandr(self._run(["--query"]))
        if screen is None:
            raise DisplayError("xrandr meldet keinen angeschlossenen Ausgang")
        return _Snapshot(screen.width, screen.height, "%s %dx%d@%g" % (screen.output, screen.width, screen.height, screen.rate), screen)

    def capture_modes(self) -> list:
        return []

    def switch(self, snapshot: _Snapshot, width: int, height: int, refresh_hz: float) -> str:
        screen: XrandrScreen = snapshot.payload
        candidates = [mode for mode in screen.modes if mode[0] == width and mode[1] == height]
        if not candidates:
            raise DisplayError("kein Modus %dx%d auf %s" % (width, height, screen.output))
        rate_text = min(candidates, key=lambda mode: (abs(mode[2] - refresh_hz) > REFRESH_TOLERANCE_HZ,
                                                       mode[2] < refresh_hz, abs(mode[2] - refresh_hz)))[3]
        original_rate = next((mode[3] for mode in screen.modes
                              if mode[0] == screen.width and mode[1] == screen.height and mode[2] == screen.rate), "")
        self._original = (screen.output, screen.width, screen.height, original_rate)
        mode_arguments = ["--output", screen.output, "--mode", "%dx%d" % (width, height)]
        try:
            self._run(mode_arguments + ["--rate", rate_text])
        except DisplayError as with_rate:
            # the monitor may not accept that size at that rate; like the original, try again with the size alone
            # and let the X server pick the rate
            try:
                self._run(mode_arguments)
            except DisplayError as without_rate:
                self._original = None
                raise DisplayError("%s; ohne Rate: %s" % (with_rate, without_rate)) from None
            return "%s %dx%d (Rate von xrandr gewählt)" % (screen.output, width, height)
        return "%s %dx%d@%s" % (screen.output, width, height, rate_text)

    def restore(self) -> str:
        if self._original is None:
            raise DisplayError("kein gemerkter Modus")
        output, width, height, rate_text = self._original
        arguments = ["--output", output, "--mode", "%dx%d" % (width, height)]
        if rate_text:
            arguments += ["--rate", rate_text]
        self._run(arguments)
        return "%dx%d (%s)" % (width, height, output)


class _NullBackend:
    name = "none"

    def read(self) -> _Snapshot:
        raise DisplayError("weder Mutter (DBus) noch X11 erreichbar")

    def capture_modes(self) -> list:
        return []

    def switch(self, snapshot, width, height, refresh_hz) -> str:
        raise DisplayError("keine Anzeige-Steuerung")

    def restore(self) -> str:
        raise DisplayError("keine Anzeige-Steuerung")


def wayland_session() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland"


def select_backend():
    """mutter when its DBus name is owned (GNOME on Wayland or X11), xrandr for a plain X11 session, else nothing.
    Under another Wayland compositor DISPLAY points at Xwayland, whose xrandr only fakes a mode towards X clients:
    the desktop the portal captures would stay as it is while we believed it switched - so that gets nothing too."""
    if mutter_available():
        return _MutterBackend()
    if os.environ.get("DISPLAY") and not wayland_session() and shutil.which("xrandr"):
        return _XrandrBackend()
    return _NullBackend()


# ---------------------------------------------------------------------------------------------- the switch

class DisplayMode:
    """match_to() before the capture starts, restore() when the stream stops - and on every way out."""

    def __init__(self, backend=None):
        self._gate = threading.RLock()
        self._backend = backend          # chosen on first use, so constructing one never touches DBus
        self._changed = False
        self._original_size: tuple[int, int] = (0, 0)

    @property
    def is_changed(self) -> bool:   # desktop is currently switched and owes a restore()
        return self._changed

    @property
    def backend_name(self) -> str:
        with self._gate:
            return self._backend_ready().name

    def _backend_ready(self):
        if self._backend is None:
            self._backend = select_backend()
            if self._backend.name == "none":
                log.write("display: weder Mutter (DBus) noch X11 gefunden - die Auflösung wird nicht umgeschaltet")
        return self._backend

    def match_for_capture(self, stream_width: int, stream_height: int, fps: int) -> bool:
        """Put the desktop into the mode that CAPTURES best for this stream - see choose_capture_mode. The
        stream's own size is only the fallback: matching it exactly is what costs a third of the frames."""
        try:
            modes = self._backend_ready().capture_modes()
        except Exception as error:   # noqa: BLE001 - a backend that cannot enumerate just gets the old behaviour
            log.write("display: konnte die Modi nicht lesen (%s)" % error)
            modes = []
        width, height, refresh = choose_capture_mode(modes, stream_width, stream_height, fps)
        if modes and (width, height) != (stream_width, stream_height):
            log.write("display: streame %dx%d, schalte den Desktop dafür auf %dx%d@%g - kleiner ginge nur "
                      "mit weniger Bildwiederholrate, und darunter gibt die Bildschirmaufnahme nur noch "
                      "zwei Drittel der Bilder heraus" % (stream_width, stream_height, width, height, refresh))
        return self.match_to(width, height, refresh)

    def match_to(self, width: int, height: int, refresh_hz: float) -> bool:
        """True if the desktop is now at this size (or already was). False means we left it alone and the stream
        will be scaled down as before - a worse picture, but nothing is broken."""
        with self._gate:
            if self._changed:
                return True
            backend = self._backend_ready()
            try:
                snapshot = backend.read()
            except DisplayError as error:
                log.write("display: konnte die aktuelle Auflösung nicht lesen (%s), streame stattdessen skaliert" % error)
                return False
            except Exception as error:   # noqa: BLE001 - a backend bug must degrade to "scaled", never to a crash
                log.write("display: Fehler beim Lesen der Auflösung (%s), streame stattdessen skaliert" % error)
                return False
            if snapshot.width == width and snapshot.height == height:
                return True   # already there

            try:
                detail = backend.switch(snapshot, width, height, refresh_hz)
            except DisplayError as error:
                log.write("display: %dx%d wurde abgelehnt (%s), streame stattdessen skaliert" % (width, height, error))
                return False
            except Exception as error:   # noqa: BLE001
                log.write("display: %dx%d wurde abgelehnt (%s), streame stattdessen skaliert" % (width, height, error))
                return False

            self._original_size = (snapshot.width, snapshot.height)
            self._changed = True
            log.write("display: Desktop auf %dx%d umgeschaltet (war %dx%d; %s); wird nach dem Stream wiederhergestellt"
                      % (width, height, snapshot.width, snapshot.height, detail))
            return True

    def restore(self) -> None:
        with self._gate:
            if not self._changed:
                return
            self._changed = False   # cleared first: a second call must never fight the first
            width, height = self._original_size
            try:
                detail = self._backend_ready().restore()
            except DisplayError as error:
                log.write("display: Wiederherstellung von %dx%d fehlgeschlagen (%s) - bitte in den Anzeige-Einstellungen zurückschalten"
                          % (width, height, error))
                return
            except Exception as error:   # noqa: BLE001
                log.write("display: Wiederherstellung von %dx%d fehlgeschlagen (%s)" % (width, height, error))
                return
            log.write("display: Desktop wieder auf %s" % detail)
