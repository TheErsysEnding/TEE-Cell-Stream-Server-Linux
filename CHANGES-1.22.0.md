# 1.22.0 — English by default, German at the flick of a row

## What changed

The program used to be German throughout. It is now **English by default**, with *System → Language*
switching to German and back **while it runs** — no restart, no reload.

## How it is built

**English is the source language.** The strings in the code *are* the English text; German is a table on
top of them. A key that goes missing therefore degrades to English rather than to something like
`ui.status.waiting`, which is the failure mode that makes home-grown translation layers unpleasant.

Not gettext: that wants .po/.mo files compiled at build time and installed under `/usr/share/locale`, and
switching language at runtime means re-binding the domain and rebuilding every widget anyway. The table is
the whole mechanism, it ships inside the package, and the only part that is real work — telling the window
to relabel itself — has to be written either way.

Two tables, deliberately:

- `ui.py` registers its own strings, right next to the widgets they label.
- `translations_de.py` carries everything else — log lines, notifications, fallback reasons — so no module
  needs a catalogue block sitting on top of its code.

## Switching live

`_apply_language` throws both pages away and builds them again rather than walking widget by widget. Every
label then comes from the same code that created it in the first place, so **a row added later cannot be
forgotten in the relabel path** — the class of bug that makes runtime language switching rot.

Nothing is lost in the rebuild: everything the pages show is read back either from the server
(`_sync_choices`) or from the settings file. The one exception is a keystroke typed into a command field
within the last 400 ms, so the pending saves are flushed before the rebuild.

## What translates and what does not

The window relabels completely. **Log lines are translated when they are written**, so lines already in the
log keep the language they were written in. That is deliberate: a log is a record of what happened, not a
view that can be re-rendered — and it is what keeps the log usable as evidence when a measurement is
quoted somewhere else.

## Also English now

The `.desktop` entry (with `Name[de]`/`Comment[de]` where the format supports it), the bundled GNOME
extension's description, and the messages the installer prints.

## Tests

`tests/test_i18n.py`, because a catalogue is exactly the kind of thing that rots quietly — a key that no
longer matches its call site does not raise, it just silently shows English. So the table is checked
against the sources, not only against itself:

- **every format specifier survives translation.** A `%d` that becomes `%s` in the other language raises at
  the call site *in that language only* — the bug nobody sees until a user switches.
- **every key still exists in the sources**, so a dead key cannot masquerade as a translation.
- **no German string outside the tables**, so nothing user-visible can be added in the wrong language.

The existing suite was updated to assert the English text.
