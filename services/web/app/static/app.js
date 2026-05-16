// RAG Console - vanilla JS frontend.
// The server (ingestion service) is the source of truth for the documents
// list. localStorage is kept only as a paint cache so reloads don't flash an
// empty list before the first /api/documents response lands.

const STORAGE_KEY = "rag.docs.v1";
const SHOW_SOURCES_KEY = "rag.showSources.v1";
const MODE_KEY = "rag.mode.v1";
const LANG_KEY = "rag.lang.v1";
const SUPPORTED_LANGS = ["en", "sv"];
const REFRESH_MS = 5000;
const POLL_MS = 2000;
const MIN_SENTENCE_CHARS = 20;
const MAX_TTS_INFLIGHT = 4;

const state = {
  docs: loadDocsCache(),
  pollers: new Map(),
  refreshHandle: null,
  pendingDeletes: new Set(),
  lastUploadId: null,
  voice: createVoiceState(),
  mediaRecorder: null,
  recordChunks: [],
  lang: "en",
  queue: [],
  queueBusy: false,
  nextQid: 1,
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
  modeSelect: document.getElementById("mode-select"),
  langSelect: document.getElementById("lang-select"),
  showSources: document.getElementById("show-sources"),
  chatLog: document.getElementById("chat-log"),
  chatForm: document.getElementById("chat-form"),
  questionInput: document.getElementById("question-input"),
  askBtn: document.getElementById("ask-btn"),
  voiceControls: document.getElementById("voice-controls"),
  micBtn: document.getElementById("mic-btn"),
  voiceStatus: document.getElementById("voice-status"),
  queuePanel: document.getElementById("queue-panel"),
  queueList: document.getElementById("queue-list"),
  queueSummary: document.getElementById("queue-summary"),
  queueClearBtn: document.getElementById("queue-clear-btn"),
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

// ---- Voice mode ---------------------------------------------------------
//
// TTS sentences are fetched in parallel (up to MAX_TTS_INFLIGHT) but played
// strictly in dispatch order. Each sentence gets a sequence number; completed
// blobs land in `pending` keyed by that number, and `flushReady` only moves
// the run of consecutive ready entries into `audioQueue`. Per-request failures
// store a "skip" sentinel so they don't stall the playback chain.
//
// All voice state is namespaced under `state.voice` and reset wholesale by
// `stopVoice()`, which swaps in a fresh voice state object so any in-flight
// fetches whose handlers fire after abort touch a now-unreferenced object.

function createVoiceState() {
  return {
    abort: new AbortController(),
    audioQueue: [],
    isSpeaking: false,
    currentAudio: null,
    seq: 0,
    nextSeq: 1,
    pending: new Map(),
    inflight: 0,
    waitQueue: [],
  };
}

function speakSentence(text) {
  const voice = state.voice;
  voice.seq += 1;
  const mySeq = voice.seq;
  const task = () => fetchAndStore(voice, mySeq, text);
  if (voice.inflight >= MAX_TTS_INFLIGHT) {
    voice.waitQueue.push(task);
  } else {
    voice.inflight += 1;
    task();
  }
}

async function fetchAndStore(voice, mySeq, text) {
  const signal = voice.abort.signal;
  let result = "skip";
  try {
    const resp = await fetch("/api/voice/tts", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, lang: state.lang }),
      signal,
    });
    if (signal.aborted) return;
    if (!resp.ok) {
      console.warn("tts failed", resp.status);
    } else {
      const blob = await resp.blob();
      if (signal.aborted) return;
      result = blob;
    }
  } catch (err) {
    if (err.name !== "AbortError") console.warn("tts error", err);
  } finally {
    if (!signal.aborted) {
      voice.pending.set(mySeq, result);
      flushReady(voice);
      voice.inflight -= 1;
      const next = voice.waitQueue.shift();
      if (next) {
        voice.inflight += 1;
        next();
      }
    }
  }
}

function flushReady(voice) {
  while (voice.pending.has(voice.nextSeq)) {
    const entry = voice.pending.get(voice.nextSeq);
    voice.pending.delete(voice.nextSeq);
    voice.nextSeq += 1;
    if (entry !== "skip") {
      voice.audioQueue.push(entry);
    }
  }
  playNext();
}

async function playNext() {
  const voice = state.voice;
  if (voice.isSpeaking) return;
  const blob = voice.audioQueue.shift();
  if (!blob) return;
  voice.isSpeaking = true;
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  voice.currentAudio = audio;
  const signal = voice.abort.signal;
  try {
    await audio.play();
    await new Promise((resolve) => {
      const done = () => resolve();
      audio.addEventListener("ended", done, { once: true });
      audio.addEventListener("error", done, { once: true });
      if (signal.aborted) resolve();
      else signal.addEventListener("abort", done, { once: true });
    });
  } catch (err) {
    if (err.name !== "AbortError") console.warn("audio play failed", err);
  } finally {
    URL.revokeObjectURL(url);
    if (state.voice === voice) {
      voice.currentAudio = null;
      voice.isSpeaking = false;
      if (voice.audioQueue.length) playNext();
    }
  }
}

function stopVoice() {
  const old = state.voice;
  old.abort.abort();
  if (old.currentAudio) {
    try { old.currentAudio.pause(); } catch (_) { /* ignore */ }
  }
  old.audioQueue = [];
  old.pending.clear();
  old.waitQueue = [];
  state.voice = createVoiceState();
}

function setMicState(label, cls) {
  els.micBtn.textContent = label;
  els.micBtn.className = cls || "";
  els.micBtn.disabled = cls === "transcribing";
}

function setVoiceStatus(text, kind) {
  setStatus(els.voiceStatus, text, kind);
}

function pickRecorderMime() {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"];
  if (typeof MediaRecorder === "undefined") return "";
  for (const m of candidates) {
    if (MediaRecorder.isTypeSupported(m)) return m;
  }
  return "";
}

async function startRecording() {
  stopVoice();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    setVoiceStatus(`Microphone unavailable: ${err.message}`, "error");
    return;
  }
  const mimeType = pickRecorderMime();
  const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
  state.recordChunks = [];
  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size) state.recordChunks.push(e.data);
  };
  recorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    const type = recorder.mimeType || "audio/webm";
    const blob = new Blob(state.recordChunks, { type });
    state.recordChunks = [];
    if (!blob.size) {
      setMicState("Start recording", "");
      setVoiceStatus("No audio captured.", "error");
      return;
    }
    await transcribeAndAsk(blob);
  };
  state.mediaRecorder = recorder;
  recorder.start();
  setMicState("Stop", "recording");
  setVoiceStatus("Listening... click Stop when you're done.", "");
}

function stopRecording() {
  const r = state.mediaRecorder;
  if (!r) return;
  state.mediaRecorder = null;
  if (r.state !== "inactive") r.stop();
}

async function transcribeAndAsk(blob) {
  setMicState("Transcribing...", "transcribing");
  setVoiceStatus("Transcribing...", "");
  const ext = (blob.type.split("/")[1] || "webm").split(";")[0];
  const fd = new FormData();
  fd.append("file", blob, `recording.${ext}`);
  if (state.lang) fd.append("lang", state.lang);
  let text = "";
  try {
    const resp = await fetch("/api/voice/transcribe", { method: "POST", body: fd });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      const msg = data?.detail || data?.error || `HTTP ${resp.status}`;
      throw new Error(msg);
    }
    const data = await resp.json();
    text = (data?.text || "").trim();
  } catch (err) {
    setMicState("Start recording", "");
    setVoiceStatus(`Transcription failed: ${err.message}`, "error");
    return;
  }
  if (!text) {
    setMicState("Start recording", "");
    setVoiceStatus("Couldn't transcribe that. Try again.", "error");
    return;
  }
  setVoiceStatus("", "");
  setMicState("Start recording", "");
  await runChat(text, { voice: true });
}

els.micBtn.addEventListener("click", () => {
  if (state.mediaRecorder && state.mediaRecorder.state === "recording") {
    stopRecording();
  } else {
    startRecording();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && els.modeSelect.value === "voice") {
    stopVoice();
    if (state.mediaRecorder && state.mediaRecorder.state === "recording") {
      stopRecording();
    }
    setMicState("Start recording", "");
    setVoiceStatus("Cancelled.", "");
  }
});

function applyMode(mode) {
  const voice = mode === "voice";
  els.chatForm.hidden = voice;
  els.voiceControls.hidden = !voice;
  if (!voice) {
    stopVoice();
    if (state.mediaRecorder && state.mediaRecorder.state === "recording") {
      stopRecording();
    }
    setMicState("Start recording", "");
    setVoiceStatus("", "");
  }
}

function loadMode() {
  try {
    return localStorage.getItem(MODE_KEY) === "voice" ? "voice" : "text";
  } catch {
    return "text";
  }
}

function saveMode(mode) {
  try { localStorage.setItem(MODE_KEY, mode); } catch { /* ignore */ }
}

(function initMode() {
  const mode = loadMode();
  els.modeSelect.value = mode;
  applyMode(mode);
  els.modeSelect.addEventListener("change", () => {
    const v = els.modeSelect.value;
    saveMode(v);
    applyMode(v);
  });
})();

function loadLang() {
  try {
    const v = localStorage.getItem(LANG_KEY);
    return SUPPORTED_LANGS.includes(v) ? v : "en";
  } catch {
    return "en";
  }
}

function saveLang(lang) {
  try { localStorage.setItem(LANG_KEY, lang); } catch { /* ignore */ }
}

(function initLang() {
  const lang = loadLang();
  state.lang = lang;
  if (els.langSelect) {
    els.langSelect.value = lang;
    els.langSelect.addEventListener("change", () => {
      const v = SUPPORTED_LANGS.includes(els.langSelect.value)
        ? els.langSelect.value
        : "en";
      state.lang = v;
      saveLang(v);
      // Switching language mid-answer would mix voices/accents; flush queued audio.
      stopVoice();
    });
  }
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
    else if (it.status === "completed") completed++;
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
    if (it.status === "completed" && typeof it.chunkCount === "number") {
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

  const anyFinished = items.some(
    (q) => q.status === "completed" || q.status === "failed",
  );
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
  state.queue = state.queue.filter(
    (q) => q.status !== "completed" && q.status !== "failed",
  );
  renderQueue();
});

async function runChat(question, { voice = false } = {}) {
  if (!question) return;
  const documentId = els.scopeSelect.value || null;

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

  // Voice-mode sentence buffer that's drained to TTS as sentences complete.
  let pending = "";

  const handleEvent = (evt) => {
    if (!evt || typeof evt !== "object") return;
    if (evt.type === "citations") {
      if (Array.isArray(evt.data) && evt.data.length) {
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
      if (voice) {
        pending += text;
        pending = drainSentences(pending);
      }
    } else if (evt.type === "error") {
      bubble.classList.add("error");
      answerNode.textContent = `Query failed: ${evt.message || "unknown error"}`;
    } else if (evt.type === "done") {
      if (!firstDeltaSeen) answerNode.textContent = "(empty answer)";
      if (voice && pending.trim()) {
        speakSentence(pending.trim());
        pending = "";
      }
    }
  };

  try {
    const body = { question };
    if (documentId) body.document_id = documentId;
    if (voice) body.voice = true;
    if (state.lang) body.lang = state.lang;
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
}

function drainSentences(buf) {
  // Match a chunk ending in . ! or ? followed by whitespace or end-of-buffer.
  const re = /([^.!?\n]+[.!?])(\s+)/g;
  let lastIndex = 0;
  let match;
  while ((match = re.exec(buf)) !== null) {
    const sentence = match[1].trim();
    if (sentence.length >= MIN_SENTENCE_CHARS) {
      speakSentence(sentence);
      lastIndex = re.lastIndex;
    }
  }
  return lastIndex > 0 ? buf.slice(lastIndex) : buf;
}

els.chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const question = els.questionInput.value.trim();
  if (!question) return;
  els.questionInput.value = "";
  runChat(question, { voice: false });
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
