"""The application: one process, one server, a window you can close without stopping anything (port of App.xaml.cs).

Adw.Application gives us the single instance for free: a second copy hands its command line to the running one,
which presents its window, and exits. --minimized (what the autostart entry passes) starts straight into the tray.
The server is started here, before any window exists, and is shut down here whatever way we go - the desktop
resolution must be put back even on SIGTERM.
"""

import atexit
import signal
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
    _signal_add = GLibUnix.signal_add
except (ValueError, ImportError):   # older PyGObject: the same function under its old name
    _signal_add = GLib.unix_signal_add

from . import APP_EXEC, APP_ID, APP_NAME, log, tray, ui  # noqa: E402
from .i18n import _, available_languages, set_language  # noqa: E402
from .settings import settings  # noqa: E402

MINIMIZED_OPTION = "minimized"
MONITOR_TICK_MS = 500
ALREADY_RUNNING_TEXT = "Another copy of the server is already running."


def _create_real_server():
    from .server import Server   # imported late: the window and tray must stay importable without ffmpeg & co
    return Server()


class CellStreamApplication(Adw.Application):
    """CellStreamApplication(); run(argv). server_factory/application_id/extra_flags exist for the tests."""

    def __init__(self, server_factory=None, application_id: str = APP_ID,
                 extra_flags: Gio.ApplicationFlags = Gio.ApplicationFlags.DEFAULT_FLAGS):
        super().__init__(application_id=application_id, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | extra_flags)
        # the saved language before anything is built: English is the default, so a fresh install is English
        saved = settings.get("language", "en")
        if saved in available_languages():
            set_language(saved)
        self._server_factory = server_factory or _create_real_server
        self.server = None
        self.window: ui.MainWindow | None = None
        self.tray: tray.TrayIcon | None = None
        self._started = False
        self.start_failed = False       # the port was taken: the process should end with status 1, like --headless
        self._quitting = False
        self._torn_down = False
        self._monitor_id = 0
        self._was_connected = False
        self._was_armed = True          # the server starts armed; a fuse tripping at start-up must still be reported
        self._tooltip = ""
        self.add_main_option(MINIMIZED_OPTION, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start without a window (tray icon only)", None)
        self.connect("startup", self._on_startup)
        self.connect("command-line", self._on_command_line)
        self.connect("shutdown", self._on_shutdown)

    # ------------------------------------------------------------------ lifecycle

    def _on_startup(self, _app) -> None:
        self.hold()   # no window may be open (--minimized), yet the server must keep running until Beenden

        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", lambda *_: self.quit_everything())
        self.add_action(action)
        action = Gio.SimpleAction.new("show-window", None)
        action.connect("activate", lambda *_: self.present_window())
        self.add_action(action)
        action = Gio.SimpleAction.new("open-log", None)
        action.connect("activate", lambda *_: self.open_log())
        self.add_action(action)

        # Python's own signal handlers only run between byte codes, which a GTK main loop blocked in poll()
        # never reaches; GLib's unix signal sources wake the loop instead
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            _signal_add(GLib.PRIORITY_HIGH, signum, self._on_unix_signal, signum)

    def _on_command_line(self, _app, command_line) -> int:
        minimized = command_line.get_options_dict().contains(MINIMIZED_OPTION)
        if not self._started:
            self._started = True
            if not self._bring_up():
                return 1
            if not minimized:
                self.present_window()
        elif not minimized:
            log.write(_("a second copy was started - showing the running one's window"))
            self.present_window()   # a second copy was started: show the running one instead
        return 0

    def _bring_up(self) -> bool:
        server = self._server_factory()
        if not server.start():
            self.start_failed = True
            self._show_already_running()   # nothing to shut down: start() closed what it opened
            return False
        self.server = server
        atexit.register(self.server.shutdown)   # whichever way we die, the desktop resolution goes back

        self.window = ui.MainWindow(self, self.server)
        self.tray = tray.TrayIcon(on_show=self.present_window, on_open_log=self.open_log, on_quit=self.quit_everything)
        self.tray.start()
        self._monitor_id = GLib.timeout_add(MONITOR_TICK_MS, self._on_monitor_tick)
        self._on_monitor_tick()
        return True

    def _show_already_running(self) -> None:
        if hasattr(Adw, "MessageDialog"):
            dialog = Adw.MessageDialog(heading=_("Already running"), body=_(ALREADY_RUNNING_TEXT), application=self)
            dialog.add_response("ok", "OK")
            dialog.connect("response", lambda *_: self.quit())
            dialog.present()
        else:   # libadwaita without MessageDialog: the dialog that replaced it
            dialog = Adw.AlertDialog(heading=_("Already running"), body=_(ALREADY_RUNNING_TEXT))
            dialog.add_response("ok", "OK")
            dialog.connect("closed", lambda *_: self.quit())
            dialog.present(None)

    def _on_unix_signal(self, signum: int) -> bool:
        log.write(_("signal %d - shutting down") % signum)
        self.quit_everything()
        return GLib.SOURCE_CONTINUE   # keep the handler: a second signal during the shutdown must not kill us mid-restore

    def quit_everything(self) -> None:
        """The tray's Beenden: stop the server (puts the desktop back), drop the icon, leave the main loop."""
        if self._quitting:
            return
        self._quitting = True
        if self._monitor_id:
            GLib.source_remove(self._monitor_id)
            self._monitor_id = 0
        try:
            self._tear_down()
        finally:
            self.quit()   # whatever the teardown did, the loop must end - or nothing can ever stop us but SIGKILL

    def _on_shutdown(self, _app) -> None:
        self._tear_down()   # covers a quit() that did not go through quit_everything (the Läuft-schon dialog)

    def _tear_down(self) -> None:
        """Server and tray go down exactly once, whichever path gets here first."""
        if self._torn_down:
            return
        self._torn_down = True
        # the window first, while its widgets can still be read: it owns the 500 ms timer and any Befehle
        # edit still inside its save pause
        if self.window is not None:
            try:
                self.window.shut_down()
            except Exception as error:   # noqa: BLE001
                log.write(_("window: could not close cleanly: %s") % error)
        # each step on its own: the icon failing to go must not keep the desktop at the streaming resolution
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception as error:   # noqa: BLE001
                log.write(_("tray: could not remove the icon: %s") % error)
        if self.server is not None:
            atexit.unregister(self.server.shutdown)   # done here, so it is not run again (uncaught) at exit
            try:
                self.server.shutdown()
            except Exception as error:   # noqa: BLE001
                log.write(_("quit: the server would not stop cleanly: %s") % error)

    # ------------------------------------------------------------------ what the tray and the menu ask for

    def present_window(self) -> None:
        if self.window is None:
            return
        # the panel hands us an xdg-activation token before Activate / a menu click; without it Wayland lets a
        # window map but refuses to raise or focus one that is already on screen
        token = self.tray.take_activation_token() if self.tray is not None else None
        self.window.present_from_tray(token)

    def open_log(self) -> None:
        ui.open_log(self.window if self.window is not None and self.window.get_visible() else None)

    # ------------------------------------------------------------------ popups, whether or not the window is visible

    def _on_monitor_tick(self) -> bool:
        try:
            self._monitor()
        except Exception as error:   # noqa: BLE001 - never let a notification hiccup stop the monitoring
            log.write(_("window: monitoring failed: %s") % error)
        return GLib.SOURCE_CONTINUE

    def _monitor(self) -> None:
        server = self.server
        connected = bool(server.is_ps3_connected)
        who = server.connected_ps3 or ""
        status = ui.status_text(bool(server.is_armed), connected, who)

        if connected != self._was_connected:
            self._was_connected = connected
            if self.tray is not None:
                self.tray.set_live(connected)
            self._notify("ps3", _("PS3 connected") if connected else _("PS3 disconnected"),
                         who + _(" is streaming.") if connected else _("Waiting for it to come back."))

        # the fuse can trip while the window is hidden, so say so where it will be seen
        if bool(server.is_armed) != self._was_armed:
            self._was_armed = bool(server.is_armed)
            if not self._was_armed and server.trip_reason:
                self._notify("fuse", _("Streaming stopped"), server.trip_reason)

        tooltip = APP_NAME + " – " + status
        if tooltip != self._tooltip:
            self._tooltip = tooltip
            if self.tray is not None:
                self.tray.set_tooltip(status)

    def _notify(self, ident: str, title: str, body: str) -> None:
        if not self.get_is_registered():
            return
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_icon(Gio.ThemedIcon.new(APP_EXEC))
        notification.set_default_action("app.show-window")
        self.send_notification(ident, notification)


_crash_log_installed = False


def install_crash_log() -> None:
    """Port of App.xaml.cs's UnhandledException hook: an exception nobody caught lands in the log, where it will be
    seen - stderr goes nowhere when the autostart entry launched us. PyGObject prints callback exceptions through
    sys.excepthook, so a failing GTK handler is covered too; the previous hook still gets the traceback."""
    global _crash_log_installed
    if _crash_log_installed:
        return
    _crash_log_installed = True
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def on_exception(kind, value, traceback):
        _log_crash("", kind, value)
        previous_hook(kind, value, traceback)

    def on_thread_exception(args):
        _log_crash(" (Thread %s)" % (args.thread.name if args.thread is not None else "?"), args.exc_type, args.exc_value)
        previous_thread_hook(args)

    sys.excepthook = on_exception
    threading.excepthook = on_thread_exception


def _log_crash(where: str, kind, value) -> None:
    try:
        log.write(_("crashed%s: %s: %s") % (where, getattr(kind, "__name__", kind), value))
    except Exception:   # noqa: BLE001 - the crash hook itself must never raise
        pass


def main(argv: list[str]) -> int:
    install_crash_log()
    app = CellStreamApplication()
    status = app.run(argv)
    return 1 if app.start_failed else status
