// Dashboard - the document library: every document, who each one is for, and
// the markets (countries and groups) that decide it.
//
// Served only to admins (see /dashboard in main.py); every endpoint it calls
// checks the role again. All server text is rendered with textContent, and all
// of the page's own words come from Lang.t (static/lang.js, app/locales).
//
// The sidebar lists the markets like folders. Picking one is the hash route
// (#all, #global, #group/<id>, #country/SE, #country/SE/sees), so Back walks
// through them; dropping documents on one makes them for it. Below them,
// #insights shows what people ask and what the documents do not answer.

const PAGE_SIZE = 25;
const TICK_MS = 3000;
const SORT_KEY = "rag.dash.sort.v1";
// The orders ingestion accepts (SORTS in services/ingestion/app/audience.py).
const SORTS = ["uploaded_desc", "uploaded_asc", "name_asc", "name_desc"];
const COUNTRY_KEY = /^[A-Z]{2}$/;
const GROUP_KEY = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// What a dragged row carries, so a drop can tell documents from files dragged
// in from the desktop - those are uploads.
const DRAG_TYPE = "application/x-rag-documents";

const state = {
  markets: { countries: [], groups: [], global_documents: 0, total_documents: 0 },
  marketsLoaded: false,
  catalogue: null,
  view: { kind: "all" },
  docs: [],
  docsLoaded: false,
  total: 0,
  offset: 0,
  q: "",
  sort: "uploaded_desc",
  selected: new Set(),
  // The row the keyboard is on, as an index into state.docs.
  active: -1,
  detail: null,
  previewUrl: null,
  queue: [],
  queueBusy: false,
  nextQid: 1,
  tickHandle: null,
  trayOpen: true,
  // #insights: its filters, the last answer from /api/insights, and which
  // questions are expanded (by wording).
  insights: { days: 30, market: "", show: "attention", data: null, error: null, open: new Set() },
  // Questions needing attention in the last 30 days, for the sidebar; null
  // until known, or when there is no question log.
  attentionCount: null,
};

const $ = (id) => document.getElementById(id);

const els = {
  rail: $("rail"),
  railClose: $("rail-close"),
  railScrim: $("rail-scrim"),
  railTop: $("rail-top"),
  railGroups: $("rail-groups"),
  railCountries: $("rail-countries"),
  railInsights: $("rail-insights"),
  newGroupBtn: $("new-group-btn"),
  addCountryBtn: $("add-country-btn"),
  libMain: $("lib-main"),
  railOpen: $("rail-open"),
  viewIcon: $("view-icon"),
  viewName: $("view-name"),
  viewCode: $("view-code"),
  viewCount: $("view-count"),
  viewActions: $("view-actions"),
  viewSub: $("view-sub"),
  searchIcon: $("search-icon"),
  search: $("doc-search"),
  toolbar: $("toolbar"),
  tableCard: $("table-card"),
  insights: $("insights"),
  uploadBtn: $("upload-btn"),
  uploadIcon: $("upload-icon"),
  uploadOptionsBtn: $("upload-options-btn"),
  uploadOptions: $("upload-options"),
  collectionInput: $("collection-input"),
  fileInput: $("file-input"),
  tableWrap: $("table-wrap"),
  selectAll: $("select-all"),
  thName: $("th-name"),
  thDate: $("th-date"),
  rows: $("doc-rows"),
  docsEmpty: $("docs-empty"),
  pageInfo: $("page-info"),
  prev: $("prev-page"),
  next: $("next-page"),
  shortcutsBtn: $("shortcuts-btn"),
  dropzone: $("dropzone"),
  dropzoneIcon: $("dropzone-icon"),
  detail: $("detail"),
  detailName: $("detail-name"),
  detailClose: $("detail-close"),
  detailMeta: $("detail-meta"),
  preview: $("preview"),
  detailEditor: $("detail-editor"),
  detailStatus: $("detail-status"),
  detailSave: $("detail-save"),
  detailDownload: $("detail-download"),
  detailDelete: $("detail-delete"),
  bulkBar: $("bulk-bar"),
  bulkCount: $("bulk-count"),
  bulkAudienceBtn: $("bulk-audience-btn"),
  bulkDeleteBtn: $("bulk-delete-btn"),
  bulkClearBtn: $("bulk-clear-btn"),
  queue: $("queue"),
  trayIcon: $("tray-icon"),
  queueSummary: $("queue-summary"),
  queueToggle: $("queue-toggle"),
  queueBody: $("queue-body"),
  queueList: $("queue-list"),
  queueClearBtn: $("queue-clear-btn"),
  toasts: $("toasts"),
  audienceDialog: $("audience-dialog"),
  audienceDialogTitle: $("audience-dialog-title"),
  dialogEditor: $("dialog-editor"),
  audienceDialogStatus: $("audience-dialog-status"),
  audienceDialogCancel: $("audience-dialog-cancel"),
  audienceDialogSave: $("audience-dialog-save"),
  groupDialog: $("group-dialog"),
  groupDialogTitle: $("group-dialog-title"),
  groupName: $("group-name"),
  groupFilter: $("group-filter"),
  groupChecklist: $("group-checklist"),
  groupWarning: $("group-warning"),
  groupDialogStatus: $("group-dialog-status"),
  groupDialogCancel: $("group-dialog-cancel"),
  groupDialogSave: $("group-dialog-save"),
  countryDialog: $("country-dialog"),
  countrySearch: $("country-search"),
  countryResults: $("country-results"),
  countryDialogClose: $("country-dialog-close"),
  confirmDialog: $("confirm-dialog"),
  confirmTitle: $("confirm-title"),
  confirmBody: $("confirm-body"),
  confirmOk: $("confirm-ok"),
  confirmCancel: $("confirm-cancel"),
  shortcutsDialog: $("shortcuts-dialog"),
  shortcutsClose: $("shortcuts-close"),
};

// --- Small helpers -------------------------------------------------------------

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c);
  }
  return node;
}

const t = (key, vars) => Lang.t(key, vars);

// Accent-insensitive matching, so "aland" finds Åland and "cote" Côte d'Ivoire.
function fold(s) {
  return String(s || "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

function setStatus(node, text, kind) {
  node.textContent = text || "";
  node.className = `status ${kind || ""}`.trim();
}

function errorText(data, status) {
  const d = data?.detail ?? data?.error;
  if (typeof d === "string") return d;
  if (d && typeof d.message === "string") return d.message;
  if (Array.isArray(d) && d.length) return d.map((e) => e?.msg || String(e)).join("; ");
  return t("dashboard.somethingWrong", { status });
}

async function api(path, { method = "GET", body } = {}) {
  const init = { method };
  if (body !== undefined) {
    init.headers = { "content-type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(path, init);
  if (Shell.bounceIfLoggedOut(resp.status)) throw new Error("signed out");
  // The role changed under an open tab; the chat is what this account may use.
  if (resp.status === 403) {
    location.href = "/";
    throw new Error("not an admin");
  }
  let data = null;
  try {
    data = await resp.json();
  } catch {
    /* empty body */
  }
  if (!resp.ok) {
    const err = new Error(errorText(data, resp.status));
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function isIndexed(status) {
  return status === "completed" || status === "degraded";
}

function isTerminal(status) {
  return isIndexed(status) || status === "failed";
}

const STATUS_KEYS = {
  uploaded: "dashboard.statusUploaded",
  processing: "dashboard.statusProcessing",
  completed: "dashboard.statusReady",
  degraded: "dashboard.statusReady",
  failed: "dashboard.statusFailed",
  uploading: "dashboard.statusUploading",
};

function statusLabel(status) {
  return STATUS_KEYS[status] ? t(STATUS_KEYS[status]) : status || t("dashboard.statusUploaded");
}

function statusBadge(doc) {
  const badge = el("span", {
    class: `badge ${doc.status || "uploaded"}`,
    text: statusLabel(doc.status),
  });
  if (doc.status === "degraded") {
    badge.title = t("dashboard.degradedNote");
  } else if (doc.status === "failed" && doc.error_message) {
    badge.title = doc.error_message;
  }
  return badge;
}

function formatDate(iso) {
  return Lang.date(iso);
}

function formatBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

// The stored original, as uploaded. A download unless ?inline=true is added.
function fileUrl(doc) {
  return `/api/documents/${encodeURIComponent(doc.id)}/file`;
}

function isImage(doc) {
  return (doc.file_type || "").toLowerCase().startsWith("image/");
}

// Rebuilding part of the page drops keyboard focus; this puts it back on the
// control with the same data-focus-key, when that control is still there.
function keepFocus(rebuild) {
  const key = document.activeElement?.dataset?.focusKey;
  rebuild();
  if (key) document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`)?.focus();
}

// In the admin's language ("Sverige", "Ruotsi"), from the browser's own country
// names; the English name the server stored is the fallback.
function countryName(code) {
  return Lang.region(code, state.markets.countries.find((c) => c.code === code)?.name);
}

// Market countries sorted by their name in the current language.
function sortedCountries() {
  return [...state.markets.countries].sort((a, b) => Lang.compare(countryName(a.code), countryName(b.code)));
}

// --- Icons and flags -------------------------------------------------------------
// Line icons drawn in currentColor. The markup is constant - never user text.

const ICONS = {
  all: '<path d="M12 3 3 8l9 5 9-5-9-5Z"/><path d="m3 13 9 5 9-5"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18"/>',
  group: '<circle cx="8.5" cy="9" r="3"/><circle cx="16" cy="10" r="2.5"/><path d="M3 19c.8-3 2.9-4.6 5.5-4.6S13.2 16 14 19"/><path d="M14.5 14.6c.5-.2 1-.3 1.5-.3 2 0 3.6 1.3 4.3 3.9"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
  download: '<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
  upload: '<path d="M12 20V9"/><path d="m7 14 5-5 5 5"/><path d="M5 4h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  more: '<circle cx="5" cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="19" cy="12" r="1.4" fill="currentColor"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
  trash: '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/>',
  questions: '<path d="M20.5 12a8.5 8.5 0 0 1-12.4 7.5L3.5 20.5l1-4.4A8.5 8.5 0 1 1 20.5 12Z"/><path d="M9.7 9.6a2.4 2.4 0 1 1 3.3 2.2c-.6.3-1 .8-1 1.4v.4"/><circle cx="12" cy="16.4" r=".5" fill="currentColor"/>',
  thumbDown: '<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>',
};

function icon(name, cls = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.innerHTML = ICONS[name] || "";
  return svg;
}

function flagEmoji(code) {
  return String.fromCodePoint(...[...code].map((ch) => 0x1f1e6 + ch.charCodeAt(0) - 65));
}

// Windows draws a flag emoji as two letters, so whether this browser can show
// flags is tested once: drawn in colour, a flag has coloured pixels.
let flagSupport = null;

function flagsSupported() {
  if (flagSupport !== null) return flagSupport;
  flagSupport = false;
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 20;
    canvas.height = 20;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.textBaseline = "top";
    ctx.font = "16px sans-serif";
    ctx.fillText(flagEmoji("SE"), 0, 0);
    const px = ctx.getImageData(0, 0, 20, 20).data;
    for (let i = 0; i < px.length; i += 4) {
      const [r, g, b, a] = [px[i], px[i + 1], px[i + 2], px[i + 3]];
      if (a > 100 && Math.max(r, g, b) - Math.min(r, g, b) > 60) {
        flagSupport = true;
        break;
      }
    }
  } catch {
    /* letters it is */
  }
  return flagSupport;
}

function flagNode(code) {
  return flagsSupported()
    ? el("span", { class: "flag", "aria-hidden": "true", text: flagEmoji(code) })
    : el("span", { class: "flag flag-code", "aria-hidden": "true", text: code });
}

// The mark beside a market's name: its flag, or an icon.
function viewMark(view) {
  if (view.kind === "country") return flagNode(view.key);
  if (view.kind === "insights") return icon("questions");
  return icon(view.kind === "global" ? "globe" : view.kind === "group" ? "group" : "all");
}

// --- Who it's for: chips ---------------------------------------------------------

function globalChip() {
  return el("span", { class: "chip chip-global" }, icon("globe"), el("span", { text: t("dashboard.global") }));
}

function audienceChips(audience, max = 2) {
  const wrap = el("span", { class: "chips" });
  if (!audience || audience.everyone) {
    wrap.append(globalChip());
    return wrap;
  }
  const items = [
    ...audience.groups.map((g) => ({
      label: g.name,
      title: (g.members || []).map(countryName).join(", "),
      cls: "chip chip-group",
      mark: () => icon("group"),
    })),
    ...audience.countries.map((c) => ({
      label: countryName(c.code),
      title: c.code,
      cls: "chip",
      mark: () => flagNode(c.code),
    })),
  ];
  for (const it of items.slice(0, max)) {
    wrap.append(el("span", { class: it.cls, title: it.title }, it.mark(), el("span", { text: it.label })));
  }
  if (items.length > max) {
    const rest = items.slice(max).map((it) => it.label).join(", ");
    wrap.append(el("span", { class: "chip chip-more", text: `+${items.length - max}`, title: rest }));
  }
  return wrap;
}

// --- Who it's for: the editor ----------------------------------------------------

let editorSeq = 0;

// One "Who is this for?" control, used in the detail panel and the bulk dialog.
// Two plain choices instead of a list box that needs Cmd/Ctrl: Everyone, or
// only the markets ticked below - with a "Covers" line spelling out what that
// means once groups are expanded.
function createAudienceEditor({ onChange } = {}) {
  const name = `audience-${++editorSeq}`;
  const picked = { countries: new Set(), groups: new Set() };
  let everyone = true;

  const everyoneInput = el("input", { type: "radio", name, value: "everyone" });
  const onlyInput = el("input", { type: "radio", name, value: "only" });
  const filter = el("input", {
    type: "search",
    placeholder: t("dashboard.filterMarkets"),
    "aria-label": t("dashboard.filterMarkets"),
  });
  const chips = el("div", { class: "chips picked", "aria-live": "polite" });
  const options = el("div", { class: "checklist" });
  const covers = el("p", { class: "covers" });
  const empty = el("p", { class: "hint", hidden: true });
  const pick = el("div", { class: "aud-pick" }, chips, filter, options, covers, empty);

  const root = el(
    "fieldset",
    { class: "audience" },
    el("legend", { "data-i18n": "dashboard.whoLegend", text: t("dashboard.whoLegend") }),
    el(
      "label",
      { class: "choice" },
      everyoneInput,
      el(
        "span",
        {},
        el("strong", { "data-i18n": "dashboard.everyone", text: t("dashboard.everyone") }),
        el("small", { "data-i18n": "dashboard.everyoneHint", text: t("dashboard.everyoneHint") }),
      ),
    ),
    el(
      "label",
      { class: "choice" },
      onlyInput,
      el(
        "span",
        {},
        el("strong", { "data-i18n": "dashboard.onlyThese", text: t("dashboard.onlyThese") }),
        el("small", { "data-i18n": "dashboard.onlyTheseHint", text: t("dashboard.onlyTheseHint") }),
      ),
    ),
    pick,
  );

  function selection() {
    return {
      everyone,
      countries: [...picked.countries],
      groups: [...picked.groups],
    };
  }

  function valid() {
    return everyone || picked.countries.size > 0 || picked.groups.size > 0;
  }

  function coveredCodes() {
    const codes = new Set(picked.countries);
    for (const id of picked.groups) {
      const g = state.markets.groups.find((x) => x.id === id);
      for (const c of g?.members || []) codes.add(c);
    }
    return [...codes];
  }

  function toggle(kind, value, on) {
    const set = kind === "group" ? picked.groups : picked.countries;
    if (on) set.add(value);
    else set.delete(value);
    render();
    onChange?.();
  }

  function renderOptions() {
    const f = fold(filter.value.trim());
    const match = (label, extra = "") => !f || fold(label).includes(f) || fold(extra).includes(f);
    const nodes = [];
    const groups = state.markets.groups.filter((g) =>
      match(g.name, (g.members || []).map(countryName).join(" ")),
    );
    const countries = sortedCountries().filter((c) => match(countryName(c.code), `${c.name} ${c.code}`));
    if (groups.length) {
      nodes.push(el("p", { class: "checklist-head", text: t("dashboard.groupsHeading") }));
      for (const g of groups) {
        const box = el("input", {
          type: "checkbox",
          checked: picked.groups.has(g.id),
          onchange: (e) => toggle("group", g.id, e.target.checked),
        });
        nodes.push(
          el(
            "label",
            { class: "check" },
            box,
            el("span", { text: g.name }),
            el("small", { text: (g.members || []).map(countryName).join(", ") }),
          ),
        );
      }
    }
    if (countries.length) {
      nodes.push(el("p", { class: "checklist-head", text: t("dashboard.countriesHeading") }));
      for (const c of countries) {
        const box = el("input", {
          type: "checkbox",
          checked: picked.countries.has(c.code),
          onchange: (e) => toggle("country", c.code, e.target.checked),
        });
        nodes.push(
          el("label", { class: "check" }, box, el("span", { text: countryName(c.code) }), el("small", { text: c.code })),
        );
      }
    }
    if (!nodes.length && f) nodes.push(el("p", { class: "hint", text: t("dashboard.noMatch") }));
    options.replaceChildren(...nodes);
  }

  function renderChips() {
    const nodes = [];
    for (const id of picked.groups) {
      const g = state.markets.groups.find((x) => x.id === id);
      nodes.push(removableChip(g?.name || t("dashboard.removedGroup"), "chip chip-group", () => toggle("group", id, false)));
    }
    for (const code of picked.countries) {
      nodes.push(removableChip(countryName(code), "chip", () => toggle("country", code, false)));
    }
    chips.replaceChildren(...nodes);
  }

  function render() {
    everyoneInput.checked = everyone;
    onlyInput.checked = !everyone;
    pick.hidden = everyone;
    const noMarkets = !state.markets.countries.length;
    empty.hidden = !noMarkets;
    empty.textContent = noMarkets ? t("dashboard.noMarketsYet") : "";
    filter.hidden = noMarkets;
    renderChips();
    renderOptions();
    const codes = coveredCodes().map(countryName).sort(Lang.compare);
    covers.textContent = codes.length ? t("dashboard.covers", { list: codes.join(", ") }) : t("dashboard.pickOne");
    covers.classList.toggle("covers-empty", !codes.length);
  }

  everyoneInput.addEventListener("change", () => {
    everyone = true;
    render();
    onChange?.();
  });
  onlyInput.addEventListener("change", () => {
    everyone = false;
    render();
    onChange?.();
    filter.focus();
  });
  filter.addEventListener("input", renderOptions);

  return {
    root,
    selection,
    valid,
    // Also what a language switch calls: the built text is redone, and
    // Lang.apply() has already relabelled the marked legend and choices.
    render() {
      filter.placeholder = t("dashboard.filterMarkets");
      filter.setAttribute("aria-label", t("dashboard.filterMarkets"));
      render();
    },
    set(audience) {
      picked.countries = new Set((audience?.countries || []).map((c) => c.code ?? c));
      picked.groups = new Set((audience?.groups || []).map((g) => g.id ?? g));
      everyone = !audience || audience.everyone !== false;
      if (everyone) {
        picked.countries.clear();
        picked.groups.clear();
      }
      filter.value = "";
      render();
    },
  };
}

function removableChip(label, cls, onRemove) {
  return el(
    "span",
    { class: `${cls} chip-removable` },
    el("span", { text: label }),
    el("button", {
      type: "button",
      class: "chip-x",
      "aria-label": t("dashboard.removeItem", { name: label }),
      text: "×",
      onclick: onRemove,
    }),
  );
}

// --- Toasts ----------------------------------------------------------------------
// What happened, briefly, with an action such as Undo when there is one. A toast
// with an action stays long enough to be used, and none goes while hovered.

function toast(text, { kind = "ok", action = null, timeout = 6000 } = {}) {
  const node = el("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : null });
  let timer = null;
  const close = () => {
    clearTimeout(timer);
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 180);
  };
  const arm = () => {
    clearTimeout(timer);
    timer = setTimeout(close, action ? Math.max(timeout, 10000) : timeout);
  };
  node.append(el("span", { class: "toast-text", text }));
  if (action) {
    node.append(
      el("button", {
        type: "button",
        class: "toast-action",
        text: action.label,
        onclick: () => {
          close();
          action.run();
        },
      }),
    );
  }
  node.append(
    el("button", { type: "button", class: "toast-close", "aria-label": t("dashboard.close"), onclick: close }, icon("close")),
  );
  node.addEventListener("mouseenter", () => clearTimeout(timer));
  node.addEventListener("mouseleave", arm);
  els.toasts.append(node);
  while (els.toasts.children.length > 4) els.toasts.firstElementChild.remove();
  arm();
  return close;
}

function flash(text, kind = "ok") {
  toast(text, { kind });
}

// --- Dialogs: confirm, and what stands in the way ----------------------------------
// In the page rather than window.confirm(), which some embedded browsers
// silently answer "no" to - and so a refusal can say what to do about it.

let confirmResolve = null;

function settleConfirm(value) {
  const resolve = confirmResolve;
  confirmResolve = null;
  resolve?.(value);
}

// Resolves to true only when `confirm` was given and pressed. Without one the
// dialog only informs, and its single button closes it.
function confirmAction({ title, body = "", items = [], confirm = null, danger = false, cancel = null }) {
  settleConfirm(false);
  els.confirmTitle.textContent = title;
  els.confirmBody.replaceChildren(
    ...[body ? el("p", { text: body }) : null, items.length ? el("ul", { class: "blockers" }, ...items) : null].filter(
      Boolean,
    ),
  );
  els.confirmOk.hidden = !confirm;
  els.confirmOk.textContent = confirm || "";
  els.confirmOk.className = danger ? "btn-danger-solid" : "";
  els.confirmCancel.textContent = cancel || t(confirm ? "dashboard.cancel" : "dashboard.close");
  const done = new Promise((resolve) => {
    confirmResolve = resolve;
  });
  if (!els.confirmDialog.open) els.confirmDialog.showModal();
  // A destructive action waits for a deliberate choice.
  (confirm && !danger ? els.confirmOk : els.confirmCancel).focus();
  return done;
}

els.confirmOk.addEventListener("click", () => {
  settleConfirm(true);
  els.confirmDialog.close();
});
els.confirmCancel.addEventListener("click", () => els.confirmDialog.close());
els.confirmDialog.addEventListener("close", () => settleConfirm(false));

// One line of "what stands in the way", with the button that deals with it.
function blocker(text, action) {
  return el(
    "li",
    {},
    el("span", { text }),
    action
      ? el("button", {
          type: "button",
          class: "btn-secondary",
          text: action.label,
          onclick: () => {
            els.confirmDialog.close();
            action.run();
          },
        })
      : null,
  );
}

// "Delete "x"?\nThis removes..." - a title and the line that explains it.
function splitMessage(text) {
  const [title, ...rest] = String(text).split("\n");
  return { title, body: rest.join(" ") };
}

function anyModalOpen() {
  return Boolean(document.querySelector("dialog[open]"));
}

// --- Popovers: menus and the upload options ----------------------------------------

let openPop = null;

function togglePopover(button, panel) {
  const opening = panel.hidden;
  closePopover();
  if (!opening) return;
  panel.hidden = false;
  placePopover(panel);
  button.setAttribute("aria-expanded", "true");
  openPop = { button, panel };
  panel.querySelector("button, input, a[href]")?.focus();
}

// A popover hangs from its button's right edge, which suits the buttons at the
// right of a row. The narrow layouts move them to the left - the view's actions
// drop under its title - and there that edge would put most of the popover
// off-screen, so it hangs from the button's left edge instead.
function placePopover(panel) {
  panel.classList.remove("from-left");
  if (panel.getBoundingClientRect().left < 8) panel.classList.add("from-left");
}

function closePopover(restoreFocus = false) {
  if (!openPop) return;
  const { button, panel } = openPop;
  openPop = null;
  panel.hidden = true;
  button.setAttribute("aria-expanded", "false");
  if (restoreFocus && button.isConnected) button.focus();
}

document.addEventListener("click", (e) => {
  if (openPop && !openPop.panel.contains(e.target) && !openPop.button.contains(e.target)) closePopover();
});

function moreMenu(label, items) {
  const panel = el(
    "div",
    { class: "popover menu", role: "menu", hidden: true },
    ...items.map((it) =>
      el(
        "button",
        {
          type: "button",
          role: "menuitem",
          class: `menu-item${it.danger ? " is-danger" : ""}`,
          onclick: () => {
            closePopover();
            it.run();
          },
        },
        it.icon ? icon(it.icon) : null,
        el("span", { text: it.label }),
      ),
    ),
  );
  const button = el(
    "button",
    {
      type: "button",
      class: "btn-icon more-btn",
      "aria-label": label,
      title: label,
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      "data-focus-key": "more",
      onclick: () => togglePopover(button, panel),
    },
    icon("more"),
  );
  return el("div", { class: "menu-wrap" }, button, panel);
}

// --- Views: which market is open -------------------------------------------------

function parseHash(hash) {
  let parts;
  try {
    parts = decodeURIComponent(String(hash || "").replace(/^#\/?/, "")).split("/");
  } catch {
    return { kind: "all" };
  }
  if (parts[0] === "global") return { kind: "global" };
  if (parts[0] === "insights") return { kind: "insights" };
  if (parts[0] === "group" && GROUP_KEY.test(parts[1] || "")) return { kind: "group", key: parts[1].toLowerCase() };
  if (parts[0] === "country") {
    const code = (parts[1] || "").toUpperCase();
    if (COUNTRY_KEY.test(code)) return { kind: "country", key: code, scope: parts[2] === "sees" ? "sees" : "limited" };
  }
  // #all, and the old #documents / #markets tabs, are all of it.
  return { kind: "all" };
}

function viewHash(view) {
  if (view.kind === "global") return "#global";
  if (view.kind === "insights") return "#insights";
  if (view.kind === "group") return `#group/${view.key}`;
  if (view.kind === "country") return `#country/${view.key}${view.scope === "sees" ? "/sees" : ""}`;
  return "#all";
}

function sameMarket(a, b) {
  return a.kind === b.kind && (a.key || "") === (b.key || "");
}

function viewExists(view) {
  if (view.kind === "country") return state.markets.countries.some((c) => c.code === view.key);
  if (view.kind === "group") return state.markets.groups.some((g) => g.id === view.key);
  return true;
}

function findGroup(id) {
  return state.markets.groups.find((g) => g.id === id);
}

function viewName(view) {
  if (view.kind === "global") return t("dashboard.global");
  if (view.kind === "insights") return t("dashboard.insights");
  if (view.kind === "country") return countryName(view.key);
  if (view.kind === "group") return findGroup(view.key)?.name || "";
  return t("dashboard.allDocuments");
}

// The Documents list's own filter for a view - the same test its count is.
function whoParams(view) {
  if (view.kind === "global") return { who: "global" };
  if (view.kind === "group") return { who: view.key };
  if (view.kind === "country") {
    return view.scope === "sees" ? { who: view.key, include_global: "true" } : { who: view.key };
  }
  return {};
}

function viewCount(view) {
  const m = state.markets;
  if (view.kind === "global") return m.global_documents || 0;
  if (view.kind === "group") return findGroup(view.key)?.documents || 0;
  if (view.kind === "country") {
    const own = m.countries.find((c) => c.code === view.key)?.documents || 0;
    // Disjoint: a document limited to a market has audience rows, a Global one none.
    return view.scope === "sees" ? own + (m.global_documents || 0) : own;
  }
  return m.total_documents || 0;
}

// Whether a new upload - always Global - belongs in this view.
function showsGlobal(view) {
  return view.kind === "all" || view.kind === "global" || (view.kind === "country" && view.scope === "sees");
}

function go(view) {
  const hash = viewHash(view);
  if (location.hash === hash) applyRoute();
  else location.hash = hash;
}

function applyRoute() {
  let view = parseHash(location.hash);
  // A market removed since the link was made, or not there at all.
  if (state.marketsLoaded && !viewExists(view)) view = { kind: "all" };
  const hash = viewHash(view);
  if (location.hash !== hash) history.replaceState(null, "", hash);
  const changed = viewHash(state.view) !== hash;
  state.view = view;
  closeRail();
  renderRail();
  renderViewHead();
  showInsightsPane(view.kind === "insights");
  if (view.kind === "insights") {
    if (changed || !state.insights.data) loadInsights();
    return;
  }
  if (changed || !state.docsLoaded) {
    state.offset = 0;
    state.selected.clear();
    state.active = -1;
    loadDocs({ fresh: true });
  }
}

window.addEventListener("hashchange", applyRoute);

// --- Markets: the sidebar --------------------------------------------------------

async function loadMarkets() {
  try {
    const data = await api("/api/markets");
    state.markets = {
      countries: data?.countries || [],
      groups: data?.groups || [],
      global_documents: data?.global_documents ?? 0,
      total_documents: data?.total_documents ?? 0,
    };
    state.marketsLoaded = true;
  } catch (err) {
    flash(t("dashboard.loadMarketsFailed", { message: err.message }), "error");
    return;
  }
  renderRail();
  if (state.docsLoaded && !viewExists(state.view)) applyRoute();
  else renderViewHead();
  detailEditor.render();
  dialogEditor.render();
  if (els.countryDialog.open) renderCountryResults();
}

function renderRail() {
  const { countries, groups, global_documents: globalDocs, total_documents: total } = state.markets;
  keepFocus(() => {
    els.railTop.replaceChildren(
      railItem({ kind: "all" }, icon("all"), t("dashboard.allDocuments"), total),
      railItem({ kind: "global" }, icon("globe"), t("dashboard.global"), globalDocs, {
        body: { everyone: true, countries: [], groups: [] },
        hint: t("dashboard.dropMakeEveryone"),
      }),
    );
    els.railGroups.replaceChildren(
      ...(groups.length
        ? groups.map((g) =>
            railItem({ kind: "group", key: g.id }, icon("group"), g.name, g.documents, {
              body: { everyone: false, countries: [], groups: [g.id] },
              hint: t("dashboard.dropMakeFor", { name: g.name }),
              title: (g.members || []).map(countryName).join(", "),
            }),
          )
        : [el("li", { class: "rail-empty", text: t("dashboard.noGroups") })]),
    );
    els.railCountries.replaceChildren(
      ...(countries.length
        ? sortedCountries().map((c) =>
            railItem({ kind: "country", key: c.code }, flagNode(c.code), countryName(c.code), c.documents, {
              body: { everyone: false, countries: [c.code], groups: [] },
              hint: t("dashboard.dropMakeFor", { name: countryName(c.code) }),
              title: c.code,
            }),
          )
        : [el("li", { class: "rail-empty", text: t("dashboard.noCountries") })]),
    );
    els.railInsights.replaceChildren(insightsRailItem());
  });
  els.newGroupBtn.disabled = !countries.length;
  els.newGroupBtn.title = countries.length ? t("dashboard.newGroup") : t("dashboard.addCountriesFirst");
}

function railItem(view, mark, label, count, drop = null) {
  const active = sameMarket(view, state.view);
  const link = el(
    "a",
    {
      href: viewHash(view),
      class: `rail-item${active ? " is-active" : ""}`,
      "aria-current": active ? "page" : null,
      title: drop?.title || null,
      "data-focus-key": `rail:${viewHash(view)}`,
      "data-drop-hint": drop?.hint || null,
    },
    el("span", { class: "rail-mark" }, mark),
    el("span", { class: "rail-label", text: label }),
    el("span", { class: `rail-count${count ? "" : " is-zero"}`, text: String(count || 0) }),
  );
  if (drop) makeDropTarget(link, drop.body, label);
  return el("li", {}, link);
}

// --- Markets: the header of the open one ------------------------------------------

function renderViewHead() {
  closePopover();
  const view = state.view;
  const name = viewName(view);
  keepFocus(() => {
    els.viewIcon.replaceChildren(viewMark(view));
    els.viewName.textContent = name;
    els.viewCode.textContent = view.kind === "country" ? view.key : "";
    els.viewCode.hidden = view.kind !== "country";
    // A country's counts are in its scope switch; Insights has its own.
    els.viewCount.textContent =
      view.kind === "country" || view.kind === "insights" ? "" : t("dashboard.documents", { n: viewCount(view) });
    const sub = viewSub(view);
    els.viewSub.replaceChildren(...sub);
    els.viewSub.hidden = !sub.length;
    els.viewActions.replaceChildren(...viewActions(view));
    els.viewActions.classList.toggle("is-wrap", view.kind === "insights");
  });
  els.railOpen.replaceChildren(icon("menu"), el("span", { text: name }));
  els.railOpen.title = t("dashboard.openLibrary");
}

function chipLink(href, mark, label, cls = "chip") {
  return el("a", { class: `${cls} chip-link`, href }, mark, el("span", { text: label }));
}

function viewSub(view) {
  if (view.kind === "all") return [el("span", { class: "muted", text: t("dashboard.allSub") })];
  if (view.kind === "global") return [el("span", { class: "muted", text: t("dashboard.globalText") })];
  if (view.kind === "insights") return [el("span", { class: "muted", text: t("dashboard.insightsSub") })];
  if (view.kind === "country") {
    const c = state.markets.countries.find((x) => x.code === view.key);
    const groups = (c?.groups || []).map((groupName) => state.markets.groups.find((g) => g.name === groupName));
    if (!groups.length) return [];
    return [
      el("span", { class: "sub-label", text: t("dashboard.partOf") }),
      ...groups
        .filter(Boolean)
        .map((g) => chipLink(viewHash({ kind: "group", key: g.id }), icon("group"), g.name, "chip chip-group")),
    ];
  }
  const g = findGroup(view.key);
  if (!g) return [];
  return [
    el("span", { class: "sub-label", text: t("dashboard.members") }),
    ...[...(g.members || [])]
      .sort((a, b) => Lang.compare(countryName(a), countryName(b)))
      .map((code) => chipLink(viewHash({ kind: "country", key: code }), flagNode(code), countryName(code))),
  ];
}

function viewActions(view) {
  if (view.kind === "insights") return insightsActions();
  if (view.kind === "all") {
    return [
      moreMenu(t("dashboard.moreActions", { name: t("dashboard.allDocuments") }), [
        { label: t("dashboard.reindexAll"), icon: "all", run: reindexAll },
      ]),
    ];
  }
  if (view.kind === "country") {
    const name = countryName(view.key);
    return [
      scopeSwitch(view),
      moreMenu(t("dashboard.moreActions", { name }), [
        { label: t("dashboard.removeNamed", { name }), icon: "trash", danger: true, run: () => removeCountry(view.key) },
      ]),
    ];
  }
  if (view.kind === "group") {
    const g = findGroup(view.key);
    if (!g) return [];
    return [
      el(
        "button",
        { type: "button", class: "btn-secondary btn-with-icon", "data-focus-key": "edit-group", onclick: () => openGroupDialog(g) },
        icon("edit"),
        el("span", { text: t("dashboard.editGroupButton") }),
      ),
      moreMenu(t("dashboard.moreActions", { name: g.name }), [
        { label: t("dashboard.deleteNamed", { name: g.name }), icon: "trash", danger: true, run: () => deleteGroup(g) },
      ]),
    ];
  }
  return [];
}

// "Limited to Sweden 3 | Everything Sweden sees 13": what is Sweden's own, or
// everything its answers can draw on - the same test retrieval filters with.
function scopeSwitch(view) {
  const name = countryName(view.key);
  const button = (scope, label, title) =>
    el(
      "button",
      {
        type: "button",
        class: "seg-btn",
        "aria-pressed": view.scope === scope ? "true" : "false",
        title,
        "data-focus-key": `seg:${scope}`,
        onclick: () => go({ ...view, scope }),
      },
      el("span", { text: label }),
      el("span", { class: "seg-count", text: String(viewCount({ ...view, scope })) }),
    );
  return el(
    "div",
    { class: "seg", role: "group", "aria-label": t("dashboard.scopeLabel") },
    button("limited", t("dashboard.limitedTo", { name }), t("dashboard.limitedHint", { name })),
    button("sees", t("dashboard.everythingSees", { name }), t("dashboard.seesHint", { name })),
  );
}

// --- Re-indexing ---------------------------------------------------------------------
// After a change to how documents are split for search: every document's chunks
// are rebuilt from the text already extracted. Nothing is parsed again, and
// documents keep answering with their old chunks until the new ones are in.

async function reindexAll() {
  const ok = await confirmAction({
    title: t("dashboard.reindexTitle"),
    body: t("dashboard.reindexBody"),
    confirm: t("dashboard.reindex"),
  });
  if (!ok) return;
  try {
    const data = await api("/api/documents/rechunk-all", { method: "POST" });
    const n = data?.queued ?? 0;
    flash(n ? t("dashboard.reindexStarted", { n }) : t("dashboard.reindexNothing"));
  } catch (err) {
    flash(err.message, "error");
  }
}

// --- Markets: removing one ---------------------------------------------------------
// Remove and Delete always answer: a confirm when nothing is in the way, or what
// is - with the button that deals with it - instead of a greyed-out button.

function showCountryBlockers(code, groups, direct) {
  if (!groups.length && !direct) return false;
  const name = countryName(code);
  const items = groups.map((groupName) => {
    const g = state.markets.groups.find((x) => x.name === groupName);
    return blocker(
      t("dashboard.blockerInGroup", { group: groupName }),
      g ? { label: t("dashboard.editGroupButton"), run: () => openGroupDialog(g) } : null,
    );
  });
  if (direct) {
    items.push(
      blocker(t("dashboard.blockerDirectDocs", { n: direct }), {
        label: t("dashboard.showThem"),
        run: () => go({ kind: "country", key: code, scope: "limited" }),
      }),
    );
  }
  confirmAction({ title: t("dashboard.stillInUse", { name }), body: t("dashboard.removeBlockedIntro"), items });
  return true;
}

async function removeCountry(code) {
  const c = state.markets.countries.find((x) => x.code === code);
  if (!c) return;
  const name = countryName(code);
  if (showCountryBlockers(code, c.groups || [], c.direct_documents || 0)) return;
  const ok = await confirmAction({
    title: t("dashboard.removeCountryTitle", { name }),
    body: t("dashboard.removeCountryBody"),
    confirm: t("dashboard.remove"),
    danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/markets/countries/${encodeURIComponent(code)}`, { method: "DELETE" });
    flash(t("dashboard.countryRemoved", { name }));
  } catch (err) {
    // Something started using it since the sidebar was drawn.
    const detail = err.data?.detail;
    if (err.status === 409 && detail && typeof detail === "object") {
      await loadMarkets();
      showCountryBlockers(code, detail.groups || [], detail.documents || 0);
      return;
    }
    flash(err.message, "error");
  }
  await loadMarkets();
}

function showGroupBlockers(g, n) {
  confirmAction({
    title: t("dashboard.stillInUse", { name: g.name }),
    body: t("dashboard.removeBlockedIntro"),
    items: [
      blocker(t("dashboard.groupBlockedDocs", { n }), {
        label: t("dashboard.showThem"),
        run: () => go({ kind: "group", key: g.id }),
      }),
    ],
  });
}

async function deleteGroup(g) {
  if (g.documents) {
    showGroupBlockers(g, g.documents);
    return;
  }
  const ok = await confirmAction({
    title: t("dashboard.deleteGroupTitle", { name: g.name }),
    body: t("dashboard.deleteGroupBody"),
    confirm: t("dashboard.delete"),
    danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/markets/groups/${g.id}`, { method: "DELETE" });
    flash(t("dashboard.groupDeleted", { name: g.name }));
  } catch (err) {
    const n = err.data?.detail?.documents;
    if (err.status === 409 && typeof n === "number") {
      await loadMarkets();
      showGroupBlockers(g, n);
      return;
    }
    flash(err.message, "error");
  }
  await loadMarkets();
}

// --- Markets: adding a country -----------------------------------------------------

async function ensureCatalogue() {
  if (state.catalogue) return;
  try {
    const data = await api("/api/markets/catalogue");
    state.catalogue = data?.countries || [];
  } catch (err) {
    flash(t("dashboard.loadCatalogueFailed", { message: err.message }), "error");
  }
}

function renderCountryResults() {
  const f = fold(els.countrySearch.value.trim());
  if (!f || !state.catalogue) {
    els.countryResults.replaceChildren();
    return;
  }
  const added = new Set(state.markets.countries.map((c) => c.code));
  // Found by its name in the admin's language, its English name or its code,
  // so "Sverige", "Sweden" and "SE" all find Sweden.
  const matches = state.catalogue
    .map((c) => ({ ...c, local: Lang.region(c.code, c.name) }))
    .filter((c) => fold(c.local).includes(f) || fold(c.name).includes(f) || c.code.toLowerCase() === f)
    // An exact code match first, then names that start with what was typed.
    .sort((a, b) => {
      const rank = (c) =>
        c.code.toLowerCase() === f ? 0 : fold(c.local).startsWith(f) || fold(c.name).startsWith(f) ? 1 : 2;
      return rank(a) - rank(b) || Lang.compare(a.local, b.local);
    })
    .slice(0, 8);
  if (!matches.length) {
    els.countryResults.replaceChildren(el("li", { class: "muted", text: t("dashboard.noCountryMatch") }));
    return;
  }
  els.countryResults.replaceChildren(
    ...matches.map((c) => {
      const isAdded = added.has(c.code);
      return el(
        "li",
        {},
        el(
          "button",
          {
            type: "button",
            class: "country-result",
            disabled: isAdded,
            onclick: () => addCountry(c),
          },
          el("span", { text: c.local }),
          el("span", { class: "market-code", text: c.code }),
          el("span", { class: "muted", text: t(isAdded ? "dashboard.added" : "dashboard.add") }),
        ),
      );
    }),
  );
}

async function addCountry(c) {
  try {
    await api("/api/markets/countries", { method: "POST", body: { code: c.code } });
    flash(t("dashboard.countryAdded", { name: Lang.region(c.code, c.name) }));
    els.countrySearch.value = "";
  } catch (err) {
    flash(err.message, "error");
  }
  await loadMarkets();
  renderCountryResults();
  els.countrySearch.focus();
}

function openCountryDialog() {
  els.countrySearch.value = "";
  renderCountryResults();
  els.countryDialog.showModal();
  els.countrySearch.focus();
  ensureCatalogue();
}

els.addCountryBtn.addEventListener("click", openCountryDialog);
els.countryDialogClose.addEventListener("click", () => els.countryDialog.close());
els.countrySearch.addEventListener("input", async () => {
  await ensureCatalogue();
  renderCountryResults();
});
els.countrySearch.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  els.countryResults.querySelector("button:not(:disabled)")?.click();
});

// --- Groups ----------------------------------------------------------------------

let editingGroup = null;
let groupPicked = new Set();

function renderGroupChecklist() {
  const f = fold(els.groupFilter.value.trim());
  const countries = sortedCountries().filter(
    (c) => !f || fold(countryName(c.code)).includes(f) || fold(c.name).includes(f) || fold(c.code).includes(f),
  );
  els.groupChecklist.replaceChildren(
    ...countries.map((c) =>
      el(
        "label",
        { class: "check" },
        el("input", {
          type: "checkbox",
          checked: groupPicked.has(c.code),
          onchange: (e) => {
            if (e.target.checked) groupPicked.add(c.code);
            else groupPicked.delete(c.code);
          },
        }),
        el("span", { text: countryName(c.code) }),
        el("small", { text: c.code }),
      ),
    ),
  );
}

function openGroupDialog(group) {
  editingGroup = group || null;
  groupPicked = new Set(group?.members || []);
  els.groupDialogTitle.textContent = group ? t("dashboard.editGroup", { name: group.name }) : t("dashboard.newGroup");
  els.groupName.value = group?.name || "";
  els.groupFilter.value = "";
  const n = group?.documents || 0;
  els.groupWarning.hidden = n === 0;
  els.groupWarning.textContent = n ? t("dashboard.groupUsedBy", { n }) : "";
  setStatus(els.groupDialogStatus, "");
  renderGroupChecklist();
  els.groupDialog.showModal();
  els.groupName.focus();
}

async function saveGroup() {
  const name = els.groupName.value.trim();
  if (!name) {
    setStatus(els.groupDialogStatus, t("dashboard.nameTheGroup"), "error");
    return;
  }
  if (!groupPicked.size) {
    setStatus(els.groupDialogStatus, t("dashboard.tickCountry"), "error");
    return;
  }
  const body = { name, members: [...groupPicked] };
  els.groupDialogSave.disabled = true;
  try {
    if (editingGroup) {
      await api(`/api/markets/groups/${editingGroup.id}`, { method: "PUT", body });
    } else {
      await api("/api/markets/groups", { method: "POST", body });
    }
    els.groupDialog.close();
    flash(t(editingGroup ? "dashboard.groupSaved" : "dashboard.groupCreated", { name }));
    await loadMarkets();
    if (editingGroup) loadDocs();
  } catch (err) {
    setStatus(els.groupDialogStatus, err.message, "error");
  } finally {
    els.groupDialogSave.disabled = false;
  }
}

els.newGroupBtn.addEventListener("click", () => openGroupDialog(null));
els.groupFilter.addEventListener("input", renderGroupChecklist);
els.groupDialogCancel.addEventListener("click", () => els.groupDialog.close());
els.groupDialogSave.addEventListener("click", saveGroup);

// --- The sidebar on a phone ------------------------------------------------------

function railIsOpen() {
  return els.rail.classList.contains("is-open");
}

function openRail() {
  els.rail.classList.add("is-open");
  els.railScrim.hidden = false;
  els.railOpen.setAttribute("aria-expanded", "true");
  els.rail.querySelector(".rail-item.is-active, .rail-item")?.focus();
}

function closeRail(restoreFocus = false) {
  if (!railIsOpen()) return;
  els.rail.classList.remove("is-open");
  els.railScrim.hidden = true;
  els.railOpen.setAttribute("aria-expanded", "false");
  if (restoreFocus) els.railOpen.focus();
}

els.railOpen.addEventListener("click", () => (railIsOpen() ? closeRail() : openRail()));
els.railClose.addEventListener("click", () => closeRail(true));
els.railScrim.addEventListener("click", () => closeRail(true));

// --- Documents table -------------------------------------------------------------

let loadSeq = 0;
let skeletonTimer = null;

// `fresh` is a new view: its rows are not the ones on screen, so a skeleton
// stands in for them if the answer takes a moment.
async function loadDocs({ fresh = false } = {}) {
  const seq = ++loadSeq;
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(state.offset),
    sort: state.sort,
    ...whoParams(state.view),
  });
  if (state.q) params.set("q", state.q);
  if (fresh) {
    clearTimeout(skeletonTimer);
    skeletonTimer = setTimeout(() => seq === loadSeq && renderSkeleton(), 120);
  }
  let data;
  try {
    data = await api(`/api/documents?${params}`);
  } catch (err) {
    if (seq !== loadSeq) return;
    clearTimeout(skeletonTimer);
    flash(t("dashboard.loadDocsFailed", { message: err.message }), "error");
    // Never leave one market's rows under another market's name.
    if (fresh) {
      state.docs = [];
      state.total = 0;
      renderDocs();
    }
    return;
  }
  // A slower, older request must not paint over a newer one.
  if (seq !== loadSeq) return;
  clearTimeout(skeletonTimer);
  state.docsLoaded = true;
  state.docs = data?.documents || [];
  state.total = data?.total ?? state.docs.length;
  if (state.offset && state.offset >= state.total) {
    state.offset = Math.max(0, Math.floor((state.total - 1) / PAGE_SIZE) * PAGE_SIZE);
    return loadDocs();
  }
  if (state.active >= state.docs.length) state.active = state.docs.length - 1;
  renderDocs();
  if (state.detail) {
    const fresh = state.docs.find((d) => d.id === state.detail.id);
    if (fresh) refreshDetailMeta(fresh);
  }
  scheduleTick();
}

function renderSkeleton() {
  els.tableWrap.hidden = false;
  els.docsEmpty.hidden = true;
  const bar = (w) => el("span", { class: "sk", style: `width:${w}` });
  els.rows.replaceChildren(
    ...[62, 48, 70, 55, 40].map((w) =>
      el(
        "tr",
        { class: "sk-row", "aria-hidden": "true" },
        el("td", { class: "col-check" }, bar("14px")),
        el("td", { class: "col-name" }, bar(`${w}%`)),
        el("td", { class: "col-who" }, bar("64px")),
        el("td", { class: "col-status" }, bar("52px")),
        el("td", { class: "col-date" }, bar("70px")),
        el("td", { class: "col-actions" }),
      ),
    ),
  );
}

function docRow(d, i) {
  const name = d.original_filename || d.id;
  const selected = state.selected.has(d.id);
  const box = el("input", {
    type: "checkbox",
    tabindex: "-1",
    checked: selected,
    "aria-label": t("dashboard.selectDoc", { name }),
    onclick: (e) => e.stopPropagation(),
    onchange: (e) => setSelected(d.id, e.target.checked),
  });
  const classes = ["doc-row"];
  if (state.detail?.id === d.id) classes.push("is-open");
  if (selected) classes.push("is-selected");
  return el(
    "tr",
    {
      class: classes.join(" "),
      // One stop for Tab; the arrow keys move between rows (roving tabindex).
      tabindex: i === Math.max(state.active, 0) ? "0" : "-1",
      draggable: "true",
      "data-focus-key": `row:${d.id}`,
      onclick: () => {
        setActive(i);
        openDetail(d);
      },
      onfocus: () => setActive(i, { focus: false }),
      ondragstart: (e) => onRowDragStart(e, d),
      ondragend: onRowDragEnd,
    },
    el(
      "td",
      {
        class: "col-check",
        // The whole cell ticks the box: a near-miss should not open the drawer.
        onclick: (e) => {
          e.stopPropagation();
          if (e.target !== box) box.click();
        },
      },
      box,
    ),
    el(
      "td",
      { class: "col-name" },
      el(
        "div",
        { class: "doc-name" },
        el("span", { class: `doc-icon${isImage(d) ? " is-image" : ""}` }, icon(isImage(d) ? "image" : "file")),
        el(
          "div",
          { class: "doc-name-text" },
          el("button", {
            type: "button",
            class: "doc-open",
            tabindex: "-1",
            text: name,
            title: name,
            onclick: (e) => {
              e.stopPropagation();
              setActive(i);
              openDetail(d);
            },
          }),
          d.collection ? el("small", { class: "muted", text: d.collection }) : null,
          // On a phone the "Who it's for" column gives its room to the name,
          // and its chips sit here instead.
          el("span", { class: "who-inline" }, audienceChips(d.audience)),
        ),
      ),
    ),
    el("td", { class: "col-who" }, audienceChips(d.audience)),
    el("td", { class: "col-status" }, statusBadge(d)),
    el("td", { class: "col-date", text: formatDate(d.created_at) }),
    el(
      "td",
      { class: "col-actions" },
      el(
        "a",
        {
          class: "row-action",
          href: fileUrl(d),
          tabindex: "-1",
          draggable: "false",
          title: t("dashboard.downloadItem", { name }),
          "aria-label": t("dashboard.downloadItem", { name }),
          onclick: (e) => e.stopPropagation(),
        },
        icon("download"),
      ),
    ),
  );
}

function renderDocs() {
  keepFocus(() => els.rows.replaceChildren(...state.docs.map(docRow)));
  const empty = state.docs.length === 0;
  els.tableWrap.hidden = empty;
  els.docsEmpty.hidden = !empty;
  if (empty) els.docsEmpty.replaceChildren(...emptyState());

  const from = state.total ? state.offset + 1 : 0;
  const to = state.offset + state.docs.length;
  els.pageInfo.textContent = state.total ? t("dashboard.pageInfo", { from, to, total: state.total }) : "";
  els.prev.disabled = state.offset === 0;
  els.next.disabled = to >= state.total;
  els.prev.parentElement.hidden = state.total <= PAGE_SIZE && state.offset === 0;
  renderSortHeaders();
  renderBulkBar();
}

function emptyState() {
  const view = state.view;
  let title;
  let hint = "";
  if (state.q) {
    title = t("dashboard.noDocsMatch");
  } else if (view.kind === "all") {
    title = t("dashboard.noDocsYet");
  } else if (view.kind === "global") {
    title = t("dashboard.emptyGlobal");
  } else {
    const name = viewName(view);
    title = t("dashboard.emptyMarket", { name });
    hint = t("dashboard.emptyMarketHint", { name });
  }
  return [
    el("span", { class: "empty-icon" }, view.kind === "all" ? icon("all") : viewMark(view)),
    el("strong", { text: title }),
    hint ? el("span", { class: "muted", text: hint }) : null,
  ].filter(Boolean);
}

function setActive(i, { focus = true } = {}) {
  if (i < 0 || i >= state.docs.length) return;
  state.active = i;
  const rows = els.rows.querySelectorAll("tr.doc-row");
  rows.forEach((row, j) => row.setAttribute("tabindex", j === i ? "0" : "-1"));
  if (focus) rows[i]?.focus();
}

function setSelected(id, on) {
  if (on) state.selected.add(id);
  else state.selected.delete(id);
  els.rows
    .querySelector(`tr[data-focus-key="${CSS.escape(`row:${id}`)}"]`)
    ?.classList.toggle("is-selected", on);
  renderBulkBar();
}

function renderBulkBar() {
  const n = state.selected.size;
  els.bulkBar.hidden = n === 0;
  document.body.classList.toggle("has-bulk", n > 0);
  els.bulkCount.textContent = t("dashboard.selectedCount", { n });
  const onPage = state.docs.map((d) => d.id);
  const pickedOnPage = onPage.filter((id) => state.selected.has(id)).length;
  els.selectAll.checked = onPage.length > 0 && pickedOnPage === onPage.length;
  els.selectAll.indeterminate = pickedOnPage > 0 && pickedOnPage < onPage.length;
}

function clearSelection() {
  state.selected.clear();
  renderDocs();
}

let searchTimer = null;
els.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = els.search.value.trim();
    state.offset = 0;
    state.active = -1;
    state.selected.clear();
    loadDocs();
  }, 250);
});

els.prev.addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - PAGE_SIZE);
  state.active = -1;
  state.selected.clear();
  loadDocs();
});

els.next.addEventListener("click", () => {
  state.offset += PAGE_SIZE;
  state.active = -1;
  state.selected.clear();
  loadDocs();
});

els.selectAll.addEventListener("change", () => {
  for (const d of state.docs) {
    if (els.selectAll.checked) state.selected.add(d.id);
    else state.selected.delete(d.id);
  }
  renderDocs();
});

els.bulkClearBtn.addEventListener("click", clearSelection);

// --- Sorting ---------------------------------------------------------------------

function loadSort() {
  try {
    const saved = localStorage.getItem(SORT_KEY);
    if (SORTS.includes(saved)) state.sort = saved;
  } catch {
    /* the default, newest first */
  }
}

function renderSortHeaders() {
  const [column, dir] = state.sort.split("_");
  for (const [th, key] of [
    [els.thName, "name"],
    [els.thDate, "uploaded"],
  ]) {
    th.setAttribute("aria-sort", column === key ? (dir === "asc" ? "ascending" : "descending") : "none");
  }
}

for (const button of document.querySelectorAll(".sort-btn")) {
  button.addEventListener("click", () => {
    const key = button.dataset.sort;
    const [column, dir] = state.sort.split("_");
    // A second click reverses; a new column starts the way it is usually read.
    const next = column === key ? `${key}_${dir === "asc" ? "desc" : "asc"}` : `${key}_${key === "name" ? "asc" : "desc"}`;
    if (!SORTS.includes(next)) return;
    state.sort = next;
    try {
      localStorage.setItem(SORT_KEY, next);
    } catch {
      /* this page only */
    }
    state.offset = 0;
    state.active = -1;
    state.selected.clear();
    renderSortHeaders();
    loadDocs();
  });
}

// --- Drag to retag ---------------------------------------------------------------
// Dropping documents on a market in the sidebar makes them for it alone, as a
// file moves into a folder. Never "also for": adding a market to a Global
// document would narrow it, the opposite of what "also" sounds like. Undo puts
// back exactly what each document was for.

function audienceBody(audience) {
  if (!audience || audience.everyone) return { everyone: true, countries: [], groups: [] };
  return {
    everyone: false,
    countries: audience.countries.map((c) => c.code ?? c),
    groups: audience.groups.map((g) => g.id ?? g),
  };
}

function bodyKey(body) {
  return JSON.stringify([body.everyone, [...body.countries].sort(), [...body.groups].sort()]);
}

function isDocDrag(e) {
  return Array.from(e.dataTransfer?.types || []).includes(DRAG_TYPE);
}

function onRowDragStart(e, d) {
  const ids = state.selected.has(d.id) ? state.docs.filter((x) => state.selected.has(x.id)).map((x) => x.id) : [d.id];
  const label = ids.length === 1 ? d.original_filename || d.id : t("dashboard.documents", { n: ids.length });
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData(DRAG_TYPE, JSON.stringify(ids));
  e.dataTransfer.setData("text/plain", label);
  const ghost = el("div", { class: "drag-ghost" }, icon("file"), el("span", { text: label }));
  document.body.append(ghost);
  e.dataTransfer.setDragImage(ghost, 14, 14);
  setTimeout(() => ghost.remove(), 0);
  document.body.classList.add("is-dragging-docs");
}

function onRowDragEnd() {
  document.body.classList.remove("is-dragging-docs");
  for (const node of document.querySelectorAll(".rail-item.is-drop")) node.classList.remove("is-drop");
}

function makeDropTarget(node, body, name) {
  node.addEventListener("dragenter", (e) => {
    if (!isDocDrag(e)) return;
    e.preventDefault();
    node.classList.add("is-drop");
  });
  node.addEventListener("dragover", (e) => {
    if (!isDocDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  });
  node.addEventListener("dragleave", (e) => {
    if (!node.contains(e.relatedTarget)) node.classList.remove("is-drop");
  });
  node.addEventListener("drop", (e) => {
    if (!isDocDrag(e)) return;
    e.preventDefault();
    onRowDragEnd();
    let ids = [];
    try {
      ids = JSON.parse(e.dataTransfer.getData(DRAG_TYPE) || "[]");
    } catch {
      return;
    }
    moveDocs(ids, body, name);
  });
}

async function moveDocs(ids, body, name) {
  const docs = ids.map((id) => state.docs.find((d) => d.id === id)).filter(Boolean);
  const target = bodyKey(body);
  const moving = docs.filter((d) => bodyKey(audienceBody(d.audience)) !== target);
  if (!moving.length) {
    if (docs.length) flash(t("dashboard.alreadyThere", { name }));
    return;
  }
  const before = moving.map((d) => ({ id: d.id, body: audienceBody(d.audience) }));
  const movedIds = moving.map((d) => d.id);
  try {
    await api("/api/documents/audience", { method: "PUT", body: { document_ids: movedIds, ...body } });
  } catch (err) {
    flash(t("dashboard.moveFailed", { message: err.message }), "error");
    return;
  }
  state.selected.clear();
  await refreshAfterChange(movedIds);
  toast(t("dashboard.moved", { n: moving.length, name }), {
    action: { label: t("dashboard.undo"), run: () => undoMove(before) },
  });
}

// One request per distinct "what it was for", so ten documents that were all
// Global go back in one go.
async function undoMove(before) {
  const batches = new Map();
  for (const { id, body } of before) {
    const key = bodyKey(body);
    if (!batches.has(key)) batches.set(key, { body, ids: [] });
    batches.get(key).ids.push(id);
  }
  let failed = 0;
  for (const { body, ids } of batches.values()) {
    try {
      await api("/api/documents/audience", { method: "PUT", body: { document_ids: ids, ...body } });
    } catch {
      failed += ids.length;
    }
  }
  await refreshAfterChange(before.map((b) => b.id));
  if (failed) flash(t("dashboard.undoFailed", { failed, total: before.length }), "error");
  else flash(t("dashboard.undone"));
}

// After documents changed who they are for: the table, the counts, and the
// drawer if it shows one of them - read afresh, since it may have left the view.
async function refreshAfterChange(ids = []) {
  const openId = state.detail?.id;
  await Promise.all([loadDocs(), loadMarkets()]);
  if (openId && ids.includes(openId) && state.detail?.id === openId) {
    try {
      const doc = await api(`/api/documents/${encodeURIComponent(openId)}`);
      if (state.detail?.id === openId) {
        refreshDetailMeta(doc);
        detailEditor.set(doc.audience);
        updateDetailSave();
      }
    } catch {
      /* the next refresh catches up */
    }
  }
}

// --- Keyboard --------------------------------------------------------------------

function isTyping(node) {
  if (!(node instanceof Element)) return false;
  if (node.isContentEditable) return true;
  if (node.tagName === "TEXTAREA" || node.tagName === "SELECT") return true;
  return node.tagName === "INPUT" && !["checkbox", "radio", "button", "submit"].includes(node.type);
}

// Arrows, Enter, Space and x belong to the control that has focus - a radio
// button, a link - unless that is a row of the table or nothing at all.
function keysAreOurs(node) {
  if (!(node instanceof Element) || node === document.body) return true;
  if (node.matches("tr.doc-row")) return true;
  return !node.closest("input, textarea, select, button, a[href], summary, iframe, [contenteditable]");
}

function activeDoc() {
  return state.docs[state.active] || null;
}

function moveActive(delta) {
  if (!state.docs.length) return;
  const i =
    state.active < 0
      ? delta > 0
        ? 0
        : state.docs.length - 1
      : Math.min(state.docs.length - 1, Math.max(0, state.active + delta));
  setActive(i);
  // With the drawer open, the arrows page through documents in it.
  if (state.detail) openDetail(state.docs[i], { deferPreview: true });
}

document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || e.isComposing) return;
  if (e.key === "Escape") {
    if (openPop) {
      e.preventDefault();
      closePopover(true);
      return;
    }
    if (anyModalOpen()) return;
    if (railIsOpen()) {
      closeRail(true);
      return;
    }
    if (isTyping(e.target)) return;
    if (state.detail) {
      e.preventDefault();
      closeDetail({ restoreFocus: true });
    } else if (state.selected.size) {
      clearSelection();
    }
    return;
  }
  if (anyModalOpen() || e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return;
  // The shortcuts are the document list's; Insights has none of its own.
  if (state.view.kind === "insights") return;
  if (e.key === "/") {
    e.preventDefault();
    els.search.focus();
    els.search.select();
    return;
  }
  if (e.key === "?") {
    e.preventDefault();
    els.shortcutsDialog.showModal();
    return;
  }
  if (!keysAreOurs(e.target)) return;
  switch (e.key) {
    case "ArrowDown":
    case "j":
      e.preventDefault();
      moveActive(1);
      break;
    case "ArrowUp":
    case "k":
      e.preventDefault();
      moveActive(-1);
      break;
    case "Enter":
    case " ":
      if (!activeDoc()) return;
      e.preventDefault();
      openDetail(activeDoc());
      break;
    case "x":
      if (!activeDoc()) return;
      e.preventDefault();
      setSelected(activeDoc().id, !state.selected.has(activeDoc().id));
      break;
    default:
  }
});

els.shortcutsBtn.addEventListener("click", () => els.shortcutsDialog.showModal());
els.shortcutsClose.addEventListener("click", () => els.shortcutsDialog.close());

// --- Detail drawer ---------------------------------------------------------------

const detailEditor = createAudienceEditor({ onChange: updateDetailSave });
els.detailEditor.append(detailEditor.root);

function updateDetailSave() {
  els.detailSave.disabled = !detailEditor.valid();
}

function refreshDetailMeta(doc) {
  state.detail = doc;
  const facts = [statusBadge(doc)];
  if (typeof doc.chunk_count === "number" && doc.chunk_count > 0) {
    facts.push(el("span", { text: t("dashboard.chunks", { n: doc.chunk_count }) }));
  }
  facts.push(el("span", { text: t("dashboard.uploadedOn", { date: formatDate(doc.created_at) }) }));
  if (doc.collection) facts.push(el("span", { text: t("dashboard.collectionIs", { name: doc.collection }) }));
  const notes = [];
  if (doc.status === "failed" && doc.error_message) {
    notes.push(el("p", { class: "doc-error", text: doc.error_message }));
  }
  if (doc.status === "degraded") {
    notes.push(
      el("p", {
        class: "doc-note",
        text: t("dashboard.degradedNote"),
      }),
    );
  }
  els.detailMeta.replaceChildren(el("div", { class: "facts" }, ...facts), ...notes);
}

function clearPreview() {
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = null;
  els.preview.replaceChildren();
}

// Shows `doc` in `target`: the file itself for a PDF or JPEG, a note otherwise.
// The file is fetched once as a blob and shown from that, so a missing original
// becomes a plain message instead of an error page inside the frame.
// `stillWanted` is asked once the file has arrived, so a slow answer for a
// document the admin has moved on from is dropped. `adopt` gets the object URL
// as it goes on screen; its owner revokes it once replaced or closed.
async function showPreview(doc, target, stillWanted, adopt) {
  const type = (doc.file_type || "").toLowerCase();
  if (type !== "application/pdf" && type !== "image/jpeg") {
    target.replaceChildren(el("p", { class: "muted", text: t("dashboard.noPreview") }));
    return;
  }
  target.replaceChildren(el("p", { class: "muted", text: t("dashboard.loadingPreview") }));
  try {
    const resp = await fetch(`${fileUrl(doc)}?inline=true`);
    if (Shell.bounceIfLoggedOut(resp.status)) return;
    if (!resp.ok) throw new Error(resp.status === 404 ? "missing" : `HTTP ${resp.status}`);
    const blob = await resp.blob();
    if (!stillWanted()) return;
    const url = URL.createObjectURL(blob);
    adopt(url);
    const label = t("dashboard.previewOf", { name: doc.original_filename });
    target.replaceChildren(
      type === "image/jpeg" ? el("img", { src: url, alt: label }) : el("iframe", { src: url, title: label }),
    );
  } catch (err) {
    if (!stillWanted()) return;
    target.replaceChildren(
      el("p", {
        class: "muted",
        text:
          err.message === "missing"
            ? t("dashboard.originalMissing")
            : t("dashboard.previewFailed", { message: err.message }),
      }),
    );
  }
}

function loadPreview(doc) {
  clearPreview();
  return showPreview(
    doc,
    els.preview,
    () => state.detail?.id === doc.id,
    (url) => {
      if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
      state.previewUrl = url;
    },
  );
}

let previewTimer = null;

// The drawer is not modal: the table stays usable beside it, and with the
// arrow keys the drawer follows the row, Quick Look style. `deferPreview` waits
// for the arrows to settle before fetching a file.
function openDetail(doc, { deferPreview = false } = {}) {
  const switching = state.detail?.id !== doc.id;
  refreshDetailMeta(doc);
  els.detailName.textContent = doc.original_filename || doc.id;
  els.detailDownload.href = fileUrl(doc);
  els.detail.hidden = false;
  document.body.classList.add("has-drawer");
  if (switching) {
    detailEditor.set(doc.audience);
    setStatus(els.detailStatus, "");
    clearTimeout(previewTimer);
    if (deferPreview) {
      clearPreview();
      els.preview.append(el("p", { class: "muted", text: t("dashboard.loadingPreview") }));
      previewTimer = setTimeout(() => loadPreview(doc), 160);
    } else {
      loadPreview(doc);
    }
  }
  updateDetailSave();
  markOpenRow();
}

function markOpenRow() {
  const key = state.detail ? `row:${state.detail.id}` : "";
  for (const row of els.rows.querySelectorAll("tr.doc-row")) row.classList.toggle("is-open", row.dataset.focusKey === key);
}

function closeDetail({ restoreFocus = false } = {}) {
  const id = state.detail?.id;
  clearTimeout(previewTimer);
  state.detail = null;
  clearPreview();
  els.detail.hidden = true;
  document.body.classList.remove("has-drawer");
  markOpenRow();
  if (restoreFocus && id) document.querySelector(`[data-focus-key="${CSS.escape(`row:${id}`)}"]`)?.focus();
}

els.detailClose.addEventListener("click", () => closeDetail({ restoreFocus: true }));

els.detailSave.addEventListener("click", async () => {
  if (!state.detail || !detailEditor.valid()) return;
  const doc = state.detail;
  els.detailSave.disabled = true;
  try {
    const updated = await api(`/api/documents/${encodeURIComponent(doc.id)}/audience`, {
      method: "PUT",
      body: detailEditor.selection(),
    });
    if (updated?.audience) detailEditor.set(updated.audience);
    if (updated && state.detail?.id === doc.id) refreshDetailMeta(updated);
    setStatus(els.detailStatus, t("dashboard.savedNoReprocess"), "ok");
    await Promise.all([loadDocs(), loadMarkets()]);
  } catch (err) {
    setStatus(els.detailStatus, err.message, "error");
  } finally {
    updateDetailSave();
  }
});

els.detailDelete.addEventListener("click", async () => {
  const doc = state.detail;
  if (!doc) return;
  const { title, body } = splitMessage(t("dashboard.confirmDeleteDoc", { name: doc.original_filename }));
  if (!(await confirmAction({ title, body, confirm: t("dashboard.delete"), danger: true }))) return;
  try {
    await api(`/api/documents/${encodeURIComponent(doc.id)}`, { method: "DELETE" });
    state.selected.delete(doc.id);
    if (state.detail?.id === doc.id) closeDetail();
    flash(t("dashboard.docDeleted", { name: doc.original_filename }));
  } catch (err) {
    setStatus(els.detailStatus, err.message, "error");
    return;
  }
  await Promise.all([loadDocs(), loadMarkets()]);
});

// --- Bulk actions ----------------------------------------------------------------

const dialogEditor = createAudienceEditor({
  onChange: () => {
    els.audienceDialogSave.disabled = !dialogEditor.valid();
  },
});
els.dialogEditor.append(dialogEditor.root);

els.bulkAudienceBtn.addEventListener("click", () => {
  const n = state.selected.size;
  if (!n) return;
  els.audienceDialogTitle.textContent = t("dashboard.bulkTitle", { n });
  dialogEditor.set({ everyone: true });
  setStatus(els.audienceDialogStatus, t("dashboard.bulkReplaces"), "");
  els.audienceDialogSave.disabled = false;
  els.audienceDialog.showModal();
});

els.audienceDialogCancel.addEventListener("click", () => els.audienceDialog.close());

els.audienceDialogSave.addEventListener("click", async () => {
  if (!dialogEditor.valid()) return;
  const ids = [...state.selected];
  els.audienceDialogSave.disabled = true;
  try {
    await api("/api/documents/audience", {
      method: "PUT",
      body: { document_ids: ids, ...dialogEditor.selection() },
    });
    els.audienceDialog.close();
    state.selected.clear();
    flash(t("dashboard.bulkUpdated", { n: ids.length }));
    // The saved answer is the new truth for an open drawer, so its editor is
    // replaced too - otherwise loadDocs leaves it alone, unsaved choices and all.
    await refreshAfterChange(ids);
  } catch (err) {
    setStatus(els.audienceDialogStatus, err.message, "error");
  } finally {
    els.audienceDialogSave.disabled = !dialogEditor.valid();
  }
});

els.bulkDeleteBtn.addEventListener("click", async () => {
  const ids = [...state.selected];
  if (!ids.length) return;
  const { title, body } = splitMessage(t("dashboard.confirmBulkDelete", { n: ids.length }));
  if (!(await confirmAction({ title, body, confirm: t("dashboard.delete"), danger: true }))) return;
  els.bulkDeleteBtn.disabled = true;
  let failed = 0;
  for (const id of ids) {
    try {
      await api(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
      state.selected.delete(id);
      if (state.detail?.id === id) closeDetail();
    } catch {
      failed++;
    }
  }
  els.bulkDeleteBtn.disabled = false;
  flash(
    failed
      ? t("dashboard.bulkDeleteFailed", { failed, total: ids.length })
      : t("dashboard.bulkDeleted", { n: ids.length }),
    failed ? "error" : "ok",
  );
  await Promise.all([loadDocs(), loadMarkets()]);
});

// --- Upload ----------------------------------------------------------------------
// One file at a time, each waiting until it has been indexed before the next
// goes up - the same pacing the chat page's sidebar used, which keeps
// LlamaParse rate-limit pressure low.

function uploadWithProgress(formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.responseType = "text";
    xhr.upload.onprogress = (evt) => {
      if (evt.lengthComputable) onProgress(evt.loaded, evt.total);
    };
    xhr.onload = () => {
      if (Shell.bounceIfLoggedOut(xhr.status)) return;
      let body = {};
      try {
        body = JSON.parse(xhr.responseText || "{}");
      } catch {
        /* ignore */
      }
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => reject(new Error(t("dashboard.networkError")));
    xhr.onabort = () => reject(new Error("aborted"));
    xhr.send(formData);
  });
}

function isUploadable(file) {
  const type = (file.type || "").toLowerCase();
  return type === "application/pdf" || type === "image/jpeg" || /\.(pdf|jpe?g)$/i.test(file.name || "");
}

// From the picker or a drop. The server checks again; this only spares a
// pointless upload and says why at once.
function enqueueFiles(files) {
  const accepted = files.filter(isUploadable);
  const skipped = files.length - accepted.length;
  if (skipped) flash(t("dashboard.notUploadable", { n: skipped }), "error");
  if (!accepted.length) return;
  const collection = els.collectionInput.value.trim() || null;
  for (const file of accepted) {
    state.queue.push({
      qid: state.nextQid++,
      file,
      filename: file.name,
      size: file.size,
      collection,
      status: "queued",
      uploadFrac: 0,
      docId: null,
      chunkCount: null,
      error: null,
    });
  }
  setTrayOpen(true);
  renderQueue();
  pumpQueue();
}

els.uploadBtn.addEventListener("click", () => els.fileInput.click());
els.uploadOptionsBtn.addEventListener("click", () => togglePopover(els.uploadOptionsBtn, els.uploadOptions));

els.fileInput.addEventListener("change", () => {
  const files = Array.from(els.fileInput.files || []);
  els.fileInput.value = "";
  if (files.length) enqueueFiles(files);
});

// Files dragged in from the desktop. Rows dragged within the page are a
// different thing (DRAG_TYPE) and never show this.
let fileDragDepth = 0;

function isFileDrag(e) {
  return Array.from(e.dataTransfer?.types || []).includes("Files");
}

// Insights has no table to drop on, so it takes no files.
const takesFiles = (e) => isFileDrag(e) && state.view.kind !== "insights";

els.libMain.addEventListener("dragenter", (e) => {
  if (!takesFiles(e)) return;
  e.preventDefault();
  fileDragDepth++;
  els.dropzone.hidden = false;
});
els.libMain.addEventListener("dragover", (e) => {
  if (!takesFiles(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
});
els.libMain.addEventListener("dragleave", (e) => {
  if (!takesFiles(e)) return;
  fileDragDepth = Math.max(0, fileDragDepth - 1);
  if (!fileDragDepth) els.dropzone.hidden = true;
});
els.libMain.addEventListener("drop", (e) => {
  if (!takesFiles(e)) return;
  e.preventDefault();
  fileDragDepth = 0;
  els.dropzone.hidden = true;
  enqueueFiles(Array.from(e.dataTransfer.files || []));
});
// A file let go anywhere else must not make the browser open it in place of
// the page - and the pointer says it cannot be dropped there.
window.addEventListener("dragover", (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  if (!takesFiles(e) || !els.libMain.contains(e.target)) e.dataTransfer.dropEffect = "none";
});
window.addEventListener("drop", (e) => {
  if (isFileDrag(e)) e.preventDefault();
});

async function pumpQueue() {
  if (state.queueBusy) return;
  const next = state.queue.find((q) => q.status === "queued");
  if (!next) {
    renderQueue();
    return;
  }
  state.queueBusy = true;
  next.status = "uploading";
  renderQueue();

  const fd = new FormData();
  fd.append("file", next.file);
  if (next.collection) fd.append("collection", next.collection);
  try {
    const { status, body } = await uploadWithProgress(fd, (loaded, total) => {
      next.uploadFrac = total ? loaded / total : 0;
      const bar = els.queueList.querySelector(`li[data-qid="${next.qid}"] .progress-bar`);
      if (bar) bar.style.width = `${(next.uploadFrac * 100).toFixed(1)}%`;
    });
    if (status < 200 || status >= 300) throw new Error(errorText(body, status));
    next.docId = body.document_id;
    next.status = "processing";
    next.file = null;
    renderQueue();
    // The new document is Global: at the top of a newest-first list that
    // includes Global ones, and one more on the sidebar's counts.
    if (!state.q && state.sort === "uploaded_desc" && showsGlobal(state.view)) state.offset = 0;
    await Promise.all([loadDocs(), loadMarkets()]);
    // The queue advances when tick() sees this document reach a final status.
  } catch (err) {
    next.status = "failed";
    next.error = err.message || String(err);
    next.file = null;
    state.queueBusy = false;
    renderQueue();
    pumpQueue();
  }
}

function renderQueue() {
  const items = state.queue;
  els.queue.hidden = items.length === 0;
  if (!items.length) {
    els.queueList.replaceChildren();
    return;
  }
  const counts = {};
  for (const it of items) {
    const key = isIndexed(it.status) ? "done" : it.status;
    counts[key] = (counts[key] || 0) + 1;
  }
  const parts = [];
  if (counts.uploading) parts.push(t("dashboard.queueUploading", { n: counts.uploading }));
  if (counts.processing) parts.push(t("dashboard.queueIndexing", { n: counts.processing }));
  if (counts.queued) parts.push(t("dashboard.queueWaiting", { n: counts.queued }));
  if (counts.done) parts.push(t("dashboard.queueDone", { n: counts.done }));
  if (counts.failed) parts.push(t("dashboard.queueFailed", { n: counts.failed }));
  els.queueSummary.textContent = parts.join(" - ");

  els.queueList.replaceChildren(
    ...items.map((it) => {
      const label = statusLabel(it.status);
      const actions = el(
        "div",
        { class: "queue-actions" },
        el("span", { class: `badge ${it.status}`, text: label }),
        it.status === "queued"
          ? el("button", {
              type: "button",
              class: "btn-secondary",
              text: t("dashboard.remove"),
              onclick: () => {
                state.queue = state.queue.filter((q) => q.qid !== it.qid);
                renderQueue();
              },
            })
          : null,
      );
      const meta = [formatBytes(it.size)];
      if (it.collection) meta.push(t("dashboard.collectionIs", { name: it.collection }));
      if (isIndexed(it.status) && typeof it.chunkCount === "number") {
        meta.push(t("dashboard.chunks", { n: it.chunkCount }));
      }
      const li = el(
        "li",
        { class: "queue-item", "data-qid": String(it.qid) },
        el("div", { class: "queue-row" }, el("div", { class: "queue-name", text: it.filename }), actions),
        el("div", { class: "queue-meta", text: meta.join(" - ") }),
      );
      if (it.status === "uploading") {
        const bar = el("div", { class: "progress-bar" });
        bar.style.width = `${(it.uploadFrac * 100).toFixed(1)}%`;
        li.append(el("div", { class: "progress" }, bar));
      }
      if (it.status === "failed" && it.error) li.append(el("div", { class: "queue-error", text: it.error }));
      return li;
    }),
  );
  els.queueClearBtn.disabled = !items.some((q) => isTerminal(q.status));
}

function setTrayOpen(open) {
  state.trayOpen = open;
  els.queueBody.hidden = !open;
  els.queue.classList.toggle("is-collapsed", !open);
  els.queueToggle.setAttribute("aria-expanded", open ? "true" : "false");
  els.queueToggle.setAttribute("aria-label", t(open ? "dashboard.hideUploads" : "dashboard.showUploads"));
}

els.queueToggle.addEventListener("click", () => setTrayOpen(!state.trayOpen));

els.queueClearBtn.addEventListener("click", () => {
  state.queue = state.queue.filter((q) => !isTerminal(q.status));
  renderQueue();
});

// --- Insights: what people ask -----------------------------------------------------
// The question log (services/query/app/answer_log.py) read back: how many
// questions were asked, how many the documents answered and, grouped by their
// wording, the ones they did not - or that someone gave a thumbs down. It never
// says who asked.

const INSIGHT_DAYS = [7, 30, 90];
// Every grouped question in one call (the query service's maximum), so both
// "Needs attention" and "All questions" are counted without asking twice.
const INSIGHT_LIMIT = 500;
// Worst first, the order a mixed question lists them in.
const COVERAGE = {
  no_hits: { key: "dashboard.covNoHits", cls: "is-bad" },
  not_covered: { key: "dashboard.covNotCovered", cls: "is-bad" },
  partial: { key: "dashboard.covPartial", cls: "is-warn" },
  answered: { key: "dashboard.covAnswered", cls: "is-ok" },
  pending: { key: "dashboard.covPending", cls: "" },
};

function showInsightsPane(on) {
  els.toolbar.hidden = on;
  els.tableCard.hidden = on;
  els.insights.hidden = !on;
  if (!on) return;
  // The drawer and the selection belong to the document list.
  if (state.detail) closeDetail();
  if (state.selected.size) clearSelection();
}

function insightsRailItem() {
  const n = state.attentionCount;
  const li = railItem({ kind: "insights" }, icon("questions"), t("dashboard.insights"), n || 0);
  const count = li.querySelector(".rail-count");
  if (n === null) {
    count.remove();
  } else if (n > 0) {
    count.classList.add("is-alert");
    count.title = t("dashboard.attentionCount", { n });
  }
  return li;
}

function setAttentionCount(n) {
  if (n === state.attentionCount) return;
  state.attentionCount = n;
  keepFocus(() => els.railInsights.replaceChildren(insightsRailItem()));
}

async function loadAttentionCount() {
  try {
    const data = await api(`/api/insights?days=30&kind=attention&limit=${INSIGHT_LIMIT}`);
    setAttentionCount(data?.enabled && data?.ready ? data.groups.length : null);
  } catch {
    /* the sidebar simply shows no count */
  }
}

let insightsSeq = 0;

async function loadInsights() {
  const seq = ++insightsSeq;
  const { days, market } = state.insights;
  els.insights.classList.add("is-loading");
  if (!state.insights.data) renderInsights();
  let data;
  try {
    const params = new URLSearchParams({ days, market, kind: "all", limit: INSIGHT_LIMIT });
    data = await api(`/api/insights?${params}`);
  } catch (err) {
    if (seq !== insightsSeq) return;
    els.insights.classList.remove("is-loading");
    state.insights.error = err.message;
    // Never leave one period's figures under another period's buttons.
    state.insights.data = null;
    renderInsights();
    return;
  }
  // A slower, older request must not paint over a newer one.
  if (seq !== insightsSeq) return;
  els.insights.classList.remove("is-loading");
  state.insights.error = null;
  state.insights.data = data;
  if (days === 30 && !market) {
    setAttentionCount(data?.enabled && data?.ready ? data.groups.filter((g) => g.needs_attention).length : null);
  }
  renderInsights();
}

function setInsightFilter(change) {
  Object.assign(state.insights, change);
  state.insights.open.clear();
  renderViewHead();
  loadInsights();
}

function shownGroups() {
  const groups = state.insights.data?.groups || [];
  return state.insights.show === "attention" ? groups.filter((g) => g.needs_attention) : groups;
}

function insightsActions() {
  const ins = state.insights;
  const period = el(
    "div",
    { class: "seg", role: "group", "aria-label": t("dashboard.insightsPeriod") },
    ...INSIGHT_DAYS.map((n) =>
      el(
        "button",
        {
          type: "button",
          class: "seg-btn",
          "aria-pressed": String(ins.days === n),
          "data-focus-key": `days:${n}`,
          onclick: () => ins.days !== n && setInsightFilter({ days: n }),
        },
        el("span", { text: t("dashboard.lastDays", { n }) }),
      ),
    ),
  );
  // "" is every question; "none" the ones asked with All markets picked in the chat.
  const market = el(
    "select",
    {
      class: "ins-market",
      "aria-label": t("dashboard.insightsMarket"),
      "data-focus-key": "ins-market",
      onchange: (e) => setInsightFilter({ market: e.target.value }),
    },
    el("option", { value: "", text: t("dashboard.anyMarket") }),
    el("option", { value: "none", text: t("dashboard.noMarketChosen") }),
    ...sortedCountries().map((c) => el("option", { value: c.code, text: countryName(c.code) })),
  );
  market.value = ins.market;
  const exportBtn = el(
    "button",
    {
      type: "button",
      class: "btn-secondary btn-with-icon",
      "data-focus-key": "ins-export",
      disabled: !shownGroups().length,
      onclick: exportInsights,
    },
    icon("download"),
    el("span", { text: t("dashboard.exportCsv") }),
  );
  return [period, market, exportBtn];
}

function renderInsights() {
  const { data, error } = state.insights;
  const exportBtn = els.viewActions.querySelector('[data-focus-key="ins-export"]');
  if (exportBtn) exportBtn.disabled = !shownGroups().length;
  let content;
  if (!data) {
    content = error
      ? [insightsEmpty(t("dashboard.loadInsightsFailed", { message: error }), "")]
      : [insightsSkeleton()];
  } else if (!data.enabled) {
    content = [insightsEmpty(t("dashboard.insightsOffTitle"), t("dashboard.insightsOffBody"))];
  } else if (!data.ready) {
    content = [insightsEmpty(t("dashboard.insightsNotReadyTitle"), t("dashboard.insightsNotReadyBody"))];
  } else {
    content = [summaryCards(data), questionsCard(data), insightsFoot(data)];
  }
  keepFocus(() => els.insights.replaceChildren(...content));
}

function insightsEmpty(title, body) {
  return el(
    "div",
    { class: "empty ins-empty" },
    el("div", { class: "empty-icon" }, icon("questions")),
    el("strong", { text: title }),
    body ? el("span", { class: "muted", text: body }) : null,
  );
}

function insightsSkeleton() {
  return el(
    "div",
    { class: "ins-cards", "aria-hidden": "true" },
    ...[58, 44, 52, 40].map((w) =>
      el(
        "div",
        { class: "ins-card" },
        el("span", { class: "sk", style: `width:${w}%` }),
        el("span", { class: "sk sk-figure", style: "width:34%" }),
        el("span", { class: "sk", style: `width:${w + 14}%` }),
      ),
    ),
  );
}

function formatNumber(n) {
  return new Intl.NumberFormat(Lang.current).format(n || 0);
}

function summaryCards(data) {
  const s = data.summary;
  // Answers still being checked count towards neither side.
  const checked = s.total - s.pending;
  const notCovered = s.not_covered + s.no_hits;
  const answered = checked
    ? new Intl.NumberFormat(Lang.current, { style: "percent", maximumFractionDigits: 0 }).format(s.answered / checked)
    : "–";
  const card = (label, value, sub, cls = "") =>
    el(
      "div",
      { class: `ins-card ${cls}`.trim() },
      el("span", { class: "ins-card-label", text: label }),
      el("strong", { class: "ins-card-value", text: value }),
      el("span", { class: "ins-card-sub", text: sub }),
    );
  return el(
    "div",
    { class: "ins-cards" },
    card(t("dashboard.cardAsked"), formatNumber(s.total), t("dashboard.cardAskedSub", { n: data.days })),
    card(t("dashboard.cardAnswered"), answered, t("dashboard.cardAnsweredSub", { n: s.partial })),
    card(
      t("dashboard.cardNotCovered"),
      formatNumber(notCovered),
      t("dashboard.cardNotCoveredSub", { n: s.no_hits }),
      notCovered ? "is-bad" : "",
    ),
    card(t("dashboard.cardDown"), formatNumber(s.down), t("dashboard.cardDownSub", { n: s.up }), s.down ? "is-warn" : ""),
  );
}

function questionsCard(data) {
  const all = data.groups;
  const attention = all.filter((g) => g.needs_attention);
  const groups = state.insights.show === "attention" ? attention : all;
  const showButton = (show, label, n) =>
    el(
      "button",
      {
        type: "button",
        class: "seg-btn",
        "aria-pressed": String(state.insights.show === show),
        "data-focus-key": `show:${show}`,
        onclick: () => {
          state.insights.show = show;
          renderInsights();
        },
      },
      el("span", { text: label }),
      el("span", { class: "seg-count", text: String(n) }),
    );
  let body;
  if (groups.length) {
    body = el("ul", { class: "ins-list" }, ...groups.map(questionRow));
  } else if (!data.summary.total) {
    body = insightsEmpty(t("dashboard.insightsNoneTitle", { n: data.days }), t("dashboard.insightsNoneBody"));
  } else {
    body = insightsEmpty(t("dashboard.insightsCalmTitle"), t("dashboard.insightsCalmBody"));
  }
  return el(
    "div",
    { class: "table-card ins-questions" },
    el(
      "div",
      { class: "ins-list-head" },
      el(
        "div",
        { class: "seg", role: "group", "aria-label": t("dashboard.showLabel") },
        showButton("attention", t("dashboard.needsAttention"), attention.length),
        showButton("all", t("dashboard.allQuestions"), all.length),
      ),
    ),
    body,
  );
}

function coverageBadge(coverage) {
  const c = COVERAGE[coverage] || COVERAGE.pending;
  return el("span", { class: `cov ${c.cls}`.trim(), text: t(c.key) });
}

// "3 hours ago" for this week, a date before that.
function timeAgo(iso) {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const seconds = Math.round((then - Date.now()) / 1000);
  if (Math.abs(seconds) > 7 * 86400) return formatDate(iso);
  try {
    const rtf = new Intl.RelativeTimeFormat(Lang.current, { numeric: "auto" });
    for (const [unit, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
      if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit);
    }
    return rtf.format(0, "minute");
  } catch {
    return formatDate(iso);
  }
}

function timeNode(iso) {
  const d = new Date(iso);
  return el("time", {
    datetime: iso,
    title: Number.isNaN(d.getTime()) ? null : d.toLocaleString(Lang.current),
    text: timeAgo(iso),
  });
}

// Which markets it was asked for: "" is a question asked for all of them.
function marketNames(codes) {
  const names = codes.map((c) => (c ? countryName(c) : t("dashboard.askedAllMarkets")));
  const shown = names.slice(0, 3).join(", ") + (names.length > 3 ? ` +${names.length - 3}` : "");
  return el("span", { title: names.join(", "), text: shown });
}

function questionRow(g) {
  const key = g.question;
  const open = state.insights.open.has(key);
  const li = el("li", { class: `ins-item${open ? " is-open" : ""}` });
  const button = el(
    "button",
    {
      type: "button",
      class: "ins-row",
      "aria-expanded": String(open),
      "data-focus-key": `q:${key}`,
      onclick: () => {
        const nowOpen = !state.insights.open.has(key);
        if (nowOpen) state.insights.open.add(key);
        else state.insights.open.delete(key);
        li.classList.toggle("is-open", nowOpen);
        button.setAttribute("aria-expanded", String(nowOpen));
        if (nowOpen) li.append(questionDetail(g));
        else li.querySelector(".ins-detail")?.remove();
      },
    },
    coverageBadge(g.worst || "pending"),
    el(
      "span",
      { class: "ins-q" },
      el("span", { class: "ins-q-text", text: g.question }),
      el(
        "span",
        { class: "ins-q-meta" },
        el("span", { text: t("dashboard.askedTimes", { n: g.count }) }),
        marketNames(g.markets),
        timeNode(g.last_asked),
      ),
    ),
    g.down
      ? el(
          "span",
          { class: "ins-down", title: t("dashboard.thumbsDownCount", { n: g.down }) },
          icon("thumbDown"),
          el("span", { text: String(g.down) }),
        )
      : null,
    icon("chevron", "icon ins-chevron"),
  );
  li.append(button);
  if (open) li.append(questionDetail(g));
  return li;
}

function questionDetail(g) {
  const section = (label, body) => el("div", { class: "ins-section" }, el("h3", { text: label }), body);
  const order = Object.keys(COVERAGE);
  const mix = Object.entries(g.coverage).sort(([a], [b]) => order.indexOf(a) - order.indexOf(b));
  return el(
    "div",
    { class: "ins-detail" },
    // Asked several times and judged differently: how often each way.
    mix.length > 1
      ? el(
          "div",
          { class: "ins-mix" },
          ...mix.map(([c, n]) => el("span", { class: `ins-pill ${COVERAGE[c]?.cls || ""}`.trim(), text: `${t(COVERAGE[c]?.key || COVERAGE.pending.key)} × ${n}` })),
        )
      : null,
    section(t("dashboard.latestAnswer"), el("p", { class: "ins-answer", text: String(g.answer || "").replace(/\s*\[\d+\]/g, "") })),
    section(
      t("dashboard.sourcesUsed"),
      g.sources.length
        ? el("div", { class: "chips" }, ...g.sources.map((name) => el("span", { class: "chip" }, icon("file"), el("span", { text: name }))))
        : el("span", { class: "muted", text: t("dashboard.noSourcesUsed") }),
    ),
    g.comments.length
      ? section(
          t("dashboard.whatPeopleSaid"),
          el("ul", { class: "ins-comments" }, ...g.comments.map((c) => el("li", {}, el("q", { text: c.text }), timeNode(c.at)))),
        )
      : null,
  );
}

function insightsFoot(data) {
  const parts = [t("dashboard.insightsPrivacy", { n: data.retention_days })];
  if (data.truncated) parts.push(t("dashboard.insightsTruncated", { n: formatNumber(data.summary.total) }));
  return el("p", { class: "ins-foot", text: parts.join(" ") });
}

// A cell a spreadsheet would run as a formula is kept as text.
function csvCell(value) {
  let s = String(value ?? "");
  if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// What is on screen, built in the browser: nothing is asked of the server again.
function exportInsights() {
  const groups = shownGroups();
  if (!groups.length) return;
  const head = [
    "question", "times_asked", "coverage", "unanswered", "thumbs_up", "thumbs_down",
    "markets", "last_asked", "documents", "comments", "latest_answer",
  ];
  const rows = groups.map((g) => [
    g.question,
    g.count,
    g.worst || "pending",
    g.unanswered,
    g.up,
    g.down,
    g.markets.map((m) => m || "all").join(" "),
    g.last_asked,
    g.sources.join("; "),
    g.comments.map((c) => c.text).join(" | "),
    g.answer,
  ]);
  // The byte-order mark makes Excel read the file as UTF-8 (å, ä, ö).
  const csv = "\ufeff" + [head, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const { days, market, show } = state.insights;
  const name = `questions-${show}-${days}d-${market || "all"}-${new Date().toISOString().slice(0, 10)}.csv`;
  const link = el("a", { href: url, download: name, hidden: true });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// --- Background refresh ----------------------------------------------------------
// Only while something is still being indexed, and only while the tab is
// visible - an idle dashboard sends nothing.

function hasWork() {
  return (
    state.queue.some((q) => q.status === "processing") ||
    state.docs.some((d) => !isTerminal(d.status))
  );
}

function scheduleTick() {
  clearTimeout(state.tickHandle);
  state.tickHandle = null;
  if (document.hidden || !hasWork()) return;
  state.tickHandle = setTimeout(tick, TICK_MS);
}

async function tick() {
  for (const q of state.queue.filter((x) => x.status === "processing" && x.docId)) {
    try {
      const doc = await api(`/api/documents/${encodeURIComponent(q.docId)}`);
      if (isTerminal(doc.status)) {
        q.status = doc.status;
        q.chunkCount = doc.chunk_count;
        if (doc.status === "failed") q.error = doc.error_message || t("dashboard.processingFailed");
        state.queueBusy = false;
      }
    } catch (err) {
      if (err.status === 404) {
        q.status = "failed";
        q.error = t("dashboard.deletedBeforeDone");
        state.queueBusy = false;
      }
    }
  }
  renderQueue();
  if (!state.queueBusy) pumpQueue();
  await loadDocs();
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    if (state.view.kind === "insights") loadInsights();
    else loadDocs();
    loadMarkets();
  } else {
    clearTimeout(state.tickHandle);
  }
});

// --- Language switch -------------------------------------------------------------
// Lang.apply() has already relabelled everything marked in the HTML; this redoes
// what the page built itself. Unsaved choices in the editors survive it.

document.addEventListener("langchange", () => {
  renderRail();
  renderViewHead();
  renderDocs();
  if (state.view.kind === "insights") renderInsights();
  renderQueue();
  setTrayOpen(state.trayOpen);
  detailEditor.render();
  dialogEditor.render();
  if (state.detail) refreshDetailMeta(state.detail);
  if (els.groupDialog.open) renderGroupChecklist();
  if (els.countryDialog.open) renderCountryResults();
});

// --- Start -----------------------------------------------------------------------

function paintIcons() {
  els.railClose.append(icon("close"));
  els.newGroupBtn.append(icon("plus"));
  els.addCountryBtn.append(icon("plus"));
  els.searchIcon.append(icon("search"));
  els.uploadIcon.append(icon("upload"));
  els.uploadOptionsBtn.append(icon("chevron"));
  els.dropzoneIcon.append(icon("upload"));
  els.detailClose.append(icon("close"));
  els.bulkClearBtn.append(icon("close"));
  els.trayIcon.append(icon("upload"));
  els.queueToggle.append(icon("chevron"));
}

paintIcons();
loadSort();
renderSortHeaders();
loadMarkets().finally(applyRoute);
loadAttentionCount();
