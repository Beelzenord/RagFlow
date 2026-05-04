// RAG Console - vanilla JS frontend.
// Persists uploaded document IDs in localStorage, polls /api/documents/{id}
// until status is terminal, and chats against /api/query.

const STORAGE_KEY = "rag.docs.v1";
const POLL_MS = 2000;

const state = {
  docs: loadDocs(),
  pollers: new Map(),
};

const els = {
  uploadForm: document.getElementById("upload-form"),
  fileInput: document.getElementById("file-input"),
  collectionInput: document.getElementById("collection-input"),
  uploadBtn: document.getElementById("upload-btn"),
  uploadStatus: document.getElementById("upload-status"),
  docsList: document.getElementById("docs-list"),
  docsEmpty: document.getElementById("docs-empty"),
  scopeSelect: document.getElementById("scope-select"),
  chatLog: document.getElementById("chat-log"),
  chatForm: document.getElementById("chat-form"),
  questionInput: document.getElementById("question-input"),
  askBtn: document.getElementById("ask-btn"),
};

function loadDocs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function saveDocs() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.docs));
}

function upsertDoc(doc) {
  const idx = state.docs.findIndex((d) => d.id === doc.id);
  if (idx === -1) {
    state.docs.unshift(doc);
  } else {
    state.docs[idx] = { ...state.docs[idx], ...doc };
  }
  saveDocs();
  renderDocs();
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

    const row = document.createElement("div");
    row.className = "doc-row";

    const name = document.createElement("div");
    name.className = "doc-name";
    name.textContent = d.filename || d.id;

    const badge = document.createElement("span");
    badge.className = `badge ${d.status || "uploaded"}`;
    badge.textContent = d.status || "uploaded";

    row.append(name, badge);

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

function startPolling(docId) {
  if (state.pollers.has(docId)) return;
  const tick = async () => {
    try {
      const resp = await fetch(`/api/documents/${docId}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      upsertDoc({
        id: docId,
        status: data.status,
        chunk_count: data.chunk_count,
        error_message: data.error_message,
        filename: data.original_filename,
        collection: data.collection,
      });
      if (isTerminal(data.status)) {
        clearInterval(state.pollers.get(docId));
        state.pollers.delete(docId);
      }
    } catch (err) {
      console.warn("poll failed for", docId, err);
    }
  };
  const handle = setInterval(tick, POLL_MS);
  state.pollers.set(docId, handle);
  tick();
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
  setStatus(els.uploadStatus, "Uploading...", "");
  try {
    const resp = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const msg = data?.detail || data?.error || `HTTP ${resp.status}`;
      throw new Error(msg);
    }
    setStatus(els.uploadStatus, `Accepted. document_id=${data.document_id}`, "ok");
    upsertDoc({
      id: data.document_id,
      status: data.status || "processing",
      filename: file.name,
      collection: collection || null,
    });
    startPolling(data.document_id);
    els.uploadForm.reset();
  } catch (err) {
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

  const pending = appendMessage("bot", "Thinking...");

  try {
    const body = { question };
    if (documentId) body.document_id = documentId;
    const resp = await fetch("/api/query", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const msg = data?.detail || data?.error || `HTTP ${resp.status}`;
      throw new Error(msg);
    }
    renderAnswer(pending, data);
  } catch (err) {
    pending.classList.add("error");
    pending.textContent = `Query failed: ${err.message}`;
  } finally {
    els.askBtn.disabled = false;
  }
});

function appendMessage(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  els.chatLog.appendChild(div);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
  return div;
}

function renderAnswer(node, data) {
  node.textContent = "";
  const answer = document.createElement("div");
  answer.textContent = data.answer || "(empty answer)";
  node.appendChild(answer);

  if (Array.isArray(data.citations) && data.citations.length) {
    const cites = document.createElement("div");
    cites.className = "citations";
    for (const c of data.citations) {
      const line = document.createElement("div");
      const loc = [c.filename, c.page_number ? `p.${c.page_number}` : null, c.heading]
        .filter(Boolean)
        .join(" - ");
      const score = typeof c.score === "number" ? ` (score ${c.score.toFixed(2)})` : "";
      line.textContent = `[${c.n}] ${loc}${score}`;
      cites.appendChild(line);
    }
    node.appendChild(cites);
  }
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
}

function setStatus(node, text, kind) {
  node.textContent = text;
  node.className = `status ${kind || ""}`.trim();
}

renderDocs();
for (const d of state.docs) {
  if (!isTerminal(d.status)) startPolling(d.id);
}
