"""The window: a view onto the server, nothing more (port of MainWindow.xaml.cs).

Closing it only hides it - the server keeps running, which is the point. Beenden (tray or menu) is the only
thing that stops it. The server is duck-typed: anything with the properties Server exposes will do (tests use a
mock), and the window never blocks on it - it polls every 500 ms like the original DispatcherTimer.
"""

import os
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_EXEC, APP_NAME, UPSTREAM_VERSION, __version__, autostart, custom_commands, log, protocol  # noqa: E402
from .settings import settings  # noqa: E402

REFRESH_TICK_MS = 500
SLOT_SAVE_DELAY_MS = 400       # typing in a Befehle field: save once the fingers pause, not per keystroke
WINDOW_WIDTH = 640
WINDOW_HEIGHT = 600
NARROW_BREAKPOINT = "max-width: 500sp"

BITRATE_LABELS = tuple("%d Mbit/s%s" % (k // 1000, " (empfohlen)" if k == 12000 else "")
                       for k in protocol.BITRATE_CHOICES_KBPS)
ENTROPY_LABELS = ("CAVLC – die PS3 decodiert deutlich schneller", "CABAC – etwas schärfer, teurer für die PS3")
SIZE_LABELS = ("1280 × 720 – gemessen: 22 ms Decode auf der PS3 (empfohlen)",
               "1408 × 800 – etwas schärfer, rund 1,2× Decodelast",
               "1536 × 864 – deutlich schärfer, rund 1,4× Decodelast",
               "1792 × 1008 – am schärfsten, rund 2× Decodelast",
               "1920 × 1088 – volles Full HD, rund 2,3× Decodelast – gemessen: 38–44 ms mit x264")

RATE_LABELS = ("Variabel – Bitrate nur, wenn sich etwas bewegt",
               "Konstante Qualität (Standard) – gemessen die niedrigste Latenz, Text bleibt scharf",
               "Konstante Bitrate – hält die Rate, füllt notfalls mit Leerdaten auf")

LOSS_RECOVERY_KINDS = ("intra", "keyframe")
LOSS_RECOVERY_LABELS = ("Intra-Refresh (Standard)", "Keyframes – falls NVENC Artefakte zeigt")
COMMAND_KINDS = ("none", "run")
COMMAND_KIND_LABELS = ("Keine", "Befehl oder URI ausführen")

STATUS_STOPPED = "Gestoppt"
STATUS_WAITING = "Warte auf eine PS3 …"
STATUS_CONNECTED = "PS3 verbunden: "
HIDE_HINT = ("Schließen des Fensters lässt den Server im Hintergrund weiterlaufen. "
             "Beenden über das Tray-Symbol oder das Menü.")
COMMANDS_INTRO = ("Die PS3 löst mit SELECT + Dreieck / Kreis / L1 / R1 die Befehle 1 bis 4 aus. Sie schickt nur die "
                  "Nummer – was dann passiert, legst du hier fest: eine URI wie steam://open/bigpicture (xdg-open) "
                  "oder eine Befehlszeile (sh -c). Ein Gerät im Netz kann also nie etwas starten, das du nicht "
                  "hier eingetragen hast.")

CSS = """
.status-card { padding: 18px; }
.status-dot { min-width: 12px; min-height: 12px; border-radius: 6px; margin: 2px; }
.status-dot-idle { background-color: #8a8a8a; }
.status-dot-live { background-color: #3dd56d; box-shadow: 0 0 6px alpha(#3dd56d, 0.7); }
.log-pane { border-top: 1px solid alpha(currentColor, 0.15); }
textview.log-view, textview.log-view > text {
  font-family: monospace; font-size: 12px; background-color: #1d1d20; color: #d6d6da;
}
"""

_css_installed = False


def dev_icon_dir() -> str:
    """data/icons of a source checkout (so the window and About get the icon before anything is installed)."""
    candidate = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "icons")
    return candidate if os.path.isdir(candidate) else ""


def install_style() -> None:
    """CSS for the whole display; Adw.StyleManager keeps following the system's light/dark choice."""
    global _css_installed
    display = Gdk.Display.get_default()
    if _css_installed or display is None:
        return
    _css_installed = True
    provider = Gtk.CssProvider()
    provider.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    icon_dir = dev_icon_dir()
    if icon_dir:
        Gtk.IconTheme.get_for_display(display).add_search_path(icon_dir)
    Gtk.Window.set_default_icon_name(APP_EXEC)


def open_log(parent: Gtk.Window | None = None) -> None:
    """Hands the log file to whatever the desktop opens .log files with."""
    if not os.path.exists(log.LOG_PATH):
        log.write("Log geöffnet")   # creates the file, so there is something to show
    try:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(log.LOG_PATH))
        launcher.launch(parent, None, _on_log_launched)
    except (GLib.Error, AttributeError) as error:
        _open_log_fallback(str(error))


def _on_log_launched(launcher, result) -> None:
    try:
        launcher.launch_finish(result)
    except GLib.Error as error:
        _open_log_fallback(error.message)


def _open_log_fallback(reason: str) -> None:
    try:
        subprocess.Popen(["xdg-open", log.LOG_PATH], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        log.write("konnte das Log nicht öffnen: %s / %s" % (reason, error))


def status_text(armed: bool, connected: bool, who: str) -> str:
    """The one line the window's status card and the tray's tooltip both show - defined once so they cannot drift."""
    if not armed:
        return STATUS_STOPPED
    return STATUS_CONNECTED + who if connected else STATUS_WAITING


class MainWindow(Adw.ApplicationWindow):
    """MainWindow(app, server). Public widgets are for the app and the tests; everything else is private."""

    def __init__(self, app: Gtk.Application, server):
        super().__init__(application=app, title=APP_NAME, default_width=WINDOW_WIDTH, default_height=WINDOW_HEIGHT,
                         icon_name=APP_EXEC)
        self._app = app
        self._server = server
        self._syncing = False               # True while widgets are being set from the server, so handlers stay quiet
        self._shown_log_generation = -1
        self._encoder_kinds: list[str] = []
        self._slot_save_timers: dict[int, int] = {}
        install_style()

        self._build()
        self._install_actions()
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)
        self._timer_id = GLib.timeout_add(REFRESH_TICK_MS, self._on_tick)
        # a mode switch asks the user whether they can still see anything; without a window to ask in
        # (headless), display_mode simply does not arm the countdown and behaves as it always did
        display = getattr(self._server, "display_mode", None)
        if display is not None and hasattr(display, "set_confirm_prompt"):
            display.set_confirm_prompt(self._prompt_display_confirm)
        self.refresh()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        self.view_stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.view_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self._header = Adw.HeaderBar(title_widget=switcher)
        self._window_title = Adw.WindowTitle(title=APP_NAME)
        self._switcher = switcher
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", primary=True, tooltip_text="Hauptmenü",
                                     menu_model=self._build_menu())
        self._header.pack_end(menu_button)
        toolbar.add_top_bar(self._header)

        self.view_stack.add_titled_with_icon(self._build_server_page(), "server", "Server", "video-display-symbolic")
        self.view_stack.add_titled_with_icon(self._build_commands_page(), "commands", "Befehle", "utilities-terminal-symbolic")
        toolbar.set_content(self.view_stack)

        # narrow window: the switcher moves from the header to a bar along the bottom
        self._switcher_bar = Adw.ViewSwitcherBar(stack=self.view_stack)
        toolbar.add_bottom_bar(self._switcher_bar)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(NARROW_BREAKPOINT))
        breakpoint.connect("apply", self._on_narrow)
        breakpoint.connect("unapply", self._on_wide)
        self.add_breakpoint(breakpoint)

    def _on_narrow(self, _breakpoint) -> None:
        self._header.set_title_widget(self._window_title)
        self._switcher_bar.set_reveal(True)

    def _on_wide(self, _breakpoint) -> None:
        self._header.set_title_widget(self._switcher)
        self._switcher_bar.set_reveal(False)

    def _build_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Log öffnen", "win.open-log")
        section.append("Autostart", "win.autostart")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Über " + APP_NAME, "win.about")
        section.append("Beenden", "app.quit")
        menu.append_section(None, section)
        return menu

    def _build_server_page(self) -> Gtk.Widget:
        # settings above, the running commentary below; the handle between them lets the log grow
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, shrink_start_child=False, shrink_end_child=False,
                          resize_start_child=True, resize_end_child=False)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=560,
                          margin_top=24, margin_bottom=24, margin_start=18, margin_end=18)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(column)
        scroller.set_child(clamp)

        column.append(self._build_status_card())
        column.append(self._build_video_group())
        column.append(self._build_input_group())
        column.append(self._build_system_group())

        paned.set_start_child(scroller)
        paned.set_end_child(self._build_log_pane())
        return paned

    def _build_status_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, css_classes=["card", "status-card"])
        self.status_dot = Gtk.Box(css_classes=["status-dot", "status-dot-idle"], valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        card.append(self.status_dot)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        self.status_label = Gtk.Label(label=STATUS_WAITING, xalign=0, wrap=True, css_classes=["title-2"])
        self.subtitle_label = Gtk.Label(label="", xalign=0, wrap=True, css_classes=["dim-label"])
        texts.append(self.status_label)
        texts.append(self.subtitle_label)
        card.append(texts)

        self.start_stop_button = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER, css_classes=["pill", "destructive-action"])
        self.start_stop_button.connect("clicked", self._on_start_stop_clicked)
        card.append(self.start_stop_button)
        return card

    def _build_video_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Video")
        self.encoder_model = Gtk.StringList()
        self.encoder_row = Adw.ComboRow(title="Encoder", subtitle="Gesperrt, solange eine PS3 streamt", model=self.encoder_model)
        self.encoder_row.connect("notify::selected", self._on_encoder_selected)
        group.add(self.encoder_row)

        self.recovery_row = Adw.ComboRow(title="Fehlerkorrektur", subtitle="Wie der Stream nach Paketverlust wieder ein sauberes Bild bekommt",
                                         model=Gtk.StringList.new(list(LOSS_RECOVERY_LABELS)))
        self.recovery_row.connect("notify::selected", self._on_recovery_selected)
        group.add(self.recovery_row)

        # The PS3's decoder, not the network, is the wall: measured 38-40 ms per frame at 11-13 Mbit/s CABAC
        # against the 16.7 ms a 60 fps frame gets, so the console dropped every other one. These two rows are
        # what buy that time back.
        self.size_row = Adw.ComboRow(title="Auflösung",
                                    subtitle="Größer heißt lesbarer Text, kostet die PS3 aber ungefähr proportional mehr Decodezeit",
                                    model=Gtk.StringList.new(list(SIZE_LABELS)))
        self.size_row.connect("notify::selected", self._on_size_selected)
        group.add(self.size_row)

        self.bitrate_row = Adw.ComboRow(title="Bitrate",
                                        subtitle="Niedriger, wenn das Bild auf der PS3 ruckelt – höher nur, solange es flüssig bleibt",
                                        model=Gtk.StringList.new(list(BITRATE_LABELS)))
        self.bitrate_row.connect("notify::selected", self._on_bitrate_selected)
        group.add(self.bitrate_row)

        self.coder_row = Adw.ComboRow(title="Entropie-Codierung",
                                      subtitle="Die PS3 decodiert CAVLC rund 43 % schneller – CABAC nur, solange das Bild flüssig bleibt",
                                      model=Gtk.StringList.new(list(ENTROPY_LABELS)))
        self.coder_row.connect("notify::selected", self._on_coder_selected)
        group.add(self.coder_row)

        self.rate_row = Adw.ComboRow(title="Ratensteuerung",
                                     subtitle="Wofür der Encoder seine Bitrate ausgibt – nur der x264-Encoder kann alle drei",
                                     model=Gtk.StringList.new(list(RATE_LABELS)))
        self.rate_row.connect("notify::selected", self._on_rate_selected)
        group.add(self.rate_row)

        self.display_row = Adw.SwitchRow(title="Desktop während des Streams an die Streamgröße anpassen",
                                         subtitle="Nach dem Stream wird die alte Auflösung wiederhergestellt")
        self.display_row.connect("notify::active", self._on_display_toggled)
        group.add(self.display_row)
        return group

    def _build_input_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Eingabe")
        self.swap_sticks_row = Adw.SwitchRow(title="Sticks im Maus-Modus tauschen", subtitle="Rechter Stick bewegt den Zeiger")
        self.swap_sticks_row.connect("notify::active", self._on_swap_sticks_toggled)
        group.add(self.swap_sticks_row)
        return group

    def _build_system_group(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        group = Adw.PreferencesGroup(title="System")
        self.autostart_row = Adw.SwitchRow(title="Beim Anmelden starten (minimiert)", subtitle="Legt einen Autostart-Eintrag an")
        self.autostart_row.connect("notify::active", self._on_autostart_row_toggled)
        group.add(self.autostart_row)
        box.append(group)
        hint = Gtk.Label(label=HIDE_HINT, xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        box.append(hint)
        return box

    def _build_log_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["log-pane"])
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_start=18, margin_end=10, margin_top=6, margin_bottom=4)
        header.append(Gtk.Label(label="Protokoll", xalign=0, hexpand=True, css_classes=["heading"]))
        open_button = Gtk.Button(label="Log öffnen", css_classes=["flat"], action_name="win.open-log", tooltip_text="Strg+L")
        header.append(open_button)
        pane.append(header)

        scroller = Gtk.ScrolledWindow(min_content_height=150, vexpand=True)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, css_classes=["log-view"],
                                     left_margin=10, right_margin=10, top_margin=6, bottom_margin=6)
        self._log_end_mark = self.log_view.get_buffer().create_mark(None, self.log_view.get_buffer().get_end_iter(), False)
        scroller.set_child(self.log_view)
        pane.append(scroller)
        return pane

    def _build_commands_page(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=560,
                          margin_top=24, margin_bottom=24, margin_start=18, margin_end=18)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(column)
        scroller.set_child(clamp)
        column.append(Gtk.Label(label=COMMANDS_INTRO, xalign=0, wrap=True, css_classes=["dim-label"]))

        self.slot_rows: list[tuple[Adw.ComboRow, Adw.EntryRow, Adw.EntryRow]] = []
        for slot in range(1, custom_commands.SLOT_COUNT + 1):
            command = custom_commands.get(slot) or {}
            group = Adw.PreferencesGroup(title="Befehl %d" % slot)
            kind_row = Adw.ComboRow(title="Aktion", model=Gtk.StringList.new(list(COMMAND_KIND_LABELS)))
            kind = command.get("kind", "none")
            kind_row.set_selected(COMMAND_KINDS.index(kind) if kind in COMMAND_KINDS else 0)
            value_row = Adw.EntryRow(title="Befehl oder URI", text=command.get("value", "") or "")
            label_row = Adw.EntryRow(title="Name", text=command.get("label", "") or "")
            value_row.set_sensitive(kind == "run")   # only Run needs a command; Keine leaves the field disabled
            kind_row.connect("notify::selected", self._on_slot_kind_changed, slot)
            value_row.connect("changed", self._on_slot_text_changed, slot)
            label_row.connect("changed", self._on_slot_text_changed, slot)
            group.add(kind_row)
            group.add(value_row)
            group.add(label_row)
            column.append(group)
            self.slot_rows.append((kind_row, value_row, label_row))
        return scroller

    # ------------------------------------------------------------------ actions and shortcuts

    def _install_actions(self) -> None:
        action = Gio.SimpleAction.new("open-log", None)
        action.connect("activate", lambda *_: open_log(self))
        self.add_action(action)

        action = Gio.SimpleAction.new("about", None)
        action.connect("activate", lambda *_: self.show_about())
        self.add_action(action)

        self._autostart_action = Gio.SimpleAction.new_stateful("autostart", None, GLib.Variant.new_boolean(autostart.is_enabled()))
        self._autostart_action.connect("change-state", self._on_autostart_change_state)
        self.add_action(self._autostart_action)
        self._set_quietly(self.autostart_row.set_active, autostart.is_enabled())

        if self._app.lookup_action("quit") is None:   # the real app brings its own (it stops the server first)
            action = Gio.SimpleAction.new("quit", None)
            action.connect("activate", self._on_fallback_quit)
            self._app.add_action(action)
        self._app.set_accels_for_action("app.quit", ["<Control>q"])
        self._app.set_accels_for_action("win.open-log", ["<Control>l"])

    def _on_fallback_quit(self, *_args) -> None:
        try:
            self._server.shutdown()
        finally:
            self._app.quit()

    def show_about(self) -> Adw.AboutDialog:
        about = Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_EXEC, version=__version__,
                                developer_name="TEE", license_type=Gtk.License.APACHE_2_0, copyright="© 2026 TEE",
                                comments=("Streamt den PC-Desktop zu einer PS3 mit der cell-stream-App und spielt "
                                          "deren Controller am PC ab.\n\nLinux-Port von cell-stream-server "
                                          "(ps3-dev, Release %s)." % UPSTREAM_VERSION))
        about.present(self)
        return about

    # ------------------------------------------------------------------ handlers

    def _on_start_stop_clicked(self, _button) -> None:
        if self._server.is_armed:
            self._server.disarm("von dir gestoppt")
        else:
            self._server.arm()
        self.refresh()

    # the encoder cannot change under a live stream, so put the selection back if one starts between the
    # dropdown being enabled and the choice being made
    def _on_encoder_selected(self, row, _pspec) -> None:
        if self._syncing:
            return
        encoders = list(self._server.available_encoders)
        index = row.get_selected()
        if index < 0 or index >= len(encoders):
            return
        chosen = encoders[index]
        if chosen is self._server.chosen_encoder:
            return
        if self._server.is_ps3_connected:
            log.write("encoders: erst den Stream beenden, dann den Encoder wechseln")
            self._sync_choices()
            return
        self._server.chosen_encoder = chosen

    def _on_recovery_selected(self, row, _pspec) -> None:
        if self._syncing:
            return
        index = row.get_selected()
        if index < 0 or index >= len(LOSS_RECOVERY_KINDS) or LOSS_RECOVERY_KINDS[index] == self._server.loss_recovery:
            return
        if self._server.is_ps3_connected:
            log.write("video: erst den Stream beenden, dann die Fehlerkorrektur wechseln")
            self._sync_choices()
            return
        self._server.loss_recovery = LOSS_RECOVERY_KINDS[index]

    def _on_bitrate_selected(self, row, _pspec) -> None:
        if self._syncing:
            return
        index = row.get_selected()
        if index < 0 or index >= len(protocol.BITRATE_CHOICES_KBPS):
            return
        if protocol.BITRATE_CHOICES_KBPS[index] == self._server.video_kbps:
            return
        if self._server.is_ps3_connected:
            log.write("video: erst den Stream beenden, dann die Bitrate wechseln")
            self._sync_choices()
            return
        self._server.video_kbps = protocol.BITRATE_CHOICES_KBPS[index]

    def _on_size_selected(self, row, _pspec) -> None:
        if self._syncing:
            return
        index = row.get_selected()
        if index < 0 or index >= len(protocol.STREAM_SIZES) or protocol.STREAM_SIZES[index] == self._server.stream_size:
            return
        if self._server.is_ps3_connected:
            log.write("video: erst den Stream beenden, dann die Auflösung wechseln")
            self._sync_choices()
            return
        self._server.stream_size = protocol.STREAM_SIZES[index]

    def _on_coder_selected(self, row, _pspec) -> None:
        if self._syncing:
            return
        index = row.get_selected()
        if index < 0 or index >= len(protocol.ENTROPY_CODERS) or protocol.ENTROPY_CODERS[index] == self._server.entropy_coder:
            return
        if self._server.is_ps3_connected:
            log.write("video: erst den Stream beenden, dann die Entropie-Codierung wechseln")
            self._sync_choices()
            return
        self._server.entropy_coder = protocol.ENTROPY_CODERS[index]

    def _on_rate_selected(self, row, _pspec) -> None:
        if self._syncing:
            return
        index = row.get_selected()
        if index < 0 or index >= len(protocol.RATE_CONTROLS) or protocol.RATE_CONTROLS[index] == self._server.rate_control:
            return
        if self._server.is_ps3_connected:
            log.write("video: erst den Stream beenden, dann die Ratensteuerung wechseln")
            self._sync_choices()
            return
        self._server.rate_control = protocol.RATE_CONTROLS[index]

    # --- the "can you still see this?" safety net ------------------------------------------------
    # A monitor can accept a mode and show nothing at all. The user is then staring at a black desktop
    # with no way to click anything, which is why the countdown - not the button - is what actually saves
    # them: display_mode reverts on its own unless someone confirms. This dialog just makes the good case
    # quick, and gives an immediate way out for the case where the picture is there but wrong.
    def _prompt_display_confirm(self, seconds: int) -> None:
        """Called from display_mode's worker thread: hand it straight to the GTK loop and return."""
        GLib.idle_add(self._show_display_confirm, seconds)

    def _show_display_confirm(self, seconds: int):
        display = self._server.display_mode
        dialog = Adw.AlertDialog(heading="Siehst du dieses Fenster?",
                                 body=self._confirm_body(seconds))
        dialog.add_response("no", "Nein, zurückschalten")
        dialog.add_response("yes", "Ja, so lassen")
        dialog.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_appearance("no", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("yes")
        dialog.set_close_response("no")

        left = {"seconds": seconds, "done": False}

        def tick():
            if left["done"]:
                return GLib.SOURCE_REMOVE
            left["seconds"] -= 1
            if left["seconds"] <= 0:
                left["done"] = True
                dialog.close()      # display_mode is switching back on its own; just get out of the way
                return GLib.SOURCE_REMOVE
            dialog.set_body(self._confirm_body(left["seconds"]))
            return GLib.SOURCE_CONTINUE

        def answered(_dialog, response):
            left["done"] = True
            if response == "yes":
                display.confirm_visible()
                log.write("display: Bild bestätigt, die neue Auflösung bleibt")
            else:
                display.reject_visible()

        dialog.connect("response", answered)
        GLib.timeout_add_seconds(1, tick)
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _confirm_body(seconds: int) -> str:
        return ("Der Desktop wurde für den Stream umgeschaltet.\n\n"
                "Wenn du das hier lesen kannst, ist alles in Ordnung. Ohne Antwort wird in %d Sekunden "
                "automatisch auf die vorherige Auflösung zurückgeschaltet." % seconds)

    def _on_display_toggled(self, row, _pspec) -> None:
        if not self._syncing:
            self._server.switch_display_mode = row.get_active()

    def _on_swap_sticks_toggled(self, row, _pspec) -> None:
        if not self._syncing:
            self._server.swap_mouse_sticks = row.get_active()

    def _on_autostart_row_toggled(self, row, _pspec) -> None:
        if not self._syncing:
            self._autostart_action.change_state(GLib.Variant.new_boolean(row.get_active()))

    def _on_autostart_change_state(self, action, value) -> None:
        wanted = value.get_boolean()
        if autostart.is_enabled() != wanted:
            autostart.set_enabled(wanted)
        actual = autostart.is_enabled()   # the file system has the last word
        action.set_state(GLib.Variant.new_boolean(actual))
        self._set_quietly(self.autostart_row.set_active, actual)

    def _on_slot_kind_changed(self, kind_row, _pspec, slot: int) -> None:
        _kind, value_row, _label = self.slot_rows[slot - 1]
        value_row.set_sensitive(kind_row.get_selected() == COMMAND_KINDS.index("run"))
        self._save_slot(slot)

    def _on_slot_text_changed(self, _row, slot: int) -> None:
        if slot in self._slot_save_timers:
            GLib.source_remove(self._slot_save_timers[slot])
        self._slot_save_timers[slot] = GLib.timeout_add(SLOT_SAVE_DELAY_MS, self._on_slot_save_due, slot)

    def _on_slot_save_due(self, slot: int) -> bool:
        self._slot_save_timers.pop(slot, None)
        self._save_slot(slot)
        return GLib.SOURCE_REMOVE

    def _save_slot(self, slot: int) -> None:
        kind_row, value_row, label_row = self.slot_rows[slot - 1]
        index = kind_row.get_selected()
        custom_commands.set(slot, {
            "kind": COMMAND_KINDS[index] if 0 <= index < len(COMMAND_KINDS) else "none",
            "value": value_row.get_text() or "",
            "label": label_row.get_text() or "",
        })

    # the X button hides us; only Beenden really exits
    def _on_close_request(self, _window) -> bool:
        try:
            self.hide_to_tray()
        except Exception as error:   # noqa: BLE001 - an exception here would return FALSE and destroy the window
            log.write("Fenster: konnte nicht in den Hintergrund gehen: %s" % error)
            self.set_visible(False)
        return True

    def _on_destroy(self, _widget) -> None:
        self.shut_down()   # belt and braces: measured not to arrive at all (see shut_down), but harmless twice

    def flush_pending_saves(self) -> None:
        """A Befehle field saves 400 ms after the last keystroke; whatever is still inside that pause goes now."""
        for slot, timer in list(self._slot_save_timers.items()):
            GLib.source_remove(timer)
            self._slot_save_timers.pop(slot, None)
            try:
                self._save_slot(slot)
            except Exception as error:   # noqa: BLE001 - a failed save must not stop the shutdown around it
                log.write("Befehl %d konnte nicht gespeichert werden: %s" % (slot, error))

    def shut_down(self) -> None:
        """Give back what the window owns, while its widgets are still readable. The app calls this before it
        quits, because the destroy signal does not get here: measured on GTK 4.22.4 / PyGObject 3.56, a handler
        connected on a Python subclass of Adw.ApplicationWindow never runs, so the 500 ms timer would keep
        ticking on a destroyed window (3 ticks in 1.5 s after destroy()) for the life of the process."""
        self.flush_pending_saves()
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0

    # ------------------------------------------------------------------ show / hide

    def hide_to_tray(self) -> None:
        self.flush_pending_saves()   # closing the window is the moment the user is done typing in Befehle
        self.set_visible(False)
        if not settings.get("hide_notice_shown", False):
            settings.set("hide_notice_shown", True)
            self._send_notification("hidden", "Läuft im Hintergrund weiter",
                                    "Der Server wartet weiter auf die PS3. Beenden über das Tray-Symbol oder das Menü.")

    def present_from_tray(self, activation_token: str | None = None) -> None:
        # a fresh map gets the focus on its own; a window already on screen is only raised when we hand the
        # compositor the token the click produced (GTK spends it on the next present)
        if activation_token:
            self.set_startup_id(activation_token)
        self.set_visible(True)
        self.unminimize()
        self.present()

    def _send_notification(self, ident: str, title: str, body: str) -> None:
        if not self._app.get_is_registered():
            return
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_icon(Gio.ThemedIcon.new(APP_EXEC))
        self._app.send_notification(ident, notification)

    # ------------------------------------------------------------------ refresh

    def _on_tick(self) -> bool:
        try:
            self.refresh()
        except Exception as error:   # noqa: BLE001 - a display glitch must not stop the timer for good
            log.write("Fenster: Aktualisierung fehlgeschlagen: %s" % error)
        return GLib.SOURCE_CONTINUE

    def refresh(self) -> None:
        """Grey until a PS3 turns up, green while it is streaming; the button and the locks follow the server."""
        server = self._server
        connected = bool(server.is_ps3_connected)
        armed = bool(server.is_armed)
        who = server.connected_ps3 or ""
        status = status_text(armed, connected, who)
        if self.status_label.get_text() != status:
            self.status_label.set_text(status)

        subtitle = server.settings_summary if (armed or server.trip_reason is None) else server.trip_reason
        if self.subtitle_label.get_text() != subtitle:
            self.subtitle_label.set_text(subtitle)

        self._set_dot(connected)
        self._set_button(armed)

        self._sync_choices()
        locked = connected   # don't let the encoder change out from under a live stream
        self.encoder_row.set_sensitive(not locked and len(self._encoder_kinds) > 0)
        self.recovery_row.set_sensitive(not locked)
        self.size_row.set_sensitive(not locked)
        self.bitrate_row.set_sensitive(not locked)
        self.coder_row.set_sensitive(not locked)

        if self.get_visible():
            self._refresh_log()

    def _set_dot(self, live: bool) -> None:
        wanted, other = ("status-dot-live", "status-dot-idle") if live else ("status-dot-idle", "status-dot-live")
        if not self.status_dot.has_css_class(wanted):
            self.status_dot.remove_css_class(other)
            self.status_dot.add_css_class(wanted)

    def _set_button(self, armed: bool) -> None:
        label, wanted, other = ("Stop", "destructive-action", "suggested-action") if armed else ("Start", "suggested-action", "destructive-action")
        if self.start_stop_button.get_label() != label:
            self.start_stop_button.set_label(label)
        if not self.start_stop_button.has_css_class(wanted):
            self.start_stop_button.remove_css_class(other)
            self.start_stop_button.add_css_class(wanted)

    def _set_quietly(self, setter, value) -> None:
        """Move a widget without its handler answering; the flag is restored, never just cleared, so a nested
        call cannot unmute the sync that is still running around it."""
        was_syncing = self._syncing
        self._syncing = True
        try:
            setter(value)
        finally:
            self._syncing = was_syncing

    def _sync_choices(self) -> None:
        """Widgets follow the server (the model is rebuilt only when the encoder list itself changes)."""
        server = self._server
        was_syncing = self._syncing
        self._syncing = True
        try:
            encoders = list(server.available_encoders)
            kinds = [encoder.kind for encoder in encoders]
            if kinds != self._encoder_kinds:
                self._encoder_kinds = kinds
                self.encoder_model.splice(0, self.encoder_model.get_n_items(), [encoder.name for encoder in encoders])
                self.encoder_row.set_subtitle("Gesperrt, solange eine PS3 streamt" if encoders else "Kein H.264-Encoder gefunden")
            chosen = server.chosen_encoder
            if chosen is not None and chosen in encoders:
                index = encoders.index(chosen)
                if self.encoder_row.get_selected() != index:
                    self.encoder_row.set_selected(index)

            recovery = server.loss_recovery
            index = LOSS_RECOVERY_KINDS.index(recovery) if recovery in LOSS_RECOVERY_KINDS else 0
            if self.recovery_row.get_selected() != index:
                self.recovery_row.set_selected(index)

            size = tuple(server.stream_size)
            index = protocol.STREAM_SIZES.index(size) if size in protocol.STREAM_SIZES else 0
            if self.size_row.get_selected() != index:
                self.size_row.set_selected(index)

            kbps = server.video_kbps
            index = protocol.BITRATE_CHOICES_KBPS.index(kbps) if kbps in protocol.BITRATE_CHOICES_KBPS else 0
            if self.bitrate_row.get_selected() != index:
                self.bitrate_row.set_selected(index)

            coder = server.entropy_coder
            index = protocol.ENTROPY_CODERS.index(coder) if coder in protocol.ENTROPY_CODERS else 0
            if self.coder_row.get_selected() != index:
                self.coder_row.set_selected(index)

            rate = getattr(server, "rate_control", "vbr")
            index = protocol.RATE_CONTROLS.index(rate) if rate in protocol.RATE_CONTROLS else 0
            if self.rate_row.get_selected() != index:
                self.rate_row.set_selected(index)

            if self.display_row.get_active() != bool(server.switch_display_mode):
                self.display_row.set_active(bool(server.switch_display_mode))
            if self.swap_sticks_row.get_active() != bool(server.swap_mouse_sticks):
                self.swap_sticks_row.set_active(bool(server.swap_mouse_sticks))
        finally:
            self._syncing = was_syncing

    def _refresh_log(self) -> None:
        generation = log.generation()
        if generation == self._shown_log_generation:
            return
        self._shown_log_generation = generation
        buffer = self.log_view.get_buffer()
        buffer.set_text(log.get_recent())
        self.log_view.scroll_to_mark(self._log_end_mark, 0.0, True, 0.0, 1.0)
