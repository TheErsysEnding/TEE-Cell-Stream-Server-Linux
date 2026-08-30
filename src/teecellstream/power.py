"""Holds the display on and the PC awake while streaming (port of Server.keepDisplayAwake).

Capturing a slept display returns black frames, so an inhibit is held WHILE streaming; when idle it is
released so the screen sleeps normally. Windows used SetThreadExecutionState; here it is the GNOME
session manager's Inhibit (idle + suspend), or the freedesktop screensaver one where that is what runs.
The inhibit dies with our bus connection, so a crash can never leave the screen stuck on.
"""

import re
import threading

from gi.repository import Gio, GLib

from . import log
from .i18n import _

APP_ID = "tee-cell-stream-server"
REASON = "Streaming to a PS3"

# org.gnome.SessionManager.Inhibit flags: 4 = do not suspend, 8 = do not mark the session idle (screen off)
INHIBIT_SUSPEND = 4
INHIBIT_IDLE = 8
INHIBIT_FLAGS = INHIBIT_SUSPEND | INHIBIT_IDLE   # = 12, the equivalent of ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED

SESSION_MANAGER = ("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager")
SCREENSAVER = ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver")
CALL_TIMEOUT_MS = 3000

_gate = threading.Lock()
_held: tuple[str, int] | None = None   # (backend, cookie) while the inhibit is ours
_failure_reported = False              # the PS3 repeats PLAY until SINFO arrives: one line per failure, not per PLAY
# the inhibit lives exactly as long as the bus connection that took it - and GLib only holds its shared session
# connection weakly. take it from a local and let go, and the connection closes the moment the call returns:
# our bus name vanishes and gnome-session releases the inhibit at once (measured: InhibitorAdded, InhibitorRemoved
# within the same millisecond). so the connection is kept here for the life of the process.
_bus = None


def keep_display_awake(streaming: bool) -> None:
    """True = inhibit (idempotent while held), False = release (no-op when nothing is held). Only logs on failure."""
    global _held, _failure_reported
    with _gate:
        if streaming:
            if _held is not None:
                return
            _held = _inhibit()
            if _held is not None:
                _failure_reported = False
            return
        if _held is None:
            _failure_reported = False
            return
        backend, cookie = _held
        _held = None
        try:
            if backend == "session":
                _call(SESSION_MANAGER, "Uninhibit", GLib.Variant("(u)", (cookie,)))
            else:
                _call(SCREENSAVER, "UnInhibit", GLib.Variant("(u)", (cookie,)))
        except GLib.Error as error:
            log.write(_("power: releasing the screen failed (%s)") % _error_text(error))


def _inhibit() -> tuple[str, int] | None:
    global _failure_reported
    try:
        reply = _call(SESSION_MANAGER, "Inhibit",
                      GLib.Variant("(susu)", (APP_ID, 0, REASON, INHIBIT_FLAGS)), GLib.VariantType("(u)"))
        return ("session", reply.unpack()[0])
    except GLib.Error as session_error:
        try:
            reply = _call(SCREENSAVER, "Inhibit", GLib.Variant("(ss)", (APP_ID, REASON)), GLib.VariantType("(u)"))
            return ("screensaver", reply.unpack()[0])
        except GLib.Error as screensaver_error:
            if not _failure_reported:
                _failure_reported = True
                log.write(_("power: cannot keep the screen awake (SessionManager: %s; ScreenSaver: %s)") % (_error_text(session_error), _error_text(screensaver_error)))
            return None


def _call(service: tuple[str, str, str], method: str, parameters: GLib.Variant, reply_type=None) -> GLib.Variant:
    """One synchronous session-bus call; a missing bus surfaces as the same GLib.Error a missing service does."""
    global _bus
    name, path, interface = service
    if _bus is None or _bus.is_closed():
        _bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return _bus.call_sync(name, path, interface, method, parameters, reply_type, Gio.DBusCallFlags.NONE,
                          CALL_TIMEOUT_MS, None)


def _error_text(error: GLib.Error) -> str:
    # "GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: The name ... " -> just the reason
    return re.sub(r"^GDBus\.Error:[^:]+:\s*", "", error.message or "").strip() or str(error)
