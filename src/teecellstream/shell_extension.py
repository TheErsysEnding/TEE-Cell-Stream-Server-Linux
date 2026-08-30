"""Switches on the GNOME extension that ships with us, so nobody has to type a command.

Without it, GNOME hands a fullscreen window straight to the monitor (direct scanout) and stops
compositing it; the screen cast then freezes on its last picture while sound and input carry on, and the
PS3 shows a still image (mutter#3074, #3903). The extension turns that off while the server runs.

The package installs the extension as root, but ENABLING one is per user, so the postinst cannot do it -
this can, because the app runs as the user. It is the same call `gnome-extensions enable` makes.

The one thing nobody can shortcut: GNOME reads newly installed extensions only when a session starts. So
right after the very first install the shell does not know it yet, and the user has to log out once. We
say so, once, instead of leaving them with a frozen picture and no explanation.
"""

from gi.repository import Gio, GLib

from . import log
from .i18n import _

UUID = "tee-cell-stream-scanout@tee.local"

SHELL_BUS_NAME = "org.gnome.Shell"
SHELL_OBJECT_PATH = "/org/gnome/Shell"
EXTENSIONS_INTERFACE = "org.gnome.Shell.Extensions"
CALL_TIMEOUT_MS = 5000

# org.gnome.Shell.Extensions state values; 1 = ENABLED is the only one we treat as "nothing to do"
STATE_ENABLED = 1

# what ensure_enabled() reports back, for the window and for tests
ENABLED = "enabled"                # we just switched it on
ALREADY = "already"                # it was on
NEEDS_LOGOUT = "needs_logout"      # installed, but this session's shell has not read it yet
UNAVAILABLE = "unavailable"        # no GNOME Shell on the bus (another desktop, or X11 without it)
FAILED = "failed"                  # the shell refused


def _call(bus, method: str, argument: GLib.Variant, reply_type: str):
    return bus.call_sync(SHELL_BUS_NAME, SHELL_OBJECT_PATH, EXTENSIONS_INTERFACE, method, argument,
                         GLib.VariantType(reply_type), Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None)


def ensure_enabled() -> str:
    """Switches the extension on if it is installed and off. Never raises; returns one of the states above."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as error:
        log.write(_("extension: no session bus (%s)") % error.message)
        return UNAVAILABLE

    try:
        reply = _call(bus, "GetExtensionInfo", GLib.Variant("(s)", (UUID,)), "(a{sv})")
    except GLib.Error as error:
        # no GNOME Shell (another desktop) is normal and not worth alarming anybody about
        log.write(_("extension: GNOME Shell not reachable (%s)") % error.message)
        return UNAVAILABLE

    info = reply.unpack()[0]
    if not info:
        # the shell has no record of it: installed after this session started
        log.write("extension: the bundled GNOME extension has not been read yet - log out and in "
                  "once, then it enables itself. Until then the picture freezes as soon as a "
                  "game runs full-screen (a borderless window helps in the meantime).")
        return NEEDS_LOGOUT

    if info.get("enabled") and int(info.get("state", 0)) == STATE_ENABLED:
        return ALREADY

    try:
        reply = _call(bus, "EnableExtension", GLib.Variant("(s)", (UUID,)), "(b)")
    except GLib.Error as error:
        log.write(_("extension: could not enable the GNOME extension (%s)") % error.message)
        return FAILED

    if not reply.unpack()[0]:
        log.write(_("extension: GNOME refused to enable the extension"))
        return FAILED

    log.write(_("extension: GNOME extension enabled - full-screen games no longer freeze"))
    return ENABLED
