# i18n - one translation table and a language that can change while the program runs.
#
# English is the source language: every string in the code IS the English text, so a missing translation
# degrades to English rather than to a key like "ui.status.waiting". German is a table on top of it.
#
# Why not gettext: it wants .po/.mo files compiled at build time and installed under /usr/share/locale, and
# switching language at runtime means re-binding the domain and rebuilding every widget anyway. The table
# below is the whole mechanism, it ships inside the package, and `set_language` tells the window to relabel
# itself - which is the only part that is actually work.
#
# WHAT TRANSLATES AND WHAT DOES NOT. The window relabels itself completely. Log lines are translated when
# they are WRITTEN, so lines already in the log keep the language they were written in - a log is a record
# of what happened, not a view that can be re-rendered. Switching language therefore changes every new line
# and leaves the old ones alone, which is also what makes the log usable as evidence.

_LANGUAGES = ("en", "de")
_DEFAULT = "en"

_language = _DEFAULT
_listeners: list = []


def language() -> str:
    """The language new text is produced in: "en" (the default) or "de"."""
    return _language


def available_languages() -> tuple[str, ...]:
    return _LANGUAGES


def set_language(code: str) -> bool:
    """Switch language and tell every listener. Returns True when something actually changed."""
    global _language
    if code not in _LANGUAGES or code == _language:
        return False
    _language = code
    for listener in list(_listeners):
        try:
            listener()
        except Exception:   # noqa: BLE001 - a broken listener must not take the language switch down
            pass
    return True


def on_language_changed(listener) -> None:
    """Register a callback for the switch. The window uses this to relabel itself in place.

    PAIR THIS WITH off_language_changed. The list holds the callback strongly, so a bound method keeps its
    object alive: a window that registers here and never unregisters is never collected, and an application
    whose last window is gone on paper still has one in memory - which showed up as a test that started an
    app, quit it, and then hung forever waiting for a main loop that could not end."""
    if listener not in _listeners:
        _listeners.append(listener)


def off_language_changed(listener) -> None:
    """Drop a callback. Safe to call for one that was never registered."""
    try:
        _listeners.remove(listener)
    except ValueError:
        pass


def _(text: str) -> str:
    """The English text, or its German translation while the language is German."""
    if _language == "en":
        return text
    return _GERMAN.get(text, text)


def n_(text: str) -> str:
    """Marks a string for translation without translating it yet - for tables built at import time, whose
    entries have to be looked up again every time the language changes."""
    return text


# The table. Keys are the exact English strings that appear in the code; values are the German ones this
# program was originally written in. Format placeholders must match on both sides or the % will raise.
_GERMAN: dict[str, str] = {}


def add_translations(pairs: dict) -> None:
    """Modules register their own strings, so each table sits next to the code that uses it."""
    _GERMAN.update(pairs)


# The catalogue for everything outside the window. Imported last so this module has no import of its own
# to fail on: translations_de imports nothing at all.
from .translations_de import GERMAN as _GERMAN_TABLE   # noqa: E402

_GERMAN.update(_GERMAN_TABLE)
