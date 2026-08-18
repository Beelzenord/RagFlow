// RAG Console - vanilla JS frontend.
// The server (ingestion service) is the source of truth for the documents
// list. localStorage is kept only as a paint cache so reloads don't flash an
// empty list before the first /api/documents response lands.

const STORAGE_KEY = "rag.docs.v1";
const SHOW_SOURCES_KEY = "rag.showSources.v1";
const THEME_KEY = "rag.theme.v1";
const REFRESH_MS = 5000;
// Follow-ups are resolved server-side against these turns, so the window is a
// tradeoff between context and prompt cost. 6 turns ~ 3 exchanges.
const MAX_HISTORY_TURNS = 6;
const MAX_TURN_CHARS = 1500;
const POLL_MS = 2000;

const state = {
  docs: loadDocsCache(),
  pollers: new Map(),
  refreshHandle: null,
  pendingDeletes: new Set(),
  lastUploadId: null,
  queue: [],
  queueBusy: false,
  nextQid: 1,
  turns: [],
};

const els = {
  uploadForm: document.getElementById("upload-form"),
  fileInput: document.getElementById("file-input"),
  collectionInput: document.getElementById("collection-input"),
  uploadBtn: document.getElementById("upload-btn"),
  uploadStatus: document.getElementById("upload-status"),
  uploadProgress: document.getElementById("upload-progress"),
  uploadBar: document.getElementById("upload-bar"),
  docsList: document.getElementById("docs-list"),
  docsEmpty: document.getElementById("docs-empty"),
  scopeSelect: document.getElementById("scope-select"),
  showSources: document.getElementById("show-sources"),
  chatLog: document.getElementById("chat-log"),
  chatForm: document.getElementById("chat-form"),
  questionInput: document.getElementById("question-input"),
  askBtn: document.getElementById("ask-btn"),
  queuePanel: document.getElementById("queue-panel"),
  queueList: document.getElementById("queue-list"),
  queueSummary: document.getElementById("queue-summary"),
  queueClearBtn: document.getElementById("queue-clear-btn"),
  themeToggle: document.getElementById("theme-toggle"),
  newChatBtn: document.getElementById("new-chat-btn"),
  logoutBtn: document.getElementById("logout-btn"),
  userBadge: document.getElementById("user-badge"),
};

// Where to send the browser when the server stops recognising it. Overwritten
// by /api/me, because with Microsoft sign-in the destination belongs to the
// platform ("/.auth/login/aad") rather than to this app.
let loginUrl = "/login";

// The session can end mid-visit, after which every /api call answers 401. Send
// the tab to the sign-in page instead of painting "HTTP 401" into the docs
// list, the upload queue and the chat log.
function bounceIfLoggedOut(status) {
  if (status !== 401) return false;
  location.href = loginUrl;
  return true;
}

function loadDocsCache() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function saveDocsCache() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state.docs));
  } catch {
    /* storage full / disabled - ignore */
  }
}

function normalizeDoc(d) {
  return {
    id: d.id,
    status: d.status,
    chunk_count: d.chunk_count,
    error_message: d.error_message,
    filename: d.original_filename || d.filename || d.id,
    collection: d.collection,
  };
}

async function refreshDocs() {
  try {
    const resp = await fetch("/api/documents");
    if (bounceIfLoggedOut(resp.status)) return;
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const list = Array.isArray(data?.documents) ? data.documents : [];
    state.docs = list.map(normalizeDoc);
    saveDocsCache();
    renderDocs();
  } catch (err) {
    console.warn("refreshDocs failed", err);
  }
}

// "degraded" means indexed, but at least one page was recovered from the raw
// PDF text layer - searchable like any other finished document.
function isIndexed(status) {
  return status === "completed" || status === "degraded";
}

function isTerminal(status) {
  return isIndexed(status) || status === "failed";
}

function renderDocs() {
  els.docsEmpty.style.display = state.docs.length ? "none" : "block";
  els.docsList.innerHTML = "";
  for (const d of state.docs) {
    const li = document.createElement("li");
    li.className = "doc";
    li.dataset.id = d.id;

    const row = document.createElement("div");
    row.className = "doc-row";

    const name = document.createElement("div");
    name.className = "doc-name";
    name.textContent = d.filename || d.id;

    const actions = document.createElement("div");
    actions.className = "doc-actions";

    const badge = document.createElement("span");
    badge.className = `badge ${d.status || "uploaded"}`;
    badge.textContent = d.status || "uploaded";

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn-danger";
    delBtn.textContent = "Delete";
    delBtn.disabled = state.pendingDeletes.has(d.id);
    delBtn.addEventListener("click", () => deleteDoc(d, li));

    actions.append(badge, delBtn);
    row.append(name, actions);

    const meta = document.createElement("div");
    meta.className = "doc-meta";
    const idSpan = document.createElement("span");
    idSpan.textContent = `id: ${d.id.slice(0, 8)}...`;
    meta.appendChild(idSpan);
    if (d.collection) {
      const c = document.createElement("span");
      c.textContent = `collection: ${d.collection}`;
      meta.appendChild(c);
    }
    if (typeof d.chunk_count === "number" && d.chunk_count > 0) {
      const cc = document.createElement("span");
      cc.textContent = `${d.chunk_count} chunks`;
      meta.appendChild(cc);
    }

    li.append(row, meta);

    if (d.status === "failed" && d.error_message) {
      const err = document.createElement("div");
      err.className = "doc-error";
      err.textContent = d.error_message;
      li.appendChild(err);
    }

    if (d.status === "degraded") {
      const note = document.createElement("div");
      note.className = "doc-note";
      note.textContent =
        "Some pages were recovered from the raw PDF text - content is complete, layout is not.";
      li.appendChild(note);
    }

    els.docsList.appendChild(li);
  }
  renderScopeOptions();
}

function renderScopeOptions() {
  const current = els.scopeSelect.value;
  els.scopeSelect.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All completed documents";
  els.scopeSelect.appendChild(all);
  for (const d of state.docs) {
    if (!isIndexed(d.status)) continue;
    const opt = document.createElement("option");
    opt.value = d.id;
    opt.textContent = d.filename || d.id;
    els.scopeSelect.appendChild(opt);
  }
  if ([...els.scopeSelect.options].some((o) => o.value === current)) {
    els.scopeSelect.value = current;
  }
}

async function deleteDoc(doc, liNode) {
  const label = doc.filename || doc.id;
  if (!confirm(`Delete "${label}"?\nThis removes the embeddings and the stored file.`)) {
    return;
  }
  state.pendingDeletes.add(doc.id);
  const btn = liNode?.querySelector(".btn-danger");
  if (btn) btn.disabled = true;

  let inlineErr = liNode?.querySelector(".doc-error.delete-error");
  if (inlineErr) inlineErr.remove();

  try {
    const resp = await fetch(`/api/documents/${doc.id}`, { method: "DELETE" });
    if (bounceIfLoggedOut(resp.status)) return;
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      const msg = data?.detail || data?.error || `HTTP ${resp.status}`;
      throw new Error(msg);
    }
    const poller = state.pollers.get(doc.id);
    if (poller) {
      clearInterval(poller);
      state.pollers.delete(doc.id);
    }
    await refreshDocs();
  } catch (err) {
    if (liNode) {
      const e = document.createElement("div");
      e.className = "doc-error delete-error";
      e.textContent = `Delete failed: ${err.message}`;
      liNode.appendChild(e);
    }
    if (btn) btn.disabled = false;
  } finally {
    state.pendingDeletes.delete(doc.id);
  }
}

function startPolling(docId) {
  if (state.pollers.has(docId)) return;
  const tick = async () => {
    try {
      const resp = await fetch(`/api/documents/${docId}`);
      if (bounceIfLoggedOut(resp.status)) return;
      if (resp.status === 404) {
        clearInterval(state.pollers.get(docId));
        state.pollers.delete(docId);
        await refreshDocs();
        return;
      }
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const idx = state.docs.findIndex((d) => d.id === docId);
      const merged = normalizeDoc(data);
      if (idx === -1) {
        state.docs.unshift(merged);
      } else {
        state.docs[idx] = { ...state.docs[idx], ...merged };
      }
      saveDocsCache();
      renderDocs();
      if (isTerminal(data.status)) {
        clearInterval(state.pollers.get(docId));
        state.pollers.delete(docId);
        if (state.lastUploadId === docId) {
          if (isIndexed(data.status)) {
            const n = typeof data.chunk_count === "number" ? data.chunk_count : 0;
            const suffix =
              data.status === "degraded" ? " (some pages recovered from raw text)" : "";
            setStatus(els.uploadStatus, `Done - ${n} chunks indexed${suffix}`, "ok");
          } else {
            const reason = data.error_message || "see document row";
            setStatus(els.uploadStatus, `Failed: ${reason}`, "error");
          }
          state.lastUploadId = null;
        }
        const qEntry = state.queue.find((q) => q.docId === docId);
        if (qEntry && (qEntry.status === "processing" || qEntry.status === "uploading")) {
          qEntry.status = data.status;
          if (typeof data.chunk_count === "number") qEntry.chunkCount = data.chunk_count;
          if (data.status === "failed") qEntry.error = data.error_message || "processing failed";
          renderQueue();
          if (state.queueBusy) {
            state.queueBusy = false;
            pumpQueue();
          }
        }
      }
    } catch (err) {
      console.warn("poll failed for", docId, err);
    }
  };
  const handle = setInterval(tick, POLL_MS);
  state.pollers.set(docId, handle);
  tick();
}

function startBackgroundRefresh() {
  stopBackgroundRefresh();
  state.refreshHandle = setInterval(refreshDocs, REFRESH_MS);
}

function stopBackgroundRefresh() {
  if (state.refreshHandle) {
    clearInterval(state.refreshHandle);
    state.refreshHandle = null;
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopBackgroundRefresh();
  } else {
    refreshDocs();
    startBackgroundRefresh();
  }
});

function loadShowSources() {
  try {
    return localStorage.getItem(SHOW_SOURCES_KEY) !== "0";
  } catch {
    return true;
  }
}

function saveShowSources(on) {
  try {
    localStorage.setItem(SHOW_SOURCES_KEY, on ? "1" : "0");
  } catch {
    /* ignore */
  }
}

function applyShowSources(on) {
  els.chatLog.classList.toggle("hide-citations", !on);
}

(function initShowSources() {
  const on = loadShowSources();
  els.showSources.checked = on;
  applyShowSources(on);
  els.showSources.addEventListener("change", () => {
    const isOn = els.showSources.checked;
    applyShowSources(isOn);
    saveShowSources(isOn);
  });
})();

function getTheme() {
  try {
    return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

function applyTheme(theme) {
  const t = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", t);
  if (els.themeToggle) {
    const light = t === "light";
    els.themeToggle.setAttribute("aria-checked", light ? "true" : "false");
    els.themeToggle.setAttribute("aria-label", light ? "Use dark mode" : "Use light mode");
  }
}

function saveTheme(theme) {
  try {
    localStorage.setItem(THEME_KEY, theme === "light" ? "light" : "dark");
  } catch {
    /* ignore */
  }
}

(function initTheme() {
  applyTheme(getTheme());
  if (!els.themeToggle) return;
  els.themeToggle.addEventListener("click", () => {
    const next = getTheme() === "light" ? "dark" : "light";
    saveTheme(next);
    applyTheme(next);
  });
})();

// The topbar cannot work out who is signed in on its own: with Microsoft
// sign-in the identity arrives in a request header the page never sees, and
// signing out has to go through the platform rather than this app.
async function initSession() {
  let info = {};
  try {
    const resp = await fetch("/api/me");
    if (resp.ok) info = await resp.json();
  } catch (err) {
    console.warn("session lookup failed", err);
  }

  if (info.login_url) loginUrl = info.login_url;

  if (info.user && els.userBadge) {
    els.userBadge.textContent = info.user;
    els.userBadge.hidden = false;
  }

  // No logout_url means there is no session to end - an open console with
  // neither a password nor Microsoft sign-in in front of it.
  if (!els.logoutBtn || !info.logout_url) return;
  els.logoutBtn.hidden = false;
  els.logoutBtn.addEventListener("click", async () => {
    els.logoutBtn.disabled = true;
    if (info.auth_mode !== "entra") {
      try {
        await fetch("/api/logout", { method: "POST" });
      } catch (err) {
        console.warn("logout failed", err);
      }
    }
    location.href = info.logout_url;
  });
}

initSession();

function setProgress(fraction) {
  const pct = Math.max(0, Math.min(1, fraction)) * 100;
  els.uploadBar.style.width = `${pct.toFixed(1)}%`;
}

function showProgress(visible) {
  if (visible) {
    els.uploadProgress.hidden = false;
    els.uploadProgress.setAttribute("aria-hidden", "false");
  } else {
    els.uploadProgress.hidden = true;
    els.uploadProgress.setAttribute("aria-hidden", "true");
  }
}

function uploadWithProgress(formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.responseType = "text";
    xhr.upload.onprogress = (evt) => {
      if (evt.lengthComputable) onProgress(evt.loaded, evt.total);
    };
    xhr.onload = () => {
      if (bounceIfLoggedOut(xhr.status)) return;
      let body = {};
      try { body = JSON.parse(xhr.responseText || "{}"); } catch { /* ignore */ }
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => reject(new Error("network error"));
    xhr.onabort = () => reject(new Error("aborted"));
    xhr.send(formData);
  });
}

els.uploadForm.addEventListener("submit", (e) => {
  e.preventDefault();
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

  showProgress(false);
  setStatus(
    els.uploadStatus,
    `${files.length} file${files.length === 1 ? "" : "s"} queued`,
    "",
  );
  els.uploadForm.reset();
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
  next.uploadFrac = 0;
  renderQueue();

  const fd = new FormData();
  fd.append("file", next.file);
  if (next.collection) fd.append("collection", next.collection);

  try {
    const { status, body } = await uploadWithProgress(fd, (loaded, total) => {
      next.uploadFrac = total ? loaded / total : 0;
      updateQueueRowProgress(next.qid, next.uploadFrac);
    });
    if (status < 200 || status >= 300) {
      const msg = body?.detail || body?.error || `HTTP ${status}`;
      throw new Error(msg);
    }
    next.uploadFrac = 1;
    next.docId = body.document_id;
    next.status = "processing";
    state.lastUploadId = body.document_id;
    state.docs.unshift({
      id: body.document_id,
      status: body.status || "processing",
      filename: next.filename,
      collection: next.collection,
    });
    saveDocsCache();
    renderDocs();
    renderQueue();
    // Hand off to per-doc poller; queue advances when poller observes a
    // terminal status (see startPolling's terminal branch).
    startPolling(body.document_id);
  } catch (err) {
    next.status = "failed";
    next.error = err.message || String(err);
    state.queueBusy = false;
    renderQueue();
    pumpQueue();
  }
}

function updateQueueRowProgress(qid, frac) {
  const bar = els.queueList.querySelector(
    `li[data-qid="${qid}"] .progress-bar`,
  );
  if (bar) bar.style.width = `${(Math.max(0, Math.min(1, frac)) * 100).toFixed(1)}%`;
}

function renderQueue() {
  const items = state.queue;
  if (!items.length) {
    els.queuePanel.hidden = true;
    els.queueList.innerHTML = "";
    els.queueSummary.textContent = "";
    return;
  }
  els.queuePanel.hidden = false;

  let queued = 0, uploading = 0, processing = 0, completed = 0, failed = 0;
  for (const it of items) {
    if (it.status === "queued") queued++;
    else if (it.status === "uploading") uploading++;
    else if (it.status === "processing") processing++;
    else if (isIndexed(it.status)) completed++;
    else if (it.status === "failed") failed++;
  }
  const parts = [];
  if (uploading) parts.push(`${uploading} uploading`);
  if (processing) parts.push(`${processing} processing`);
  if (queued) parts.push(`${queued} queued`);
  if (completed) parts.push(`${completed} completed`);
  if (failed) parts.push(`${failed} failed`);
  els.queueSummary.textContent = parts.join(" - ");

  els.queueList.innerHTML = "";
  for (const it of items) {
    const li = document.createElement("li");
    li.className = "queue-item";
    li.dataset.qid = String(it.qid);

    const row = document.createElement("div");
    row.className = "queue-row";

    const name = document.createElement("div");
    name.className = "queue-name";
    name.textContent = it.filename;

    const actions = document.createElement("div");
    actions.className = "queue-actions";

    const badge = document.createElement("span");
    badge.className = `badge ${it.status}`;
    badge.textContent = it.status;
    actions.appendChild(badge);

    if (it.status === "queued") {
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "btn-secondary";
      rm.textContent = "Remove";
      rm.addEventListener("click", () => {
        const idx = state.queue.findIndex((q) => q.qid === it.qid);
        if (idx !== -1 && state.queue[idx].status === "queued") {
          state.queue.splice(idx, 1);
          renderQueue();
        }
      });
      actions.appendChild(rm);
    }

    row.append(name, actions);
    li.appendChild(row);

    const meta = document.createElement("div");
    meta.className = "queue-meta";
    if (typeof it.size === "number") {
      const sz = document.createElement("span");
      sz.textContent = formatBytes(it.size);
      meta.appendChild(sz);
    }
    if (it.collection) {
      const c = document.createElement("span");
      c.textContent = `collection: ${it.collection}`;
      meta.appendChild(c);
    }
    if (isIndexed(it.status) && typeof it.chunkCount === "number") {
      const cc = document.createElement("span");
      cc.textContent = `${it.chunkCount} chunks`;
      meta.appendChild(cc);
    }
    if (meta.childNodes.length) li.appendChild(meta);

    if (it.status === "uploading") {
      const prog = document.createElement("div");
      prog.className = "progress";
      const bar = document.createElement("div");
      bar.className = "progress-bar";
      bar.style.width = `${(it.uploadFrac * 100).toFixed(1)}%`;
      prog.appendChild(bar);
      li.appendChild(prog);
    }

    if (it.status === "failed" && it.error) {
      const err = document.createElement("div");
      err.className = "queue-error";
      err.textContent = it.error;
      li.appendChild(err);
    }

    els.queueList.appendChild(li);
  }

  const anyFinished = items.some((q) => isTerminal(q.status));
  els.queueClearBtn.disabled = !anyFinished;
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

els.queueClearBtn.addEventListener("click", () => {
  state.queue = state.queue.filter((q) => !isTerminal(q.status));
  renderQueue();
});

async function runChat(question) {
  if (!question) return;
  const documentId = els.scopeSelect.value || null;
  const history = state.turns.slice(-MAX_HISTORY_TURNS);

  appendMessage("user", question);
  els.askBtn.disabled = true;

  const bubble = appendMessage("bot", "");
  const answerNode = document.createElement("div");
  answerNode.className = "answer";
  const spinner = document.createElement("span");
  spinner.className = "spinner";
  spinner.setAttribute("aria-label", "thinking");
  answerNode.appendChild(spinner);
  bubble.appendChild(answerNode);
  let firstDeltaSeen = false;
  let answered = false;

  const handleEvent = (evt) => {
    if (!evt || typeof evt !== "object") return;
    if (evt.type === "rewrite") {
      if (evt.text) bubble.appendChild(renderRewrite(evt.text));
    } else if (evt.type === "citations") {
      if (Array.isArray(evt.data) && evt.data.length) {
        bubble.appendChild(renderSourceDocs(evt.data));
        bubble.appendChild(renderCitations(evt.data));
      }
    } else if (evt.type === "delta") {
      if (!firstDeltaSeen) {
        answerNode.textContent = "";
        firstDeltaSeen = true;
      }
      const text = evt.text || "";
      answerNode.appendChild(document.createTextNode(text));
      els.chatLog.scrollTop = els.chatLog.scrollHeight;
    } else if (evt.type === "error") {
      bubble.classList.add("error");
      answerNode.textContent = `Query failed: ${evt.message || "unknown error"}`;
    } else if (evt.type === "done") {
      if (!firstDeltaSeen) {
        answerNode.textContent = "(empty answer)";
      } else {
        // Models sometimes still emit [1]/ strip leftover markers from the final text.
        answerNode.textContent = stripInlineCitations(answerNode.textContent);
        answered = true;
      }
    }
  };

  try {
    const body = { question };
    if (documentId) body.document_id = documentId;
    if (history.length) body.history = history;
    const resp = await fetch("/api/query/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (bounceIfLoggedOut(resp.status)) return;
    if (!resp.ok || !resp.body) {
      const data = await resp.json().catch(() => ({}));
      const msg = data?.detail || data?.error || `HTTP ${resp.status}`;
      throw new Error(msg);
    }

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        try {
          handleEvent(JSON.parse(line));
        } catch (err) {
          console.warn("bad ndjson line", line, err);
        }
      }
    }
    if (buf.trim()) {
      try { handleEvent(JSON.parse(buf)); } catch (_) { /* ignore */ }
    }
  } catch (err) {
    bubble.classList.add("error");
    answerNode.textContent = `Query failed: ${err.message}`;
  } finally {
    els.askBtn.disabled = false;
    // Only successful exchanges become context - errors, empty answers and
    // "nothing found" replies would just be noise in the next rewrite.
    if (answered) {
      recordTurn("user", question);
      recordTurn("assistant", answerNode.textContent);
    }
  }
}

function recordTurn(role, content) {
  const text = String(content || "").trim();
  if (!text) return;
  state.turns.push({ role, content: text.slice(0, MAX_TURN_CHARS) });
  // Keep a little more than we send so trimming never drops a live pair.
  const cap = MAX_HISTORY_TURNS * 2;
  if (state.turns.length > cap) state.turns = state.turns.slice(-cap);
  updateNewChatBtn();
}

function resetChat() {
  state.turns = [];
  els.chatLog.innerHTML = "";
  updateNewChatBtn();
  els.questionInput.focus();
}

function updateNewChatBtn() {
  if (els.newChatBtn) els.newChatBtn.disabled = state.turns.length === 0;
}

if (els.newChatBtn) {
  els.newChatBtn.addEventListener("click", resetChat);
  updateNewChatBtn();
}

els.chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const question = els.questionInput.value.trim();
  if (!question) return;
  els.questionInput.value = "";
  runChat(question);
});

function appendMessage(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (text) div.textContent = text;
  els.chatLog.appendChild(div);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
  return div;
}

function stripInlineCitations(text) {
  return String(text || "")
    .replace(/\s*\[\d+\]/g, "")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trimEnd();
}

function renderRewrite(text) {
  /** Shows how a follow-up was interpreted; hidden with source details. */
  const div = document.createElement("div");
  div.className = "rewrite";
  div.textContent = `Searched for: ${text}`;
  return div;
}

function renderSourceDocs(list) {
  /** Always-visible download links for documents used in the answer. */
  const wrap = document.createElement("div");
  wrap.className = "source-docs";

  const label = document.createElement("div");
  label.className = "source-docs-label";
  label.textContent = "Documents";
  wrap.appendChild(label);

  const links = document.createElement("div");
  links.className = "source-docs-links";

  const seen = new Set();
  for (const c of list) {
    if (!c.document_id || seen.has(c.document_id)) continue;
    seen.add(c.document_id);
    const name = c.filename || "document";
    const a = document.createElement("a");
    a.className = "source-doc-link";
    a.href = `/api/documents/${encodeURIComponent(c.document_id)}/file`;
    a.download = name;
    a.title = `Download ${name}`;
    a.textContent = name;
    links.appendChild(a);
  }

  if (!links.childNodes.length) return document.createDocumentFragment();
  wrap.appendChild(links);
  return wrap;
}

function renderCitations(list) {
  /** Optional detail rows (page, heading, score) gated by "Show source details". */
  const cites = document.createElement("div");
  cites.className = "citations";
  for (const c of list) {
    const line = document.createElement("div");
    const loc = [c.filename, c.page_number ? `p.${c.page_number}` : null, c.heading]
      .filter(Boolean)
      .join(" - ");
    // "vector", "keyword" or "both" - which retriever found this chunk. The
    // score is always vector similarity, so a keyword-only hit can score low
    // and still be the right answer; showing both together explains that.
    const found = { dense: "vector", lexical: "keyword", both: "both" }[c.retrieval];
    const detail = [
      typeof c.score === "number" ? `score ${c.score.toFixed(2)}` : null,
      found ? `found by ${found}` : null,
    ].filter(Boolean);
    line.textContent = `[${c.n}] ${loc}${detail.length ? ` (${detail.join(", ")})` : ""}`;
    cites.appendChild(line);
  }
  return cites;
}

function setStatus(node, text, kind) {
  node.textContent = text;
  node.className = `status ${kind || ""}`.trim();
}

(function initCollapsiblePanels() {
  const panels = [
    { el: document.getElementById("upload-details"), key: "rag.collapse.upload.v1" },
    { el: document.getElementById("docs-details"), key: "rag.collapse.docs.v1" },
  ];
  for (const { el, key } of panels) {
    if (!el) continue;
    try {
      const saved = localStorage.getItem(key);
      if (saved === "0") el.open = false;
      else if (saved === "1") el.open = true;
    } catch {
      /* ignore */
    }
    el.addEventListener("toggle", () => {
      try {
        localStorage.setItem(key, el.open ? "1" : "0");
      } catch {
        /* ignore */
      }
    });
  }
})();

renderDocs();
refreshDocs();
startBackgroundRefresh();
for (const d of state.docs) {
  if (!isTerminal(d.status)) startPolling(d.id);
}
