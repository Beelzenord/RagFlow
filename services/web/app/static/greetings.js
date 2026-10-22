// Which line the empty chat box shows next. Pure: app.js keeps the saved state
// (in localStorage) and does the animating; tests/test_greetings.py runs this
// file in Node.
//
// The general lines go round as a shuffled bag - each once before any repeats,
// never the same twice in a row. A line that fits the time ("Good morning") can
// open a visit, once a day each.

(function () {
  const LINES = [
    "chat.placeholder",
    "chat.lineHi",
    "chat.lineGoodToSee",
    "chat.lineAskAway",
    "chat.lineBigIdea",
    "chat.lineCoffee",
    "chat.lineGreatBusiness",
    "chat.lineBoldPlans",
    "chat.lineFinePrint",
  ];

  // By the browser's clock. The day-specific ones come first: they are rarer
  // than a time of day, so they win when both fit.
  const TIMED = [
    { key: "chat.lineMonday", when: (d) => d.getDay() === 1 && d.getHours() < 12 },
    { key: "chat.lineFriday", when: (d) => d.getDay() === 5 && d.getHours() >= 12 },
    { key: "chat.lineWeekend", when: (d) => d.getDay() === 0 || d.getDay() === 6 },
    { key: "chat.lineMorning", when: (d) => d.getHours() >= 5 && d.getHours() < 11 },
    { key: "chat.lineEvening", when: (d) => d.getHours() >= 18 && d.getHours() < 23 },
    { key: "chat.lineNight", when: (d) => d.getHours() >= 23 || d.getHours() < 5 },
  ];
  const TIMED_KEYS = TIMED.map((t) => t.key);

  function day(d) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function timedFor(date) {
    return TIMED.filter((t) => t.when(date)).map((t) => t.key);
  }

  // Whatever was stored, as something safe to use: lines this version no
  // longer has are dropped, and anything malformed starts afresh.
  function clean(saved) {
    const s = saved && typeof saved === "object" ? saved : {};
    const bag = Array.isArray(s.bag) ? s.bag.filter((k, i, all) => LINES.includes(k) && all.indexOf(k) === i) : [];
    const last = LINES.includes(s.last) || TIMED_KEYS.includes(s.last) ? s.last : null;
    const timedShown = {};
    if (s.timedShown && typeof s.timedShown === "object") {
      for (const key of TIMED_KEYS) {
        if (typeof s.timedShown[key] === "string") timedShown[key] = s.timedShown[key];
      }
    }
    return { bag, last, timedShown };
  }


  function shuffle(list, random) {
    const out = [...list];
    for (let i = out.length - 1; i > 0; i--) {
      const j = Math.floor(random() * (i + 1));
      [out[i], out[j]] = [out[j], out[i]];
    }
    return out;
  }

  // A fresh round that does not open with the line just shown. Almost every
  // shuffle qualifies; if a broken random() keeps failing, LINES rotated does.
  function refill(last, random) {
    for (let tries = 0; tries < 40; tries++) {
      const bag = shuffle(LINES, random);
      if (bag[0] !== last) return bag;
    }
    const at = LINES.indexOf(last);
    return at < 0 ? [...LINES] : [...LINES.slice(at + 1), ...LINES.slice(0, at + 1)];
  }

  // The key to show now, and the state to store for next time. `first` is a
  // visit's opening line, the one moment a time-of-day greeting may take.
  function next(saved, date = new Date(), { first = false, random = Math.random } = {}) {
    const s = clean(saved);
    if (first) {
      const today = day(date);
      const timed = timedFor(date).find((key) => s.timedShown[key] !== today);
      if (timed) {
        return { key: timed, saved: { ...s, last: timed, timedShown: { ...s.timedShown, [timed]: today } } };
      }
    }
    // A leftover bag that would repeat the last line - only possible from
    // tampered or stale state - is replaced rather than trusted.
    const bag = s.bag.length && s.bag[0] !== s.last ? s.bag : refill(s.last, random);
    const key = bag[0];
    return { key, saved: { bag: bag.slice(1), last: key, timedShown: s.timedShown } };
  }

  window.Greetings = { LINES, TIMED_KEYS, timedFor, next };
})();
