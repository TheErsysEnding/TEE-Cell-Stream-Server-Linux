"""Start at log-in: an XDG autostart entry (the Windows server used the Run registry key for this).

The entry starts us with --minimized, so a boot goes straight to the tray rather than putting a window in the
user's face. Path: ~/.config/autostart/tee-cell-stream-server.desktop (TEE_CST_AUTOSTART_PATH overrides it for tests).
"""

import os
import shutil
import sys

from . import APP_EXEC, APP_NAME, log
from .i18n import _

FILE_NAME = APP_EXEC + ".desktop"
MINIMIZED_SWITCH = "--minimized"


def path() -> str:
    override = os.environ.get("TEE_CST_AUTOSTART_PATH")
    if override:
        return override
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config_home, "autostart", FILE_NAME)


def _quote_exec_arg(argument: str) -> str:
    """Desktop-entry Exec quoting: double quotes, with the few characters the spec reserves escaped.
    "%" is a field code (%f, %u, ...) wherever it stands, so a literal one has to be doubled."""
    if argument and all(ch.isalnum() or ch in "-_./:=+,@" for ch in argument):
        return argument
    escaped = argument.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + escaped.replace("%", "%%") + '"'


def exec_line() -> str:
    """The installed launcher when there is one; a checkout runs straight out of its source tree."""
    if shutil.which(APP_EXEC):
        return APP_EXEC + " " + MINIMIZED_SWITCH
    source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return "env PYTHONPATH=%s %s -m teecellstream %s" % (_quote_exec_arg(source_root), _quote_exec_arg(sys.executable), MINIMIZED_SWITCH)


def _entry_text() -> str:
    return "\n".join((
        "[Desktop Entry]",
        "Type=Application",
        "Name=" + APP_NAME,
        "Comment=Stream the PC desktop to a PS3 (cell-stream)",
        "Exec=" + exec_line(),
        "Icon=" + APP_EXEC,
        "Terminal=false",
        "StartupNotify=false",
        "X-GNOME-Autostart-enabled=true",
        "",
    ))


def is_enabled() -> bool:
    """True when the entry exists and is not switched off in place (GNOME Tweaks toggles it that way)."""
    try:
        with open(path(), "r", encoding="utf-8") as handle:
            lines = [line.strip() for line in handle]
    except OSError:
        return False
    for line in lines:
        key, _sep, value = line.partition("=")
        key = key.strip()
        value = value.strip().lower()
        if key == "Hidden" and value == "true":
            return False
        if key == "X-GNOME-Autostart-enabled" and value == "false":
            return False
    return True


def set_enabled(wanted: bool) -> bool:
    """Writes or removes the entry. Returns False (and logs why) when the file system says no."""
    target = path()
    try:
        if wanted:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            temporary = target + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(_entry_text())
            os.replace(temporary, target)
        else:
            try:
                os.remove(target)
            except FileNotFoundError:
                pass
        log.write("autostart: starts at login (minimised)" if wanted else "autostart: no longer starts at login")
        return True
    except OSError as error:
        log.write(_("autostart: could not change the setting: %s") % error)
        return False
