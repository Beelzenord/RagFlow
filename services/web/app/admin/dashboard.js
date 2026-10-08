// Dashboard - upload documents, decide who each is for, and manage markets.
//
// Served only to admins (see /dashboard in main.py); every endpoint it calls
// checks the role again. All server text is rendered with textContent.

const PAGE_SIZE = 25;
const TICK_MS = 3000;

const state = {
  markets: { countries: [], groups: [], global_documents: 0 },
  catalogue: null,
  docs: [],
  total: 0,
  offset: 0,
  q: "",
  who: "",
  selected: new Set(),
  detail: null,
  previewUrl: null,
  queue: [],
  queueBusy: false,
  nextQid: 1,
  tickHandle: null,
};

const $ = (id) => document.getElementById(id);

const els = {
  status: $("dash-status"),
  tabs: { documents: $("tab-documents"), markets: $("tab-markets") },
  views: { documents: $("view-documents"), markets: $("view-markets") },
  uploadBtn: $("upload-btn"),
  fileInput: $("file-input"),
  collectionInput: $("collection-input"),
  queue: $("queue"),
  queueSummary: $("queue-summary"),
  queueList: $("queue-list"),
  queueClearBtn: $("queue-clear-btn"),
  docsWrap: $("dash-docs"),
  search: $("doc-search"),
  whoFilter: $("who-filter"),
  bulkBar: $("bulk-bar"),
  bulkCount: $("bulk-count"),
  bulkAudienceBtn: $("bulk-audience-btn"),
  bulkDeleteBtn: $("bulk-delete-btn"),
  bulkClearBtn: $("bulk-clear-btn"),
  selectAll: $("select-all"),
  rows: $("doc-rows"),
  docsEmpty: $("docs-empty"),
  prev: $("prev-page"),
  next: $("next-page"),
  pageInfo: $("page-info"),
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
  globalCount: $("global-count"),
  countriesSummary: $("countries-summary"),
  countryList: $("country-list"),
  countrySearch: $("country-search"),
  countryResults: $("country-results"),
  groupList: $("group-list"),
  newGroupBtn: $("new-group-btn"),
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

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

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

function flash(text, kind = "ok") {
  setStatus(els.status, text, kind);
}

function errorText(data, status) {
  const d = data?.detail ?? data?.error;
  if (typeof d === "string") return d;
  if (d && typeof d.message === "string") return d.message;
  if (Array.isArray(d) && d.length) return d.map((e) => e?.msg || String(e)).join("; ");
  return `Something went wrong (HTTP ${status})`;
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

const STATUS_LABELS = {
  uploaded: "Queued",
  processing: "Indexing...",
  completed: "Ready",
  degraded: "Ready",
  failed: "Failed",
};

function statusBadge(doc) {
  const badge = el("span", {
    class: `badge ${doc.status || "uploaded"}`,
    text: STATUS_LABELS[doc.status] || doc.status || "Queued",
  });
  if (doc.status === "degraded") {
    badge.title = "Some pages were recovered from the raw PDF text - content is complete, layout is not.";
  } else if (doc.status === "failed" && doc.error_message) {
    badge.title = doc.error_message;
  }
  return badge;
}

function formatDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
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

function countryName(code) {
  return state.markets.countries.find((c) => c.code === code)?.name || code;
}

// --- Who it's for: chips ---------------------------------------------------------

function globalChip() {
  return el("span", { class: "chip chip-global", text: "Global" });
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
    })),
    ...audience.countries.map((c) => ({ label: c.name, title: c.code, cls: "chip" })),
  ];
  for (const it of items.slice(0, max)) {
    wrap.append(el("span", { class: it.cls, text: it.label, title: it.title }));
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
    placeholder: "Filter markets and groups",
    "aria-label": "Filter markets and groups",
  });
  const chips = el("div", { class: "chips picked", "aria-live": "polite" });
  const options = el("div", { class: "checklist" });
  const covers = el("p", { class: "covers" });
  const empty = el("p", { class: "hint", hidden: true });
  const pick = el("div", { class: "aud-pick" }, chips, filter, options, covers, empty);

  const root = el(
    "fieldset",
    { class: "audience" },
    el("legend", { text: "Who is this for?" }),
    el(
      "label",
      { class: "choice" },
      everyoneInput,
      el(
        "span",
        {},
        el("strong", { text: "Everyone" }),
        el("small", { text: "Global - every market" }),
      ),
    ),
    el(
      "label",
      { class: "choice" },
      onlyInput,
      el(
        "span",
        {},
        el("strong", { text: "Only these markets" }),
        el("small", { text: "Pick countries or groups" }),
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
    const countries = state.markets.countries.filter((c) => match(c.name, c.code));
    if (groups.length) {
      nodes.push(el("p", { class: "checklist-head", text: "Groups" }));
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
      nodes.push(el("p", { class: "checklist-head", text: "Countries" }));
      for (const c of countries) {
        const box = el("input", {
          type: "checkbox",
          checked: picked.countries.has(c.code),
          onchange: (e) => toggle("country", c.code, e.target.checked),
        });
        nodes.push(
          el("label", { class: "check" }, box, el("span", { text: c.name }), el("small", { text: c.code })),
        );
      }
    }
    if (!nodes.length && f) nodes.push(el("p", { class: "hint", text: "Nothing matches that filter." }));
    options.replaceChildren(...nodes);
  }

  function renderChips() {
    const nodes = [];
    for (const id of picked.groups) {
      const g = state.markets.groups.find((x) => x.id === id);
      nodes.push(removableChip(g?.name || "Removed group", "chip chip-group", () => toggle("group", id, false)));
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
    empty.textContent = noMarkets ? "No markets yet. Add countries on the Markets tab first." : "";
    filter.hidden = noMarkets;
    renderChips();
    renderOptions();
    const codes = coveredCodes().map(countryName).sort((a, b) => a.localeCompare(b));
    covers.textContent = codes.length
      ? `Covers: ${codes.join(", ")}`
      : "Pick at least one market, or choose Everyone.";
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
    render,
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
      "aria-label": `Remove ${label}`,
      text: "×",
      onclick: onRemove,
    }),
  );
}

// --- Tabs ------------------------------------------------------------------------

function showView() {
  const view = location.hash === "#markets" ? "markets" : "documents";
  for (const [name, tab] of Object.entries(els.tabs)) {
    const active = name === view;
    tab.setAttribute("aria-selected", active ? "true" : "false");
    els.views[name].hidden = !active;
  }
  setStatus(els.status, "");
}

window.addEventListener("hashchange", showView);

// --- Markets ---------------------------------------------------------------------

async function loadMarkets() {
  try {
    const data = await api("/api/markets");
    state.markets = {
      countries: data?.countries || [],
      groups: data?.groups || [],
      global_documents: data?.global_documents ?? 0,
    };
  } catch (err) {
    flash(`Could not load markets: ${err.message}`, "error");
    return;
  }
  renderMarkets();
  renderWhoFilter();
  detailEditor.render();
  dialogEditor.render();
}

function renderMarkets() {
  const { countries, groups, global_documents: globalDocs } = state.markets;
  els.globalCount.textContent = `(${plural(globalDocs, "document")})`;
  els.countriesSummary.textContent = countries.length
    ? `${plural(countries.length, "country", "countries")} in your market.`
    : "No countries yet. Until you add one, every document is for everyone.";

  els.countryList.replaceChildren(
    ...countries.map((c) => {
      const usage = [];
      if (c.documents) usage.push(plural(c.documents, "document"));
      if (c.groups?.length) usage.push(`in ${c.groups.join(", ")}`);
      // What actually blocks removal: a group containing it, or a document
      // that names it directly. The database refuses either way.
      const reasons = [];
      if (c.groups?.length) reasons.push(`it is in ${c.groups.join(", ")}`);
      if (c.direct_documents) {
        reasons.push(`${plural(c.direct_documents, "document")} ${c.direct_documents === 1 ? "is" : "are"} limited to it`);
      }
      const blocked = reasons.length > 0;
      const why = blocked ? `Can't remove: ${reasons.join(" and ")}.` : "";
      return el(
        "li",
        { class: "market-item" },
        el(
          "div",
          { class: "market-main" },
          el("span", { class: "market-name", text: c.name }),
          el("span", { class: "market-code", text: c.code }),
          el("span", { class: "muted", text: usage.join(" - ") }),
        ),
        el("button", {
          type: "button",
          class: "btn-danger",
          text: "Remove",
          disabled: blocked,
          title: why || `Remove ${c.name}`,
          "aria-label": `Remove ${c.name}`,
          onclick: () => removeCountry(c),
        }),
        blocked ? el("p", { class: "market-why", text: why }) : null,
      );
    }),
  );

  els.groupList.replaceChildren(
    ...groups.map((g) => {
      const blocked = g.documents > 0;
      const why = blocked
        ? `Can't delete: ${plural(g.documents, "document")} ${g.documents === 1 ? "is" : "are"} limited to it.`
        : "";
      return el(
        "li",
        { class: "group-card" },
        el(
          "div",
          { class: "group-head" },
          el("strong", { text: g.name }),
          el("span", { class: "muted", text: plural(g.documents, "document") }),
        ),
        el(
          "div",
          { class: "chips" },
          ...(g.members || []).map((code) => el("span", { class: "chip", text: countryName(code), title: code })),
        ),
        el(
          "div",
          { class: "group-actions" },
          el("button", { type: "button", class: "btn-secondary", text: "Edit", onclick: () => openGroupDialog(g) }),
          el("button", {
            type: "button",
            class: "btn-danger",
            text: "Delete",
            disabled: blocked,
            title: why || `Delete ${g.name}`,
            onclick: () => deleteGroup(g),
          }),
        ),
        blocked ? el("p", { class: "market-why", text: why }) : null,
      );
    }),
  );
  if (!groups.length) {
    els.groupList.append(el("li", { class: "muted", text: "No groups yet." }));
  }
  els.newGroupBtn.disabled = !countries.length;
  els.newGroupBtn.title = countries.length ? "" : "Add countries first";
  renderCountryResults();
}

async function removeCountry(c) {
  if (!confirm(`Remove ${c.name} from your markets?`)) return;
  try {
    await api(`/api/markets/countries/${encodeURIComponent(c.code)}`, { method: "DELETE" });
    flash(`${c.name} removed.`);
  } catch (err) {
    flash(err.message, "error");
  }
  await loadMarkets();
}

async function ensureCatalogue() {
  if (state.catalogue) return;
  try {
    const data = await api("/api/markets/catalogue");
    state.catalogue = data?.countries || [];
  } catch (err) {
    flash(`Could not load the country list: ${err.message}`, "error");
  }
}

function renderCountryResults() {
  const f = fold(els.countrySearch.value.trim());
  if (!f || !state.catalogue) {
    els.countryResults.replaceChildren();
    return;
  }
  const added = new Set(state.markets.countries.map((c) => c.code));
  const matches = state.catalogue
    .filter((c) => fold(c.name).includes(f) || c.code.toLowerCase() === f)
    // An exact code match first, then names that start with what was typed.
    .sort((a, b) => {
      const rank = (c) => (c.code.toLowerCase() === f ? 0 : fold(c.name).startsWith(f) ? 1 : 2);
      return rank(a) - rank(b) || a.name.localeCompare(b.name);
    })
    .slice(0, 8);
  if (!matches.length) {
    els.countryResults.replaceChildren(el("li", { class: "muted", text: "No country matches that." }));
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
          el("span", { text: c.name }),
          el("span", { class: "market-code", text: c.code }),
          el("span", { class: "muted", text: isAdded ? "Added" : "Add" }),
        ),
      );
    }),
  );
}

async function addCountry(c) {
  try {
    await api("/api/markets/countries", { method: "POST", body: { code: c.code } });
    flash(`${c.name} added. It can now be picked for documents, groups and in the chat.`);
    els.countrySearch.value = "";
  } catch (err) {
    flash(err.message, "error");
  }
  await loadMarkets();
  els.countrySearch.focus();
}

els.countrySearch.addEventListener("focus", ensureCatalogue);
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
  const countries = state.markets.countries.filter(
    (c) => !f || fold(c.name).includes(f) || fold(c.code).includes(f),
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
        el("span", { text: c.name }),
        el("small", { text: c.code }),
      ),
    ),
  );
}

function openGroupDialog(group) {
  editingGroup = group || null;
  groupPicked = new Set(group?.members || []);
  els.groupDialogTitle.textContent = group ? `Edit ${group.name}` : "New group";
  els.groupName.value = group?.name || "";
  els.groupFilter.value = "";
  const n = group?.documents || 0;
  els.groupWarning.hidden = n === 0;
  els.groupWarning.textContent = n
    ? `Used by ${plural(n, "document")} - ${n === 1 ? "it" : "they"} will follow this change as soon as you save.`
    : "";
  setStatus(els.groupDialogStatus, "");
  renderGroupChecklist();
  els.groupDialog.showModal();
  els.groupName.focus();
}

async function saveGroup() {
  const name = els.groupName.value.trim();
  if (!name) {
    setStatus(els.groupDialogStatus, "Give the group a name.", "error");
    return;
  }
  if (!groupPicked.size) {
    setStatus(els.groupDialogStatus, "Tick at least one country.", "error");
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
    flash(editingGroup ? `${name} saved.` : `${name} created.`);
    await loadMarkets();
    if (editingGroup) loadDocs();
  } catch (err) {
    setStatus(els.groupDialogStatus, err.message, "error");
  } finally {
    els.groupDialogSave.disabled = false;
  }
}

async function deleteGroup(g) {
  if (!confirm(`Delete the group ${g.name}? The countries in it stay in your market.`)) return;
  try {
    await api(`/api/markets/groups/${g.id}`, { method: "DELETE" });
    flash(`${g.name} deleted.`);
  } catch (err) {
    flash(err.message, "error");
  }
  await loadMarkets();
}

els.newGroupBtn.addEventListener("click", () => openGroupDialog(null));
els.groupFilter.addEventListener("input", renderGroupChecklist);
els.groupDialogCancel.addEventListener("click", () => els.groupDialog.close());
els.groupDialogSave.addEventListener("click", saveGroup);

// --- Documents table -------------------------------------------------------------

function renderWhoFilter() {
  const current = els.whoFilter.value || state.who;
  const opts = [
    el("option", { value: "", text: "Anyone" }),
    el("option", { value: "global", text: "Global (everyone)" }),
  ];
  if (state.markets.groups.length) {
    opts.push(
      el(
        "optgroup",
        { label: "Groups" },
        ...state.markets.groups.map((g) => el("option", { value: g.id, text: g.name })),
      ),
    );
  }
  if (state.markets.countries.length) {
    opts.push(
      el(
        "optgroup",
        { label: "Countries" },
        ...state.markets.countries.map((c) => el("option", { value: c.code, text: c.name })),
      ),
    );
  }
  els.whoFilter.replaceChildren(...opts);
  const values = [...els.whoFilter.querySelectorAll("option")].map((o) => o.value);
  els.whoFilter.value = values.includes(current) ? current : "";
  state.who = els.whoFilter.value;
}

let loadSeq = 0;

async function loadDocs() {
  const seq = ++loadSeq;
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(state.offset) });
  if (state.q) params.set("q", state.q);
  if (state.who) params.set("who", state.who);
  let data;
  try {
    data = await api(`/api/documents?${params}`);
  } catch (err) {
    flash(`Could not load documents: ${err.message}`, "error");
    return;
  }
  // A slower, older request must not paint over a newer one.
  if (seq !== loadSeq) return;
  state.docs = data?.documents || [];
  state.total = data?.total ?? state.docs.length;
  if (state.offset && state.offset >= state.total) {
    state.offset = Math.max(0, Math.floor((state.total - 1) / PAGE_SIZE) * PAGE_SIZE);
    return loadDocs();
  }
  renderDocs();
  if (state.detail) {
    const fresh = state.docs.find((d) => d.id === state.detail.id);
    if (fresh) refreshDetailMeta(fresh);
  }
  scheduleTick();
}

function renderDocs() {
  const rows = state.docs.map((d) => {
    const box = el("input", {
      type: "checkbox",
      checked: state.selected.has(d.id),
      "aria-label": `Select ${d.original_filename}`,
      onclick: (e) => e.stopPropagation(),
      onchange: (e) => {
        if (e.target.checked) state.selected.add(d.id);
        else state.selected.delete(d.id);
        renderBulkBar();
      },
    });
    const name = el("button", {
      type: "button",
      class: "link-btn",
      text: d.original_filename || d.id,
      onclick: (e) => {
        e.stopPropagation();
        openDetail(d);
      },
    });
    const tr = el(
      "tr",
      {
        class: state.detail?.id === d.id ? "is-open" : "",
        onclick: () => openDetail(d),
      },
      el("td", { class: "col-check" }, box),
      el("td", { class: "col-name" }, name, d.collection ? el("small", { class: "muted", text: d.collection }) : null),
      el("td", {}, audienceChips(d.audience)),
      el("td", {}, statusBadge(d)),
      el("td", { class: "col-date", text: formatDate(d.created_at) }),
    );
    return tr;
  });
  els.rows.replaceChildren(...rows);

  const filtered = state.q || state.who;
  els.docsEmpty.hidden = state.docs.length > 0;
  els.docsEmpty.textContent = filtered
    ? "No documents match these filters."
    : "No documents yet. Upload one above to get started.";

  const from = state.total ? state.offset + 1 : 0;
  const to = state.offset + state.docs.length;
  els.pageInfo.textContent = state.total ? `${from}-${to} of ${state.total}` : "";
  els.prev.disabled = state.offset === 0;
  els.next.disabled = to >= state.total;
  renderBulkBar();
}

function renderBulkBar() {
  const n = state.selected.size;
  els.bulkBar.hidden = n === 0;
  els.bulkCount.textContent = `${plural(n, "document")} selected`;
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
    state.selected.clear();
    loadDocs();
  }, 250);
});

els.whoFilter.addEventListener("change", () => {
  state.who = els.whoFilter.value;
  state.offset = 0;
  state.selected.clear();
  loadDocs();
});

els.prev.addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - PAGE_SIZE);
  state.selected.clear();
  loadDocs();
});

els.next.addEventListener("click", () => {
  state.offset += PAGE_SIZE;
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

// --- Detail panel ----------------------------------------------------------------

const detailEditor = createAudienceEditor({ onChange: updateDetailSave });
els.detailEditor.append(detailEditor.root);

function updateDetailSave() {
  els.detailSave.disabled = !detailEditor.valid();
}

function refreshDetailMeta(doc) {
  state.detail = doc;
  const facts = [statusBadge(doc)];
  if (typeof doc.chunk_count === "number" && doc.chunk_count > 0) {
    facts.push(el("span", { text: `${doc.chunk_count} chunks` }));
  }
  facts.push(el("span", { text: `Uploaded ${formatDate(doc.created_at)}` }));
  if (doc.collection) facts.push(el("span", { text: `Collection: ${doc.collection}` }));
  const notes = [];
  if (doc.status === "failed" && doc.error_message) {
    notes.push(el("p", { class: "doc-error", text: doc.error_message }));
  }
  if (doc.status === "degraded") {
    notes.push(
      el("p", {
        class: "doc-note",
        text: "Some pages were recovered from the raw PDF text - content is complete, layout is not.",
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

// The file is fetched once as a blob and shown from that, so a missing original
// becomes a plain message instead of an error page inside the frame.
async function loadPreview(doc) {
  clearPreview();
  const type = (doc.file_type || "").toLowerCase();
  if (type !== "application/pdf" && type !== "image/jpeg") {
    els.preview.append(el("p", { class: "muted", text: "No preview for this file type." }));
    return;
  }
  els.preview.append(el("p", { class: "muted", text: "Loading preview..." }));
  try {
    const resp = await fetch(`/api/documents/${encodeURIComponent(doc.id)}/file?inline=true`);
    if (Shell.bounceIfLoggedOut(resp.status)) return;
    if (!resp.ok) throw new Error(resp.status === 404 ? "missing" : `HTTP ${resp.status}`);
    const blob = await resp.blob();
    if (state.detail?.id !== doc.id) return;
    state.previewUrl = URL.createObjectURL(blob);
    const view =
      type === "image/jpeg"
        ? el("img", { src: state.previewUrl, alt: `Preview of ${doc.original_filename}` })
        : el("iframe", { src: state.previewUrl, title: `Preview of ${doc.original_filename}` });
    els.preview.replaceChildren(view);
  } catch (err) {
    if (state.detail?.id !== doc.id) return;
    els.preview.replaceChildren(
      el("p", {
        class: "muted",
        text: err.message === "missing" ? "Original file not available." : `Preview failed: ${err.message}`,
      }),
    );
  }
}

function openDetail(doc) {
  const switching = state.detail?.id !== doc.id;
  refreshDetailMeta(doc);
  els.detailName.textContent = doc.original_filename || doc.id;
  els.detailDownload.href = `/api/documents/${encodeURIComponent(doc.id)}/file`;
  els.detail.hidden = false;
  els.docsWrap.classList.add("has-detail");
  if (switching) {
    detailEditor.set(doc.audience);
    setStatus(els.detailStatus, "");
    loadPreview(doc);
  }
  updateDetailSave();
  renderDocs();
  if (window.matchMedia("(max-width: 1100px)").matches) {
    els.detail.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function closeDetail() {
  state.detail = null;
  clearPreview();
  els.detail.hidden = true;
  els.docsWrap.classList.remove("has-detail");
  renderDocs();
}

els.detailClose.addEventListener("click", closeDetail);

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
    setStatus(els.detailStatus, "Saved. It applies from the next question - nothing was re-processed.", "ok");
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
  if (!confirm(`Delete "${doc.original_filename}"?\nThis removes its embeddings and the stored file.`)) return;
  try {
    await api(`/api/documents/${encodeURIComponent(doc.id)}`, { method: "DELETE" });
    state.selected.delete(doc.id);
    closeDetail();
    flash(`Deleted ${doc.original_filename}.`);
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
  els.audienceDialogTitle.textContent = `Who ${n === 1 ? "is this document" : `are these ${n} documents`} for?`;
  dialogEditor.set({ everyone: true });
  setStatus(els.audienceDialogStatus, "Replaces what each of them is set to now.", "");
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
    flash(`Updated ${plural(ids.length, "document")}. Nothing was re-processed.`);
    const openId = state.detail?.id;
    await Promise.all([loadDocs(), loadMarkets()]);
    // loadDocs refreshes the open panel's facts but leaves its editor alone, so
    // as not to wipe unsaved choices; here the saved answer is the new truth.
    if (openId && ids.includes(openId)) {
      const fresh = state.docs.find((d) => d.id === openId);
      if (fresh) detailEditor.set(fresh.audience);
    }
  } catch (err) {
    setStatus(els.audienceDialogStatus, err.message, "error");
  } finally {
    els.audienceDialogSave.disabled = !dialogEditor.valid();
  }
});

els.bulkDeleteBtn.addEventListener("click", async () => {
  const ids = [...state.selected];
  if (!ids.length) return;
  if (!confirm(`Delete ${plural(ids.length, "document")}?\nThis removes their embeddings and stored files.`)) return;
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
    failed ? `${failed} of ${ids.length} could not be deleted.` : `Deleted ${plural(ids.length, "document")}.`,
    failed ? "error" : "ok",
  );
  await Promise.all([loadDocs(), loadMarkets()]);
});

// --- Upload queue ----------------------------------------------------------------
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
    xhr.onerror = () => reject(new Error("network error"));
    xhr.onabort = () => reject(new Error("aborted"));
    xhr.send(formData);
  });
}

els.uploadBtn.addEventListener("click", () => els.fileInput.click());

els.fileInput.addEventListener("change", () => {
  const files = Array.from(els.fileInput.files || []);
  if (!files.length) return;
  const collection = els.collectionInput.value.trim() || null;
  for (const file of files) {
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
  els.fileInput.value = "";
  renderQueue();
  pumpQueue();
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
    // The new document is Global and at the top of the newest-first list.
    if (!state.q && !state.who) state.offset = 0;
    await loadDocs();
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
  if (counts.uploading) parts.push(`${counts.uploading} uploading`);
  if (counts.processing) parts.push(`${counts.processing} indexing`);
  if (counts.queued) parts.push(`${counts.queued} waiting`);
  if (counts.done) parts.push(`${counts.done} done`);
  if (counts.failed) parts.push(`${counts.failed} failed`);
  els.queueSummary.textContent = parts.join(" - ");

  els.queueList.replaceChildren(
    ...items.map((it) => {
      const label = it.status === "uploading" ? "Uploading" : STATUS_LABELS[it.status] || it.status;
      const actions = el(
        "div",
        { class: "queue-actions" },
        el("span", { class: `badge ${it.status}`, text: label }),
        it.status === "queued"
          ? el("button", {
              type: "button",
              class: "btn-secondary",
              text: "Remove",
              onclick: () => {
                state.queue = state.queue.filter((q) => q.qid !== it.qid);
                renderQueue();
              },
            })
          : null,
      );
      const meta = [formatBytes(it.size)];
      if (it.collection) meta.push(`collection: ${it.collection}`);
      if (isIndexed(it.status) && typeof it.chunkCount === "number") meta.push(`${it.chunkCount} chunks`);
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

els.queueClearBtn.addEventListener("click", () => {
  state.queue = state.queue.filter((q) => !isTerminal(q.status));
  renderQueue();
});

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
        if (doc.status === "failed") q.error = doc.error_message || "processing failed";
        state.queueBusy = false;
      }
    } catch (err) {
      if (err.status === 404) {
        q.status = "failed";
        q.error = "deleted before it finished";
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
    loadDocs();
    loadMarkets();
  } else {
    clearTimeout(state.tickHandle);
  }
});

// --- Start -----------------------------------------------------------------------

showView();
loadMarkets().then(loadDocs);
