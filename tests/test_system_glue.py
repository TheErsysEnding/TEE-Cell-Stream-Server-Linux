"""System glue: netinfo, custom_commands, power, display_mode.

Safe to run unattended on a live GNOME desktop: the display is only read (GetCurrentState, and
ApplyMonitorsConfig in VERIFY mode, which validates without switching). The real switch+restore only runs
with TEE_CST_DISPLAY_TEST=1. The power test really inhibits and releases the session's idle timer.

Run: cd <project> && PYTHONPATH=src python3 -m unittest tests.test_system_glue -v
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tee-cst-glue-")
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_TMP, "settings.json"))
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_TMP, "server.log"))
# an agent/CI shell may lack these; without them Gio cannot find the user's session bus
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=%s/bus" % os.environ["XDG_RUNTIME_DIR"])

from gi.repository import Gio, GLib  # noqa: E402

from teecellstream import custom_commands, display_mode, log, netinfo, power, protocol  # noqa: E402
from teecellstream.settings import SETTINGS_PATH, Settings, settings  # noqa: E402


def _session_bus():
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        return None


def _name_has_owner(name: str) -> bool:
    bus = _session_bus()
    if bus is None:
        return False
    try:
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", (name,)), None, Gio.DBusCallFlags.NONE, 3000, None)
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False



def _server_is_streaming() -> bool:
    """True while the installed server is mid-stream: it switches the desktop and holds a session inhibitor,
    which is exactly what these two tests assert the absence of. Skip rather than report a false failure."""
    try:
        out = subprocess.run(["pgrep", "-f", "python3 -m teecellstream"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    try:
        with open(os.path.expanduser("~/.local/state/tee-cell-stream-server/server.log"), encoding="utf-8") as handle:
            tail = handle.readlines()[-400:]
    except OSError:
        return False
    for line in reversed(tail):
        if "Stream beendet" in line or "gestoppt:" in line:
            return False
        if "display: Desktop auf" in line or "live: erstes Frame" in line:
            return True
    return False


MUTTER_ON_BUS = _name_has_owner(display_mode.MUTTER_BUS_NAME)
SESSION_MANAGER_ON_BUS = _name_has_owner("org.gnome.SessionManager")
SCREENSAVER_ON_BUS = _name_has_owner("org.freedesktop.ScreenSaver")


# ------------------------------------------------------------------------------------------------ netinfo

# shaped like the output of: `ip -j -4 addr show up` (virbr0 is admin-up but has no carrier)
IP_SAMPLE = json.dumps([
    {"ifindex": 1, "ifname": "lo", "flags": ["LOOPBACK", "UP", "LOWER_UP"], "operstate": "UNKNOWN",
     "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host", "label": "lo"}]},
    {"ifindex": 2, "ifname": "enp4s0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "operstate": "UP",
     "addr_info": [{"family": "inet", "local": "10.42.0.1", "prefixlen": 24, "broadcast": "10.42.0.255",
                    "scope": "global", "noprefixroute": True, "label": "enp4s0"}]},
    {"ifindex": 3, "ifname": "enp5s0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "operstate": "UP",
     "addr_info": [{"family": "inet", "local": "192.168.1.50", "prefixlen": 24, "broadcast": "192.168.1.255",
                    "scope": "global", "dynamic": True, "label": "enp5s0"}]},
    {"ifindex": 4, "ifname": "virbr0", "flags": ["NO-CARRIER", "BROADCAST", "MULTICAST", "UP"], "operstate": "DOWN",
     "addr_info": [{"family": "inet", "local": "192.168.122.1", "prefixlen": 24, "broadcast": "192.168.122.255",
                    "scope": "global", "label": "virbr0"}]},
])


class NetinfoTests(unittest.TestCase):
    def test_captured_sample(self):
        targets = netinfo.parse_beacon_targets(IP_SAMPLE)
        self.assertEqual(targets, [("255.255.255.255", 38311), ("10.42.0.255", 38311), ("192.168.1.255", 38311)])

    def test_global_first_and_port(self):
        self.assertEqual(netinfo.parse_beacon_targets("[]"), [("255.255.255.255", protocol.BEACON_PORT)])
        self.assertEqual(netinfo.parse_beacon_targets(IP_SAMPLE, port=4711)[1], ("10.42.0.255", 4711))

    def test_dedup_derived_and_skipped_links(self):
        sample = json.dumps([
            # two addresses on one subnet -> one target; the second has no "broadcast" key -> derived from the prefix
            {"ifname": "eth0", "flags": ["BROADCAST", "UP", "LOWER_UP"], "operstate": "UP",
             "addr_info": [{"family": "inet", "local": "10.0.0.5", "prefixlen": 24, "broadcast": "10.0.0.255"},
                           {"family": "inet", "local": "10.0.0.6", "prefixlen": 24},
                           {"family": "inet6", "local": "fe80::1", "prefixlen": 64}]},
            # a bridge reports operstate UNKNOWN while working: included
            {"ifname": "br0", "flags": ["BROADCAST", "UP", "LOWER_UP"], "operstate": "UNKNOWN",
             "addr_info": [{"family": "inet", "local": "172.16.0.1", "prefixlen": 16}]},
            # a VPN tunnel has no broadcast address at all: skipped, not turned into a unicast
            {"ifname": "wg0", "flags": ["POINTOPOINT", "NOARP", "UP", "LOWER_UP"], "operstate": "UNKNOWN",
             "addr_info": [{"family": "inet", "local": "10.8.0.2", "prefixlen": 32}]},
            # admin-down never shows up in `show up`, but be safe
            {"ifname": "eth1", "flags": ["BROADCAST"], "operstate": "DOWN",
             "addr_info": [{"family": "inet", "local": "10.9.0.1", "prefixlen": 24, "broadcast": "10.9.0.255"}]},
            "garbage",
        ])
        self.assertEqual(netinfo.parse_beacon_targets(sample),
                         [("255.255.255.255", 38311), ("10.0.0.255", 38311), ("172.16.255.255", 38311)])

    def test_garbage_gives_only_global(self):
        for junk in ("", "not json", "{}", "[1, 2]", '[{"addr_info": [{"family": "inet", "broadcast": "x"}]}]'):
            self.assertEqual(netinfo.parse_beacon_targets(junk), [("255.255.255.255", 38311)], junk)

    def test_real_call_matches_ip(self):
        targets = netinfo.get_beacon_targets()
        self.assertEqual(targets[0], ("255.255.255.255", 38311))
        self.assertEqual(len(targets), len(set(targets)))
        for ip, port in targets:
            self.assertEqual(port, 38311)
            self.assertEqual(len(ip.split(".")), 4)
        try:
            output = subprocess.run(["ip", "-j", "-4", "addr", "show", "up"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest("ip nicht verfügbar")
        self.assertEqual(targets, netinfo.parse_beacon_targets(output))

    def test_ip_missing_falls_back_to_global(self):
        with mock.patch.object(netinfo.shutil, "which", return_value=None), \
             mock.patch.object(netinfo.subprocess, "run", side_effect=FileNotFoundError("ip")):
            self.assertEqual(netinfo.get_beacon_targets(), [("255.255.255.255", 38311)])


# ------------------------------------------------------------------------------------------------ custom commands

def _wait_for(predicate, timeout_s=5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class CustomCommandsTests(unittest.TestCase):
    def setUp(self):
        settings.set(custom_commands.SETTINGS_KEY, None)   # back to "never saved"
        custom_commands.reload()

    def _file_slots(self):
        with open(SETTINGS_PATH, encoding="utf-8") as handle:
            return json.load(handle)["custom_commands"]

    def test_first_run_seeds_big_picture(self):
        self.assertEqual(custom_commands.get(1), {"kind": "run", "value": "steam://open/bigpicture", "label": "Big Picture"})
        for slot in (2, 3, 4):
            self.assertEqual(custom_commands.get(slot), {"kind": "none", "value": "", "label": ""})
        self.assertIsNone(custom_commands.get(0))
        self.assertIsNone(custom_commands.get(5))
        self.assertEqual(self._file_slots()[0]["value"], "steam://open/bigpicture")   # seeded AND saved

    def test_roundtrip_through_settings_file(self):
        command = {"kind": "run", "value": "xdg-open https://example.org", "label": "Beispiel"}
        custom_commands.set(3, command)
        custom_commands.reload()
        self.assertEqual(custom_commands.get(3), command)
        self.assertEqual(self._file_slots()[2], command)
        fresh = Settings(SETTINGS_PATH)   # a second process would read exactly this
        self.assertEqual(fresh.get("custom_commands")[2], command)
        self.assertEqual(len(fresh.get("custom_commands")), custom_commands.SLOT_COUNT)
        custom_commands.set(3, {"kind": "none"})
        self.assertEqual(custom_commands.get(3), {"kind": "none", "value": "", "label": ""})
        custom_commands.set(9, command)   # ignored, like the original
        self.assertEqual(custom_commands.get(4)["kind"], "none")

    def test_normalises_odd_saved_shapes(self):
        settings.set(custom_commands.SETTINGS_KEY, [{"kind": "Run", "value": "true"}, "junk", {"kind": "guide", "value": 5}])
        custom_commands.reload()
        self.assertEqual(custom_commands.get(1), {"kind": "run", "value": "true", "label": ""})
        self.assertEqual(custom_commands.get(2), {"kind": "none", "value": "", "label": ""})
        self.assertEqual(custom_commands.get(3), {"kind": "none", "value": "", "label": ""})
        self.assertEqual(custom_commands.get(4), {"kind": "none", "value": "", "label": ""})
        self.assertEqual(len(self._file_slots()), 4)   # padded and written back in the canonical shape

    def test_move_up_into_empty_first_slot(self):
        second = {"kind": "run", "value": "echo two", "label": "Zwei"}
        settings.set(custom_commands.SETTINGS_KEY, [{"kind": "none", "value": "", "label": ""}, second,
                                                    {"kind": "none", "value": "", "label": ""}, {"kind": "none", "value": "", "label": ""}])
        custom_commands.reload()
        self.assertEqual(custom_commands.get(1), second)
        self.assertEqual(custom_commands.get(2)["kind"], "none")
        self.assertEqual(self._file_slots()[0], second)   # the move is remembered

    def test_no_move_when_first_slot_is_bound_or_second_empty(self):
        first = {"kind": "run", "value": "echo one", "label": ""}
        second = {"kind": "run", "value": "echo two", "label": ""}
        empty = {"kind": "none", "value": "", "label": ""}
        settings.set(custom_commands.SETTINGS_KEY, [first, second, empty, empty])
        custom_commands.reload()
        self.assertEqual(custom_commands.get(1), first)
        self.assertEqual(custom_commands.get(2), second)
        settings.set(custom_commands.SETTINGS_KEY, [empty, empty, second, empty])   # all-empty is "saved", not first run
        custom_commands.reload()
        self.assertEqual(custom_commands.get(1), empty)
        self.assertEqual(custom_commands.get(3), second)

    def test_uri_detection_and_command_line(self):
        for uri in ("steam://open/bigpicture", "https://example.org/x?y=1", "file:///tmp", "x-scheme.v2+a://host"):
            self.assertTrue(custom_commands.is_uri(uri), uri)
            self.assertEqual(custom_commands.command_line(uri), ["xdg-open", uri])
        for command in ("ls -la", "/usr/bin/foo --bar", "C:\\Games\\x.exe", "mailto:someone", "://nope", "1abc://x", "", "  "):
            self.assertFalse(custom_commands.is_uri(command), command)
        self.assertEqual(custom_commands.command_line("  echo hi  "), ["sh", "-c", "echo hi"])

    def test_run_shell_command_touches_file(self):
        marker = os.path.join(_TMP, "ran-%d.txt" % os.getpid())
        # the child writes its pid and session id: start_new_session must make them equal (detached from us)
        custom_commands.set(2, {"kind": "run", "value": "set -- $(cat /proc/$$/stat); echo $1 $6 > '%s'" % marker, "label": "Test"})
        custom_commands.run(2)
        self.assertTrue(_wait_for(lambda: os.path.exists(marker) and os.path.getsize(marker) > 0), "Befehl lief nicht")
        with open(marker, encoding="utf-8") as handle:
            pid, sid = handle.read().split()
        self.assertEqual(pid, sid, "Kindprozess ist nicht in einer eigenen Session")
        self.assertIn("custom 2: gestartet:", log.get_recent())

    def test_run_uri_goes_to_xdg_open_without_launching(self):
        custom_commands.set(1, {"kind": "run", "value": "steam://open/bigpicture", "label": "Big Picture"})
        with mock.patch.object(custom_commands, "_spawn") as spawn:
            custom_commands.run(1)
        spawn.assert_called_once_with(["xdg-open", "steam://open/bigpicture"])

    def test_run_unbound_and_bogus_slots_only_log(self):
        before = log.generation()
        custom_commands.run(4)
        custom_commands.run(0)
        custom_commands.run(42)
        custom_commands.set(3, {"kind": "run", "value": "   ", "label": "leer"})
        custom_commands.run(3)
        self.assertEqual(log.generation() - before, 4)
        self.assertIn("custom 42: an diesem Slot hängt nichts", log.get_recent())

    def test_run_failure_is_logged_not_raised(self):
        custom_commands.set(2, {"kind": "run", "value": "echo x", "label": ""})
        with mock.patch.object(custom_commands, "_spawn", side_effect=FileNotFoundError("sh")):
            custom_commands.run(2)
        self.assertIn("custom 2: konnte echo x nicht starten", log.get_recent())


# ------------------------------------------------------------------------------------------------ power

def _our_inhibitors() -> list[tuple[str, int]]:
    """(app_id, flags) of every SessionManager inhibitor that is ours."""
    bus = _session_bus()
    reply = bus.call_sync("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager",
                          "GetInhibitors", None, None, Gio.DBusCallFlags.NONE, 3000, None)
    found = []
    for path in reply.unpack()[0]:
        try:
            app_id = bus.call_sync("org.gnome.SessionManager", path, "org.gnome.SessionManager.Inhibitor", "GetAppId",
                                   None, None, Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
            if app_id != power.APP_ID:
                continue
            flags = bus.call_sync("org.gnome.SessionManager", path, "org.gnome.SessionManager.Inhibitor", "GetFlags",
                                  None, None, Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        except GLib.Error:
            continue   # an inhibitor that vanished between the listing and the query
        found.append((app_id, flags))
    return found


class PowerTests(unittest.TestCase):
    def tearDown(self):
        power.keep_display_awake(False)

    @unittest.skipUnless(SESSION_MANAGER_ON_BUS, "org.gnome.SessionManager nicht auf dem Session-Bus")
    @unittest.skipIf(_server_is_streaming(), "ein laufender Stream hält bereits eine Energiesperre")
    def test_inhibit_appears_and_disappears(self):
        log_before = log.get_recent()
        # We cannot tell our own leftover from a second copy of this code legitimately holding one right now
        # (the installed app mid-stream, or a parallel test run). Skipping is the honest verdict; failing here
        # was flaky by construction. The class-level skipIf is evaluated at import time and misses a stream
        # that starts later, so this has to be checked again inside the test.
        if _our_inhibitors():
            self.skipTest("ein anderer Prozess hält gerade eine Energiesperre unter unserem Namen")
        power.keep_display_awake(True)
        self.assertEqual(_our_inhibitors(), [(power.APP_ID, 12)])
        power.keep_display_awake(True)   # idempotent: still exactly one
        self.assertEqual(_our_inhibitors(), [(power.APP_ID, 12)])
        power.keep_display_awake(False)
        self.assertEqual(_our_inhibitors(), [])
        power.keep_display_awake(False)  # nothing held: silent no-op
        self.assertEqual(_our_inhibitors(), [])
        self.assertNotIn("power:", log.get_recent()[len(log_before):])   # success is silent

    @unittest.skipUnless(_session_bus() is not None and not SESSION_MANAGER_ON_BUS and SCREENSAVER_ON_BUS,
                         "nur ohne SessionManager relevant")
    def test_screensaver_fallback_does_not_raise(self):
        power.keep_display_awake(True)
        power.keep_display_awake(False)

    def test_failure_is_logged_once_then_silent(self):
        with mock.patch.object(power, "_call", side_effect=GLib.Error("GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: weg")):
            power.keep_display_awake(True)
            power.keep_display_awake(True)
            power.keep_display_awake(True)
        recent = log.get_recent()
        self.assertEqual(recent.count("power: kann den Bildschirm nicht wach halten"), 1)
        self.assertIn("weg", recent)
        self.assertNotIn("GDBus.Error", recent)
        power.keep_display_awake(False)   # nothing was held


# ------------------------------------------------------------------------------------------------ display mode

def _mode(mode_id, width, height, refresh, **properties):
    return (mode_id, width, height, refresh, 1.0, [1.0, 2.0], properties)


def _synthetic_state(serial=7):
    """Two monitors: a 2560x1440 primary at 0,0 and a 1920x1080 one to its right; plus one left and one below."""
    dp1_modes = [_mode("2560x1440@144.000", 2560, 1440, 144.0, **{"is-current": True}),
                 _mode("1280x720@60.000+vrr", 1280, 720, 60.0, **{"refresh-rate-mode": "variable"}),
                 _mode("1280x720@59.943", 1280, 720, 59.943),
                 _mode("1280x720@60.000", 1280, 720, 60.0),
                 _mode("1280x720@50.000", 1280, 720, 50.0),
                 _mode("1280x720@75.000", 1280, 720, 75.0)]
    hdmi_modes = [_mode("1920x1080@60.000", 1920, 1080, 60.0, **{"is-current": True}),
                  _mode("1280x720@60.000", 1280, 720, 60.0)]
    small_modes = [_mode("1024x768@60.000", 1024, 768, 60.0, **{"is-current": True})]
    return (serial,
            [(("DP-1", "ACM", "XZ1440", "1"), dp1_modes, {"is-builtin": False}),
             (("HDMI-1", "DEL", "U2412", "2"), hdmi_modes, {}),
             (("DP-3", "AAA", "left", "3"), small_modes, {}),
             (("DP-4", "AAA", "below", "4"), small_modes, {})],
            [(0, 0, 1.0, 0, True, [("DP-1", "ACM", "XZ1440", "1")], {}),
             (2560, 0, 1.0, 0, False, [("HDMI-1", "DEL", "U2412", "2")], {}),
             (-1024, 0, 1.0, 0, False, [("DP-3", "AAA", "left", "3")], {}),
             (0, 1440, 1.0, 0, False, [("DP-4", "AAA", "below", "4")], {})],
            {"layout-mode": 1, "supports-changing-layout-mode": True})


XRANDR_SAMPLE = (
    "Screen 0: minimum 8 x 8, current 4480 x 1440, maximum 32767 x 32767\n"
    "HDMI-0 connected 1920x1080+2560+0 (normal left inverted right x axis y axis) 527mm x 296mm\n"
    "   1920x1080     60.00*+  59.94    50.00  \n"
    "   1280x720      60.00    59.94    50.00  \n"
    "DP-0 disconnected (normal left inverted right x axis y axis)\n"
    "DP-2 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 597mm x 336mm\n"
    "   2560x1440     320.00*+ 300.00   240.00   165.00    59.95  \n"
    "   1920x1080     240.00   165.00   119.93   119.88   100.00    59.94    50.00  \n"
    "   1280x720      60.00    59.94    50.00  \n"
    "   1024x768i     60.00  \n"
)


class _FakeBackend:
    name = "fake"

    def __init__(self, width=2560, height=1440, switch_error=None, restore_error=None, modes=None):
        self.width, self.height = width, height
        self.switch_error, self.restore_error = switch_error, restore_error
        self.modes = modes or []
        self.calls = []

    def capture_modes(self):
        self.calls.append("capture_modes")
        return list(self.modes)

    def read(self):
        self.calls.append("read")
        return display_mode._Snapshot(self.width, self.height, "fake")

    def switch(self, snapshot, width, height, refresh_hz):
        self.calls.append(("switch", width, height, refresh_hz))
        if self.switch_error:
            raise display_mode.DisplayError(self.switch_error)
        return "fake-mode"

    def restore(self):
        self.calls.append("restore")
        if self.restore_error:
            raise display_mode.DisplayError(self.restore_error)
        return "%dx%d" % (self.width, self.height)


class DisplayModeParsingTests(unittest.TestCase):
    def test_parse_synthetic_state(self):
        state = display_mode.parse_current_state(_synthetic_state())
        self.assertEqual(state.serial, 7)
        self.assertEqual(state.layout_mode, 1)
        self.assertEqual([monitor.connector for monitor in state.monitors], ["DP-1", "HDMI-1", "DP-3", "DP-4"])
        primary = state.primary_logical_monitor()
        self.assertEqual(primary.connectors, ["DP-1"])
        self.assertTrue(primary.primary)
        monitor = state.find_monitor("DP-1")
        self.assertEqual(monitor.current_mode().id, "2560x1440@144.000")
        self.assertEqual(monitor.modes[1].is_variable_rate, True)
        self.assertEqual(monitor.modes[0].is_variable_rate, False)
        self.assertIsNone(state.find_monitor("nope"))

    def test_find_mode_prefers_exact_fixed_rate(self):
        modes = display_mode.parse_current_state(_synthetic_state()).find_monitor("DP-1").modes
        self.assertEqual(display_mode.find_mode(modes, 1280, 720, 60).id, "1280x720@60.000")
        self.assertEqual(display_mode.find_mode(modes, 1280, 720, 59.9).id, "1280x720@59.943")   # within 0.2 Hz, closest
        self.assertEqual(display_mode.find_mode(modes, 1280, 720, 70).id, "1280x720@75.000")    # no match: at or above, closest
        self.assertEqual(display_mode.find_mode(modes, 1280, 720, 90).id, "1280x720@75.000")    # nothing above: highest below
        self.assertEqual(display_mode.find_mode(modes, 1280, 720, 52).id, "1280x720@59.943")
        self.assertIsNone(display_mode.find_mode(modes, 640, 480, 60))
        vrr_only = [display_mode.Mode("1280x720@60.000+vrr", 1280, 720, 60.0, properties={"refresh-rate-mode": "variable"})]
        self.assertEqual(display_mode.find_mode(vrr_only, 1280, 720, 60).id, "1280x720@60.000+vrr")

    def test_rank_modes_orders_the_fallbacks(self):
        modes = display_mode.parse_current_state(_synthetic_state()).find_monitor("DP-1").modes
        ranked = [mode.id for mode in display_mode.rank_modes(modes, 1280, 720, 60)]
        self.assertEqual(ranked[:3], ["1280x720@60.000", "1280x720@59.943", "1280x720@60.000+vrr"])   # [1] is the retry
        self.assertEqual(ranked[-1], "1280x720@50.000")
        self.assertEqual(display_mode.rank_modes(modes, 640, 480, 60), [])
        self.assertEqual(display_mode.find_mode(modes, 1280, 720, 60), display_mode.rank_modes(modes, 1280, 720, 60)[0])

    def test_config_carries_monitor_properties(self):
        serial, monitors, logical, properties = _synthetic_state()
        monitors = list(monitors)
        monitors[0] = (monitors[0][0], monitors[0][1], {"is-underscanning": False, "color-mode": 0, "is-builtin": False})
        monitors[1] = (monitors[1][0], monitors[1][1], {"is-underscanning": True, "color-mode": 2, "rgb-range": 3,
                                                        "supported-color-modes": [0, 2], "display-name": "Dell"})
        state = display_mode.parse_current_state((serial, monitors, logical, properties))
        hdmi_expected = {"underscanning": GLib.Variant("b", True), "color-mode": GLib.Variant("u", 2), "rgb-range": GLib.Variant("u", 3)}
        current = display_mode.current_config(state)
        self.assertEqual(current[0][5][0][2], {"color-mode": GLib.Variant("u", 0)})   # underscanning off is left out
        self.assertEqual(current[1][5][0][:2], ("HDMI-1", "1920x1080@60.000"))
        self.assertEqual(current[1][5][0][2], hdmi_expected)
        self.assertEqual(current[2][5][0][2], {})                                      # nothing reported: nothing sent
        primary = state.primary_logical_monitor()
        config = display_mode.build_config(state, primary, display_mode.find_mode(state.find_monitor("DP-1").modes, 1280, 720, 60))
        self.assertEqual(config[0][5][0], ("DP-1", "1280x720@60.000", {"color-mode": GLib.Variant("u", 0)}))
        self.assertEqual(config[1][5][0][2], hdmi_expected)
        variant = GLib.Variant(display_mode.APPLY_CONFIG_TYPE, (state.serial, display_mode.METHOD_TEMPORARY, config, {}))
        self.assertEqual(variant.get_type_string(), display_mode.APPLY_CONFIG_TYPE)
        self.assertEqual(variant.unpack()[2][1][5][0][2], {"underscanning": True, "color-mode": 2, "rgb-range": 3})

    def test_build_config_shifts_monitors_right_and_below(self):
        state = display_mode.parse_current_state(_synthetic_state())
        primary = state.primary_logical_monitor()
        new_mode = display_mode.find_mode(state.find_monitor("DP-1").modes, 1280, 720, 60)
        config = display_mode.build_config(state, primary, new_mode)
        self.assertEqual(config, [
            (0, 0, 1.0, 0, True, [("DP-1", "1280x720@60.000", {})]),
            (1280, 0, 1.0, 0, False, [("HDMI-1", "1920x1080@60.000", {})]),       # right: follows the new right edge
            (-1024, 0, 1.0, 0, False, [("DP-3", "1024x768@60.000", {})]),         # left: untouched
            (0, 720, 1.0, 0, False, [("DP-4", "1024x768@60.000", {})]),           # below: follows the new bottom edge
        ])
        # the variant mutter wants must build from exactly this shape
        variant = GLib.Variant(display_mode.APPLY_CONFIG_TYPE, (state.serial, display_mode.METHOD_TEMPORARY, config, {}))
        self.assertEqual(variant.get_type_string(), display_mode.APPLY_CONFIG_TYPE)
        # and the original layout, as restore() re-applies it
        self.assertEqual(display_mode.current_config(state)[1], (2560, 0, 1.0, 0, False, [("HDMI-1", "1920x1080@60.000", {})]))

    def test_build_config_scaled_primary_uses_logical_width(self):
        serial, monitors, logical, properties = _synthetic_state()
        logical = list(logical)
        logical[0] = (0, 0, 2.0, 0, True, [("DP-1", "ACM", "XZ1440", "1")], {})     # 2560/2 x 1440/2 = 1280x720 logical px
        logical[1] = (1280, 0, 1.0, 0, False, [("HDMI-1", "DEL", "U2412", "2")], {})
        logical[3] = (0, 720, 1.0, 0, False, [("DP-4", "AAA", "below", "4")], {})
        state = display_mode.parse_current_state((serial, monitors, logical, properties))
        primary = state.primary_logical_monitor()
        new_mode = display_mode.find_mode(state.find_monitor("DP-1").modes, 1280, 720, 60)
        config = display_mode.build_config(state, primary, new_mode)
        self.assertEqual(config[0][2], 1.0)                     # streaming at scale 1: one desktop pixel = one video pixel
        self.assertEqual(config[1][0], 1280)                    # logical size is 1280x720 before and after, so nothing moves
        self.assertEqual(config[3][1], 720)
        # physical layout mode positions in device pixels: the same scale-2 primary is 2560 wide there
        logical[1] = (2560, 0, 1.0, 0, False, [("HDMI-1", "DEL", "U2412", "2")], {})
        state = display_mode.parse_current_state((serial, monitors, logical, {"layout-mode": 2}))
        config = display_mode.build_config(state, state.primary_logical_monitor(), new_mode)
        self.assertEqual(config[1][0], 1280)                    # 2560 - (2560 - 1280)

    def test_build_config_rotated_primary(self):
        serial, monitors, logical, properties = _synthetic_state()
        logical = list(logical)
        logical[0] = (0, 0, 1.0, 1, True, [("DP-1", "ACM", "XZ1440", "1")], {})     # 90 degrees: 1440 wide, 2560 tall
        logical[1] = (1440, 0, 1.0, 0, False, [("HDMI-1", "DEL", "U2412", "2")], {})
        state = display_mode.parse_current_state((serial, monitors, logical, properties))
        config = display_mode.build_config(state, state.primary_logical_monitor(),
                                           display_mode.find_mode(state.find_monitor("DP-1").modes, 1280, 720, 60))
        self.assertEqual(config[1][0], 720)                     # 1440 - (1440 - 720)

    def test_xrandr_parse_sample(self):
        sample = XRANDR_SAMPLE
        screen = display_mode.parse_xrandr(sample)
        self.assertEqual((screen.output, screen.width, screen.height, screen.rate), ("DP-2", 2560, 1440, 320.0))
        self.assertIn((1280, 720, 60.0, "60.00"), screen.modes)
        self.assertIn((1024, 768, 60.0, "60.00"), screen.modes)
        self.assertNotIn((1920, 1080, 60.0, "60.00"), screen.modes)   # HDMI-0's modes are not DP-2's
        self.assertIsNone(display_mode.parse_xrandr("Screen 0: minimum 8 x 8\nDP-0 disconnected\n"))
        # without a primary, the first connected output wins
        self.assertEqual(display_mode.parse_xrandr(sample.replace(" primary", "")).output, "HDMI-0")

    @unittest.skipUnless(os.environ.get("DISPLAY") and display_mode.shutil.which("xrandr"), "kein X11/xrandr")
    def test_xrandr_real_query_parses(self):
        try:
            output = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError) as error:
            self.skipTest("xrandr --query: %s" % error)
        screen = display_mode.parse_xrandr(output)
        if screen is None:
            self.skipTest("xrandr meldet keinen Ausgang")
        self.assertGreater(screen.width, 0)
        self.assertTrue(screen.modes)


class MutterBackendTests(unittest.TestCase):
    """The mutter backend over the synthetic 2560x1440 + 3 monitors state, GetCurrentState/ApplyMonitorsConfig faked."""

    def _backend_and_snapshot(self):
        state = display_mode.parse_current_state(_synthetic_state())
        backend = display_mode._MutterBackend()
        with mock.patch.object(display_mode, "read_current_state", return_value=state):
            snapshot = backend.read()
        self.assertEqual((snapshot.width, snapshot.height), (2560, 1440))
        return backend, snapshot

    def test_switch_applies_the_best_mode_temporarily_and_remembers_the_original(self):
        backend, snapshot = self._backend_and_snapshot()
        with mock.patch.object(display_mode, "apply_config") as apply:
            detail = backend.switch(snapshot, 1280, 720, 60)
        apply.assert_called_once()
        serial, config, method = apply.call_args[0][:3]   # (serial, config, method, bus, properties)
        self.assertEqual((serial, method), (7, display_mode.METHOD_TEMPORARY))
        self.assertEqual(config[0], (0, 0, 1.0, 0, True, [("DP-1", "1280x720@60.000", {})]))
        self.assertEqual(config[1][0], 1280)
        self.assertIn("DP-1 Modus 1280x720@60.000, Skalierung 1", detail)
        original = backend._original
        self.assertEqual((original.width, original.height, original.mode_id, original.scale, original.connector),
                         (2560, 1440, "2560x1440@144.000", 1.0, "DP-1"))
        self.assertEqual(original.config[1][0], 2560)

    def test_switch_retries_once_with_the_next_best_mode(self):
        backend, snapshot = self._backend_and_snapshot()
        applied = []

        def apply(serial, config, method, bus=None, properties=None):   # mirrors apply_config(serial, config, method, bus, properties)
            applied.append(config[0][5][0][1])
            if len(applied) == 1:
                raise display_mode.DisplayError("Invalid mode '1280x720@60.000' specified")

        with mock.patch.object(display_mode, "apply_config", side_effect=apply):
            detail = backend.switch(snapshot, 1280, 720, 60)
        self.assertEqual(applied, ["1280x720@60.000", "1280x720@59.943"])
        self.assertIn("1280x720@59.943", detail)
        self.assertIsNotNone(backend._original)

    def test_switch_refused_twice_reports_both_and_forgets(self):
        backend, snapshot = self._backend_and_snapshot()
        with mock.patch.object(display_mode, "apply_config", side_effect=display_mode.DisplayError("Logical monitors not adjacent")) as apply:
            with self.assertRaises(display_mode.DisplayError) as refused:
                backend.switch(snapshot, 1280, 720, 60)
        self.assertEqual(apply.call_count, 2)
        self.assertIn("1280x720@60.000: Logical monitors not adjacent; 1280x720@59.943: Logical monitors not adjacent", str(refused.exception))
        self.assertIsNone(backend._original)
        with self.assertRaises(display_mode.DisplayError):
            backend.restore()

    def test_switch_without_any_720p_mode(self):
        backend, snapshot = self._backend_and_snapshot()
        with mock.patch.object(display_mode, "apply_config") as apply:
            with self.assertRaises(display_mode.DisplayError) as refused:
                backend.switch(snapshot, 640, 480, 60)
        apply.assert_not_called()
        self.assertEqual(str(refused.exception), "kein Modus 640x480 auf DP-1")

    def test_restore_reads_a_fresh_serial_and_retries_once(self):
        backend, snapshot = self._backend_and_snapshot()
        with mock.patch.object(display_mode, "apply_config"):
            backend.switch(snapshot, 1280, 720, 60)
        calls = []

        def apply(serial, config, method, bus=None, properties=None):   # mirrors apply_config(serial, config, method, bus, properties)
            calls.append((serial, config[0][5][0][1], config[1][0], method))
            if len(calls) == 1:
                raise display_mode.DisplayError("stale")

        fresh = [display_mode.parse_current_state(_synthetic_state(serial=9)), display_mode.parse_current_state(_synthetic_state(serial=10))]
        with mock.patch.object(display_mode, "read_current_state", side_effect=fresh), \
             mock.patch.object(display_mode, "apply_config", side_effect=apply):
            detail = backend.restore()
        self.assertEqual(calls, [(9, "2560x1440@144.000", 2560, 1), (10, "2560x1440@144.000", 2560, 1)])
        self.assertEqual(detail, "2560x1440 (DP-1 Modus 2560x1440@144.000, Skalierung 1)")

    def test_xrandr_switch_retries_without_rate_and_restores(self):
        backend = display_mode._XrandrBackend()
        runs = []

        def run(arguments):
            runs.append(list(arguments))
            if arguments[:1] == ["--query"]:
                return XRANDR_SAMPLE
            if "--rate" in arguments and "1280x720" in arguments:
                raise display_mode.DisplayError("xrandr: Configure crtc 0 failed")
            return ""

        with mock.patch.object(backend, "_run", side_effect=run):
            snapshot = backend.read()
            self.assertEqual((snapshot.width, snapshot.height), (2560, 1440))
            detail = backend.switch(snapshot, 1280, 720, 60)
            self.assertEqual(runs[1], ["--output", "DP-2", "--mode", "1280x720", "--rate", "60.00"])
            self.assertEqual(runs[2], ["--output", "DP-2", "--mode", "1280x720"])
            self.assertEqual(detail, "DP-2 1280x720 (Rate von xrandr gewählt)")
            self.assertEqual(backend.restore(), "2560x1440 (DP-2)")
            self.assertEqual(runs[3], ["--output", "DP-2", "--mode", "2560x1440", "--rate", "320.00"])

    def test_xrandr_switch_refused_twice(self):
        backend = display_mode._XrandrBackend()
        with mock.patch.object(backend, "_run", side_effect=lambda arguments: XRANDR_SAMPLE if arguments[:1] == ["--query"]
                               else (_ for _ in ()).throw(display_mode.DisplayError("xrandr: nope"))):
            snapshot = backend.read()
            with self.assertRaises(display_mode.DisplayError) as refused:
                backend.switch(snapshot, 1280, 720, 60)
        self.assertEqual(str(refused.exception), "xrandr: nope; ohne Rate: xrandr: nope")
        self.assertIsNone(backend._original)


class DisplayModeSwitchLogicTests(unittest.TestCase):
    """The class's own behaviour (flags, logging, single restore) against a fake backend."""

    def test_already_matching_size_is_a_noop(self):
        backend = _FakeBackend(1280, 720)
        mode = display_mode.DisplayMode(backend=backend)
        self.assertTrue(mode.match_to(1280, 720, 60))
        self.assertFalse(mode.is_changed)
        self.assertEqual(backend.calls, ["read"])
        mode.restore()
        self.assertEqual(backend.calls, ["read"])   # nothing to restore

    def test_switch_then_single_restore(self):
        backend = _FakeBackend(2560, 1440)
        mode = display_mode.DisplayMode(backend=backend)
        self.assertTrue(mode.match_to(1280, 720, 60))
        self.assertTrue(mode.is_changed)
        self.assertTrue(mode.match_to(1280, 720, 60))   # a repeat PLAY: no second read, no second switch
        self.assertEqual(backend.calls, ["read", ("switch", 1280, 720, 60)])
        self.assertIn("display: Desktop auf 1280x720 umgeschaltet (war 2560x1440; fake-mode)", log.get_recent())
        mode.restore()
        mode.restore()
        self.assertFalse(mode.is_changed)
        self.assertEqual(backend.calls, ["read", ("switch", 1280, 720, 60), "restore"])
        self.assertIn("display: Desktop wieder auf 2560x1440", log.get_recent())

    def test_a_desktop_already_on_the_target_says_so_and_asks_nothing(self):
        """The old wording promised a switch even when there was nothing to switch, which read like a bug.
        And with nothing switched there is nothing to confirm either."""
        backend = _FakeBackend(2560, 1440, modes=[display_mode.Mode(id="1440", width=2560, height=1440,
                                                                    refresh=320.0)])
        mode = display_mode.DisplayMode(backend=backend)
        asked = []
        mode.set_confirm_prompt(lambda seconds: asked.append(seconds))
        switches_before = log.get_recent().count("umgeschaltet")   # das Log gehoert allen Tests gemeinsam
        self.assertTrue(mode.match_for_capture(1920, 1088, 60))
        self.assertFalse(mode.is_changed)
        self.assertEqual([], asked, "ohne Wechsel darf nicht gefragt werden")
        recent = log.get_recent()
        self.assertIn("steht schon auf 2560x1440", recent)
        self.assertEqual(switches_before, recent.count("umgeschaltet"), "es wurde nichts umgeschaltet")
        self.assertNotIn(("switch", 2560, 1440, 320.0), backend.calls)

    def test_the_same_target_is_not_announced_twice(self):
        """A repeated PLAY runs through here again; the log should not fill up with the same line."""
        backend = _FakeBackend(2560, 1440, modes=[display_mode.Mode(id="1440", width=2560, height=1440,
                                                                    refresh=320.0)])
        mode = display_mode.DisplayMode(backend=backend)
        mode.match_for_capture(1920, 1088, 60)
        before = log.get_recent().count("steht schon auf")
        mode.match_for_capture(1920, 1088, 60)
        self.assertEqual(before, log.get_recent().count("steht schon auf"))

    def test_an_unconfirmed_switch_reverts_by_itself(self):
        """The case this exists for: the monitor accepts the mode and shows nothing, so the user cannot
        answer at all. The countdown, not the button, is what brings the picture back."""
        backend = _FakeBackend(2560, 1440)
        mode = display_mode.DisplayMode(backend=backend)
        asked = []
        mode.set_confirm_prompt(lambda seconds: asked.append(seconds))
        with mock.patch.object(display_mode, "CONFIRM_SECONDS", 0.2):
            self.assertTrue(mode.match_to(1920, 1080, 120))
            mode.arm_confirmation()
            self.assertEqual([0.2], asked, "der Nutzer muss gefragt worden sein")
            time.sleep(0.6)
        self.assertFalse(mode.is_changed, "ohne Antwort muss die alte Auflösung zurück sein")
        self.assertEqual(backend.calls, ["read", ("switch", 1920, 1080, 120), "restore"])
        self.assertIn("keine Bestätigung", log.get_recent())

    def test_a_confirmed_switch_stays(self):
        backend = _FakeBackend(2560, 1440)
        mode = display_mode.DisplayMode(backend=backend)
        mode.set_confirm_prompt(lambda seconds: None)
        with mock.patch.object(display_mode, "CONFIRM_SECONDS", 0.2):
            mode.match_to(1920, 1080, 120)
            mode.arm_confirmation()
            mode.confirm_visible()
            time.sleep(0.5)
        self.assertTrue(mode.is_changed, "bestätigt heißt bleiben")
        self.assertEqual(backend.calls, ["read", ("switch", 1920, 1080, 120)])

    def test_saying_no_switches_back_at_once(self):
        backend = _FakeBackend(2560, 1440)
        mode = display_mode.DisplayMode(backend=backend)
        mode.set_confirm_prompt(lambda seconds: None)
        mode.match_to(1920, 1080, 120)
        mode.arm_confirmation()
        mode.reject_visible()
        self.assertFalse(mode.is_changed)
        self.assertEqual(backend.calls, ["read", ("switch", 1920, 1080, 120), "restore"])

    def test_without_a_window_nothing_is_armed(self):
        """Headless has nobody to ask, so it must behave exactly as it did before this existed."""
        backend = _FakeBackend(2560, 1440)
        mode = display_mode.DisplayMode(backend=backend)
        with mock.patch.object(display_mode, "CONFIRM_SECONDS", 0.2):
            mode.match_to(1920, 1080, 120)
            mode.arm_confirmation()
            time.sleep(0.5)
        self.assertTrue(mode.is_changed, "ohne Dialog darf nichts zurückgeschaltet werden")

    def test_a_dialog_that_will_not_open_keeps_the_mode(self):
        backend = _FakeBackend(2560, 1440)
        mode = display_mode.DisplayMode(backend=backend)
        def broken(_seconds):
            raise RuntimeError("kein Display")
        mode.set_confirm_prompt(broken)
        with mock.patch.object(display_mode, "CONFIRM_SECONDS", 0.2):
            mode.match_to(1920, 1080, 120)
            mode.arm_confirmation()
            time.sleep(0.5)
        self.assertTrue(mode.is_changed)
        self.assertIn("Rückfrage ging nicht auf", log.get_recent())

    def test_refusal_logs_and_streams_scaled(self):
        backend = _FakeBackend(2560, 1440, switch_error="Logical monitors not adjacent")
        mode = display_mode.DisplayMode(backend=backend)
        self.assertFalse(mode.match_to(1280, 720, 60))
        self.assertFalse(mode.is_changed)
        self.assertIn("display: 1280x720 wurde abgelehnt (Logical monitors not adjacent), streame stattdessen skaliert", log.get_recent())
        mode.restore()
        self.assertNotIn("restore", backend.calls)

    def test_unreadable_state_logs_and_streams_scaled(self):
        mode = display_mode.DisplayMode(backend=display_mode._NullBackend())
        self.assertFalse(mode.match_to(1280, 720, 60))
        self.assertIn("display: konnte die aktuelle Auflösung nicht lesen", log.get_recent())

    def test_restore_failure_clears_flag_and_logs(self):
        backend = _FakeBackend(2560, 1440, restore_error="stale")
        mode = display_mode.DisplayMode(backend=backend)
        self.assertTrue(mode.match_to(1280, 720, 60))
        mode.restore()
        self.assertFalse(mode.is_changed)   # cleared first, so nothing fights a second call
        self.assertIn("display: Wiederherstellung von 2560x1440 fehlgeschlagen (stale)", log.get_recent())


class DisplayModeRealTests(unittest.TestCase):
    """Read-only against the live compositor; the real switch needs TEE_CST_DISPLAY_TEST=1."""

    @unittest.skipUnless(MUTTER_ON_BUS, "org.gnome.Mutter.DisplayConfig nicht auf dem Session-Bus")
    @unittest.skipIf(_server_is_streaming(), "ein laufender Stream hält den Desktop auf der Stream-Auflösung")
    def test_real_state_primary_dp2_and_720p60_mode(self):
        state = display_mode.read_current_state()
        self.assertGreaterEqual(state.serial, 0)
        primary = state.primary_logical_monitor()
        self.assertEqual(primary.connectors[0], "DP-2")
        monitor = state.find_monitor("DP-2")
        self.assertEqual(monitor.current_mode().id, "2560x1440@320.001")
        self.assertEqual(monitor.current_mode().refresh, 320.00146484375)
        mode = display_mode.find_mode(monitor.modes, 1280, 720, 60)
        self.assertEqual(mode.id, "1280x720@60.000")
        self.assertFalse(mode.is_variable_rate)
        # the configs we would apply (with the carried-over monitor properties), checked by mutter in VERIFY mode:
        # it validates and switches nothing - the serial, which every real change bumps, proves that below
        display_mode.apply_config(state.serial, display_mode.current_config(state), display_mode.METHOD_VERIFY)
        display_mode.apply_config(state.serial, display_mode.build_config(state, primary, mode), display_mode.METHOD_VERIFY)
        if monitor.properties.get("is-underscanning") is None:
            # a monitor that cannot underscan must refuse the key: proves mutter reads the property name we send
            bad = []
            for x, y, scale, transform, is_primary, monitors in display_mode.current_config(state):
                monitors = [(connector, mode_id, dict(props, underscanning=GLib.Variant("b", True)) if connector == "DP-2" else props)
                            for connector, mode_id, props in monitors]
                bad.append((x, y, scale, transform, is_primary, monitors))
            with self.assertRaises(display_mode.DisplayError) as refused:
                display_mode.apply_config(state.serial, bad, display_mode.METHOD_VERIFY)
            self.assertIn("nderscanning", str(refused.exception))
        after = display_mode.read_current_state()
        self.assertEqual(after.serial, state.serial, "VERIFY hat die Anzeige verändert?!")
        self.assertEqual(after.find_monitor("DP-2").current_mode().id, "2560x1440@320.001")

    @unittest.skipUnless(MUTTER_ON_BUS, "org.gnome.Mutter.DisplayConfig nicht auf dem Session-Bus")
    def test_real_backend_selection_is_mutter(self):
        self.assertEqual(display_mode.select_backend().name, "mutter")
        self.assertEqual(display_mode.DisplayMode().backend_name, "mutter")

    def test_backend_selection_without_bus_or_display(self):
        # a plain X11 session: no Mutter on the bus, DISPLAY set, and NOT a Wayland session (the developer's
        # own session is Wayland, so that check must be neutralised too or xrandr is rightly refused)
        with mock.patch.object(display_mode, "mutter_available", return_value=False), \
             mock.patch.object(display_mode, "wayland_session", return_value=False), \
             mock.patch.dict(os.environ, {"DISPLAY": ":9"}), mock.patch.object(display_mode.shutil, "which", return_value="/usr/bin/xrandr"):
            self.assertEqual(display_mode.select_backend().name, "xrandr")
        with mock.patch.object(display_mode, "mutter_available", return_value=False), \
             mock.patch.dict(os.environ, {"DISPLAY": ""}):
            self.assertEqual(display_mode.select_backend().name, "none")

    @unittest.skipUnless(MUTTER_ON_BUS and os.environ.get("TEE_CST_DISPLAY_TEST") == "1", "TEE_CST_DISPLAY_TEST=1 nicht gesetzt")
    def test_real_switch_and_restore(self):
        before = display_mode.read_current_state()
        primary = before.primary_logical_monitor()
        original = before.find_monitor(primary.connectors[0]).current_mode().id
        mode = display_mode.DisplayMode()
        self.addCleanup(mode.restore)
        self.assertTrue(mode.match_to(1280, 720, 60))
        self.assertTrue(mode.is_changed)
        during = display_mode.read_current_state()
        during_primary = during.primary_logical_monitor()
        self.assertEqual(during_primary.scale, 1.0)
        self.assertEqual(during.find_monitor(during_primary.connectors[0]).current_mode().id, "1280x720@60.000")
        time.sleep(1.0)
        mode.restore()
        self.assertFalse(mode.is_changed)
        after = display_mode.read_current_state()
        after_primary = after.primary_logical_monitor()
        self.assertEqual(after.find_monitor(after_primary.connectors[0]).current_mode().id, original)
        self.assertEqual(after_primary.scale, primary.scale)
        self.assertEqual((after_primary.x, after_primary.y), (primary.x, primary.y))
        self.assertIn("display: Desktop wieder auf 2560x1440", log.get_recent())


if __name__ == "__main__":
    unittest.main()


class ChooseCaptureModeTests(unittest.TestCase):
    """Which desktop mode to stream from. GNOME's ScreenCast gives out about two thirds of the refresh rate,
    so matching the stream's own 60 Hz - what the Windows original does - costs a third of the frames."""

    @staticmethod
    def _mode(width, height, refresh, variable=False):
        return display_mode.Mode("%dx%d@%g" % (width, height, refresh), width, height, refresh, 1.0, [1.0],
                                 {"refresh-rate-mode": "variable"} if variable else {})

    def _developer_monitor(self):
        # the real monitor this was measured on: 720p caps at 60 Hz, 1080p reaches 240, 1440p reaches 320
        return [self._mode(1280, 720, 60), self._mode(1280, 720, 50),
                self._mode(1920, 1080, 240), self._mode(1920, 1080, 60),
                self._mode(2560, 1440, 320.001), self._mode(2560, 1440, 60),
                self._mode(1024, 768, 75)]

    def test_smallest_mode_with_enough_refresh_wins(self):
        # 1080p@240 over 1440p@320: both leave room, and 1080p is 8.3 MB a frame against 14.7 MB
        self.assertEqual(display_mode.choose_capture_mode(self._developer_monitor(), 1280, 720, 60),
                         (1920, 1080, 240.0))

    def test_never_trades_refresh_for_a_smaller_mode(self):
        # 1280x720 is the perfect size and is NOT chosen: at 60 Hz the compositor would hand out ~40 fps
        chosen = display_mode.choose_capture_mode(self._developer_monitor(), 1280, 720, 60)
        self.assertNotEqual((chosen[0], chosen[1]), (1280, 720))
        self.assertGreaterEqual(chosen[2], 60 * display_mode.CAPTURE_REFRESH_FACTOR)


    def test_an_ordinary_desktop_size_is_preferred(self):
        """An exotic mode may not exist on someone else's screen, or may exist and show nothing. The
        stream is scaled from an ordinary size instead - which costs sharpness, never the picture."""
        modes = [display_mode.Mode(id="%dx%d" % (w, h), width=w, height=h, refresh=float(r))
                 for w, h, r in ((1600, 900, 144), (1920, 1080, 240), (2560, 1440, 320))]
        self.assertEqual((1920, 1080, 240.0), display_mode.choose_capture_mode(modes, 1536, 864, 60),
                         "1600x900 waere kleiner, ist aber keine Standardgroesse")

    def test_an_ordinary_size_that_is_too_slow_is_skipped(self):
        """Measured on the development screen: 1280x720 tops out at 60 Hz there, and a 60 Hz desktop hands
        the screen cast only about two thirds of the frames. Scaling beats losing a third of them."""
        modes = [display_mode.Mode(id="720", width=1280, height=720, refresh=60.0),
                 display_mode.Mode(id="1080", width=1920, height=1080, refresh=240.0)]
        self.assertEqual((1920, 1080, 240.0), display_mode.choose_capture_mode(modes, 1280, 720, 60))

    def test_a_stream_taller_than_every_ordinary_size_still_gets_one(self):
        """1920x1088 fits inside no ordinary size; the largest this screen can do quickly is right."""
        modes = [display_mode.Mode(id="1080", width=1920, height=1080, refresh=240.0),
                 display_mode.Mode(id="1440", width=2560, height=1440, refresh=320.0)]
        self.assertEqual((2560, 1440, 320.0), display_mode.choose_capture_mode(modes, 1920, 1088, 60))

    def test_plain_60hz_monitor_falls_back_to_the_originals_behaviour(self):
        modes = [self._mode(1280, 720, 60), self._mode(1920, 1080, 60), self._mode(2560, 1440, 60)]
        self.assertEqual(display_mode.choose_capture_mode(modes, 1280, 720, 60), (1280, 720, 60.0))

    def test_no_modes_at_all_falls_back(self):
        self.assertEqual(display_mode.choose_capture_mode([], 1280, 720, 60), (1280, 720, 60.0))

    def test_never_upscales(self):
        """A mode smaller than the stream would mean scaling UP - never worth it, whatever its refresh."""
        modes = [self._mode(1024, 768, 240), self._mode(1920, 1080, 144)]
        self.assertEqual(display_mode.choose_capture_mode(modes, 1280, 720, 60), (1920, 1080, 144.0))

    def test_variable_rate_twins_are_skipped(self):
        """A '+vrr' mode's rate is not a promise, so it must not be what we count on."""
        modes = [self._mode(1920, 1080, 240, variable=True), self._mode(2560, 1440, 320)]
        self.assertEqual(display_mode.choose_capture_mode(modes, 1280, 720, 60), (2560, 1440, 320.0))

    def test_higher_frame_rates_demand_more_refresh(self):
        modes = [self._mode(1920, 1080, 100), self._mode(2560, 1440, 200)]
        self.assertEqual(display_mode.choose_capture_mode(modes, 1280, 720, 60), (1920, 1080, 100.0))
        self.assertEqual(display_mode.choose_capture_mode(modes, 1280, 720, 120), (2560, 1440, 200.0))

    def test_real_monitor_picks_something_sane(self):
        backend = display_mode.select_backend()
        modes = backend.capture_modes()
        if not modes:
            self.skipTest("dieses Backend zählt keine Modi auf")
        width, height, refresh = display_mode.choose_capture_mode(modes, 1280, 720, 60)
        self.assertGreaterEqual(width, 1280)
        self.assertGreaterEqual(height, 720)
        self.assertTrue(any(m.width == width and m.height == height and abs(m.refresh - refresh) < 0.01 for m in modes))

