"""The translation layer: English is what stands in the code, German is a table on top of it.

These tests exist because the German half was produced mechanically from the original German source, and a
catalogue is exactly the kind of thing that rots quietly - a key that no longer matches its call site does
not raise, it just silently shows English. So the table is checked against the sources, not only itself.
"""
import atexit
import glob
import os
import re
import shutil
import sys
import tempfile
import unittest

# The settings and log modules read their paths from the environment AT IMPORT TIME, so every test module
# has to redirect them BEFORE it imports the package - and the redirect only takes for whichever module is
# imported first. Leaving this out made the whole suite run against the user's real settings file and log
# whenever this module happened to be loaded first, which showed up as unrelated tests hanging much later.
_TMP = tempfile.mkdtemp(prefix="teecst-i18n-test-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_TMP, "settings.json"))
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_TMP, "server.log"))
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from teecellstream import i18n, ui   # noqa: E402  - ui registers its own half of the table

SOURCES = {os.path.basename(path): open(path, encoding="utf-8").read()
           for path in glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                              "src", "teecellstream", "*.py"))}
ALL_SOURCE = "".join(SOURCES.values())

# No space flag: a literal "43 % faster" in prose would otherwise read as the format specifier %f.
_SPEC = re.compile(r"%[-#0+]*[\d.*]*[hlL]?([diouxXeEfFgGcrsa%])")


def specifiers(text: str) -> list:
    return [m.group(1) for m in _SPEC.finditer(text)]


class CatalogueTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.set_language, "en")

    def test_english_is_the_default(self):
        self.assertEqual("en", i18n.language())
        self.assertEqual("Stopped", i18n._("Stopped"))

    def test_german_translates_and_switches_back(self):
        i18n.set_language("de")
        self.assertEqual("Gestoppt", i18n._("Stopped"))
        i18n.set_language("en")
        self.assertEqual("Stopped", i18n._("Stopped"))

    def test_an_unknown_string_falls_back_to_english(self):
        i18n.set_language("de")
        self.assertEqual("nothing translates this", i18n._("nothing translates this"))

    def test_switching_tells_the_listeners_and_only_on_a_real_change(self):
        seen = []
        i18n.on_language_changed(lambda: seen.append(i18n.language()))
        self.assertTrue(i18n.set_language("de"))
        self.assertFalse(i18n.set_language("de"), "no change means no notification")
        self.assertFalse(i18n.set_language("klingon"))
        self.assertEqual(["de"], seen)

    def test_every_format_specifier_survives_translation(self):
        """A %d that turns into %s in the other language raises at the call site, in the other language
        only - which is exactly the bug nobody sees until a user switches."""
        for english, german in i18n._GERMAN.items():
            self.assertEqual(specifiers(english), specifiers(german),
                             "format specifiers differ:\n  en: %r\n  de: %r" % (english, german))

    def test_every_key_still_exists_in_the_sources(self):
        """A key nobody passes to _() any more is dead weight that reads as a translation."""
        orphans = []
        for english in i18n._GERMAN:
            # a string may be split across lines in the source, so the first fragment is what is searched
            head = english.split("\n")[0][:40]
            if head and head not in ALL_SOURCE:
                orphans.append(english)
        self.assertEqual([], orphans)

    def test_everything_handed_to_the_translator_has_a_translation(self):
        """The check that matters most: a string wrapped in _() but never added to the table shows English
        in both languages and says nothing about it. This caught 32 of them when the tables were written."""
        catalogue = set(i18n._GERMAN) | set(i18n._GERMAN.values())
        call = re.compile(r'_\(\s*"((?:[^"\\]|\\.)*)"')
        untranslated = []
        for name, text in SOURCES.items():
            if name in ("i18n.py", "translations_de.py"):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for match in call.finditer(line):
                    literal = match.group(1).replace("\\n", "\n")   # the source spells newlines out
                    # a message split over several source lines is catalogued as the joined string, so a
                    # leading fragment counts as covered when some key starts with it
                    if literal in catalogue or any(key.startswith(literal) for key in catalogue):
                        continue
                    untranslated.append("%s:%d %r" % (name, number, literal))
        self.assertEqual([], untranslated)

    def test_no_german_is_left_in_the_source_strings(self):
        """Everything the user can read has to be English in the code; German lives only in the tables."""
        table_files = ("translations_de.py", "i18n.py")
        umlaut = re.compile(r'"[^"\n]*[äöüßÄÖÜ][^"\n]*"')
        offenders = []
        for name, text in SOURCES.items():
            if name in table_files:
                continue
            for line in text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#") or stripped.startswith('"""'):
                    continue
                for match in umlaut.finditer(line):
                    # the source spells a newline as a backslash and an n; the table holds the real one
                    literal = match.group(0).strip('"').replace("\\n", "\n")
                    # ui.py's own catalogue block: a long German value is written as several adjacent
                    # string fragments, so a fragment counts as part of the table, not as a stray string
                    if any(literal in value for value in i18n._GERMAN.values()):
                        continue
                    offenders.append("%s: %s" % (name, literal))
        self.assertEqual([], offenders)


class LanguageRowTest(unittest.TestCase):
    def test_the_codes_and_labels_line_up(self):
        self.assertEqual(len(ui.LANGUAGE_CODES), len(ui.LANGUAGE_LABELS))
        self.assertEqual(tuple(i18n.available_languages()), ui.LANGUAGE_CODES)

    def test_the_bitrate_list_follows_the_language(self):
        self.addCleanup(i18n.set_language, "en")
        self.assertIn(" (recommended)", "".join(ui.bitrate_labels()))
        i18n.set_language("de")
        self.assertIn(" (empfohlen)", "".join(ui.bitrate_labels()))

    def test_the_status_line_follows_the_language(self):
        self.addCleanup(i18n.set_language, "en")
        self.assertEqual("Stopped", ui.status_text(False, False, ""))
        i18n.set_language("de")
        self.assertEqual("Gestoppt", ui.status_text(False, False, ""))
        self.assertEqual("PS3 verbunden: 10.0.0.1", ui.status_text(True, True, "10.0.0.1"))


if __name__ == "__main__":
    unittest.main()
