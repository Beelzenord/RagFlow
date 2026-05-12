// RAG Console - vanilla JS frontend.
// The server (ingestion service) is the source of truth for the documents
// list. localStorage is kept only as a paint cache so reloads don't flash an
// empty list before the first /api/documents response lands.

const STORAGE_KEY = "rag.docs.v1";
const SHOW_SOURCES_KEY = "rag.showSources.v1";
const REFRESH_MS = 5000;
const POLL_MS = 2000;

const state = {
  docs: loadDocsCache(),
  pollers: new Map(),
  refreshHandle: null,
  pendingDeletes: new Set(),
  lastUploadId: null,
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
};

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

function isTerminal(status) {
  return status === "completed" || status === "failed";
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
    if (d.status !== "completed") continue;
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
          if (data.status === "completed") {
            const n = typeof data.chunk_count === "number" ? data.chunk_count : 0;
            setStatus(els.uploadStatus, `Done - ${n} chunks indexed`, "ok");
          } else {
            const reason = data.error_message || "see document row";
            setStatus(els.uploadStatus, `Failed: ${reason}`, "error");
          }
          state.lastUploadId = null;
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
      let body = {};
      try { body = JSON.parse(xhr.responseText || "{}"); } catch { /* ignore */ }
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => reject(new Error("network error"));
    xhr.onabort = () => reject(new Error("aborted"));
    xhr.send(formData);
  });
}

els.uploadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = els.fileInput.files[0];
  if (!file) return;
  const collection = els.collectionInput.value.trim();

  const fd = new FormData();
  fd.append("file", file);
  if (collection) fd.append("collection", collection);

  els.uploadBtn.disabled = true;
  setProgress(0);
  showProgress(true);
  setStatus(els.uploadStatus, "Uploading 0%...", "");

  try {
    const { status, body } = await uploadWithProgress(fd, (loaded, total) => {
      const frac = total ? loaded / total : 0;
      setProgress(frac);
      const pct = Math.round(frac * 100);
      if (pct < 100) {
        setStatus(els.uploadStatus, `Uploading ${pct}%...`, "");
      } else {
        setStatus(els.uploadStatus, "Uploaded, server processing...", "");
      }
    });

    if (status < 200 || status >= 300) {
      const msg = body?.detail || body?.error || `HTTP ${status}`;
      throw new Error(msg);
    }

    setProgress(1);
    showProgress(false);
    setStatus(els.uploadStatus, "Uploaded, server processing...", "");
    state.lastUploadId = body.document_id;
    state.docs.unshift({
      id: body.document_id,
      status: body.status || "processing",
      filename: file.name,
      collection: collection || null,
    });
    saveDocsCache();
    renderDocs();
    startPolling(body.document_id);
    els.uploadForm.reset();
  } catch (err) {
    showProgress(false);
    setStatus(els.uploadStatus, `Upload failed: ${err.message}`, "error");
  } finally {
    els.uploadBtn.disabled = false;
  }
});

els.chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = els.questionInput.value.trim();
  if (!question) return;
  const documentId = els.scopeSelect.value || null;

  appendMessage("user", question);
  els.questionInput.value = "";
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
  let citationsNode = null;

  const handleEvent = (evt) => {
    if (!evt || typeof evt !== "object") return;
    if (evt.type === "citations") {
      if (Array.isArray(evt.data) && evt.data.length) {
        citationsNode = renderCitations(evt.data);
        bubble.appendChild(citationsNode);
      }
    } else if (evt.type === "delta") {
      if (!firstDeltaSeen) {
        answerNode.textContent = "";
        firstDeltaSeen = true;
      }
      answerNode.appendChild(document.createTextNode(evt.text || ""));
      els.chatLog.scrollTop = els.chatLog.scrollHeight;
    } else if (evt.type === "error") {
      bubble.classList.add("error");
      answerNode.textContent = `Query failed: ${evt.message || "unknown error"}`;
    } else if (evt.type === "done") {
      if (!firstDeltaSeen) answerNode.textContent = "(empty answer)";
    }
  };

  try {
    const body = { question };
    if (documentId) body.document_id = documentId;
    const resp = await fetch("/api/query/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
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
  }
});

function appendMessage(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (text) div.textContent = text;
  els.chatLog.appendChild(div);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
  return div;
}

function renderCitations(list) {
  const cites = document.createElement("div");
  cites.className = "citations";
  for (const c of list) {
    const line = document.createElement("div");
    const loc = [c.filename, c.page_number ? `p.${c.page_number}` : null, c.heading]
      .filter(Boolean)
      .join(" - ");
    const score = typeof c.score === "number" ? ` (score ${c.score.toFixed(2)})` : "";
    line.textContent = `[${c.n}] ${loc}${score}`;
    cites.appendChild(line);
  }
  return cites;
}

function setStatus(node, text, kind) {
  node.textContent = text;
  node.className = `status ${kind || ""}`.trim();
}

renderDocs();
refreshDocs();
startBackgroundRefresh();
for (const d of state.docs) {
  if (!isTerminal(d.status)) startPolling(d.id);
}
