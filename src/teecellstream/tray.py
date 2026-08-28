"""A dot in the panel: grey while waiting, green while a PS3 streams (port of the WinForms NotifyIcon).

GNOME has no tray of its own; the AppIndicator/KStatusNotifierItem extension (Ubuntu ships it enabled) hosts
org.kde.StatusNotifierWatcher and shows whatever registers there. We publish an org.kde.StatusNotifierItem plus
its com.canonical.dbusmenu menu on a private session-bus connection: the host watches the item's bus name and
removes the icon the moment that name vanishes, so closing our own connection is the one clean way to leave.
No watcher on the bus -> no tray, no error: the window and the notifications still work.

Best effort throughout: nothing here may take the server down.
"""

import os
import struct

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402

from . import APP_EXEC, APP_ID, APP_NAME, log  # noqa: E402

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"
ITEM_PATH = "/StatusNotifierItem"
MENU_INTERFACE = "com.canonical.dbusmenu"
MENU_PATH = "/MenuBar"

ICON_IDLE = APP_EXEC + "-idle"
ICON_LIVE = APP_EXEC + "-live"
PIXMAP_SIZES = (22, 24, 32, 48)

MENU_SHOW, MENU_OPEN_LOG, MENU_SEPARATOR, MENU_QUIT = 1, 2, 3, 4

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <method name="ProvideXdgActivationToken"><arg name="token" type="s" direction="in"/></method>
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="u" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
    <signal name="ItemActivationRequested"><arg name="id" type="i"/><arg name="timestamp" type="u"/></signal>
  </interface>
</node>
"""


def dev_icon_dir() -> str:
    """data/icons of a source checkout, or "" once installed (then hicolor has the icons)."""
    candidate = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "icons")
    return candidate if os.path.isdir(candidate) else ""


def _icon_search_dirs() -> list[str]:
    dirs = [dev_icon_dir()] if dev_icon_dir() else []
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs.extend(os.path.join(base, "icons") for base in [data_home] + data_dirs.split(":") if base)
    return dirs


def load_pixmaps(icon_name: str) -> list[tuple[int, int, bytes]]:
    """The icon as raw ARGB32 (network byte order) in the sizes hicolor has - the SNI fallback when the host
    cannot resolve IconName (a theme path it does not read, or no icon cache yet right after installing)."""
    pixmaps = []
    for size in PIXMAP_SIZES:
        for base in _icon_search_dirs():
            path = os.path.join(base, "hicolor", "%dx%d" % (size, size), "apps", icon_name + ".png")
            if not os.path.isfile(path):
                continue
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
                if not pixbuf.get_has_alpha():
                    pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
                width, height, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride()
                rgba = pixbuf.get_pixels()
                argb = bytearray()
                for y in range(height):
                    row = rgba[y * stride:y * stride + width * 4]
                    for x in range(width):
                        r, g, b, a = row[x * 4:x * 4 + 4]
                        argb += struct.pack(">BBBB", a, r, g, b)
                pixmaps.append((width, height, bytes(argb)))
            except (GLib.Error, ValueError):
                pass
            break
    return pixmaps


def _register(connection: Gio.DBusConnection, path: str, info, on_call, on_get) -> int:
    """GLib 2.86 deprecated register_object for the closure variant; older ones only have the original."""
    if hasattr(connection, "register_object_with_closures2"):
        return connection.register_object_with_closures2(path, info, on_call, on_get, None)
    return connection.register_object(path, info, on_call, on_get, None)


class TrayIcon:
    """Call start() on the main thread with the main loop about to run; stop() before the process ends."""

    def __init__(self, on_show, on_open_log, on_quit, title: str = APP_NAME):
        self._on_show = on_show
        self._on_open_log = on_open_log
        self._on_quit = on_quit
        self._title = title
        self._tooltip = title
        self._live = False
        self._connection: Gio.DBusConnection | None = None
        self._bus_name = "%s-%d-1" % (ITEM_INTERFACE, os.getpid())
        self._registrations: list[int] = []
        self._watch_id = 0
        self._menu_revision = 1
        self._icon_theme_path = dev_icon_dir()
        self._pixmaps: dict[str, list] = {}
        self.is_registered = False   # the watcher accepted us (so the panel shows the dot)
        self._logged_no_watcher = False
        self._activation_token: str | None = None
        self._menu_items = (
            (MENU_SHOW, "Anzeigen", self._on_show),
            (MENU_OPEN_LOG, "Log öffnen", self._on_open_log),
            (MENU_SEPARATOR, None, None),
            (MENU_QUIT, "Beenden", self._on_quit),
        )

    # ------------------------------------------------------------------ lifecycle

    @property
    def is_started(self) -> bool:
        return self._connection is not None

    @property
    def bus_name(self) -> str:
        return self._bus_name

    @property
    def unique_name(self) -> str | None:
        return self._connection.get_unique_name() if self._connection is not None else None

    def start(self) -> bool:
        """Publishes the item and asks the watcher to show it. False = no bus at all (nothing else fails)."""
        if self._connection is not None:
            return True
        try:
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
            connection = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
            connection.set_exit_on_close(False)
            item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
            menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
            self._registrations.append(_register(connection, ITEM_PATH, item_info, self._on_item_call, self._on_item_get))
            self._registrations.append(_register(connection, MENU_PATH, menu_info, self._on_menu_call, self._on_menu_get))
            reply = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                                         GLib.Variant("(su)", (self._bus_name, 0)), GLib.VariantType("(u)"),
                                         Gio.DBusCallFlags.NONE, 3000, None)
            if reply.unpack()[0] != 1:   # not the primary owner: a stale copy with our pid? cannot happen, but be honest
                log.write("tray: Busname %s nicht bekommen" % self._bus_name)
        except GLib.Error as error:
            log.write("tray: kein Session-Bus (%s) - ohne Tray-Symbol" % error.message)
            return False
        self._connection = connection
        # the watcher comes and goes with the shell (and its extension); register whenever it is there
        self._watch_id = Gio.bus_watch_name_on_connection(connection, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
                                                          self._on_watcher_appeared, self._on_watcher_vanished)
        return True

    def stop(self) -> None:
        """Closing the connection is what makes the host drop the icon (it watches our bus name)."""
        connection, self._connection = self._connection, None
        self.is_registered = False   # first thing: a half-torn-down item must never still claim to be shown
        self._logged_no_watcher = False   # a later start() may find a different desktop; let it say so again
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if connection is None:
            return
        for registration in self._registrations:
            try:
                connection.unregister_object(registration)
            except GLib.Error:
                pass
        self._registrations = []
        try:
            connection.flush_sync(None)
            connection.close_sync(None)
        except GLib.Error:
            pass

    def _on_watcher_appeared(self, connection, _name, _owner) -> None:
        connection.call(WATCHER_NAME, WATCHER_PATH, WATCHER_NAME, "RegisterStatusNotifierItem",
                        GLib.Variant("(s)", (self._bus_name,)), None, Gio.DBusCallFlags.NONE, 5000, None, self._on_registered)

    def _on_watcher_vanished(self, _connection, _name) -> None:
        self.is_registered = False
        if not self._logged_no_watcher:
            self._logged_no_watcher = True
            log.write("tray: kein StatusNotifierWatcher (AppIndicator-Erweiterung aus?) - ohne Tray-Symbol")

    def _on_registered(self, connection, result) -> None:
        try:
            connection.call_finish(result)
        except GLib.Error as error:
            log.write("tray: Registrierung fehlgeschlagen: %s" % error.message)
            return
        self.is_registered = True

    # ------------------------------------------------------------------ what the app changes

    def set_live(self, live: bool) -> None:
        """Green while a PS3 streams, grey otherwise."""
        live = bool(live)
        if live == self._live:
            return
        self._live = live
        self._emit(ITEM_PATH, ITEM_INTERFACE, "NewIcon", None)

    def set_tooltip(self, text: str) -> None:
        if text == self._tooltip:
            return
        self._tooltip = text
        self._emit(ITEM_PATH, ITEM_INTERFACE, "NewToolTip", None)

    @property
    def icon_name(self) -> str:
        return ICON_LIVE if self._live else ICON_IDLE

    def _emit(self, path: str, interface: str, name: str, parameters) -> None:
        if self._connection is None:
            return
        try:
            self._connection.emit_signal(None, path, interface, name, parameters)
        except GLib.Error as error:
            log.write("tray: Signal %s fehlgeschlagen: %s" % (name, error.message))

    def _dispatch(self, callback) -> None:
        """Run the app's handler after the DBus reply has gone out (Quit tears the connection down)."""
        if callback is None:
            return

        def run():
            try:
                callback()
            except Exception as error:   # noqa: BLE001 - a tray click must never take the server down
                log.write("tray: Aktion fehlgeschlagen: %s" % error)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(run)

    # ------------------------------------------------------------------ org.kde.StatusNotifierItem

    def _on_item_call(self, _connection, _sender, _path, _interface, method, parameters, invocation) -> None:
        if method in ("Activate", "SecondaryActivate"):
            self._dispatch(self._on_show)
        elif method == "ProvideXdgActivationToken":
            # on Wayland the panel sends this right before Activate (and before a menu click): only with this
            # token may our window come to the front. The show handler collects it via take_activation_token().
            self._activation_token = parameters.unpack()[0] or None
        # ContextMenu: the host shows our dbusmenu itself. Scroll: nothing to do.
        invocation.return_value(None)

    def take_activation_token(self) -> str | None:
        """The xdg-activation token the panel provided for the click being handled, once; None without one."""
        token, self._activation_token = self._activation_token, None
        return token

    def _pixmaps_for(self, icon_name: str):
        if icon_name not in self._pixmaps:
            self._pixmaps[icon_name] = load_pixmaps(icon_name)
        return self._pixmaps[icon_name]

    def _on_item_get(self, _connection, _sender, _path, _interface, name):
        empty_pixmaps = GLib.Variant("a(iiay)", [])
        if name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", APP_ID)
        if name == "Title":
            return GLib.Variant("s", self._title)
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "WindowId":
            return GLib.Variant("u", 0)
        if name == "IconName":
            return GLib.Variant("s", self.icon_name)
        if name == "IconPixmap":
            return GLib.Variant("a(iiay)", self._pixmaps_for(self.icon_name))
        if name in ("OverlayIconName", "AttentionIconName", "AttentionMovieName"):
            return GLib.Variant("s", "")
        if name in ("OverlayIconPixmap", "AttentionIconPixmap"):
            return empty_pixmaps
        if name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", (APP_EXEC, [], self._title, self._tooltip))
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)
        if name == "IconThemePath":
            return GLib.Variant("s", self._icon_theme_path)
        return None

    # ------------------------------------------------------------------ com.canonical.dbusmenu

    def _item_properties(self, item_id: int) -> dict:
        if item_id == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        for ident, label, _callback in self._menu_items:
            if ident != item_id:
                continue
            if label is None:
                return {"type": GLib.Variant("s", "separator"), "visible": GLib.Variant("b", True)}
            return {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", True), "visible": GLib.Variant("b", True)}
        return {}

    def _layout(self, item_id: int, depth: int) -> tuple:
        """(id, properties, children) as plain Python, so it can be packed exactly once at the top level.
        The children must be boxed Variants (the `av` of the layout); a Variant handed back into another
        GLib.Variant(...) would be iterated and unpacked by PyGObject and lose its typed leaves."""
        children = []
        if item_id == 0 and depth != 0:
            children = [GLib.Variant("(ia{sv}av)", self._layout(ident, depth - 1 if depth > 0 else depth))
                        for ident, _label, _cb in self._menu_items]
        return (item_id, self._item_properties(item_id), children)

    def _on_menu_call(self, _connection, _sender, _path, _interface, method, parameters, invocation) -> None:
        if method == "GetLayout":
            parent_id, depth, _names = parameters.unpack()
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self._menu_revision, self._layout(parent_id, depth))))
        elif method == "GetGroupProperties":
            ids, _names = parameters.unpack()
            wanted = ids or [0] + [ident for ident, _label, _cb in self._menu_items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(ident, self._item_properties(ident)) for ident in wanted],)))
        elif method == "GetProperty":
            item_id, name = parameters.unpack()
            value = self._item_properties(item_id).get(name)
            if value is None:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", "unknown property " + name)
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event_id, _data, _timestamp = parameters.unpack()
            self._on_menu_event(item_id, event_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event_id, _data, _timestamp in parameters.unpack()[0]:
                self._on_menu_event(item_id, event_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", "no such method " + method)

    def _on_menu_event(self, item_id: int, event_id: str) -> None:
        if event_id != "clicked":
            return
        for ident, _label, callback in self._menu_items:
            if ident == item_id:
                self._dispatch(callback)

    def _on_menu_get(self, _connection, _sender, _path, _interface, name):
        if name == "Version":
            return GLib.Variant("u", 3)
        if name == "TextDirection":
            return GLib.Variant("s", "ltr")
        if name == "Status":
            return GLib.Variant("s", "normal")
        if name == "IconThemePath":
            return GLib.Variant("as", [self._icon_theme_path] if self._icon_theme_path else [])
        return None
