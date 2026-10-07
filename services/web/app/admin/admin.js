// Filing page: the corpus inventory, retagging, and the country catalogue.
//
// Served only to admins (see /admin in main.py), and every endpoint it calls is
// guarded on its own. Filenames are user-controlled, so everything from the
// server goes into the page through textContent - never innerHTML.

const PAGE_SIZE = 25;
const SEARCH_DEBOUNCE_MS = 250;

const state = {
  docs: [],
  total: 0,
  offset: 0,
  // Selection is limited to the rows on screen and cleared whenever the list
  // changes underneath it, so a bulk action can never reach a document the
  // admin cannot currently see.
  selected: new Set(),
  scopes: { groups: [], regions: [] },
  retag: null, // { ids: [...], label: "..." }
  regions: [],
};

const $ = (id) => document.getElementById(id);

const els = {
  tabDocs: $("tab-documents"),
  tabCountries: $("tab-countries"),
  panelDocs: $("panel-documents"),
  panelCountries: $("panel-countries"),
  fQ: $("f-q"),
  fScope: $("f-scope"),
  fStatus: $("f-status"),
  fCollection: $("f-collection"),
  fSort: $("f-sort"),
  selectAll: $("select-all"),
  selectionCount: $("selection-count"),
  bulkRetag: $("bulk-retag"),
  bulkDelete: $("bulk-delete"),
  backfill: $("backfill"),
  docStatus: $("doc-status"),
  docRows: $("doc-rows"),
  docEmpty: $("doc-empty"),
  pagePrev: $("page-prev"),
  pageNext: $("page-next"),
  pageInfo: $("page-info"),
  cQ: $("c-q"),
  cShow: $("c-show"),
  countryStatus: $("country-status"),
  countrySummary: $("country-summary"),
  countryRows: $("country-rows"),
  countryEmpty: $("country-empty"),
  dialog: $("retag-dialog"),
  retagForm: $("retag-form"),
  retagTarget: $("retag-target"),
  retagScope: $("retag-scope"),
  retagError: $("retag-error"),
  retagCancel: $("retag-cancel"),
  retagSave: $("retag-save"),
};

// ---------------------------------------------------------------- utilities

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c) node.append(c);
  return node;
}

function setStatus(node, message, kind = "") {
  node.textContent = message || "";
  node.className = `status ${kind}`.trim();
}

// The ingestion service answers with a plain string, an object carrying a
// message (409s explain themselves that way), or FastAPI's validation list.
// All three have to reach the admin as a sentence.
function errorText(body, status) {
  const d = body?.detail ?? body?.error;
  if (typeof d === "string") return d;
  if (d && typeof d === "object" && !Array.isArray(d) && d.message) return d.message;
  if (Array.isArray(d) && d.length) return d.map((e) => e.msg || String(e)).join("; ");
  return `Request failed (HTTP ${status})`;
}

async function api(path, options = {}) {
  const init = { ...options, headers: { ...(options.headers || {}) } };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
    init.headers["content-type"] = "application/json";
  }
  const resp = await fetch(path, init);
  // Signed out mid-visit: going back to /admin lets the login gate send the
  // browser wherever sign-in lives (the app's form, or Microsoft's).
  if (resp.status === 401) {
    location.href = "/admin";
    throw new Error("signed out");
  }
  // No longer an admin: this page is not theirs.
  if (resp.status === 403) {
    location.href = "/";
    throw new Error("not an admin");
  }
  const body = await resp.json().catch(() => ({}));
  return { ok: resp.ok, status: resp.status, body };
}

// Lets "Cote" find Côte d'Ivoire and "Aland" find the Åland Islands.
function fold(s) {
  return String(s || "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

function debounce(fn, ms) {
  let t = 0;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

// --------------------------------------------------------------------- tabs

function showTab(name) {
  const countries = name === "countries";
  els.tabDocs.setAttribute("aria-selected", String(!countries));
  els.tabCountries.setAttribute("aria-selected", String(countries));
  els.panelDocs.hidden = countries;
  els.panelCountries.hidden = !countries;
  if (countries && !state.regions.length) loadRegions();
  const hash = countries ? "#countries" : "#documents";
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

els.tabDocs.addEventListener("click", () => showTab("documents"));
els.tabCountries.addEventListener("click", () => showTab("countries"));

// ------------------------------------------------------------------- scopes

// Built from the database so the pickers can never offer a code the server
// would refuse. Called again after a country is enabled or disabled.
async function loadScopes() {
  const { ok, status, body } = await api("/api/scopes");
  if (!ok) {
    setStatus(els.docStatus, `Could not load the scope list: ${errorText(body, status)}`, "error");
    return;
  }
  state.scopes = { groups: body.groups || [], regions: body.regions || [] };

  // Filter: keep "Anything" and "Unscoped", rebuild the rest, keep the choice.
  const current = els.fScope.value;
  while (els.fScope.options.length > 2) els.fScope.remove(2);
  els.fScope.append(...scopeOptgroups());
  if ([...els.fScope.options].some((o) => o.value === current)) els.fScope.value = current;

  els.retagScope.textContent = "";
  els.retagScope.append(...scopeOptgroups());
}

function scopeOptgroups() {
  const group = (label, items) => {
    if (!items.length) return null;
    const g = el("optgroup", { label });
    for (const it of items) g.append(el("option", { value: it.code, text: it.label }));
    return g;
  };
  return [group("Groups", state.scopes.groups), group("Countries", state.scopes.regions)].filter(
    Boolean,
  );
}

// ---------------------------------------------------------------- documents

function filterParams() {
  const p = new URLSearchParams();
  const put = (k, v) => {
    if (v && String(v).trim()) p.set(k, String(v).trim());
  };
  put("q", els.fQ.value);
  put("scope", els.fScope.value);
  put("status", els.fStatus.value);
  put("collection", els.fCollection.value);
  p.set("sort", els.fSort.value || "attention");
  p.set("limit", String(PAGE_SIZE));
  p.set("offset", String(state.offset));
  return p;
}

async function loadDocs() {
  const { ok, status, body } = await api(`/api/documents?${filterParams()}`);
  if (!ok) {
    setStatus(els.docStatus, errorText(body, status), "error");
    return;
  }
  state.docs = Array.isArray(body.documents) ? body.documents : [];
  state.total = Number(body.total) || 0;
  // Deleting the last row of the last page leaves an empty page behind.
  if (!state.docs.length && state.offset > 0 && state.total > 0) {
    state.offset = Math.max(0, state.offset - PAGE_SIZE);
    return loadDocs();
  }
  state.selected.clear();
  renderDocs();
}

function scopeBadges(doc) {
  const wrap = el("span", { class: "scope-badges" });
  const labels = Array.isArray(doc.scope_labels) ? doc.scope_labels : [];
  if (!labels.length) {
    // Not "Everywhere": nobody chose that, and the difference has to show.
    wrap.append(el("span", { class: "scope-badge unscoped", text: "unscoped" }));
    return wrap;
  }
  for (const label of labels) wrap.append(el("span", { class: "scope-badge", text: label }));
  return wrap;
}

function renderDocs() {
  els.docRows.textContent = "";
  els.docEmpty.hidden = state.docs.length > 0;

  for (const doc of state.docs) {
    const checkbox = el("input", {
      type: "checkbox",
      "aria-label": `Select ${doc.original_filename}`,
      onchange: (e) => {
        if (e.target.checked) state.selected.add(doc.id);
        else state.selected.delete(doc.id);
        updateSelection();
      },
    });

    const name = el("div", { class: "admin-doc-name" });
    name.append(el("span", { text: doc.original_filename || doc.id }));
    if (Number(doc.duplicate_count) > 0) {
      const n = Number(doc.duplicate_count);
      name.append(
        el("span", {
          class: "badge duplicate",
          text: "duplicate",
          title: `The same file is in the corpus ${n} more time${n === 1 ? "" : "s"}`,
        }),
      );
    }
    const nameCell = el("td", {}, name);
    if (doc.status === "failed" && doc.error_message) {
      nameCell.append(el("div", { class: "muted admin-doc-error", text: doc.error_message }));
    }

    els.docRows.append(
      el(
        "tr",
        {},
        el("td", { class: "col-check" }, checkbox),
        nameCell,
        el("td", {}, el("span", { class: `badge ${doc.status || "uploaded"}`, text: doc.status || "uploaded" })),
        el("td", {}, scopeBadges(doc)),
        el("td", { class: "num", text: String(doc.chunk_count ?? "") }),
        el("td", { text: doc.collection || "" }),
        el("td", { class: "col-date", text: formatDate(doc.created_at) }),
        el(
          "td",
          { class: "col-actions" },
          el("button", {
            type: "button",
            class: "btn-secondary",
            text: "Retag",
            onclick: () => openRetag([doc.id], doc.original_filename, doc),
          }),
        ),
      ),
    );
  }

  const first = state.total ? state.offset + 1 : 0;
  const last = state.offset + state.docs.length;
  els.pageInfo.textContent = state.total
    ? `${first}-${last} of ${state.total}`
    : "";
  els.pagePrev.disabled = state.offset === 0;
  els.pageNext.disabled = last >= state.total;
  updateSelection();
}

function updateSelection() {
  const n = state.selected.size;
  els.selectionCount.textContent = n ? `${n} selected` : "Nothing selected";
  els.bulkRetag.disabled = n === 0;
  els.bulkDelete.disabled = n === 0;
  const onPage = state.docs.length;
  els.selectAll.checked = onPage > 0 && n === onPage;
  els.selectAll.indeterminate = n > 0 && n < onPage;
}

els.selectAll.addEventListener("change", () => {
  const on = els.selectAll.checked;
  state.selected = new Set(on ? state.docs.map((d) => d.id) : []);
  for (const box of els.docRows.querySelectorAll('input[type="checkbox"]')) box.checked = on;
  updateSelection();
});

function refilter() {
  state.offset = 0;
  loadDocs();
}

els.fQ.addEventListener("input", debounce(refilter, SEARCH_DEBOUNCE_MS));
els.fCollection.addEventListener("input", debounce(refilter, SEARCH_DEBOUNCE_MS));
for (const s of [els.fScope, els.fStatus, els.fSort]) s.addEventListener("change", refilter);
$("doc-filters").addEventListener("submit", (e) => e.preventDefault());

els.pagePrev.addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - PAGE_SIZE);
  loadDocs();
});
els.pageNext.addEventListener("click", () => {
  state.offset += PAGE_SIZE;
  loadDocs();
});

// -------------------------------------------------------------------- retag

function openRetag(ids, label, doc = null) {
  state.retag = { ids, label };
  els.retagTarget.textContent = label;
  setStatus(els.retagError, "", "error");
  // A single document opens on what it is tagged with now, so the admin edits
  // rather than starts over. A selection starts empty: there is no one answer.
  const current = new Set(
    doc && Array.isArray(doc.scope_entries) ? doc.scope_entries.map((e) => e.code) : [],
  );
  for (const opt of els.retagScope.options) opt.selected = current.has(opt.value);
  els.dialog.showModal();
  els.retagScope.focus();
}

els.bulkRetag.addEventListener("click", () => {
  const n = state.selected.size;
  openRetag([...state.selected], `${n} document${n === 1 ? "" : "s"}`);
});

els.retagCancel.addEventListener("click", () => els.dialog.close());

els.retagForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const codes = [...els.retagScope.selectedOptions].map((o) => o.value);
  if (!codes.length) {
    setStatus(els.retagError, "Pick at least one - Everywhere for a global policy.", "error");
    return;
  }
  const { ids } = state.retag;
  const scope = codes.join(",");
  els.retagSave.disabled = true;
  try {
    const res =
      ids.length === 1
        ? await api(`/api/documents/${encodeURIComponent(ids[0])}/scope`, {
            method: "PATCH",
            body: { scope },
          })
        : await api("/api/documents/scope", {
            method: "PATCH",
            body: { document_ids: ids, scope },
          });
    if (!res.ok) {
      setStatus(els.retagError, errorText(res.body, res.status), "error");
      return;
    }
    els.dialog.close();
    const labels = (res.body.scope_labels || []).join(", ");
    setStatus(
      els.docStatus,
      `${state.retag.label} now applies to ${labels}. Nothing was re-parsed.`,
      "ok",
    );
    await loadDocs();
  } finally {
    els.retagSave.disabled = false;
  }
});

// ------------------------------------------------------------------- delete

els.bulkDelete.addEventListener("click", async () => {
  const ids = [...state.selected];
  const n = ids.length;
  if (
    !n ||
    !confirm(
      `Delete ${n} document${n === 1 ? "" : "s"}? This removes the files and their index entries and cannot be undone.`,
    )
  ) {
    return;
  }
  els.bulkDelete.disabled = true;
  let failed = 0;
  // One at a time: there is no bulk delete upstream, and parallel deletes
  // would only race each other for the same database.
  for (const id of ids) {
    const res = await api(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) failed += 1;
  }
  setStatus(
    els.docStatus,
    failed
      ? `Deleted ${n - failed} of ${n}. ${failed} could not be deleted - see the list.`
      : `Deleted ${n} document${n === 1 ? "" : "s"}.`,
    failed ? "error" : "ok",
  );
  await loadDocs();
});

// ----------------------------------------------------------------- backfill

els.backfill.addEventListener("click", async () => {
  els.backfill.disabled = true;
  setStatus(els.docStatus, "Checking older files...", "");
  try {
    const { ok, status, body } = await api("/api/documents/backfill-fingerprints", {
      method: "POST",
    });
    if (!ok) {
      setStatus(els.docStatus, errorText(body, status), "error");
      return;
    }
    const missing = Array.isArray(body.missing_original) ? body.missing_original.length : 0;
    setStatus(
      els.docStatus,
      `${body.hashed} older file${body.hashed === 1 ? "" : "s"} checked` +
        (missing ? `; ${missing} could not be read because the original is missing.` : "."),
      missing ? "error" : "ok",
    );
    await loadDocs();
  } finally {
    els.backfill.disabled = false;
  }
});

// ---------------------------------------------------------------- countries

async function loadRegions() {
  const { ok, status, body } = await api("/api/regions");
  if (!ok) {
    setStatus(els.countryStatus, errorText(body, status), "error");
    return;
  }
  state.regions = Array.isArray(body.regions) ? body.regions : [];
  renderRegions();
}

function usageText(r) {
  const parts = [];
  const n = Number(r.documents_using) || 0;
  if (n) parts.push(`${n} document${n === 1 ? "" : "s"}`);
  if (Array.isArray(r.groups) && r.groups.length) parts.push(r.groups.join(", "));
  return parts.join(" · ");
}

function renderRegions() {
  const term = fold(els.cQ.value.trim());
  const show = els.cShow.value;
  const rows = state.regions.filter((r) => {
    if (show === "active" && !r.active) return false;
    if (show === "inactive" && r.active) return false;
    if (!term) return true;
    return fold(r.label).includes(term) || fold(r.code) === term;
  });

  const enabled = state.regions.filter((r) => r.active).length;
  els.countrySummary.textContent = `${enabled} of ${state.regions.length} countries enabled`;
  els.countryRows.textContent = "";
  els.countryEmpty.hidden = rows.length > 0;

  for (const r of rows) {
    const usage = usageText(r);
    const inUse = Boolean(usage);
    let action;
    if (r.active) {
      action = el("button", {
        type: "button",
        class: "btn-secondary",
        text: "Disable",
        // Explained before the click rather than refused after it.
        disabled: inUse,
        title: inUse ? `Still used by ${usage}` : undefined,
        onclick: () => toggleRegion(r, false),
      });
    } else {
      action = el("button", {
        type: "button",
        text: "Enable",
        onclick: () => toggleRegion(r, true),
      });
    }
    els.countryRows.append(
      el(
        "tr",
        { class: r.active ? "" : "inactive" },
        el("td", { text: r.label }),
        el("td", {}, el("code", { text: r.code })),
        el("td", { class: "muted", text: usage || "—" }),
        el("td", { class: "col-actions" }, action),
      ),
    );
  }
}

async function toggleRegion(r, active) {
  const { ok, status, body } = await api(`/api/regions/${encodeURIComponent(r.code)}`, {
    method: "PATCH",
    body: { active },
  });
  if (!ok) {
    setStatus(els.countryStatus, errorText(body, status), "error");
    await loadRegions(); // usage may have changed since the list was drawn
    return;
  }
  setStatus(
    els.countryStatus,
    active
      ? `${r.label} is enabled and can now be picked when tagging documents.`
      : `${r.label} is disabled and no longer offered in the pickers.`,
    "ok",
  );
  r.active = active;
  renderRegions();
  // The pickers on the Documents tab come from /api/scopes.
  await loadScopes();
}

els.cQ.addEventListener("input", debounce(renderRegions, 100));
els.cShow.addEventListener("change", renderRegions);

// --------------------------------------------------------------------- boot

(async function boot() {
  showTab(location.hash === "#countries" ? "countries" : "documents");
  await loadScopes();
  await loadDocs();
})();
