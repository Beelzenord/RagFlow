// Interface language: which one, the words for it, and formatting in it.
//
// Loaded in <head>, before the page draws, followed by /strings.js, which hands
// over the dictionaries (services/web/app/locales/*.json). The HTML carries the
// English text itself, so a page whose dictionaries failed to load still reads
// correctly - just in English.
//
// The order a language is chosen in: what this person picked, then their
// browser's languages, then English.

(function () {
  const KEY = "rag.lang.v1";
  const SUPPORTED = ["en", "sv", "nb", "fi", "da"];
  // Each language named in itself, as a picker should show it.
  const NAMES = { en: "English", sv: "Svenska", nb: "Norsk", fi: "Suomi", da: "Dansk" };
  // Norwegian arrives as "no", "nb" or "nn"; the dictionary is Bokmål.
  const ALIASES = { no: "nb", nn: "nb" };
  const ATTRS = [
    ["data-i18n-placeholder", "placeholder"],
    ["data-i18n-aria-label", "aria-label"],
    ["data-i18n-title", "title"],
  ];
  const dicts = {};

  // The first supported language in a browser-style preference list.
  function pick(list) {
    for (const tag of list || []) {
      const base = String(tag || "").toLowerCase().split(/[-_]/)[0];
      const code = ALIASES[base] || base;
      if (SUPPORTED.includes(code)) return code;
    }
    return "en";
  }

  function stored() {
    try {
      const value = localStorage.getItem(KEY);
      return SUPPORTED.includes(value) ? value : null;
    } catch {
      return null;
    }
  }

  function browserLanguages() {
    if (navigator.languages && navigator.languages.length) return navigator.languages;
    return navigator.language ? [navigator.language] : [];
  }

  const root = document.documentElement;
  let current = stored() || pick(browserLanguages());
  root.lang = current;
  // Hidden until translated, so a Swedish visitor never sees the English that
  // is in the HTML. apply() always lifts it, dictionaries or not.
  if (current !== "en") root.classList.add("i18n-pending");

  function entry(key) {
    if (dicts[current] && key in dicts[current]) return dicts[current][key];
    if (dicts.en && key in dicts.en) return dicts.en[key];
    return undefined;
  }

  function fill(text, vars) {
    if (!vars) return text;
    return text.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? String(vars[name]) : match));
  }

  // The text for `key` in the current language, falling back to English and
  // then to the key itself. A plural entry ({one, other}) is chosen by vars.n.
  function t(key, vars) {
    let value = entry(key);
    if (value === undefined) return key;
    if (typeof value === "object") {
      let form = "other";
      try {
        form = new Intl.PluralRules(current).select(Number(vars?.n ?? 0));
      } catch {
        /* keep "other" */
      }
      value = value[form] ?? value.other ?? "";
    }
    return fill(value, vars);
  }

  // Translates every marked element under `scope`. An element whose key no
  // dictionary has keeps the English already in the page.
  function apply(scope) {
    const base = scope || document;
    base.querySelectorAll("[data-i18n]").forEach((node) => {
      const value = entry(node.dataset.i18n);
      if (value !== undefined) node.textContent = t(node.dataset.i18n);
    });
    for (const [attr, target] of ATTRS) {
      base.querySelectorAll(`[${attr}]`).forEach((node) => {
        const key = node.getAttribute(attr);
        if (entry(key) !== undefined) node.setAttribute(target, t(key));
      });
    }
    root.classList.remove("i18n-pending");
  }

  let regionNames = null;
  let regionNamesFor = null;

  // A country's name in the current language - "Sverige", "Ruotsi" - from the
  // browser's own data, so the ISO list needs no translating. Falls back to the
  // name the server stored, in English.
  function region(code, fallback) {
    try {
      if (regionNamesFor !== current) {
        regionNames = new Intl.DisplayNames([current], { type: "region" });
        regionNamesFor = current;
      }
      const name = regionNames.of(code);
      if (name && name !== code) return name;
    } catch {
      /* fall through */
    }
    return fallback || code;
  }

  function date(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleDateString(current, { year: "numeric", month: "short", day: "numeric" });
  }

  function compare(a, b) {
    return String(a).localeCompare(String(b), current);
  }

  // Switches without a reload, so a conversation on screen survives it. Pages
  // re-render what they built in code on "langchange".
  function set(code) {
    if (!SUPPORTED.includes(code) || code === current) return;
    current = code;
    try {
      localStorage.setItem(KEY, code);
    } catch {
      /* the choice lasts for this page only */
    }
    root.lang = code;
    apply();
    document.dispatchEvent(new CustomEvent("langchange", { detail: code }));
  }

  function add(byLanguage) {
    for (const [lang, entries] of Object.entries(byLanguage || {})) {
      dicts[lang] = Object.assign(dicts[lang] || {}, entries);
    }
  }

  window.Lang = {
    supported: SUPPORTED,
    names: NAMES,
    get current() {
      return current;
    },
    pick,
    t,
    apply,
    region,
    date,
    compare,
    set,
    add,
  };

  document.addEventListener("DOMContentLoaded", () => apply());
})();
