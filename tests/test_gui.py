"""GUI tests: window, application, tray, autostart, icons - against a mock server, on the live desktop.

Safe to run unattended: the window appears for about two seconds and closes itself, a tray dot appears and
vanishes, nothing is typed anywhere, no portal dialog, no display-mode switch, no real notification (the
notification hooks are stubbed), and settings/log/autostart go to temporary files.

Run: cd <project> && PYTHONPATH=src python3 -m unittest tests.test_gui -v
"""

import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

_TEMP = tempfile.mkdtemp(prefix="tee-cst-gui-")
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_TEMP, "settings.json"))
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_TEMP, "server.log"))
os.environ["TEE_CST_AUTOSTART_PATH"] = os.path.join(_TEMP, "autostart", "tee-cell-stream-server.desktop")
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=%s/bus" % os.environ["XDG_RUNTIME_DIR"])

# custom_commands.py belongs to another module group; the window only needs get/set/SLOT_COUNT, so a stand-in
# keeps this test independent of it (and of whatever it seeds on first start).
# It is bound into ui.py alone (below the imports), NEVER left in sys.modules: a fake in the registry is
# inherited by every test module imported after this one, and `unittest discover` loads test_system_glue
# after test_gui - its CustomCommandsTests then ran against the stub and died on SETTINGS_KEY.
_fake_commands = types.ModuleType("teecellstream.custom_commands")
_fake_commands.SLOT_COUNT = 4
_fake_commands.slots = {1: {"kind": "run", "value": "steam://open/bigpicture", "label": "Big Picture"},
                        2: {"kind": "none", "value": "", "label": ""},
                        3: {"kind": "none", "value": "", "label": ""},
                        4: {"kind": "none", "value": "", "label": ""}}
_fake_commands.set_calls = []
_fake_commands.get = lambda slot: dict(_fake_commands.slots[slot])
_fake_commands.set = lambda slot, command: (_fake_commands.slots.__setitem__(slot, dict(command)), _fake_commands.set_calls.append((slot, dict(command))))
_fake_commands.run = lambda slot: None

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from teecellstream import APP_EXEC, autostart, log, protocol, tray, ui  # noqa: E402
from teecellstream import app as app_module  # noqa: E402
from teecellstream import settings as settings_module  # noqa: E402
from teecellstream.app import ALREADY_RUNNING_TEXT, CellStreamApplication, install_crash_log  # noqa: E402
from teecellstream.settings import settings  # noqa: E402

ui.custom_commands = _fake_commands   # the window looks it up in its own globals at call time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_DIR = os.path.join(PROJECT, "data", "icons")
HAVE_DISPLAY = Gtk.init_check()
TEST_APP_ID = "de.tee.CellStreamServer.Test"


def _session_bus():
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        return None


def _watcher_items(bus) -> list[str]:
    reply = bus.call_sync(tray.WATCHER_NAME, tray.WATCHER_PATH, "org.freedesktop.DBus.Properties", "Get",
                          GLib.Variant("(ss)", (tray.WATCHER_NAME, "RegisteredStatusNotifierItems")), None,
                          Gio.DBusCallFlags.NONE, 3000, None)
    return list(reply.unpack()[0])


def _have_watcher() -> bool:
    bus = _session_bus()
    if bus is None:
        return False
    try:
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", (tray.WATCHER_NAME,)), None, Gio.DBusCallFlags.NONE, 2000, None)
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


HAVE_WATCHER = _have_watcher()


class FakeEncoder:
    def __init__(self, kind, name):
        self.kind = kind
        self.name = name

    def __repr__(self):
        return "FakeEncoder(%s)" % self.kind


class MockServer:
    """The properties the window reads and the calls it makes - nothing else."""

    def __init__(self):
        self.is_armed = True
        self.trip_reason = None
        self.is_ps3_connected = False
        self.connected_ps3 = None
        self.available_encoders = [FakeEncoder("nvenc", "NVIDIA GPU (NVENC)"), FakeEncoder("x264", "CPU (x264 – weniger fps möglich)")]
        self.chosen_encoder = self.available_encoders[0]
        self.loss_recovery = "intra"
        self.video_kbps = 6000              # the two knobs that decide the PS3's decode cost
        self.entropy_coder = "cavlc"
        self.rate_control = "vbr"
        self.stream_size = (1280, 720)
        self.swap_mouse_sticks = False
        self.switch_display_mode = True
        self.start_calls = 0
        self.start_result = True
        self.shutdown_calls = 0
        self.arm_calls = 0
        self.disarm_reasons = []

    @property
    def settings_summary(self):
        return "%dx%d mit 60 fps, %d Mbit/s, %s, %s" % (
            self.stream_size[0], self.stream_size[1], self.video_kbps // 1000, self.entropy_coder.upper(),
            "Intra-Refresh" if self.loss_recovery == "intra" else "Keyframes")

    def start(self):
        self.start_calls += 1
        return self.start_result

    def arm(self):
        self.arm_calls += 1
        self.is_armed = True
        self.trip_reason = None

    def disarm(self, why):
        self.disarm_reasons.append(why)
        self.is_armed = False
        self.trip_reason = why

    def shutdown(self):
        self.shutdown_calls += 1
        if getattr(self, "shutdown_error", None) is not None:
            raise self.shutdown_error


class Steps:
    """Runs checks one after another inside the main loop; failures are collected and raised afterwards."""

    def __init__(self, testcase):
        self.testcase = testcase
        self.failures = []
        self._steps = []

    def add(self, delay_ms, func):
        self._steps.append((delay_ms, func))

    def start(self):
        self._run_next()

    def _run_next(self):
        if not self._steps:
            return
        delay, func = self._steps.pop(0)

        def fire():
            try:
                func()
            except Exception as error:   # noqa: BLE001 - reported after the loop ends
                self.failures.append("%s: %r" % (getattr(func, "__name__", "step"), error))
            self._run_next()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(delay, fire)

    def check(self):
        self.testcase.assertEqual([], self.failures)


# ---------------------------------------------------------------------- icons


class IconFilesTest(unittest.TestCase):
    def _load(self, relative):
        path = os.path.join(ICON_DIR, relative)
        self.assertTrue(os.path.isfile(path), path)
        return GdkPixbuf.Pixbuf.new_from_file(path)

    def test_app_icon_sizes(self):
        for size in (16, 22, 24, 32, 48, 64, 128, 256):
            pixbuf = self._load("hicolor/%dx%d/apps/%s.png" % (size, size, APP_EXEC))
            self.assertEqual((size, size), (pixbuf.get_width(), pixbuf.get_height()))

    def test_dot_icons(self):
        expected = {"idle": (0x8A, 0x8A, 0x8A), "live": (0x3D, 0xD5, 0x6D)}
        for state, rgb in expected.items():
            for size in (22, 24, 32, 48):
                pixbuf = self._load("hicolor/%dx%d/apps/%s-%s.png" % (size, size, APP_EXEC, state))
                self.assertEqual((size, size), (pixbuf.get_width(), pixbuf.get_height()))
                self.assertTrue(pixbuf.get_has_alpha())
                pixels = pixbuf.get_pixels()
                stride = pixbuf.get_rowstride()
                centre = (size // 2) * stride + (size // 2) * 4
                self.assertEqual(rgb, tuple(pixels[centre:centre + 3]), "%s %d centre colour" % (state, size))
                self.assertEqual(255, pixels[centre + 3])
                self.assertEqual(0, pixels[3], "%s %d corner must be transparent" % (state, size))
            unthemed = self._load("%s-%s.png" % (APP_EXEC, state))
            self.assertTrue(unthemed.get_has_alpha())

    def test_desktop_and_udev_files(self):
        with open(os.path.join(PROJECT, "data", "tee-cell-stream-server.desktop"), encoding="utf-8") as handle:
            desktop = handle.read()
        for line in ("Name=TEE Cell Stream Server", "Exec=tee-cell-stream-server", "Icon=tee-cell-stream-server",
                     "StartupWMClass=de.tee.CellStreamServer", "Categories=Network;Game;", "Comment[de]=PC-Desktop zur PS3 streamen (cell-stream)"):
            self.assertIn(line, desktop)
        with open(os.path.join(PROJECT, "data", "70-tee-cell-stream-uinput.rules"), encoding="utf-8") as handle:
            rules = handle.read()
        self.assertIn('KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"', rules)


# ---------------------------------------------------------------------- autostart


FILE_UNDER_A_FILE = "tee-cell-stream-server.desktop"


class AutostartTest(unittest.TestCase):
    def setUp(self):
        autostart.set_enabled(False)

    def tearDown(self):
        autostart.set_enabled(False)

    def test_roundtrip(self):
        self.assertEqual(os.environ["TEE_CST_AUTOSTART_PATH"], autostart.path())
        self.assertFalse(autostart.is_enabled())
        self.assertTrue(autostart.set_enabled(True))
        self.assertTrue(autostart.is_enabled())
        with open(autostart.path(), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("[Desktop Entry]", text)
        self.assertIn("X-GNOME-Autostart-enabled=true", text)
        exec_line = [line for line in text.splitlines() if line.startswith("Exec=")][0]
        self.assertTrue(exec_line.endswith(" --minimized"), exec_line)
        self.assertIn("teecellstream" if not autostart.shutil.which(APP_EXEC) else APP_EXEC, exec_line)
        self.assertTrue(autostart.set_enabled(False))
        self.assertFalse(os.path.exists(autostart.path()))
        self.assertFalse(autostart.is_enabled())

    def test_disabled_in_place_counts_as_off(self):
        autostart.set_enabled(True)
        with open(autostart.path(), "a", encoding="utf-8") as handle:
            handle.write("X-GNOME-Autostart-enabled=false\n")
        self.assertFalse(autostart.is_enabled())

    def test_exec_quoting(self):
        self.assertEqual("plain", autostart._quote_exec_arg("plain"))
        self.assertEqual('"with space"', autostart._quote_exec_arg("with space"))
        self.assertEqual('"a\\"b\\$c"', autostart._quote_exec_arg('a"b$c'))

    def test_exec_quoting_doubles_a_literal_percent(self):
        # "%" is a field code in an Exec line (%f, %u, ...): a home directory containing one would otherwise
        # be swallowed by the launcher and we would start with a broken PYTHONPATH
        self.assertEqual('"a%%b"', autostart._quote_exec_arg("a%b"))
        self.assertEqual('"/home/spieler/100%% sicher"', autostart._quote_exec_arg("/home/spieler/100% sicher"))
        self.assertNotIn("%", autostart.exec_line().replace("%%", ""))

    def test_a_write_that_fails_is_reported_and_leaves_it_off(self):
        blocked = os.path.join(_TEMP, "blocked-autostart")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("kein Verzeichnis\n")   # makedirs on this path can only fail
        saved = os.environ["TEE_CST_AUTOSTART_PATH"]
        os.environ["TEE_CST_AUTOSTART_PATH"] = os.path.join(blocked, FILE_UNDER_A_FILE)
        try:
            self.assertFalse(autostart.set_enabled(True))
            self.assertFalse(autostart.is_enabled())
            self.assertIn("Autostart: konnte die Einstellung nicht ändern", log.get_recent())
        finally:
            os.environ["TEE_CST_AUTOSTART_PATH"] = saved


# ---------------------------------------------------------------------- the window


@unittest.skipIf(not HAVE_DISPLAY, "no display")
class MainWindowTest(unittest.TestCase):
    def _run_window(self, populate_steps):
        """Builds MainWindow(app, mock) from activate, runs the loop, returns (mock, notifications, steps)."""
        mock = MockServer()
        notifications = []
        settings.set("hide_notice_shown", False)
        app = Adw.Application(application_id=TEST_APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        steps = Steps(self)
        holder = {}

        class TestWindow(ui.MainWindow):
            def _send_notification(self, ident, title, body):   # no real popup on the developer's desktop
                notifications.append((ident, title, body))

        def on_activate(_app):
            # activate fires with nothing holding the app: without this hold g_application_run would leave its
            # loop at once, unregister, and only then drain the idle - the window would be built on an
            # unregistered app (Gtk-CRITICAL) and none of the steps below would ever run
            app.hold()

            def build():
                holder["window"] = TestWindow(app, mock)   # an application window holds the app from now on
                holder["window"].present()
                app.release()
                populate_steps(steps, holder, mock, notifications, app)
                steps.add(150, lambda: app.quit())
                steps.start()
                return GLib.SOURCE_REMOVE
            GLib.idle_add(build)

        app.connect("activate", on_activate)
        app.run(None)
        window = holder.get("window")
        if window is not None:
            window.shut_down()   # the destroy signal never arrives; without this every test leaks its timer
        steps.check()
        return mock, notifications, window

    def test_window_reflects_the_server(self):
        def populate(steps, holder, mock, notifications, app):
            def initial():
                window = holder["window"]
                self.assertEqual("TEE Cell Stream Server", window.get_title())
                self.assertEqual(ui.STATUS_WAITING, window.status_label.get_text())
                self.assertEqual(mock.settings_summary, window.subtitle_label.get_text())
                self.assertEqual("Stop", window.start_stop_button.get_label())
                self.assertTrue(window.start_stop_button.has_css_class("destructive-action"))
                self.assertTrue(window.start_stop_button.has_css_class("pill"))
                self.assertTrue(window.status_dot.has_css_class("status-dot-idle"))
                self.assertTrue(window.encoder_row.get_sensitive())
                self.assertEqual(2, window.encoder_model.get_n_items())
                self.assertEqual("NVIDIA GPU (NVENC)", window.encoder_model.get_string(0))
                self.assertEqual(0, window.encoder_row.get_selected())
                self.assertEqual(0, window.recovery_row.get_selected())
                self.assertTrue(window.display_row.get_active())
                self.assertFalse(window.swap_sticks_row.get_active())
                self.assertIsNotNone(window.view_stack.get_child_by_name("server"))
                self.assertIsNotNone(window.view_stack.get_child_by_name("commands"))
                self.assertEqual(4, len(window.slot_rows))
                self.assertEqual("steam://open/bigpicture", window.slot_rows[0][1].get_text())
                self.assertEqual(["<Control>q"], app.get_accels_for_action("app.quit"))
                self.assertEqual(["<Control>l"], app.get_accels_for_action("win.open-log"))
                # the log view follows log.write, but only on the tick and only when the generation moved
                log.write("gui-test: Zeile eins")

            def log_shown_then_stop():
                window = holder["window"]
                buffer = window.log_view.get_buffer()
                text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
                self.assertIn("gui-test: Zeile eins", text)
                # the user stops the server: label flips, server gets the reason
                window.start_stop_button.emit("clicked")
                self.assertEqual(["von dir gestoppt"], mock.disarm_reasons)
                self.assertEqual("Start", window.start_stop_button.get_label())
                self.assertTrue(window.start_stop_button.has_css_class("suggested-action"))
                self.assertFalse(window.start_stop_button.has_css_class("destructive-action"))
                self.assertEqual(ui.STATUS_STOPPED, window.status_label.get_text())
                self.assertEqual("von dir gestoppt", window.subtitle_label.get_text())
                window.start_stop_button.emit("clicked")
                self.assertEqual(1, mock.arm_calls)
                self.assertEqual("Stop", window.start_stop_button.get_label())
                # the fuse trips behind the window's back: the tick picks it up
                mock.is_armed = False
                mock.trip_reason = "auf diesem PC funktioniert kein Video-Encoder. Nach der Behebung auf Start drücken."

            def tripped_then_connect():
                window = holder["window"]
                self.assertEqual("Start", window.start_stop_button.get_label())
                self.assertEqual(mock.trip_reason, window.subtitle_label.get_text())
                mock.is_armed = True
                mock.trip_reason = None
                mock.is_ps3_connected = True
                mock.connected_ps3 = "10.42.0.151"

            def connected():
                window = holder["window"]
                self.assertEqual(ui.STATUS_CONNECTED + "10.42.0.151", window.status_label.get_text())
                self.assertTrue(window.status_dot.has_css_class("status-dot-live"))
                self.assertFalse(window.status_dot.has_css_class("status-dot-idle"))
                self.assertFalse(window.encoder_row.get_sensitive())
                self.assertFalse(window.recovery_row.get_sensitive())
                # a change that slips through while streaming is put back
                window.encoder_row.set_selected(1)
                self.assertIs(mock.available_encoders[0], mock.chosen_encoder)
                self.assertEqual(0, window.encoder_row.get_selected())
                mock.is_ps3_connected = False
                mock.connected_ps3 = None

            def disconnected_and_choices():
                window = holder["window"]
                self.assertTrue(window.encoder_row.get_sensitive())
                self.assertTrue(window.status_dot.has_css_class("status-dot-idle"))
                window.encoder_row.set_selected(1)
                self.assertIs(mock.available_encoders[1], mock.chosen_encoder)
                window.recovery_row.set_selected(1)
                self.assertEqual("keyframe", mock.loss_recovery)
                window.display_row.set_active(False)
                self.assertFalse(mock.switch_display_mode)
                window.swap_sticks_row.set_active(True)
                self.assertTrue(mock.swap_mouse_sticks)
                # Befehle: the kind saves at once, the text after a short pause
                kind_row, value_row, label_row = window.slot_rows[1]
                kind_row.set_selected(1)
                self.assertTrue(value_row.get_sensitive())
                value_row.set_text("firefox https://example.org")
                label_row.set_text("Browser")

            def commands_saved():
                slot2 = _fake_commands.slots[2]
                self.assertEqual({"kind": "run", "value": "firefox https://example.org", "label": "Browser"}, slot2)
                # autostart from the switch row and the menu action agree with the file
                window = holder["window"]
                window.autostart_row.set_active(True)
                self.assertTrue(autostart.is_enabled())
                self.assertTrue(window.lookup_action("autostart").get_state().get_boolean())
                window.lookup_action("autostart").change_state(GLib.Variant.new_boolean(False))
                self.assertFalse(autostart.is_enabled())
                self.assertFalse(window.autostart_row.get_active())
                # the About dialog opens on top of the window and can be closed again
                about = window.show_about()
                self.assertIsInstance(about, Adw.AboutDialog)
                self.assertEqual("TEE Cell Stream Server", about.get_application_name())
                about.force_close()
                # closing hides, does not quit; the one-time notice goes out once
                self.assertTrue(window.get_visible())
                window.close()

            def hidden():
                window = holder["window"]
                self.assertFalse(window.get_visible())
                self.assertTrue(settings.get("hide_notice_shown", False))
                self.assertEqual(1, len(notifications))
                self.assertEqual("hidden", notifications[0][0])
                self.assertEqual(0, mock.shutdown_calls)
                window.present_from_tray()
                self.assertTrue(window.get_visible())
                window.close()
                self.assertEqual(1, len(notifications), "the notice is shown only once")
                # tray click / close, five times in a row: no leak of notices, no stuck state
                for _cycle in range(5):
                    window.present_from_tray()
                    self.assertTrue(window.get_visible())
                    window.close()
                    self.assertFalse(window.get_visible())
                self.assertEqual(1, len(notifications))
                self.assertEqual(0, mock.shutdown_calls)

            steps.add(300, initial)
            steps.add(700, log_shown_then_stop)
            steps.add(700, tripped_then_connect)
            steps.add(700, connected)
            steps.add(700, disconnected_and_choices)
            steps.add(700, commands_saved)
            steps.add(300, hidden)

        mock, _notifications, window = self._run_window(populate)
        self.assertIsNotNone(window)
        self.assertEqual(0, mock.shutdown_calls)   # the window never stops the server; only Beenden does

    # ------------------------------------------------------------------ the two rows added for the PS3's decoder

    def test_the_new_video_rows_sync_both_ways_and_lock_while_streaming(self):
        def populate(steps, holder, mock, notifications, app):
            def initial():
                window = holder["window"]
                # both lists come straight from protocol.py, so the rows can never offer a value the server rejects
                self.assertEqual(len(protocol.BITRATE_CHOICES_KBPS), window.bitrate_row.get_model().get_n_items())
                self.assertEqual(len(protocol.ENTROPY_CODERS), window.coder_row.get_model().get_n_items())
                self.assertEqual(len(protocol.STREAM_SIZES), window.size_row.get_model().get_n_items())
                self.assertEqual(protocol.STREAM_SIZES.index((1280, 720)), window.size_row.get_selected())
                self.assertTrue(window.size_row.get_sensitive())
                self.assertEqual(protocol.BITRATE_CHOICES_KBPS.index(6000), window.bitrate_row.get_selected())
                self.assertEqual(protocol.ENTROPY_CODERS.index("cavlc"), window.coder_row.get_selected())
                # the recommendation marks whatever the default bitrate currently is
                self.assertIn("(empfohlen)", ui.BITRATE_LABELS[protocol.BITRATE_CHOICES_KBPS.index(protocol.KBPS)])
                self.assertTrue(ui.ENTROPY_LABELS[0].startswith("CAVLC"), ui.ENTROPY_LABELS)
                self.assertTrue(window.bitrate_row.get_sensitive())
                self.assertTrue(window.coder_row.get_sensitive())
                # window -> server
                window.bitrate_row.set_selected(protocol.BITRATE_CHOICES_KBPS.index(12000))
                self.assertEqual(12000, mock.video_kbps)
                window.coder_row.set_selected(protocol.ENTROPY_CODERS.index("cabac"))
                self.assertEqual("cabac", mock.entropy_coder)
                window.size_row.set_selected(protocol.STREAM_SIZES.index((1536, 864)))
                self.assertEqual((1536, 864), mock.stream_size)
                # server -> window: something else moved them, the refresh tick must follow without a fight
                mock.video_kbps = 4000
                mock.entropy_coder = "cavlc"
                mock.stream_size = (1280, 720)

            def followed_the_server():
                window = holder["window"]
                self.assertEqual(protocol.BITRATE_CHOICES_KBPS.index(4000), window.bitrate_row.get_selected())
                self.assertEqual(protocol.ENTROPY_CODERS.index("cavlc"), window.coder_row.get_selected())
                self.assertIn("4 Mbit/s", window.subtitle_label.get_text())
                self.assertIn("CAVLC", window.subtitle_label.get_text())
                self.assertEqual(protocol.STREAM_SIZES.index((1280, 720)), window.size_row.get_selected())
                self.assertIn("1280x720", window.subtitle_label.get_text())
                self.assertEqual(4000, mock.video_kbps, "following the server must not write back to it")
                mock.is_ps3_connected = True
                mock.connected_ps3 = "10.42.0.151"

            def locked_while_streaming():
                window = holder["window"]
                self.assertFalse(window.bitrate_row.get_sensitive())
                self.assertFalse(window.coder_row.get_sensitive())
                self.assertFalse(window.size_row.get_sensitive())
                # a change that slips through between the PS3 connecting and the next tick is put back
                window.bitrate_row.set_selected(protocol.BITRATE_CHOICES_KBPS.index(12000))
                self.assertEqual(4000, mock.video_kbps, "the running stream keeps its bitrate")
                self.assertEqual(protocol.BITRATE_CHOICES_KBPS.index(4000), window.bitrate_row.get_selected())
                window.coder_row.set_selected(protocol.ENTROPY_CODERS.index("cabac"))
                self.assertEqual("cavlc", mock.entropy_coder)
                self.assertEqual(protocol.ENTROPY_CODERS.index("cavlc"), window.coder_row.get_selected())
                window.size_row.set_selected(protocol.STREAM_SIZES.index((1792, 1008)))
                self.assertEqual((1280, 720), mock.stream_size, "der laufende Stream behält seine Größe")
                recent = log.get_recent()
                self.assertIn("video: erst den Stream beenden, dann die Auflösung wechseln", recent)
                self.assertIn("video: erst den Stream beenden, dann die Bitrate wechseln", recent)
                self.assertIn("video: erst den Stream beenden, dann die Entropie-Codierung wechseln", recent)
                mock.is_ps3_connected = False
                mock.connected_ps3 = None
                # a hand-edited settings file: the server hands back values these rows do not offer
                mock.video_kbps = 7777
                mock.entropy_coder = "brotli"

            def unknown_values_fall_back_without_writing_back():
                window = holder["window"]
                self.assertEqual(0, window.bitrate_row.get_selected())
                self.assertEqual(0, window.coder_row.get_selected())
                self.assertEqual(7777, mock.video_kbps, "showing a fallback must not push it into the server")
                self.assertEqual("brotli", mock.entropy_coder)

            steps.add(300, initial)
            steps.add(700, followed_the_server)
            steps.add(700, locked_while_streaming)
            steps.add(700, unknown_values_fall_back_without_writing_back)

        self._run_window(populate)

    def test_closing_hides_even_when_hiding_itself_fails(self):
        # measured on GTK 4.22: an exception escaping close-request leaves the handler at FALSE and GTK
        # destroys the window after all - the tray's Anzeigen would then re-show a destroyed window
        def populate(steps, holder, mock, notifications, app):
            def close_with_a_broken_hide():
                window = holder["window"]

                def explode():
                    raise RuntimeError("Benachrichtigung kaputt")
                window.hide_to_tray = explode
                window.close()
                del window.hide_to_tray
                self.assertFalse(window.get_visible())
                self.assertIsNotNone(window.get_application(), "close must hide the window, never destroy it")
                self.assertIn("Fenster: konnte nicht in den Hintergrund gehen", log.get_recent())
                window.present_from_tray()
                self.assertTrue(window.get_visible())
                self.assertEqual(0, mock.shutdown_calls)

            steps.add(300, close_with_a_broken_hide)

        self._run_window(populate)

    def test_a_pc_without_any_encoder(self):
        def populate(steps, holder, mock, notifications, app):
            def break_the_encoders():
                mock.available_encoders = []
                mock.chosen_encoder = None
                mock.is_armed = False
                mock.trip_reason = "auf diesem PC funktioniert kein Video-Encoder (ffmpeg fehlt oder kann kein H.264). Nach der Behebung auf Start drücken."

            def window_says_so():
                window = holder["window"]
                self.assertEqual(0, window.encoder_model.get_n_items())
                self.assertFalse(window.encoder_row.get_sensitive())
                self.assertEqual("Kein H.264-Encoder gefunden", window.encoder_row.get_subtitle())
                self.assertEqual(ui.STATUS_STOPPED, window.status_label.get_text())
                self.assertEqual(mock.trip_reason, window.subtitle_label.get_text())
                self.assertEqual("Start", window.start_stop_button.get_label())
                # the other knobs stay usable: the user may want to change one before pressing Start again
                self.assertTrue(window.bitrate_row.get_sensitive())
                self.assertTrue(window.coder_row.get_sensitive())
                self.assertTrue(window.recovery_row.get_sensitive())

            steps.add(300, break_the_encoders)
            steps.add(700, window_says_so)

        self._run_window(populate)

    def test_a_pending_command_edit_is_flushed_when_the_window_hides(self):
        # the Befehle fields save 400 ms after the last keystroke; closing the window inside that pause is the
        # most likely way to lose an edit, so hiding writes it out at once
        def populate(steps, holder, mock, notifications, app):
            def type_and_close():
                window = holder["window"]
                kind_row, value_row, label_row = window.slot_rows[2]
                kind_row.set_selected(ui.COMMAND_KINDS.index("run"))
                value_row.set_text("nautilus /home")
                label_row.set_text("Dateien")
                self.assertIn(3, window._slot_save_timers, "the save is still inside the 400 ms pause")
                window.close()
                self.assertEqual({}, window._slot_save_timers)
                self.assertEqual({"kind": "run", "value": "nautilus /home", "label": "Dateien"}, _fake_commands.slots[3])

            steps.add(300, type_and_close)

        self._run_window(populate)

    def test_shut_down_gives_the_refresh_timer_back(self):
        # measured on GTK 4.22.4 / PyGObject 3.56: a destroy handler connected on a Python subclass of
        # Adw.ApplicationWindow never runs, so nothing but this call stops the 500 ms tick - a destroyed
        # window kept ticking three more times in 1.5 s and stayed alive for the life of the process
        ticks = []

        def populate(steps, holder, mock, notifications, app):
            def count_then_shut_down():
                window = holder["window"]
                original = window.refresh
                window.refresh = lambda: (ticks.append(1), original())[1]

            def stop_it():
                window = holder["window"]
                self.assertGreater(len(ticks), 0, "the window is supposed to poll every 500 ms")
                self.assertNotEqual(0, window._timer_id)
                window.shut_down()
                self.assertEqual(0, window._timer_id)
                ticks.append("stopped")

            def stayed_quiet():
                self.assertEqual("stopped", ticks[-1], "the tick fired again after shut_down")
                holder["window"].shut_down()   # twice is harmless

            steps.add(100, count_then_shut_down)
            steps.add(1200, stop_it)
            steps.add(1200, stayed_quiet)

        self._run_window(populate)

    def test_the_window_only_ever_touches_gtk_from_the_main_thread(self):
        # the window is a poller, never a callback target: a server churning away on its own threads must
        # never make a widget move off the main loop, and must never break a refresh either
        main_thread = threading.get_ident()
        seen_threads = set()
        stop = threading.Event()

        def populate(steps, holder, mock, notifications, app):
            def start_the_churn():
                window = holder["window"]
                original = window.refresh

                def recording():
                    seen_threads.add(threading.get_ident())
                    original()
                window.refresh = recording

                def churn():
                    tick = 0
                    while not stop.is_set():
                        mock.video_kbps = protocol.BITRATE_CHOICES_KBPS[tick % len(protocol.BITRATE_CHOICES_KBPS)]
                        mock.entropy_coder = protocol.ENTROPY_CODERS[tick % len(protocol.ENTROPY_CODERS)]
                        mock.loss_recovery = ui.LOSS_RECOVERY_KINDS[tick % len(ui.LOSS_RECOVERY_KINDS)]
                        mock.is_ps3_connected = bool(tick % 2)
                        mock.connected_ps3 = "10.42.0.151" if tick % 2 else None
                        tick += 1
                        time.sleep(0.002)
                threading.Thread(target=churn, name="gui-churn", daemon=True).start()

            def churned_without_damage():
                stop.set()
                self.assertEqual({main_thread}, seen_threads, "a widget moved outside the main loop")
                self.assertNotIn("Fenster: Aktualisierung fehlgeschlagen", log.get_recent())

            steps.add(100, start_the_churn)
            steps.add(1600, churned_without_damage)

        try:
            self._run_window(populate)
        finally:
            stop.set()



# ---------------------------------------------------------------------- the application


@unittest.skipIf(not HAVE_DISPLAY, "no display")
class ApplicationTest(unittest.TestCase):
    def _make_app(self, notifications):
        mock = MockServer()

        class TestApp(CellStreamApplication):
            def _notify(self, ident, title, body):
                notifications.append((ident, title, body))

        app = TestApp(server_factory=lambda: mock, application_id=TEST_APP_ID, extra_flags=Gio.ApplicationFlags.NON_UNIQUE)
        return app, mock

    def test_minimized_start_and_quit_action(self):
        notifications = []
        app, mock = self._make_app(notifications)
        steps = Steps(self)

        def started():
            self.assertEqual(1, mock.start_calls)
            self.assertIsNotNone(app.window)
            self.assertFalse(app.window.get_visible(), "--minimized must not show the window")
            self.assertIsNotNone(app.tray)
            self.assertTrue(app.tray.is_started or not HAVE_WATCHER)
            mock.is_ps3_connected = True
            mock.connected_ps3 = "10.42.0.151"

        def connected():
            self.assertEqual([("ps3", "PS3 verbunden", "10.42.0.151 streamt.")], notifications)
            self.assertEqual(tray.ICON_LIVE, app.tray.icon_name)
            mock.is_ps3_connected = False
            mock.connected_ps3 = None
            mock.is_armed = False
            mock.trip_reason = "der Encoder ist kaputt. Nach der Behebung auf Start drücken."

        def tripped():
            self.assertEqual(3, len(notifications))
            self.assertEqual(("ps3", "PS3 getrennt", "Warte, bis sie wiederkommt."), notifications[1])
            self.assertEqual(("fuse", "Streaming gestoppt", mock.trip_reason), notifications[2])
            self.assertEqual(tray.ICON_IDLE, app.tray.icon_name)
            # the token the panel provided before its Activate reaches the window exactly once (the window's
            # present is stubbed: a made-up token must never reach the compositor on the developer's desktop)
            presented = []
            app.window.present_from_tray = lambda token=None: presented.append(token)
            app.tray._activation_token = "tee-test-token"
            app.present_window()
            app.present_window()
            self.assertEqual(["tee-test-token", None], presented)
            app.activate_action("quit", None)

        steps.add(400, started)
        steps.add(700, connected)
        steps.add(700, tripped)
        steps.add(3000, lambda: self.fail("quit did not end the main loop"))
        GLib.idle_add(lambda: (steps.start(), GLib.SOURCE_REMOVE)[1])
        app.run(["tee-cell-stream-server", "--minimized"])
        steps.check()
        self.assertEqual(1, mock.shutdown_calls)
        self.assertFalse(app.tray.is_started)

    def test_bind_failure_shows_dialog_and_quits(self):
        notifications = []
        app, mock = self._make_app(notifications)
        mock.start_result = False   # what Server.start() returns when another copy holds udp :38310
        steps = Steps(self)
        seen = []

        def answer_dialog():
            dialogs = [window for window in app.get_windows() if isinstance(window, Adw.MessageDialog)]
            self.assertEqual(1, len(dialogs), [type(window) for window in app.get_windows()])
            self.assertEqual(ALREADY_RUNNING_TEXT, dialogs[0].get_body())
            seen.append(dialogs[0].get_heading())
            dialogs[0].response("ok")

        steps.add(400, answer_dialog)
        steps.add(3000, lambda: self.fail("the dialog's OK did not end the main loop"))
        GLib.idle_add(lambda: (steps.start(), GLib.SOURCE_REMOVE)[1])
        app.run(["tee-cell-stream-server"])
        steps.check()
        self.assertEqual(["Läuft schon"], seen)
        self.assertTrue(app.start_failed, "main() turns this into exit status 1, like --headless")
        self.assertEqual(1, mock.start_calls)
        self.assertIsNone(app.window, "no window without a server")
        self.assertIsNone(app.tray)
        self.assertEqual(0, mock.shutdown_calls, "a server that never started has nothing to shut down")

    def test_sigint_shuts_the_server_down(self):
        notifications = []
        app, mock = self._make_app(notifications)
        steps = Steps(self)

        def send_signal():
            self.assertTrue(app.window.get_visible())
            os.kill(os.getpid(), signal.SIGINT)

        steps.add(500, send_signal)
        steps.add(3000, lambda: self.fail("SIGINT did not end the main loop"))
        GLib.idle_add(lambda: (steps.start(), GLib.SOURCE_REMOVE)[1])
        app.run(["tee-cell-stream-server"])
        steps.check()
        self.assertEqual(1, mock.shutdown_calls)

    def test_quit_survives_a_failing_teardown(self):
        # a server that throws on shutdown must not leave an app nothing but SIGKILL can end
        notifications = []
        app, mock = self._make_app(notifications)
        mock.shutdown_error = RuntimeError("Mutter antwortet nicht")
        steps = Steps(self)
        steps.add(400, lambda: app.activate_action("quit", None))
        steps.add(3000, lambda: self.fail("quit did not end the main loop although the teardown raised"))
        GLib.idle_add(lambda: (steps.start(), GLib.SOURCE_REMOVE)[1])
        app.run(["tee-cell-stream-server", "--minimized"])
        steps.check()
        self.assertEqual(1, mock.shutdown_calls)
        self.assertFalse(app.tray.is_started, "the tray must go even when the server would not")
        self.assertIn("ließ sich nicht sauber stoppen: Mutter antwortet nicht", log.get_recent())

    def test_the_exit_hook_goes_with_the_teardown(self):
        # atexit is the last-resort restore of the desktop resolution. Left registered, a shutdown that
        # already failed once is run again at interpreter exit, where nothing is left to catch it.
        class Recorder:
            def __init__(self):
                self.registered = []

            def register(self, func):
                self.registered.append(func)

            def unregister(self, func):
                self.registered = [known for known in self.registered if known != func]

        notifications = []
        app, mock = self._make_app(notifications)
        mock.shutdown_error = RuntimeError("Mutter antwortet nicht")
        recorder = Recorder()
        saved, app_module.atexit = app_module.atexit, recorder
        steps = Steps(self)

        def registered_then_quit():
            self.assertEqual(1, len(recorder.registered), "the server must be armed for a hard exit")
            app.activate_action("quit", None)

        steps.add(400, registered_then_quit)
        steps.add(3000, lambda: self.fail("quit did not end the main loop"))
        GLib.idle_add(lambda: (steps.start(), GLib.SOURCE_REMOVE)[1])
        try:
            app.run(["tee-cell-stream-server", "--minimized"])
        finally:
            app_module.atexit = saved
        steps.check()
        self.assertEqual(1, mock.shutdown_calls)
        self.assertEqual([], recorder.registered, "the hook must be dropped once the teardown has run")

    def test_quit_flushes_the_window_and_stops_its_timer(self):
        notifications = []
        app, mock = self._make_app(notifications)
        steps = Steps(self)

        def type_then_quit():
            window = app.window
            kind_row, value_row, label_row = window.slot_rows[3]
            kind_row.set_selected(ui.COMMAND_KINDS.index("run"))
            value_row.set_text("systemctl suspend")
            label_row.set_text("Standby")
            self.assertIn(4, window._slot_save_timers)
            app.activate_action("quit", None)

        steps.add(400, type_then_quit)
        steps.add(3000, lambda: self.fail("quit did not end the main loop"))
        GLib.idle_add(lambda: (steps.start(), GLib.SOURCE_REMOVE)[1])
        app.run(["tee-cell-stream-server", "--minimized"])
        steps.check()
        self.assertEqual({"kind": "run", "value": "systemctl suspend", "label": "Standby"}, _fake_commands.slots[4])
        self.assertEqual({}, app.window._slot_save_timers)
        self.assertEqual(0, app.window._timer_id, "the 500 ms tick must not outlive the application")
        self.assertEqual(1, mock.shutdown_calls)

    def test_crash_log_hook(self):
        # port of App.xaml.cs's UnhandledException log: an exception in a GLib callback lands in the log
        install_crash_log()
        notifications = []
        app, _mock = self._make_app(notifications)
        steps = Steps(self)
        quiet = io.StringIO()

        def raise_in_callback():
            sys.stderr = quiet   # PyGObject prints the traceback; keep it out of the test output
            GLib.idle_add(lambda: 1 // 0)

        def check():
            sys.stderr = sys.__stderr__
            self.assertIn("abgestürzt: ZeroDivisionError:", log.get_recent())
            self.assertIn("ZeroDivisionError", quiet.getvalue(), "the traceback still goes to stderr")
            app.activate_action("quit", None)

        steps.add(300, raise_in_callback)
        steps.add(300, check)
        steps.add(3000, lambda: self.fail("quit did not end the main loop"))
        GLib.idle_add(lambda: (steps.start(), GLib.SOURCE_REMOVE)[1])
        try:
            app.run(["tee-cell-stream-server", "--minimized"])
        finally:
            sys.stderr = sys.__stderr__
        steps.check()
        # and a thread that dies uncaught
        worker = threading.Thread(target=lambda: [][1], name="crash-probe", daemon=True)
        sys.stderr = quiet
        try:
            worker.start()
            worker.join(2)
        finally:
            sys.stderr = sys.__stderr__
        self.assertIn("abgestürzt (Thread crash-probe): IndexError:", log.get_recent())


class OpenLogTest(unittest.TestCase):
    def test_fallback_to_xdg_open(self):
        # no Gtk.FileLauncher (older GTK, or it refused): the log goes to xdg-open - recorded here, not launched
        spawned = []
        saved_gtk, saved_popen = ui.Gtk, ui.subprocess.Popen
        ui.Gtk = types.SimpleNamespace(FileLauncher=None)   # .new raises AttributeError, like a GTK without it
        ui.subprocess.Popen = lambda argv, **kw: spawned.append((argv, kw))
        try:
            ui.open_log(None)
        finally:
            ui.Gtk, ui.subprocess.Popen = saved_gtk, saved_popen
        self.assertEqual([["xdg-open", log.LOG_PATH]], [argv for argv, _kw in spawned])
        self.assertTrue(spawned[0][1].get("start_new_session"), "the editor must outlive the server")
        self.assertTrue(os.path.exists(log.LOG_PATH), "an empty log is created so there is something to open")


# ---------------------------------------------------------------------- the application with the real server


@unittest.skipIf(not HAVE_DISPLAY, "no display")
@unittest.skipIf(not shutil.which("ffmpeg"), "no ffmpeg")
class RealServerApplicationTest(unittest.TestCase):
    """app.py + ui.py against teecellstream.server.Server itself - on a private port, with the beacon pointed
    at this machine only, the test capture source and no display-mode switch, so nothing reaches the LAN, the
    portal or the desktop. The encoder probe (ffmpeg, ~0.5 s) runs for real."""

    def test_app_runs_the_real_server(self):
        from teecellstream import netinfo
        from teecellstream.server import Server

        # a free UDP port for the server and a listener for its beacon
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        server_port = probe.getsockname()[1]
        probe.close()
        beacon_sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        beacon_sink.bind(("127.0.0.1", 0))
        beacon_sink.settimeout(0.5)
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(2.0)

        saved = (protocol.SERVER_PORT, netinfo.get_beacon_targets, sys.getswitchinterval(),
                 os.environ.get("TEE_CST_TEST_SOURCE"), os.environ.get("TEE_CST_NO_DISPLAY_SWITCH"))
        protocol.SERVER_PORT = server_port
        netinfo.get_beacon_targets = lambda: [beacon_sink.getsockname()]
        os.environ["TEE_CST_TEST_SOURCE"] = "1"
        os.environ["TEE_CST_NO_DISPLAY_SWITCH"] = "1"
        holder = {}

        def make_server():
            holder["server"] = Server()
            return holder["server"]

        notifications = []

        class TestApp(CellStreamApplication):
            def _notify(self, ident, title, body):
                notifications.append((ident, title, body))

        app = TestApp(server_factory=make_server, application_id=TEST_APP_ID, extra_flags=Gio.ApplicationFlags.NON_UNIQUE)
        steps = Steps(self)
        try:
            def started():
                server = holder["server"]
                self.assertIs(server, app.server)
                self.assertTrue(server.is_armed)
                self.assertFalse(server.is_ps3_connected)
                self.assertIsNotNone(app.window)
                self.assertFalse(app.window.get_visible())
                window = app.window
                self.assertEqual(ui.STATUS_WAITING, window.status_label.get_text())
                self.assertEqual(server.settings_summary, window.subtitle_label.get_text())
                self.assertIn("1280x720 mit 60 fps", window.subtitle_label.get_text())
                names = [window.encoder_model.get_string(i) for i in range(window.encoder_model.get_n_items())]
                self.assertEqual([encoder.name for encoder in server.available_encoders], names)
                self.assertGreater(len(names), 0, "ffmpeg found no H.264 encoder at all")
                self.assertEqual(server.available_encoders.index(server.chosen_encoder), window.encoder_row.get_selected())
                self.assertTrue(window.encoder_row.get_sensitive())
                # the real socket is up: it answers TIME and its beacon reaches our sink
                client.sendto(b"TIME", ("127.0.0.1", server_port))
                reply, _sender = client.recvfrom(64)
                self.assertTrue(reply.startswith(b"TIME "), reply)
                self.assertGreater(int(reply[5:]), 0)
                beacon, _sender = beacon_sink.recvfrom(64)
                self.assertEqual(protocol.BEACON_MESSAGE, beacon)
                # a setting made in the window lands in the real server (and its settings file)
                window.swap_sticks_row.set_active(True)
                self.assertTrue(server.swap_mouse_sticks)
                window.swap_sticks_row.set_active(False)
                self.assertFalse(server.swap_mouse_sticks)
                window.present_from_tray()

            def shown_then_quit():
                self.assertTrue(app.window.get_visible())
                app.activate_action("quit", None)

            steps.add(1500, started)
            steps.add(400, shown_then_quit)
            steps.add(5000, lambda: self.fail("quit did not end the main loop"))
            GLib.idle_add(lambda: (steps.start(), GLib.SOURCE_REMOVE)[1])
            app.run(["tee-cell-stream-server", "--minimized"])
            steps.check()
            server = holder["server"]
            self.assertFalse(server._running, "quit must shut the real server down")
            self.assertFalse(app.tray.is_started)
            self.assertEqual([], notifications, "no PS3 came, so nothing to announce")
        finally:
            if "server" in holder:
                holder["server"].shutdown()
            protocol.SERVER_PORT, netinfo.get_beacon_targets = saved[0], saved[1]
            sys.setswitchinterval(saved[2])
            for key, value in (("TEE_CST_TEST_SOURCE", saved[3]), ("TEE_CST_NO_DISPLAY_SWITCH", saved[4])):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            beacon_sink.close()
            client.close()


# ---------------------------------------------------------------------- the whole thing as a process

# the app exactly as __main__ starts it, except: a private port, the beacon kept on this machine, a test bus
# name (a real copy of the server may be running on this desktop), and the test capture source
PROCESS_BOOTSTRAP = r"""
import sys
sys.path.insert(0, %(src)r)
from teecellstream import netinfo, protocol
protocol.SERVER_PORT = %(port)d
netinfo.get_beacon_targets = lambda: [("127.0.0.1", %(beacon)d)]
from teecellstream.app import CellStreamApplication
sys.exit(CellStreamApplication(application_id=%(app_id)r).run(sys.argv))
"""


@unittest.skipIf(not HAVE_DISPLAY, "no display")
@unittest.skipIf(not shutil.which("ffmpeg"), "no ffmpeg")
class ProcessTest(unittest.TestCase):
    """Single instance and SIGTERM, as seen from outside the process (what the autostart entry and a log-out do)."""

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="tee-cst-proc-")
        self.log_path = os.path.join(self.temp, "server.log")
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.env = dict(os.environ, TEE_CST_SETTINGS_PATH=os.path.join(self.temp, "settings.json"), TEE_CST_LOG_PATH=self.log_path,
                        TEE_CST_AUTOSTART_PATH=os.path.join(self.temp, "autostart.desktop"),
                        TEE_CST_TEST_SOURCE="1", TEE_CST_NO_DISPLAY_SWITCH="1", PYTHONUNBUFFERED="1")
        self.code = PROCESS_BOOTSTRAP % {"src": os.path.join(PROJECT, "src"), "port": self.port, "beacon": self.port + 1,
                                         "app_id": TEST_APP_ID + ".Process"}
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.output.close()

    def _spawn(self, *args):
        output = open(os.path.join(self.temp, "out-%d.txt" % len(self.children)), "w+b")
        child = subprocess.Popen([sys.executable, "-c", self.code, *args], env=self.env, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT)
        child.output = output
        self.children.append(child)
        return child

    def _output(self, child) -> str:
        child.output.seek(0)
        return child.output.read().decode("utf-8", "replace")

    def _log(self) -> str:
        try:
            with open(self.log_path, encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""

    def _wait_for(self, predicate, timeout_s, what):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail("timeout waiting for " + what + "\nlog:\n" + self._log() + "\noutput:\n" + "".join(self._output(c) for c in self.children))

    def test_single_instance_and_sigterm(self):
        first = self._spawn("--minimized")
        self._wait_for(lambda: "lausche auf udp :%d" % self.port in self._log(), 15, "the server to bind")
        self._wait_for(lambda: "bereit:" in self._log(), 15, "the encoder probe")

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(3.0)
        try:
            client.sendto(b"TIME", ("127.0.0.1", self.port))
            reply, _sender = client.recvfrom(64)
            self.assertTrue(reply.startswith(b"TIME "), reply)
        finally:
            client.close()

        # a second copy hands its command line to the first and exits at once; the first shows its window
        second = self._spawn()
        self._wait_for(lambda: second.poll() is not None, 20, "the second copy to exit")
        self.assertEqual(0, second.returncode, self._output(second))
        self._wait_for(lambda: "eine zweite Kopie wurde gestartet" in self._log(), 5, "the first copy to notice the second")
        self.assertIsNone(first.poll(), "the first copy must keep running")

        first.send_signal(signal.SIGTERM)
        self._wait_for(lambda: first.poll() is not None, 15, "the first copy to exit on SIGTERM")
        self.assertEqual(0, first.returncode, self._output(first))
        text = self._log()
        self.assertIn("Signal 15 - beende", text)
        self.assertNotIn("Traceback", self._output(first))


# ---------------------------------------------------------------------- the tray


@unittest.skipIf(not HAVE_WATCHER, "no org.kde.StatusNotifierWatcher on the session bus")
class TrayTest(unittest.TestCase):
    def test_registers_menu_and_unregisters(self):
        calls = []
        icon = tray.TrayIcon(on_show=lambda: calls.append("show:%s" % icon.take_activation_token()),
                             on_open_log=lambda: calls.append("log"), on_quit=lambda: calls.append("quit"))
        loop = GLib.MainLoop()
        failures = []
        bus = _session_bus()

        def ours(items):
            unique = icon.unique_name
            return [item for item in items if item == icon.bus_name or (unique and item.startswith(unique + "@"))]

        def wait_for(predicate, timeout_s):
            deadline = GLib.get_monotonic_time() + int(timeout_s * 1e6)
            while GLib.get_monotonic_time() < deadline:
                if predicate():
                    return True
                threading.Event().wait(0.1)
            return False

        def worker():
            # runs off the main loop: sync calls to our own service would otherwise deadlock
            try:
                self.assertTrue(wait_for(lambda: ours(_watcher_items(bus)), 8), "the watcher did not list our item")
                self.assertTrue(wait_for(lambda: icon.is_registered, 3))
                props = bus.call_sync(icon.bus_name, tray.ITEM_PATH, "org.freedesktop.DBus.Properties", "GetAll",
                                      GLib.Variant("(s)", (tray.ITEM_INTERFACE,)), None, Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
                self.assertEqual("ApplicationStatus", props["Category"])
                self.assertEqual("de.tee.CellStreamServer", props["Id"])
                self.assertEqual("Active", props["Status"])
                self.assertEqual(tray.ICON_IDLE, props["IconName"])
                self.assertEqual(tray.MENU_PATH, props["Menu"])
                self.assertEqual(ICON_DIR, props["IconThemePath"])
                self.assertEqual([22, 24, 32, 48], [width for width, _h, _data in props["IconPixmap"]])
                self.assertEqual(22 * 22 * 4, len(props["IconPixmap"][0][2]))

                layout = bus.call_sync(icon.bus_name, tray.MENU_PATH, tray.MENU_INTERFACE, "GetLayout",
                                       GLib.Variant("(iias)", (0, -1, [])), None, Gio.DBusCallFlags.NONE, 3000, None).unpack()
                _revision, (root_id, _root_props, children) = layout
                self.assertEqual(0, root_id)
                labels = [child[1].get("label", child[1].get("type")) for child in children]
                self.assertEqual(["Anzeigen", "Log öffnen", "separator", "Beenden"], labels)

                GLib.idle_add(lambda: (icon.set_live(True), GLib.SOURCE_REMOVE)[1])
                self.assertTrue(wait_for(lambda: bus.call_sync(icon.bus_name, tray.ITEM_PATH, "org.freedesktop.DBus.Properties", "Get",
                                                               GLib.Variant("(ss)", (tray.ITEM_INTERFACE, "IconName")), None,
                                                               Gio.DBusCallFlags.NONE, 3000, None).unpack()[0] == tray.ICON_LIVE, 3))

                # the panel's click sequence on Wayland: token first, then Activate; a click without one gives None
                bus.call_sync(icon.bus_name, tray.ITEM_PATH, tray.ITEM_INTERFACE, "ProvideXdgActivationToken",
                              GLib.Variant("(s)", ("tee-probe-token",)), None, Gio.DBusCallFlags.NONE, 3000, None)
                bus.call_sync(icon.bus_name, tray.ITEM_PATH, tray.ITEM_INTERFACE, "Activate", GLib.Variant("(ii)", (0, 0)),
                              None, Gio.DBusCallFlags.NONE, 3000, None)
                bus.call_sync(icon.bus_name, tray.ITEM_PATH, tray.ITEM_INTERFACE, "Activate", GLib.Variant("(ii)", (0, 0)),
                              None, Gio.DBusCallFlags.NONE, 3000, None)
                bus.call_sync(icon.bus_name, tray.MENU_PATH, tray.MENU_INTERFACE, "Event",
                              GLib.Variant("(isvu)", (tray.MENU_QUIT, "clicked", GLib.Variant("i", 0), 0)),
                              None, Gio.DBusCallFlags.NONE, 3000, None)
                self.assertTrue(wait_for(lambda: calls == ["show:tee-probe-token", "show:None", "quit"], 3), calls)

                GLib.idle_add(lambda: (icon.stop(), GLib.SOURCE_REMOVE)[1])
                self.assertTrue(wait_for(lambda: not icon.is_started, 3))
                self.assertTrue(wait_for(lambda: not ours(_watcher_items(bus)), 8), "the watcher still lists our item")
            except Exception as error:   # noqa: BLE001
                failures.append(repr(error))
            finally:
                GLib.idle_add(loop.quit)

        def begin():
            self.assertTrue(icon.start())
            self.assertEqual("org.kde.StatusNotifierItem-%d-1" % os.getpid(), icon.bus_name)
            threading.Thread(target=worker, name="tray-test", daemon=True).start()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(begin)
        GLib.timeout_add(25000, lambda: (failures.append("timeout"), loop.quit(), GLib.SOURCE_REMOVE)[2])
        loop.run()
        icon.stop()
        self.assertEqual([], failures)


@unittest.skipIf(not HAVE_WATCHER, "no org.kde.StatusNotifierWatcher on the session bus")
class TrayRestartTest(unittest.TestCase):
    def test_five_start_stop_cycles(self):
        # the bus name must be released and re-acquired every time, and the watcher must see us come and go
        icon = tray.TrayIcon(on_show=lambda: None, on_open_log=lambda: None, on_quit=lambda: None)
        loop = GLib.MainLoop()
        failures = []
        bus = _session_bus()
        seen_before = log.generation()

        def on_main(func):
            done = threading.Event()
            result = {}

            def run():
                try:
                    result["value"] = func()
                except Exception as error:   # noqa: BLE001
                    result["error"] = error
                done.set()
                return GLib.SOURCE_REMOVE
            GLib.idle_add(run)
            self.assertTrue(done.wait(5), "main loop did not run the step")
            if "error" in result:
                raise result["error"]
            return result["value"]

        def ours(items):
            unique = icon.unique_name
            return [item for item in items if item == icon.bus_name or (unique and item.startswith(unique + "@"))]

        def wait_for(predicate, timeout_s):
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if predicate():
                    return True
                time.sleep(0.05)
            return False

        def worker():
            try:
                for cycle in range(5):
                    self.assertTrue(on_main(icon.start), "cycle %d: start" % cycle)
                    self.assertTrue(on_main(icon.start), "start twice is harmless")
                    self.assertTrue(wait_for(lambda: icon.is_registered and ours(_watcher_items(bus)), 8), "cycle %d: not listed" % cycle)
                    on_main(icon.stop)
                    self.assertFalse(icon.is_started)
                    self.assertFalse(icon.is_registered)
                    self.assertTrue(wait_for(lambda: not ours(_watcher_items(bus)), 8), "cycle %d: still listed" % cycle)
                    on_main(icon.stop)   # stop twice is harmless too
            except Exception as error:   # noqa: BLE001
                failures.append(repr(error))
            finally:
                GLib.idle_add(loop.quit)

        GLib.idle_add(lambda: (threading.Thread(target=worker, name="tray-restart-test", daemon=True).start(), GLib.SOURCE_REMOVE)[1])
        GLib.timeout_add(60000, lambda: (failures.append("timeout"), loop.quit(), GLib.SOURCE_REMOVE)[2])
        loop.run()
        icon.stop()
        self.assertEqual([], failures)
        recent = log.get_recent()
        self.assertNotIn("Busname", recent, "the name must be free again after every stop")
        self.assertNotIn("Registrierung fehlgeschlagen", recent)
        self.assertGreaterEqual(log.generation(), seen_before)


class TrayWithoutWatcherTest(unittest.TestCase):
    def test_start_is_harmless_without_a_bus(self):
        saved = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/tee-cst-bus"
        try:
            icon = tray.TrayIcon(on_show=lambda: None, on_open_log=lambda: None, on_quit=lambda: None)
            self.assertFalse(icon.start())
            self.assertFalse(icon.is_started)
            icon.set_live(True)                      # must not raise without a connection
            icon.set_tooltip("Warte auf eine PS3 …")
            self.assertFalse(icon.is_registered)
            icon.stop()
            self.assertFalse(icon.is_registered)
            self.assertIsNone(icon.take_activation_token())
        finally:
            if saved is None:
                del os.environ["DBUS_SESSION_BUS_ADDRESS"]
            else:
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = saved


# ---------------------------------------------------------------------- structure and old settings files


class ArchitectureTest(unittest.TestCase):
    """The window polls the server; the server never reaches into the window. That is the only reason no
    GTK call can happen off the main thread, so it is checked here rather than left to good manners."""

    ENGINE = ("server", "live_streamer", "audio", "capture", "portal", "pad_receiver", "desktop_input",
              "virtual_gamepad", "display_mode", "power", "custom_commands", "netinfo", "childproc",
              "stream_sender", "encoders", "protocol", "clock", "log", "settings")

    def test_no_engine_module_touches_the_toolkit_or_the_window(self):
        for name in self.ENGINE:
            with open(os.path.join(PROJECT, "src", "teecellstream", name + ".py"), encoding="utf-8") as handle:
                source = handle.read()
            for forbidden in ("Gtk", "Adw", "Gdk", "from .ui", "from .app", "from .tray", "import ui", "import tray"):
                self.assertNotIn(forbidden, source, "%s.py must stay usable headless: found %r" % (name, forbidden))

    def test_the_window_and_the_tray_agree_on_the_status_line(self):
        # app.py writes the tray tooltip, ui.py the status card - one function, so they cannot drift
        self.assertEqual(ui.STATUS_STOPPED, ui.status_text(False, False, ""))
        self.assertEqual(ui.STATUS_STOPPED, ui.status_text(False, True, "10.42.0.151"))
        self.assertEqual(ui.STATUS_WAITING, ui.status_text(True, False, ""))
        self.assertEqual(ui.STATUS_CONNECTED + "10.42.0.151", ui.status_text(True, True, "10.42.0.151"))


class OldSettingsFileTest(unittest.TestCase):
    """A settings.json written before the bitrate and entropy knobs existed must keep working."""

    def test_missing_keys_fall_back_to_the_measured_defaults(self):
        path = os.path.join(_TEMP, "settings-1.0.0.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"encoder": "nvenc", "loss_recovery": "keyframe", "switch_display_mode": False,
                       "swap_mouse_sticks": True, "hide_notice_shown": True}, handle)
        old = settings_module.Settings(path)
        self.assertEqual(6000, old.get("video_kbps", protocol.KBPS))
        self.assertEqual("cavlc", old.get("entropy_coder", "cavlc"))
        self.assertIn(old.get("video_kbps", protocol.KBPS), protocol.BITRATE_CHOICES_KBPS)
        self.assertIn(old.get("entropy_coder", "cavlc"), protocol.ENTROPY_CODERS)
        # and everything the old file did say is still honoured
        self.assertEqual("nvenc", old.get("encoder", None))
        self.assertEqual("keyframe", old.get("loss_recovery", "intra"))
        self.assertFalse(old.get("switch_display_mode", True))
        self.assertTrue(old.get("swap_mouse_sticks", False))

    def test_a_junk_file_still_leaves_usable_defaults(self):
        path = os.path.join(_TEMP, "settings-junk.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ das ist kein JSON")
        junk = settings_module.Settings(path)
        self.assertEqual(6000, junk.get("video_kbps", protocol.KBPS))
        self.assertEqual("cavlc", junk.get("entropy_coder", "cavlc"))


if __name__ == "__main__":
    unittest.main()
