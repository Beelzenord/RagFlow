"""The chat box's rolling greetings: which line comes next.

    cd services/web && python3 -m unittest discover -s tests -t .

Runs static/greetings.js in Node's vm module - no npm packages - with fixed
dates, and checks the round (every line once, no repeats) and the time-of-day
lines. Skipped where node is not installed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"

SCRIPT = r"""
const fs = require("fs"), vm = require("vm");
const ctx = { window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const G = ctx.window.Greetings;
// Local time, so the hours are what the browser's clock would say.
const at = (m, d, h) => new Date(2026, m - 1, d, h, 0);
// Tuesday 20 October 2026, 14:00: no time of day line fits.
const quiet = at(10, 20, 14);
const copy = (x) => JSON.parse(JSON.stringify(x));
const out = { lines: G.LINES, timed: G.TIMED_KEYS };

let saved = null;
out.seq = [];
for (let i = 0; i < G.LINES.length * 6; i++) {
  const r = G.next(saved, quiet);
  out.seq.push(r.key);
  saved = copy(r.saved);
}

out.timedFor = {
  tue07: G.timedFor(at(10, 20, 7)),
  mon09: G.timedFor(at(10, 19, 9)),
  fri15: G.timedFor(at(10, 23, 15)),
  wed01: G.timedFor(at(10, 21, 1)),
  sat14: G.timedFor(at(10, 24, 14)),
  tue14: G.timedFor(quiet),
  tue20: G.timedFor(at(10, 20, 20)),
};

// Visits: 07:00, a second line in that visit, another visit at 08:00, and the
// next morning.
let s = null;
const visits = [];
for (const [date, first] of [[at(10, 20, 7), true], [at(10, 20, 7), false], [at(10, 20, 8), true], [at(10, 21, 7), true]]) {
  const r = G.next(s, date, { first });
  visits.push(r.key);
  s = copy(r.saved);
}
out.morning = visits;

let m = null;
out.monday = [];
for (let i = 0; i < 3; i++) {
  const r = G.next(m, at(10, 19, 9), { first: true });
  out.monday.push(r.key);
  m = copy(r.saved);
}

out.junk = [null, 5, "x", [], {
  bag: ["nope", "chat.lineHi", "chat.lineHi"],
  last: 42,
  timedShown: { "chat.lineMorning": 7, evil: "2026-01-01" },
}].map((j) => copy(G.next(j, quiet)));

out.broken = [() => 0, () => 0.999999].map((random) => G.next(null, quiet, { random }).key);
out.stale = G.next({ bag: ["chat.lineHi", "chat.lineCoffee"], last: "chat.lineHi" }, quiet).key;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class GreetingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        out = subprocess.run(
            ["node", "-e", SCRIPT, str(APP_DIR / "static" / "greetings.js")],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        cls.r = json.loads(out.strip().splitlines()[-1])
        cls.lines = cls.r["lines"]

    def test_never_the_same_line_twice_in_a_row(self) -> None:
        seq = self.r["seq"]
        for a, b in zip(seq, seq[1:]):
            self.assertNotEqual(a, b)

    def test_each_round_shows_every_line_once(self) -> None:
        seq, n = self.r["seq"], len(self.lines)
        for i in range(0, len(seq), n):
            with self.subTest(round=i // n):
                self.assertEqual(sorted(seq[i:i + n]), sorted(self.lines))

    def test_time_windows(self) -> None:
        t = self.r["timedFor"]
        self.assertEqual(t["tue07"], ["chat.lineMorning"])
        # Monday is rarer than a morning, so it comes first.
        self.assertEqual(t["mon09"], ["chat.lineMonday", "chat.lineMorning"])
        self.assertEqual(t["fri15"], ["chat.lineFriday"])
        self.assertEqual(t["wed01"], ["chat.lineNight"])
        self.assertEqual(t["sat14"], ["chat.lineWeekend"])
        self.assertEqual(t["tue14"], [])
        self.assertEqual(t["tue20"], ["chat.lineEvening"])

    def test_a_time_line_opens_a_visit_once_a_day(self) -> None:
        first, second_line, later_visit, next_day = self.r["morning"]
        self.assertEqual(first, "chat.lineMorning")
        self.assertIn(second_line, self.lines)
        self.assertIn(later_visit, self.lines)
        self.assertEqual(next_day, "chat.lineMorning")
        monday, morning, general = self.r["monday"]
        self.assertEqual((monday, morning), ("chat.lineMonday", "chat.lineMorning"))
        self.assertIn(general, self.lines)

    def test_stored_state_cannot_break_it(self) -> None:
        for got in self.r["junk"]:
            with self.subTest(got=got):
                self.assertIn(got["key"], self.lines)
                self.assertTrue(set(got["saved"]["bag"]) <= set(self.lines))
                for key, value in got["saved"]["timedShown"].items():
                    self.assertIn(key, self.r["timed"])
                    self.assertIsInstance(value, str)
        for key in self.r["broken"]:
            self.assertIn(key, self.lines)
        # A stored bag that would repeat the last line is not trusted.
        self.assertNotEqual(self.r["stale"], "chat.lineHi")

    def test_every_line_has_words(self) -> None:
        en = json.loads((APP_DIR / "locales" / "en.json").read_text(encoding="utf-8"))
        for key in self.lines + self.r["timed"]:
            with self.subTest(key=key):
                self.assertIn(key, en)


if __name__ == "__main__":
    unittest.main()
