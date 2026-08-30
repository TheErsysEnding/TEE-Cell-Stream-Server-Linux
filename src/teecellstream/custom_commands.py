"""The four PC actions the PS3 can fire by slot number (port of CustomCommands.cs).

The PS3 triggers them with CUSTOM 1..4 and only ever sends the slot number; what each slot does is
defined here and on the window's Befehle page, and remembered in the settings file. A device on the LAN
can only ask for a slot, never send a raw command, so it cannot make the PC run anything the user did
not set up here.

A slot is a dict {"kind": "none"|"run", "value": str, "label": str}. "run" starts a program or URI:
something that looks like a URI (steam://open/bigpicture, https://...) goes to xdg-open, the desktop's
equivalent of ShellExecute; anything else is a command line for `sh -c`.
"""

import re
import subprocess
import threading

from . import log
from .settings import settings
from .i18n import _

SLOT_COUNT = 4
KIND_NONE = "none"
KIND_RUN = "run"
KINDS = (KIND_NONE, KIND_RUN)
SETTINGS_KEY = "custom_commands"

# first run seeds slot 1 = Steam Big Picture, matching the PS3's default
DEFAULT_SLOT_1 = {"kind": KIND_RUN, "value": "steam://open/bigpicture", "label": "Big Picture"}

# RFC 3986 scheme followed by "://" - a bare "mailto:" style URI is deliberately not matched, a Windows
# drive letter ("C:\...") never has the slashes either, so command lines are not mistaken for URIs
URI_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

_gate = threading.RLock()
_slots: list[dict] | None = None   # loaded on first use, so importing this module does not touch the settings


def empty_command() -> dict:
    return {"kind": KIND_NONE, "value": "", "label": ""}


def _normalise(raw) -> dict:
    """A saved slot in any shape (older file, hand-edited) becomes a well-formed one; junk becomes empty."""
    if not isinstance(raw, dict):
        return empty_command()
    kind = str(raw.get("kind") or KIND_NONE).strip().lower()   # like the original's Enum.Parse: unknown = None
    if kind not in KINDS:
        kind = KIND_NONE
    value = raw.get("value")
    label = raw.get("label")
    return {"kind": kind, "value": value if isinstance(value, str) else "", "label": label if isinstance(label, str) else ""}


def _load_all() -> list[dict]:
    saved = settings.get(SETTINGS_KEY, None)
    if not isinstance(saved, list):
        # nothing saved yet: first run
        loaded = [empty_command() for _slot in range(SLOT_COUNT)]
        loaded[0] = dict(DEFAULT_SLOT_1)
        _save_all(loaded)
        return loaded

    loaded = [_normalise(entry) for entry in saved[:SLOT_COUNT]]
    while len(loaded) < SLOT_COUNT:
        loaded.append(empty_command())
    if _move_up_into_empty_first_slot(loaded) or loaded != saved:
        _save_all(loaded)
    return loaded


def _move_up_into_empty_first_slot(loaded: list[dict]) -> bool:
    """The Guide action was dropped upstream, which left slot 1 empty wherever it had been saved. Move slot 2
    up into it so the first slot is the one that does something. True = moved (and needs saving)."""
    if loaded[0]["kind"] != KIND_NONE or loaded[1]["kind"] == KIND_NONE:
        return False
    loaded[0] = loaded[1]
    loaded[1] = empty_command()
    return True


def _save_all(slots: list[dict]) -> None:
    try:
        settings.set(SETTINGS_KEY, [dict(slot) for slot in slots])
    except Exception as error:   # noqa: BLE001 - the settings file being unwritable must not break the slot
        log.write(_("custom: could not save - %s") % error)


def _slots_loaded() -> list[dict]:
    global _slots
    with _gate:
        if _slots is None:
            _slots = _load_all()
        return _slots


def reload() -> None:
    """Forgets the slots so the next access re-reads the settings (tests, and a settings file edited by hand)."""
    global _slots
    with _gate:
        _slots = None


def get(slot: int) -> dict | None:
    """Slot 1..SLOT_COUNT as a copy; None for a number the PS3 made up."""
    if not 1 <= slot <= SLOT_COUNT:
        return None
    with _gate:
        return dict(_slots_loaded()[slot - 1])


def set(slot: int, command: dict) -> None:   # noqa: A001 - the name is the module's contract
    if not 1 <= slot <= SLOT_COUNT:
        return
    with _gate:
        slots = _slots_loaded()
        slots[slot - 1] = _normalise(command)
        _save_all(slots)


def is_uri(value: str) -> bool:
    return URI_PATTERN.match(value or "") is not None


def command_line(value: str) -> list[str]:
    """What actually gets started for a slot's value."""
    value = value.strip()
    if is_uri(value):
        return ["xdg-open", value]
    return ["sh", "-c", value]


def _spawn(argv: list[str]) -> None:
    # detached: its own session so the server's signals (and its exit) never reach what the user launched -
    # Big Picture must outlive us. no pipes either, or a chatty program would fill them and stall.
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
    # somebody has to collect the exit status or the child lingers as a zombie for as long as we run
    threading.Thread(target=child.wait, name="custom-reaper-%d" % child.pid, daemon=True).start()


def run(slot: int) -> None:
    """Runs the action bound to a slot (CUSTOM <slot> from the PS3)."""
    command = get(slot)
    if command is None or command["kind"] != KIND_RUN or not command["value"].strip():
        log.write(_("custom %d: nothing is bound to this slot") % slot)
        return
    value = command["value"].strip()
    try:
        _spawn(command_line(value))
        log.write(_("custom %d: started: %s") % (slot, value))
    except Exception as error:   # noqa: BLE001 - a missing xdg-open or sh is the user's news, not a crash
        log.write(_("custom %d: could not start %s - %s") % (slot, value, error))
