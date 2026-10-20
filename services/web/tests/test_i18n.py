"""Interface languages: complete dictionaries, and no Dashboard wording for readers.

    cd services/web && python3 -m unittest discover -s tests -t .

The dictionary checks need only the standard library. The route checks are
skipped where fastapi and httpx are not installed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient

    HTTP_DEPS = True
except ImportError:  # pragma: no cover - depends on the environment
    HTTP_DEPS = False

APP_DIR = Path(__file__).resolve().parent.parent / "app"
LOCALES_DIR = APP_DIR / "locales"
LANGUAGES = ("en", "sv", "nb", "fi", "da")
PLACEHOLDER = re.compile(r"\{(\w+)\}")

PAGES = [APP_DIR / "static" / "index.html", APP_DIR / "static" / "login.html", APP_DIR / "admin" / "dashboard.html"]
SCRIPTS = [APP_DIR / "static" / "app.js", APP_DIR / "static" / "shell.js", APP_DIR / "admin" / "dashboard.js"]


def load(lang: str) -> dict:
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def placeholders(value: object) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(placeholders(v) for v in value.values()))
    return set(PLACEHOLDER.findall(str(value)))


class DictionaryTests(unittest.TestCase):
    """A missing or broken entry falls back to English silently, so it is caught here."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.locales = {lang: load(lang) for lang in LANGUAGES}
        cls.en = cls.locales["en"]

    def test_every_language_has_exactly_the_english_keys(self) -> None:
        for lang, entries in self.locales.items():
            with self.subTest(lang=lang):
                self.assertEqual(sorted(set(self.en) - set(entries)), [], "missing")
                self.assertEqual(sorted(set(entries) - set(self.en)), [], "not in en.json")

    def test_placeholders_match_english(self) -> None:
        # A translation that drops {name} prints a sentence about nothing; one
        # that invents {nmae} prints the braces.
        for lang, entries in self.locales.items():
            for key, english in self.en.items():
                with self.subTest(lang=lang, key=key):
                    self.assertEqual(placeholders(entries[key]), placeholders(english))

    def test_plurals_have_one_and_other(self) -> None:
        for lang, entries in self.locales.items():
            for key, english in self.en.items():
                if not isinstance(english, dict):
                    continue
                with self.subTest(lang=lang, key=key):
                    self.assertIsInstance(entries[key], dict)
                    self.assertEqual(set(entries[key]), {"one", "other"})
                    self.assertIn("{n}", entries[key]["other"] + entries[key]["one"])

    def test_nothing_is_empty(self) -> None:
        for lang, entries in self.locales.items():
            for key, value in entries.items():
                forms = value.values() if isinstance(value, dict) else [value]
                for form in forms:
                    with self.subTest(lang=lang, key=key):
                        self.assertTrue(str(form).strip())

    def test_every_key_the_pages_use_exists(self) -> None:
        attr = re.compile(r'data-i18n(?:-[a-z-]+)?="([^"]+)"')
        for page in PAGES:
            for key in attr.findall(page.read_text(encoding="utf-8")):
                with self.subTest(page=page.name, key=key):
                    self.assertIn(key, self.en)

    def test_every_key_the_scripts_use_exists(self) -> None:
        # Lang.t("x"), t("x") and the keys kept in lookup tables or data-i18n
        # attributes the scripts build: any quoted namespaced key.
        quoted = re.compile(r'["\']((?:common|nav|chat|voice|login|dashboard)\.[A-Za-z]+)["\']')
        for script in SCRIPTS:
            keys = set(quoted.findall(script.read_text(encoding="utf-8")))
            self.assertTrue(keys, script.name)
            for key in keys:
                with self.subTest(script=script.name, key=key):
                    self.assertIn(key, self.en)

    def test_every_dashboard_key_is_used(self) -> None:
        # The other way round: wording the Dashboard no longer shows would
        # otherwise linger in five languages, and be translated again for nothing.
        source = "".join(
            path.read_text(encoding="utf-8")
            for path in (APP_DIR / "admin" / "dashboard.html", APP_DIR / "admin" / "dashboard.js")
        )
        for key in (k for k in self.en if k.startswith("dashboard.")):
            with self.subTest(key=key):
                self.assertRegex(source, rf'["\']{re.escape(key)}["\']')

    def test_translations_are_translated(self) -> None:
        # Most entries should differ from English. Equal ones are fine
        # ("Status", "Chat", "{n} dokument"), but a file that is mostly English
        # was copied rather than translated.
        for lang in LANGUAGES[1:]:
            entries = self.locales[lang]
            same = [k for k, v in self.en.items() if entries[k] == v]
            with self.subTest(lang=lang):
                self.assertLess(len(same), len(self.en) * 0.1, same)


# Runs static/lang.js in Node with just enough of a browser around it, then
# prints Lang.regionsOf for each case. No npm packages: only Node's vm module.
REGIONS_SCRIPT = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {
  window: {},
  navigator: { languages: ["en-US"] },
  localStorage: { getItem: () => null, setItem() {} },
  document: { documentElement: { classList: { add() {}, remove() {} } }, addEventListener() {} },
  CustomEvent: function () {},
  Intl,
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const cases = JSON.parse(process.argv[2]);
const fn = ctx.window.Lang[process.argv[3]];
console.log(JSON.stringify(cases.map((c) => fn(c))));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class RegionsOfTests(unittest.TestCase):
    """Where "Answers for" starts comes from these regions, so their edge cases
    decide whether a Swede starts on Sweden."""

    CASES = {
        "first region in preference order": (["en-US", "sv-SE"], ["US", "SE"]),
        "a language alone names no country": (["sv"], []),
        "script subtags are skipped": (["zh-Hant-TW"], ["TW"]),
        "numeric UN regions are not countries": (["es-419"], []),
        "case and underscores are tolerated": (["nb-no", "da_DK"], ["NO", "DK"]),
        "duplicates collapse": (["sv-SE", "en-SE", "sv"], ["SE"]),
        "rubbish is ignored": (["", "x", "not a tag", None], []),
    }

    # Seen in testing: a Swede's browser listing en-US first started them on the
    # United States once the US was a market. English regions rank last.
    RANKED = {
        "a non-English region beats an English one": (["en-US", "sv-SE"], ["SE", "US"]),
        "English alone still counts": (["en-US"], ["US"]),
        "a Briton stays British": (["en-GB", "en"], ["GB"]),
        "browser order holds within each tier": (["en-US", "de-DE", "en-GB", "nb-NO"], ["DE", "NO", "US", "GB"]),
        "a region-less language adds nothing": (["en-US", "sv"], ["US"]),
    }

    def run_lang(self, function: str, cases: dict) -> None:
        inputs = [case[0] for case in cases.values()]
        out = subprocess.run(
            ["node", "-e", REGIONS_SCRIPT, str(APP_DIR / "static" / "lang.js"), json.dumps(inputs), function],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        results = json.loads(out.strip().splitlines()[-1])
        for (name, (_, expected)), got in zip(cases.items(), results):
            with self.subTest(name):
                self.assertEqual(got, expected)

    def test_regions_of(self) -> None:
        self.run_lang("regionsOf", self.CASES)

    def test_ranked_regions(self) -> None:
        self.run_lang("rankedRegions", self.RANKED)


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class StringsRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import auth, main

        self.main = main
        self.auth = auth

    def get(self, role: str, path: str = "/strings.js", **headers: str):
        with mock.patch.object(self.auth, "FORCED_ROLE", role):
            with TestClient(self.main.app) as client:
                return client.get(path, headers=headers)

    def payload(self, resp) -> dict:
        body = resp.text
        self.assertTrue(body.startswith("Lang.add(") and body.rstrip().endswith(");"))
        return json.loads(body.strip()[len("Lang.add(") : -2])

    def test_reader_gets_every_language_without_dashboard_wording(self) -> None:
        resp = self.get("reader")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("javascript", resp.headers["content-type"])
        data = self.payload(resp)
        self.assertEqual(sorted(data), sorted(LANGUAGES))
        for lang, entries in data.items():
            with self.subTest(lang=lang):
                self.assertFalse([k for k in entries if k.startswith(("dashboard.", "nav."))])
                self.assertIn("chat.answersFor", entries)
        self.assertNotIn("dashboard", resp.text.lower())

    def test_admin_also_gets_the_dashboard(self) -> None:
        data = self.payload(self.get("admin"))
        self.assertEqual(data["sv"]["nav.dashboard"], load("sv")["nav.dashboard"])
        self.assertEqual(len(data["en"]), len(load("en")))

    def test_the_two_copies_cannot_be_confused_in_a_cache(self) -> None:
        reader, admin = self.get("reader"), self.get("admin")
        self.assertNotEqual(reader.headers["etag"], admin.headers["etag"])
        self.assertEqual(reader.headers["cache-control"], "no-cache")
        again = self.get("reader", **{"if-none-match": reader.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        # A reader promoted to admin revalidates with the reader's ETag and must
        # get the admin copy, not a 304.
        promoted = self.get("admin", **{"if-none-match": reader.headers["etag"]})
        self.assertEqual(promoted.status_code, 200)

    def test_the_sign_in_page_can_load_its_language_before_sign_in(self) -> None:
        self.assertIn("/strings.js", self.auth.PUBLIC_PATHS)
        self.assertIn("/lang.js", self.auth.PUBLIC_PATHS)

    def test_reader_chat_page_and_engine_never_mention_the_dashboard(self) -> None:
        for path in ("/", "/lang.js"):
            with self.subTest(path=path):
                self.assertNotIn("dashboard", self.get("reader", path).text.lower())


if __name__ == "__main__":
    unittest.main()
