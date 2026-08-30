"""xdg-desktop-portal ScreenCast session - Linux's stand-in for Windows' ddagrab.

On Wayland no process may read the screen on its own: the desktop's portal asks the user which monitor to
share and hands us a PipeWire stream of it. The dance is CreateSession -> SelectSources -> Start (the
dialog) -> OpenPipeWireRemote, each step a Request object on the bus whose Response signal carries the
result. A restore token from the first run lets the portal skip the dialog on every later run, so the
PS3 can connect while nobody sits at the PC.

Everything here is synchronous and runs on whichever thread calls it (the receive thread at PLAY, the
warm-up thread at server start); the Response signal is waited for on a private GLib main context, so
no GTK main loop is needed - the headless server works too.
"""

import random
import threading

from . import log
from .i18n import _

try:
    import gi
    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gio, GLib
except (ImportError, ValueError):   # no PyGObject: the portal is simply not available (X11 fallback instead)
    Gio = None
    GLib = None

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
REQUEST_PATH_PREFIX = PORTAL_OBJECT_PATH + "/request/"

# restore_token / persist_mode arrived in ScreenCast v4. Without them the share dialog would come up on
# every PLAY, which defeats the point of an appliance the PS3 connects to on its own.
MIN_SCREENCAST_VERSION = 4

SOURCE_TYPE_MONITOR = 1
CURSOR_MODE_EMBEDDED = 2          # the pointer is painted into the frames, like ddagrab's draw_mouse
PERSIST_MODE_UNTIL_REVOKED = 2    # remember the choice until the user revokes it in the desktop's settings

RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1
RESPONSE_OTHER = 2

DIALOG_TIMEOUT_S = 120            # how long the user gets to answer the share dialog
CALL_TIMEOUT_MS = 30_000          # the method call itself; the dialog's own wait is the Response signal, not the call
CLOSE_TIMEOUT_MS = 5_000

_token_counter = 0
_token_gate = threading.Lock()


class PortalError(Exception):
    """The portal would not give us the screen; the message is already in the log."""


class PortalCancelled(PortalError):
    """Response code 1: the user closed the dialog without sharing."""


def sender_to_path_segment(unique_name: str) -> str:
    """':1.584' -> '1_584', the form the portal uses inside request/session object paths."""
    return unique_name.lstrip(":").replace(".", "_")


def request_path(unique_name: str, handle_token: str) -> str:
    """Where the portal will create the Request object for a call carrying handle_token.

    Knowing the path up front lets us subscribe to its Response BEFORE making the call - the portal may
    answer (a cached restore token, an error) before the method call itself returns.
    """
    return REQUEST_PATH_PREFIX + sender_to_path_segment(unique_name) + "/" + handle_token


def _new_token() -> str:
    global _token_counter
    with _token_gate:
        _token_counter += 1
        return "teecst%d_%d" % (_token_counter, random.randrange(1 << 30))


def _session_bus():
    if Gio is None:
        return None
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        return None


def screencast_version() -> int:
    """The ScreenCast interface's version property, or 0 when there is no portal / no session bus.

    Reading a property never shows a dialog, so this is safe to call at any time (tests included).
    """
    bus = _session_bus()
    if bus is None:
        return 0
    try:
        reply = bus.call_sync(PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", (SCREENCAST_INTERFACE, "version")), GLib.VariantType("(v)"),
                              Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None)
        return int(reply.unpack()[0])
    except (GLib.Error, TypeError, ValueError):
        return 0


def is_available() -> bool:
    """True when the desktop offers a ScreenCast portal new enough for restore tokens."""
    return screencast_version() >= MIN_SCREENCAST_VERSION


class ScreenCastSession:
    """One portal session: open() runs the dialog dance, open_pipewire_remote() gives the fd, close() ends it."""

    def __init__(self):
        self._bus = None
        self.session_handle: str | None = None
        self.node_id: int | None = None
        self.restore_token: str | None = None
        self._closed = False

    # ------------------------------------------------------------------ the dance

    def open(self, restore_token: str | None, timeout_s: float = DIALOG_TIMEOUT_S) -> tuple[int, str | None]:
        """CreateSession -> SelectSources -> Start. Returns (pipewire node id, restore token to save).

        restore_token: the one saved from the last run, or None for a first run (shows the dialog).
        Raises PortalCancelled / PortalError; the reason is already logged.
        """
        self._bus = _session_bus()
        if self._bus is None:
            log.write(_("portal: no session bus - screen sharing is not possible"))
            raise PortalError("no session bus")

        session_token = _new_token()
        results = self._request("CreateSession", [], {"session_handle_token": GLib.Variant("s", session_token)},
                                timeout_s)
        self.session_handle = results.get("session_handle")
        if not isinstance(self.session_handle, str) or not self.session_handle:
            log.write(_("portal: CreateSession returned no session"))
            raise PortalError("CreateSession without a session_handle")

        options = {
            "types": GLib.Variant("u", SOURCE_TYPE_MONITOR),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", CURSOR_MODE_EMBEDDED),
            "persist_mode": GLib.Variant("u", PERSIST_MODE_UNTIL_REVOKED),
        }
        if restore_token:
            options["restore_token"] = GLib.Variant("s", restore_token)
            log.write(_("portal: requesting screen sharing with the saved token"))
        else:
            log.write(_("portal: the sharing dialog is up - please pick the monitor and allow it"))
        self._request("SelectSources", [GLib.Variant("o", self.session_handle)], options, timeout_s)

        results = self._request("Start", [GLib.Variant("o", self.session_handle), GLib.Variant("s", "")], {}, timeout_s)
        streams = results.get("streams") or []
        if not streams:
            log.write(_("portal: sharing started, but with no stream"))
            raise PortalError("Start without streams")
        self.node_id = int(streams[0][0])
        token = results.get("restore_token")
        self.restore_token = token if isinstance(token, str) and token else None
        log.write(_("portal: screen sharing is running (PipeWire node %d%s)") % (self.node_id, ", without a token" if self.restore_token is None else ""))
        return self.node_id, self.restore_token

    def open_pipewire_remote(self) -> int:
        """The PipeWire connection fd for this session. The caller owns (and must close) it."""
        if self.session_handle is None:
            raise PortalError("no session")
        try:
            reply, fd_list = self._bus.call_with_unix_fd_list_sync(
                PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, SCREENCAST_INTERFACE, "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self.session_handle, {})), GLib.VariantType("(h)"),
                Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None, None)
        except GLib.Error as error:
            log.write(_("portal: OpenPipeWireRemote failed: %s") % error.message)
            raise PortalError("OpenPipeWireRemote: " + error.message) from error
        index = reply.unpack()[0]
        fd = fd_list.get(index)   # get() hands back a dup that is ours to close
        return fd

    def close(self) -> None:
        """Ends the session (and the PipeWire stream with it). Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        if self._bus is None or self.session_handle is None:
            return
        try:
            self._bus.call_sync(PORTAL_BUS_NAME, self.session_handle, SESSION_INTERFACE, "Close", None, None,
                                Gio.DBusCallFlags.NONE, CLOSE_TIMEOUT_MS, None)
            log.write(_("portal: session closed"))
        except GLib.Error as error:
            log.write(_("portal: the session would not close: %s") % error.message)

    # ------------------------------------------------------------------ request/response plumbing

    def _request(self, method: str, leading: list, options: dict, timeout_s: float) -> dict:
        """Calls a ScreenCast method and waits for its Request's Response. Returns the results dict."""
        handle_token = _new_token()
        options = dict(options)
        options["handle_token"] = GLib.Variant("s", handle_token)
        parameters = GLib.Variant.new_tuple(*leading, GLib.Variant("a{sv}", options))
        expected_path = request_path(self._bus.get_unique_name(), handle_token)

        # the Response is delivered to the main context that was thread-default at subscribe time, so
        # give this thread its own and pump it here - no dependence on a GTK loop anywhere.
        #
        # The waiting is done by iterating that context, NOT with a GLib.MainLoop, and neither callback
        # below may close over anything GLib owns. A MainLoop captured by on_response keeps the context
        # alive for as long as GDBus holds the subscription's closure - which outlives signal_unsubscribe -
        # and every GMainContext owns an eventfd. Measured against the real portal: exactly three leaked
        # eventfds per screen share, one per Request, climbing with every PLAY the PS3 sends.
        context = GLib.MainContext()
        context.push_thread_default()
        outcome: dict = {}
        expired: list = []
        subscriptions = []
        request_paths = [expected_path]
        timeout_source = None
        try:
            def on_response(_bus, _sender, _path, _interface, _signal, parameters, *_user):
                try:
                    code, results = parameters.unpack()
                except (TypeError, ValueError):
                    code, results = RESPONSE_OTHER, {}
                if "code" not in outcome:
                    outcome["code"], outcome["results"] = code, results

            def subscribe(path):
                subscriptions.append(self._bus.signal_subscribe(PORTAL_BUS_NAME, REQUEST_INTERFACE, "Response", path,
                                                                None, Gio.DBusSignalFlags.NONE, on_response))

            subscribe(expected_path)   # BEFORE the call: the answer can beat the call's own return
            try:
                reply = self._bus.call_sync(PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, SCREENCAST_INTERFACE, method, parameters,
                                            GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None)
            except GLib.Error as error:
                log.write(_("portal: %s failed: %s") % (method, error.message))
                raise PortalError(method + ": " + error.message) from error
            actual_path = reply.unpack()[0]
            if actual_path != expected_path:
                # a portal older than v0.9 names the request itself; listen there too (a small race, unavoidable)
                request_paths.append(actual_path)
                subscribe(actual_path)

            def on_timeout(*_args):
                expired.append(True)
                return False   # once is enough

            timeout_source = GLib.timeout_source_new(int(timeout_s * 1000))
            timeout_source.set_callback(on_timeout)
            timeout_source.attach(context)
            while "code" not in outcome and not expired:
                context.iteration(True)   # blocks until the Response arrives or the timeout wakes us
        finally:
            if timeout_source is not None:
                timeout_source.destroy()
            for subscription in subscriptions:
                self._bus.signal_unsubscribe(subscription)
            context.pop_thread_default()

        if "code" not in outcome:
            log.write(_("portal: no answer to %s after %d s - dialog abandoned") % (method, int(timeout_s)))
            for path in request_paths:   # the portal may have named the request itself; close whichever exists
                self._close_request(path)
            raise PortalError("%s: no answer after %d s" % (method, int(timeout_s)))
        code = outcome["code"]
        if code == RESPONSE_CANCELLED:
            log.write(_("portal: the user cancelled screen sharing"))
            raise PortalCancelled("screen sharing cancelled")
        if code != RESPONSE_SUCCESS:
            log.write(_("portal: screen sharing failed (%s reports code %d)") % (method, code))
            raise PortalError("%s: Antwortcode %d" % (method, code))
        results = outcome.get("results")
        return results if isinstance(results, dict) else {}

    def _close_request(self, path: str) -> None:
        """Request.Close: tells the portal to take a still-open dialog down after our timeout."""
        try:
            self._bus.call_sync(PORTAL_BUS_NAME, path, REQUEST_INTERFACE, "Close", None, None,
                                Gio.DBusCallFlags.NONE, CLOSE_TIMEOUT_MS, None)
        except GLib.Error:
            pass
