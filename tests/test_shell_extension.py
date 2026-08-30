"""shell_extension: switching on the bundled GNOME extension without a terminal and without sudo.

Run: cd <project> && PYTHONPATH=src python3 -m unittest tests.test_shell_extension -v
"""

import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tee-cst-ext-test-")
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_TMP, "settings.json"))
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_TMP, "server.log"))
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=%s/bus" % os.environ["XDG_RUNTIME_DIR"])

from gi.repository import GLib  # noqa: E402

from teecellstream import log, shell_extension  # noqa: E402


def _info(**fields) -> GLib.Variant:
    """What GetExtensionInfo returns: (a{sv}) — empty when the shell has never heard of the uuid."""
    return GLib.Variant("(a{sv})", ({k: GLib.Variant("b", v) if isinstance(v, bool) else GLib.Variant("d", v)
                                     for k, v in fields.items()},))


class EnsureEnabledTests(unittest.TestCase):
    """Every branch, with the bus faked - the real one is exercised by the live test at the bottom."""

    def setUp(self):
        self._calls = []

    def _run(self, replies):
        """replies: one entry per _call, each either a GLib.Variant or an Exception to raise."""
        def fake_call(_bus, method, _argument, _reply_type):
            self._calls.append(method)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with mock.patch.object(shell_extension.Gio, "bus_get_sync", return_value=object()), \
             mock.patch.object(shell_extension, "_call", fake_call):
            return shell_extension.ensure_enabled()

    def test_already_on_changes_nothing(self):
        state = self._run([_info(enabled=True, state=float(shell_extension.STATE_ENABLED))])
        self.assertEqual(state, shell_extension.ALREADY)
        self.assertEqual(self._calls, ["GetExtensionInfo"], "es darf nichts geschaltet werden")

    def test_installed_but_off_gets_switched_on(self):
        state = self._run([_info(enabled=False, state=2.0), GLib.Variant("(b)", (True,))])
        self.assertEqual(state, shell_extension.ENABLED)
        self.assertEqual(self._calls, ["GetExtensionInfo", "EnableExtension"])
        self.assertIn("extension enabled", log.get_recent())

    def test_enabled_flag_without_active_state_is_still_switched_on(self):
        """'enabled' in gsettings but state != ENABLED means it did not load - ask for it again."""
        state = self._run([_info(enabled=True, state=3.0), GLib.Variant("(b)", (True,))])
        self.assertEqual(state, shell_extension.ENABLED)

    def test_unknown_to_the_shell_asks_for_a_logout(self):
        """The package installs it as root, but GNOME reads new extensions only when a session starts."""
        state = self._run([GLib.Variant("(a{sv})", ({},))])
        self.assertEqual(state, shell_extension.NEEDS_LOGOUT)
        recent = log.get_recent()
        self.assertIn("log out and in once", recent)
        self.assertIn("borderless window", recent, "the workaround for now has to be in there")

    def test_no_gnome_shell_is_not_an_error(self):
        state = self._run([GLib.Error.new_literal(GLib.quark_from_string("g-dbus-error-quark"),
                                                  "name org.gnome.Shell not provided", 2)])
        self.assertEqual(state, shell_extension.UNAVAILABLE)

    def test_shell_refuses(self):
        state = self._run([_info(enabled=False, state=2.0), GLib.Variant("(b)", (False,))])
        self.assertEqual(state, shell_extension.FAILED)
        self.assertIn("refused to enable", log.get_recent())

    def test_enable_call_itself_fails(self):
        state = self._run([_info(enabled=False, state=2.0),
                           GLib.Error.new_literal(GLib.quark_from_string("g-dbus-error-quark"), "kaputt", 2)])
        self.assertEqual(state, shell_extension.FAILED)

    def test_no_session_bus(self):
        with mock.patch.object(shell_extension.Gio, "bus_get_sync",
                               side_effect=GLib.Error.new_literal(GLib.quark_from_string("g-io-error-quark"),
                                                                  "kein Bus", 2)):
            self.assertEqual(shell_extension.ensure_enabled(), shell_extension.UNAVAILABLE)

    def test_never_raises(self):
        """The server calls this on a start-up thread; whatever DBus does, it must come back with a state."""
        for reply in (GLib.Error.new_literal(GLib.quark_from_string("q"), "x", 1),
                      GLib.Variant("(a{sv})", ({},)),
                      _info(enabled=False, state=2.0)):
            try:
                self._run([reply, GLib.Variant("(b)", (True,))])
            except Exception as error:   # noqa: BLE001
                self.fail("ensure_enabled() hat geworfen: %r" % error)


class LiveShellTests(unittest.TestCase):
    """Against the real GNOME Shell, if one is there. Leaves the extension exactly as it found it."""

    @staticmethod
    def _state():
        try:
            from gi.repository import Gio
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = shell_extension._call(bus, "GetExtensionInfo",
                                          GLib.Variant("(s)", (shell_extension.UUID,)), "(a{sv})")
            return reply.unpack()[0]
        except Exception:   # noqa: BLE001
            return None

    def test_real_call_reports_a_known_state(self):
        state = shell_extension.ensure_enabled()
        self.assertIn(state, (shell_extension.ENABLED, shell_extension.ALREADY, shell_extension.NEEDS_LOGOUT,
                              shell_extension.UNAVAILABLE, shell_extension.FAILED))

    def test_switches_a_disabled_extension_back_on(self):
        info = self._state()
        if not info:
            self.skipTest("GNOME Shell kennt die Erweiterung hier nicht")
        was_on = bool(info.get("enabled"))
        if not was_on:
            self.skipTest("die Erweiterung ist aus - dieser Test würde den Zustand des Nutzers ändern")

        from gi.repository import Gio
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        shell_extension._call(bus, "DisableExtension", GLib.Variant("(s)", (shell_extension.UUID,)), "(b)")
        try:
            self.assertEqual(shell_extension.ensure_enabled(), shell_extension.ENABLED)
            self.assertTrue(self._state().get("enabled"), "sie muss danach wieder an sein")
        finally:
            if was_on and not (self._state() or {}).get("enabled"):
                shell_extension._call(bus, "EnableExtension", GLib.Variant("(s)", (shell_extension.UUID,)), "(b)")
