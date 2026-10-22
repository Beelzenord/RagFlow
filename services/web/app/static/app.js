// Rahmqvist Assistant - the chat page. Vanilla JS.
// The topbar (theme, sign-in, the admin's Dashboard switch) lives in shell.js;
// uploading and organising documents lives on the Dashboard.

// Left behind by the old sidebar's documents list, which cached every filename
// in the browser. Cleared on load so a reader never inherits one.
const LEGACY_DOCS_KEY = "rag.docs.v1";
const SHOW_SOURCES_KEY = "rag.showSources.v1";
const MARKET_KEY = "rag.market.v1";
// Follow-ups are resolved server-side against these turns, so the window is a
// tradeoff between context and prompt cost. 6 turns ~ 3 exchanges.
const MAX_HISTORY_TURNS = 6;
const MAX_TURN_CHARS = 1500;
const VOICE_LANG_KEY = "rag.voiceLang.v1";
// Enough of a cushion to absorb a network hiccup between chunks without being
// audible as a delay before the answer starts.
const AUDIO_LEAD_SECONDS = 0.06;
// The empty chat box's rolling greeting: where its round has got to, so the
// next visit carries on, and how long each line stays.
const GREETINGS_KEY = "rag.greetings.v1";
const GREETING_MS = 7000;

const state = {
  turns: [],
  // What the two pickers were last filled from, so a language switch can
  // relabel them without asking the server again.
  marketCountries: [],
  // "Answers for" is decided once per page load (see initialMarket); after that
  // a re-render keeps whatever is selected, so the picker never flips under
  // someone mid-conversation.
  marketDecided: false,
  // True while the selection is our guess from the browser's region rather than
  // this person's own choice.
  marketGuessed: false,
  pickerDocs: [],
  // Set from /api/me. Starts false so a reader is never briefly offered a
  // control the server would refuse; the server checks again regardless.
  canWrite: false,
  streaming: false,
  // Set while an answer is in flight so the send button can cancel it.
  abort: null,
  // The line on the empty box, the timer for the next, and where the round is.
  // `started` stays false if greetings.js is missing - the plain placeholder
  // then remains.
  greeting: { started: false, key: null, timer: null, first: true, saved: null },
};

// Voice keeps its own state so nothing in the written path can be disturbed by
// it. Declared here rather than beside the voice code because updateSendBtn runs
// during the initial paint and reads voice.recording.
const voice = {
  recording: false,
  streaming: false,
  abort: null,
  recorder: null,
  micStream: null,
  chunks: [],
  audioCtx: null,
  // When the current recording started, and the loudest frame heard during it.
  // Both gate the upload; see shouldSendRecording.
  startedAt: 0,
  meter: null,
  // The next free instant on the output timeline. Every chunk starts here, so
  // playback order is a consequence of arrival order and nothing else.
  nextStartTime: 0,
  sources: [],
  // A chunk boundary can fall inside a 16-bit sample; the odd byte waits here
  // for the rest of its pair rather than being played as noise.
  pcmCarry: null,
};

const els = {
  marketField: document.getElementById("market-field"),
  marketSelect: document.getElementById("market-select"),
  scopeSelect: document.getElementById("scope-select"),
  showSources: document.getElementById("show-sources"),
  chatLog: document.getElementById("chat-log"),
  chatForm: document.getElementById("chat-form"),
  questionInput: document.getElementById("question-input"),
  composerRoll: document.getElementById("composer-roll"),
  askBtn: document.getElementById("ask-btn"),
  newChatBtn: document.getElementById("new-chat-btn"),
  voiceControls: document.getElementById("voice-controls"),
  micBtn: document.getElementById("mic-btn"),
  voiceLang: document.getElementById("voice-lang"),
  voiceStatus: document.getElementById("voice-status"),
};

const bounceIfLoggedOut = Shell.bounceIfLoggedOut;

try {
  localStorage.removeItem(LEGACY_DOCS_KEY);
} catch {
  /* storage disabled - ignore */
}

// "degraded" means indexed, but at least one page was recovered from the raw
// PDF text layer - searchable like any other finished document.
function isIndexed(status) {
  return status === "completed" || status === "degraded";
}

// "Search in": an admin can aim a question at one document. Filled when the
// page loads and again whenever the tab comes back into view, so a document
// finished on the Dashboard in another tab shows up without a reload - and
// nothing polls.
async function loadDocumentPicker() {
  try {
    const resp = await fetch("/api/documents?limit=1000");
    if (bounceIfLoggedOut(resp.status)) return;
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const docs = Array.isArray(data?.documents) ? data.documents : [];
    state.pickerDocs = docs.filter((d) => isIndexed(d.status));
    renderDocumentPicker();
  } catch (err) {
    console.warn("document list failed", err);
  }
}

function renderDocumentPicker() {
  const current = els.scopeSelect.value;
  const all = document.createElement("option");
  all.value = "";
  all.textContent = Lang.t("chat.allDocuments");
  const options = [all];
  for (const d of state.pickerDocs) {
    const opt = document.createElement("option");
    opt.value = d.id;
    opt.textContent = d.original_filename || d.id;
    options.push(opt);
  }
  els.scopeSelect.replaceChildren(...options);
  if (options.some((o) => o.value === current)) els.scopeSelect.value = current;
}

// "Answers for": the asker's market. Answers then draw on documents for
// everyone plus documents for that market.
//
// What is stored is only ever this person's own choice: a country code, or
// ALL_MARKETS for a deliberate "All markets" - kept distinct from "never chosen"
// (no key), or a default guess would override that choice on every visit. The
// guess itself is never stored, so it can improve (a market added later, a
// browser set up differently) instead of hardening into a choice nobody made.
const ALL_MARKETS = "all";

function loadSavedMarket() {
  try {
    const value = localStorage.getItem(MARKET_KEY);
    if (value === ALL_MARKETS) return { kind: "all", code: "" };
    if (value && /^[A-Z]{2}$/.test(value)) return { kind: "code", code: value };
  } catch {
    /* storage disabled - nothing chosen */
  }
  return { kind: "none", code: "" };
}

function saveMarket(code) {
  try {
    localStorage.setItem(MARKET_KEY, code || ALL_MARKETS);
  } catch {
    /* the choice lasts for this page only */
  }
}

// Where "Answers for" starts: this person's choice if it is still a market, or
// their deliberate "All markets"; otherwise the first country in their browser's
// language settings that is a market (sv-SE -> Sweden); otherwise all markets.
// A saved country an admin has since removed counts as no choice.
function initialMarket(codes) {
  const saved = loadSavedMarket();
  if (saved.kind === "code" && codes.includes(saved.code)) return { code: saved.code, guessed: false };
  if (saved.kind === "all") return { code: "", guessed: false };
  const guess = Lang.browserRegions().find((region) => codes.includes(region));
  return guess ? { code: guess, guessed: true } : { code: "", guessed: false };
}

async function loadMarketPicker() {
  let countries = [];
  try {
    const resp = await fetch("/api/markets");
    if (bounceIfLoggedOut(resp.status)) return;
    if (resp.ok) {
      const data = await resp.json();
      countries = Array.isArray(data?.countries) ? data.countries : [];
    }
  } catch (err) {
    console.warn("market list failed", err);
  }
  state.marketCountries = countries;
  renderMarketPicker();
}

function renderMarketPicker() {
  const countries = state.marketCountries;
  // With no markets every document is for everyone, so there is nothing to pick.
  if (!countries.length) {
    els.marketField.hidden = true;
    els.marketSelect.value = "";
    return;
  }
  const all = document.createElement("option");
  all.value = "";
  all.textContent = Lang.t("chat.allMarkets");
  // Country names in the reader's language ("Sverige", "Ruotsi"), sorted by
  // that language's rules.
  const named = countries
    .map((c) => ({ code: c.code, name: Lang.region(c.code, c.name) }))
    .sort((a, b) => Lang.compare(a.name, b.name));
  const options = [all];
  for (const c of named) {
    const opt = document.createElement("option");
    opt.value = c.code;
    opt.textContent = c.name;
    options.push(opt);
  }
  const codes = countries.map((c) => c.code);
  let selected = els.marketSelect.value;
  // Decided on the first render with markets, and again only if the selected
  // market has since been removed.
  if (!state.marketDecided || (selected && !codes.includes(selected))) {
    const start = initialMarket(codes);
    selected = start.code;
    state.marketGuessed = start.guessed;
    state.marketDecided = true;
  }
  els.marketSelect.replaceChildren(...options);
  els.marketSelect.value = selected;
  els.marketSelect.title = state.marketGuessed ? Lang.t("chat.marketGuessed") : "";
  els.marketField.hidden = false;
}

els.marketSelect.addEventListener("change", () => {
  saveMarket(els.marketSelect.value);
  state.marketGuessed = false;
  els.marketSelect.title = "";
});

function currentMarket() {
  return els.marketField.hidden ? null : els.marketSelect.value || null;
}

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

// What the page becomes once the role is known. A reader is left with the
// conversation: no document picker, nothing asking the server for filenames,
// and no source details, because the toggle that governs them is an admin
// control they cannot reach.
async function initSession() {
  const info = await Shell.session;
  state.canWrite = info.can_write === true;
  loadMarketPicker();
  if (state.canWrite) loadDocumentPicker();
  // A market or document added on the Dashboard in another tab appears when
  // this one comes back into view.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    loadMarketPicker();
    if (state.canWrite) loadDocumentPicker();
  });
  if (!state.canWrite) {
    applyShowSources(false);
  }
  // Without an ElevenLabs key every voice request would 503, so the control is
  // withheld rather than offered and then refused.
  if (info.voice_enabled === true) initVoice(info.voice_languages);
}

initSession();

// Static text is relabelled by Lang.apply(); this is what the page built itself.
// Messages already in the conversation keep the words they were written in.
document.addEventListener("langchange", () => {
  renderMarketPicker();
  if (state.canWrite) renderDocumentPicker();
  updateSendBtn();
  followUiLanguageForVoice();
  // The same line, in the new language - no roll, nothing new drawn.
  if (state.greeting.key) showGreeting(state.greeting.key, { animate: false });
});

async function readNdjson(resp, onEvent) {
  /** Feed one parsed object per line to onEvent, in arrival order. */
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
        onEvent(JSON.parse(line));
      } catch (err) {
        console.warn("bad ndjson line", line, err);
      }
    }
  }
  if (buf.trim()) {
    try { onEvent(JSON.parse(buf)); } catch (_) { /* ignore */ }
  }
}

async function runChat(question) {
  if (!question) return;
  const documentId = els.scopeSelect.value || null;
  const history = state.turns.slice(-MAX_HISTORY_TURNS);

  appendMessage("user", question);
  state.streaming = true;
  state.abort = new AbortController();
  updateSendBtn();

  const bubble = appendMessage("bot", "");
  const answerNode = document.createElement("div");
  answerNode.className = "answer";
  const spinner = document.createElement("span");
  spinner.className = "spinner";
  spinner.setAttribute("aria-label", Lang.t("chat.thinking"));
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
      answerNode.textContent = Lang.t("chat.failed", { message: evt.message || Lang.t("chat.unknownError") });
    } else if (evt.type === "done") {
      if (!firstDeltaSeen) {
        answerNode.textContent = Lang.t("chat.empty");
      } else {
        // Models sometimes still emit [1]/ strip leftover markers from the final text.
        answerNode.textContent = stripInlineCitations(answerNode.textContent);
        answered = true;
      }
    }
  };

  try {
    const body = { question };
    const market = currentMarket();
    if (documentId) body.document_id = documentId;
    else if (market) body.market = market;
    if (history.length) body.history = history;
    const resp = await fetch("/api/query/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: state.abort.signal,
    });
    if (bounceIfLoggedOut(resp.status)) return;
    if (!resp.ok || !resp.body) {
      const data = await resp.json().catch(() => ({}));
      const msg = data?.detail || data?.error || `HTTP ${resp.status}`;
      throw new Error(msg);
    }

    await readNdjson(resp, handleEvent);
  } catch (err) {
    if (err.name === "AbortError") {
      // Stopping is a decision, not a failure, so whatever already arrived
      // stands as the answer and counts as context for the next turn.
      if (firstDeltaSeen) {
        answerNode.textContent = stripInlineCitations(answerNode.textContent);
        answered = true;
      } else {
        answerNode.textContent = Lang.t("chat.stopped");
      }
    } else {
      bubble.classList.add("error");
      answerNode.textContent = Lang.t("chat.failed", { message: err.message });
    }
  } finally {
    state.streaming = false;
    state.abort = null;
    updateSendBtn();
    // Only successful exchanges become context - errors, empty answers and
    // "nothing found" replies would just be noise in the next rewrite.
    if (answered) {
      recordTurn("user", question);
      recordTurn("assistant", answerNode.textContent);
    }
    els.questionInput.focus();
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
  cancelVoiceTurn();
  els.chatLog.innerHTML = "";
  if (state.greeting.started) showGreeting(drawGreeting());
  updateNewChatBtn();
  autoGrowInput();
  updateSendBtn();
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
  if (state.streaming) return;
  const question = els.questionInput.value.trim();
  if (!question) return;
  els.questionInput.value = "";
  autoGrowInput();
  updateSendBtn();
  runChat(question);
});

// The send button is the form's submit control, so while an answer is streaming
// its click has to be caught before the form ever sees it.
els.askBtn.addEventListener("click", (e) => {
  if (!state.streaming) return;
  e.preventDefault();
  state.abort?.abort();
});

els.questionInput.addEventListener("input", () => {
  autoGrowInput();
  updateSendBtn();
});

els.questionInput.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
  // Enter is also how an IME accepts the current candidate, which is not a send.
  if (e.isComposing) return;
  e.preventDefault();
  if (state.streaming) return;
  els.chatForm.requestSubmit();
});

function autoGrowInput() {
  // Collapse first, so the field shrinks again after a long question is sent.
  const input = els.questionInput;
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
}

function updateSendBtn() {
  const busy = state.streaming;
  els.chatForm.classList.toggle("is-streaming", busy);
  els.askBtn.disabled = !busy && !els.questionInput.value.trim();
  els.askBtn.setAttribute("aria-label", Lang.t(busy ? "chat.stop" : "chat.send"));
  updateMicBtn();
  // Every change to what is typed, and to streaming, comes through here.
  syncGreeting();
}

// --- Composer greetings ----------------------------------------------------------
// Over the empty box, a short line that rolls up to make way for the next every
// few seconds (static/greetings.js picks them). Decoration only: the box's name
// for screen readers is the fixed chat.inputLabel, and the overlay is
// aria-hidden. It rolls only while nothing is typed, nothing is streaming and
// the tab is in view; focus does not stop it, since the box keeps focus after
// every send.

function loadGreetings() {
  try {
    return JSON.parse(localStorage.getItem(GREETINGS_KEY) || "null");
  } catch {
    return null;
  }
}

// The next line, its place in the round kept in memory and, where it can be,
// in storage for the next visit.
function drawGreeting() {
  const g = state.greeting;
  const { key, saved } = Greetings.next(g.saved, new Date(), { first: g.first });
  g.first = false;
  g.saved = saved;
  try {
    localStorage.setItem(GREETINGS_KEY, JSON.stringify(saved));
  } catch {
    /* the round lasts for this page only */
  }
  return key;
}

// Swaps the line: the old one rolls up and away as the new one rolls in from
// below (a plain crossfade under reduced motion, see styles.css).
function showGreeting(key, { animate = true } = {}) {
  state.greeting.key = key;
  const box = els.composerRoll;
  const line = document.createElement("span");
  line.className = "roll-line";
  line.textContent = Lang.t(key);
  if (animate) {
    for (const old of box.querySelectorAll(".roll-line:not(.is-leaving)")) {
      old.classList.add("is-leaving");
      const drop = () => old.remove();
      old.addEventListener("animationend", drop, { once: true });
      // animationend never comes for a hidden overlay or a background tab.
      setTimeout(drop, 900);
    }
    line.classList.add("is-entering");
    line.addEventListener("animationend", () => line.classList.remove("is-entering"), { once: true });
    box.append(line);
  } else {
    box.replaceChildren(line);
  }
  fitGreeting(line);
}

// A line too long for a narrow box first gets a slightly smaller size, down to
// 12px, and only then an ellipsis.
function fitGreeting(line) {
  line.style.fontSize = "";
  if (!line.clientWidth || line.scrollWidth <= line.clientWidth) return;
  const base = parseFloat(getComputedStyle(line).fontSize) || 14;
  // Text does not scale quite linearly with its size, so the estimate is
  // checked and stepped down until the line really fits.
  let size = Math.max(12, Math.floor(base * (line.clientWidth / line.scrollWidth) * 10) / 10);
  line.style.fontSize = `${size}px`;
  while (size > 12 && line.scrollWidth > line.clientWidth) {
    size = Math.max(12, size - 0.2);
    line.style.fontSize = `${size}px`;
  }
}

function greetingIdle() {
  return !els.questionInput.value && !state.streaming && !document.hidden;
}

function refitGreeting() {
  const line = els.composerRoll?.querySelector(".roll-line:not(.is-leaving)");
  if (line) fitGreeting(line);
}

function syncGreeting() {
  const g = state.greeting;
  if (!g.started) return;
  const wasHidden = els.composerRoll.hidden;
  els.composerRoll.hidden = Boolean(els.questionInput.value);
  // A hidden line has no width, so one drawn while text was typed fits now.
  if (wasHidden && !els.composerRoll.hidden) refitGreeting();
  clearTimeout(g.timer);
  g.timer = null;
  if (!greetingIdle()) return;
  g.timer = setTimeout(() => {
    if (greetingIdle()) showGreeting(drawGreeting());
    syncGreeting();
  }, GREETING_MS);
}

function startGreetings() {
  if (!els.composerRoll || !window.Greetings) return;
  // The native placeholder (and the attribute Lang.apply would refill it from)
  // gives way to the overlay; without this script it simply stays.
  els.questionInput.placeholder = "";
  els.questionInput.removeAttribute("data-i18n-placeholder");
  state.greeting.saved = loadGreetings();
  state.greeting.started = true;
  els.composerRoll.hidden = Boolean(els.questionInput.value);
  showGreeting(drawGreeting());
  syncGreeting();
}

document.addEventListener("visibilitychange", syncGreeting);
// A box that changes width (a phone turned, the window resized) refits its line.
window.addEventListener("resize", refitGreeting);

startGreetings();
updateSendBtn();

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
  div.textContent = Lang.t("chat.searchedFor", { text });
  return div;
}

function renderSourceDocs(list) {
  /** Always-visible download links for documents used in the answer. */
  const wrap = document.createElement("div");
  wrap.className = "source-docs";

  const label = document.createElement("div");
  label.className = "source-docs-label";
  label.textContent = Lang.t("chat.sources");
  wrap.appendChild(label);

  const links = document.createElement("div");
  links.className = "source-docs-links";

  const seen = new Set();
  for (const c of list) {
    if (!c.document_id || seen.has(c.document_id)) continue;
    seen.add(c.document_id);
    const name = c.filename || Lang.t("chat.untitled");
    const a = document.createElement("a");
    a.className = "source-doc-link";
    a.href = `/api/documents/${encodeURIComponent(c.document_id)}/file`;
    a.download = name;
    a.title = Lang.t("chat.download", { name });
    a.textContent = name;
    const market = marketTag(c.audience);
    if (market) a.appendChild(market);
    links.appendChild(a);
  }

  if (!links.childNodes.length) return document.createDocumentFragment();
  wrap.appendChild(links);
  return wrap;
}

// "· Sverige" after a source that is limited to certain markets, so it is plain
// which country an answer came from. Nothing for a document that is for
// everyone. Countries are named in the reader's language, like the rest of the
// message, and keep that wording if the language changes later; group names
// are shown as the admin typed them.
function marketTag(audience) {
  const countries = Array.isArray(audience?.countries) ? audience.countries.map(String) : [];
  const groups = Array.isArray(audience?.groups) ? audience.groups.map(String) : [];
  const names = [...countries.map((code) => Lang.region(code)), ...groups].filter(Boolean);
  if (!names.length) return null;
  const span = document.createElement("span");
  span.className = "source-doc-market";
  span.textContent = ` · ${names.slice(0, 2).join(", ")}${names.length > 2 ? ` +${names.length - 2}` : ""}`;
  span.title = names.join(", ");
  return span;
}

function renderCitations(list) {
  /** Optional detail rows (page, heading, score) gated by "Show source details". */
  const cites = document.createElement("div");
  cites.className = "citations";
  for (const c of list) {
    const line = document.createElement("div");
    const loc = [c.filename, c.page_number ? Lang.t("chat.page", { n: c.page_number }) : null, c.heading]
      .filter(Boolean)
      .join(" - ");
    // "vector", "keyword" or "both" - which retriever found this chunk. The
    // score is always vector similarity, so a keyword-only hit can score low
    // and still be the right answer; showing both together explains that.
    const found = { dense: "chat.foundVector", lexical: "chat.foundKeyword", both: "chat.foundBoth" }[
      c.retrieval
    ];
    const detail = [
      typeof c.score === "number" ? Lang.t("chat.score", { score: c.score.toFixed(2) }) : null,
      found ? Lang.t("chat.foundBy", { how: Lang.t(found) }) : null,
    ].filter(Boolean);
    line.textContent = `[${c.n}] ${loc}${detail.length ? ` (${detail.join(", ")})` : ""}`;
    cites.appendChild(line);
  }
  return cites;
}

// ---------------------------------------------------------------------------
// Voice: push to talk
//
// One recording goes up, one NDJSON stream comes back carrying the transcript,
// the answer text and the audio. Audio is appended in arrival order and nothing
// here reorders, retries or drops a chunk - the previous version fired a request
// per sentence and tried to resequence the replies, which is where the wrong
// order and the missing short sentences came from. The written chat path is not
// touched: this shares the renderers and state.turns, and nothing else.
// ---------------------------------------------------------------------------

function initVoice(languages) {
  if (!els.micBtn || !els.voiceControls) return;
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    return;
  }
  els.voiceControls.hidden = false;
  renderVoiceLanguages(languages);

  // A voice language picked here sticks; until then it follows the interface.
  const saved = savedVoiceLanguage();
  if (els.voiceLang) els.voiceLang.value = saved || defaultVoiceLanguage();
  els.voiceLang?.addEventListener("change", () => {
    try {
      localStorage.setItem(VOICE_LANG_KEY, els.voiceLang.value);
    } catch {
      /* ignore */
    }
  });

  els.micBtn.addEventListener("click", () => {
    if (voice.recording) stopRecording();
    else if (voice.streaming) cancelVoiceTurn();
    else startRecording();
  });
}

// Only the languages the server has a voice of its own for (VOICE_LANGUAGES in
// voice.py) - offering one it would read in another language's voice would be
// a promise it cannot keep. Each is named in itself, as the interface picker
// names it. With a single language there is nothing to choose, so no picker.
function renderVoiceLanguages(languages) {
  if (!els.voiceLang) return;
  const offered = (Array.isArray(languages) ? languages : []).filter((code) => Lang.supported.includes(code));
  // An older server that does not say keeps the options in the page.
  if (!offered.length) return;
  els.voiceLang.replaceChildren(...offered.map((code) => new Option(Lang.names[code] || code, code)));
  els.voiceLang.hidden = offered.length < 2;
}

// The interface's language when it has a voice; otherwise English, which
// always does.
function defaultVoiceLanguage() {
  const offered = [...(els.voiceLang?.options || [])].map((o) => o.value);
  if (offered.includes(Lang.current)) return Lang.current;
  return offered.includes("en") ? "en" : offered[0] || "en";
}

function savedVoiceLanguage() {
  try {
    const saved = localStorage.getItem(VOICE_LANG_KEY);
    return saved && [...(els.voiceLang?.options || [])].some((o) => o.value === saved) ? saved : null;
  } catch {
    return null;
  }
}

function followUiLanguageForVoice() {
  if (els.voiceLang && !savedVoiceLanguage()) els.voiceLang.value = defaultVoiceLanguage();
  updateMicBtn();
  setVoiceStatus("");
}

function setVoiceStatus(text) {
  if (els.voiceStatus) els.voiceStatus.textContent = text || "";
}

function updateMicBtn() {
  if (!els.micBtn) return;
  els.micBtn.classList.toggle("is-recording", voice.recording);
  els.micBtn.setAttribute(
    "aria-label",
    Lang.t(voice.recording ? "voice.stopRecording" : voice.streaming ? "voice.stopSpeaking" : "voice.ask"),
  );
  // The written composer stays usable, but two answers streaming into the same
  // log at once would interleave.
  els.micBtn.disabled = state.streaming;
}

function pickRecorderMime() {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"];
  for (const type of candidates) {
    if (MediaRecorder.isTypeSupported?.(type)) return type;
  }
  return "";
}

async function startRecording() {
  if (voice.recording || voice.streaming) return;
  try {
    voice.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
  } catch (err) {
    setVoiceStatus(Lang.t(err.name === "NotAllowedError" ? "voice.blocked" : "voice.noMic"));
    return;
  }

  // Creating the context inside the click keeps it out of the autoplay-blocked
  // state; created later, the first answer would be silent.
  ensureAudioCtx();

  const mime = pickRecorderMime();
  voice.chunks = [];
  try {
    voice.recorder = new MediaRecorder(voice.micStream, mime ? { mimeType: mime } : undefined);
  } catch {
    releaseMic();
    setVoiceStatus(Lang.t("voice.unsupported"));
    return;
  }

  voice.recorder.addEventListener("dataavailable", (e) => {
    if (e.data && e.data.size) voice.chunks.push(e.data);
  });
  voice.recorder.addEventListener("stop", () => {
    const type = voice.recorder?.mimeType || mime || "audio/webm";
    const blob = new Blob(voice.chunks, { type });
    voice.chunks = [];
    const durationMs = voice.startedAt ? Date.now() - voice.startedAt : 0;
    const peakRms = stopMicMeter();
    voice.startedAt = 0;
    releaseMic();
    voice.recording = false;
    updateMicBtn();
    if (shouldSendRecording(blob, durationMs, peakRms)) sendVoiceTurn(blob);
    else setVoiceStatus(blob.size > 0 ? Lang.t("voice.notHeard") : "");
  });

  voice.meter = startMicMeter(voice.micStream);
  voice.recorder.start();
  voice.startedAt = Date.now();
  voice.recording = true;
  setVoiceStatus(Lang.t("voice.listening"));
  updateMicBtn();
}

function stopRecording() {
  if (!voice.recording) return;
  try {
    voice.recorder?.stop();
  } catch {
    releaseMic();
    voice.recording = false;
    updateMicBtn();
  }
}

function releaseMic() {
  // The meter holds a node fed by the mic stream, so it has to go before the
  // tracks do. Harmless when the stop handler already collected it.
  stopMicMeter();
  // Leaving the track live keeps the browser's recording indicator on, which
  // reads as "still listening" long after the turn is over.
  voice.micStream?.getTracks().forEach((t) => t.stop());
  voice.micStream = null;
  voice.recorder = null;
}

function cancelVoiceTurn() {
  voice.abort?.abort();
  stopPlayback();
}

// A tap on the mic button, a mic that is muted at the OS level, or a recorder
// that captured a room and no speaker all produce a blob with a non-zero size.
// Uploading one costs a Scribe call to be told there is nothing in it, and
// risks the model answering silence with a subtitle artifact instead. Both
// gates below run entirely in the browser, so the cheapest rejection is also
// the earliest one.
const MIN_RECORDING_MS = 400;
// RMS over [-1, 1] samples. Speech sits around 0.05 and up; room tone with
// noiseSuppression on sits near 0.002. The floor is deliberately far below
// speech - it is here to catch silence, not to judge how quietly someone talks.
const MIN_RECORDING_RMS = 0.01;
// How often the meter samples. Frequent enough to catch a short word, far too
// coarse to cost anything.
const METER_INTERVAL_MS = 100;

// Watch the live mic and remember the loudest frame. Every failure path returns
// null, which shouldSendRecording reads as "unknown" and lets the upload
// through: a broken meter must never be able to swallow a real question.
function startMicMeter(stream) {
  try {
    const ctx = ensureAudioCtx();
    if (!ctx || typeof ctx.createAnalyser !== "function") return null;
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    const source = ctx.createMediaStreamSource(stream);
    source.connect(analyser);

    const frame = new Float32Array(analyser.fftSize);
    const meter = { analyser, source, peak: 0, timer: 0 };
    meter.timer = setInterval(() => {
      try {
        analyser.getFloatTimeDomainData(frame);
        let sum = 0;
        for (let i = 0; i < frame.length; i += 1) sum += frame[i] * frame[i];
        const rms = Math.sqrt(sum / frame.length);
        if (rms > meter.peak) meter.peak = rms;
      } catch {
        /* leave peak where it is; stopMicMeter still reports it */
      }
    }, METER_INTERVAL_MS);
    return meter;
  } catch {
    return null;
  }
}

// Returns the loudest RMS seen, or null when no meter ran. Never throws - it is
// called from the recorder's stop handler, where an exception would strand the
// UI in the recording state.
function stopMicMeter() {
  const meter = voice.meter;
  voice.meter = null;
  if (!meter) return null;
  try {
    clearInterval(meter.timer);
    meter.source.disconnect();
    meter.analyser.disconnect();
  } catch {
    /* the context may already be gone */
  }
  return meter.peak;
}

// Fail-open by construction: only a positive measurement can reject a clip.
function shouldSendRecording(blob, durationMs, peakRms) {
  if (!blob || blob.size <= 0) return false;
  if (durationMs > 0 && durationMs < MIN_RECORDING_MS) return false;
  if (peakRms !== null && peakRms < MIN_RECORDING_RMS) return false;
  return true;
}

function ensureAudioCtx() {
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  if (!voice.audioCtx) voice.audioCtx = new Ctor();
  if (voice.audioCtx.state === "suspended") voice.audioCtx.resume().catch(() => {});
  return voice.audioCtx;
}

function stopPlayback() {
  for (const src of voice.sources) {
    try {
      src.stop();
    } catch {
      /* already finished */
    }
  }
  voice.sources = [];
  voice.pcmCarry = null;
  voice.nextStartTime = 0;
}

function base64ToBytes(b64) {
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function enqueueAudio(b64, sampleRate) {
  /** Schedule one PCM chunk immediately after the one before it. */
  const ctx = ensureAudioCtx();
  if (!ctx) return;

  let bytes = base64ToBytes(b64);
  if (voice.pcmCarry) {
    const merged = new Uint8Array(voice.pcmCarry.length + bytes.length);
    merged.set(voice.pcmCarry, 0);
    merged.set(bytes, voice.pcmCarry.length);
    bytes = merged;
    voice.pcmCarry = null;
  }
  if (bytes.length % 2 === 1) {
    voice.pcmCarry = bytes.slice(bytes.length - 1);
    bytes = bytes.subarray(0, bytes.length - 1);
  }
  const samples = bytes.length / 2;
  if (samples < 1) return;

  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
  const buffer = ctx.createBuffer(1, samples, sampleRate || 22050);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < samples; i++) channel[i] = view.getInt16(i * 2, true) / 32768;

  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(ctx.destination);
  // Append-only. If decoding fell behind the clock, restart from now rather
  // than scheduling in the past, which the browser would play all at once.
  const startAt = Math.max(voice.nextStartTime, ctx.currentTime + AUDIO_LEAD_SECONDS);
  src.start(startAt);
  voice.nextStartTime = startAt + buffer.duration;
  voice.sources.push(src);
  src.addEventListener("ended", () => {
    voice.sources = voice.sources.filter((s) => s !== src);
  });
}

async function sendVoiceTurn(blob) {
  const lang = els.voiceLang?.value || "en";
  const documentId = els.scopeSelect.value || null;
  const history = state.turns.slice(-MAX_HISTORY_TURNS);

  stopPlayback();
  voice.streaming = true;
  voice.abort = new AbortController();
  setVoiceStatus(Lang.t("voice.thinking"));
  updateMicBtn();

  const form = new FormData();
  form.append("file", blob, blob.type.includes("mp4") ? "audio.mp4" : "audio.webm");
  form.append("lang", lang);
  const market = currentMarket();
  if (documentId) form.append("document_id", documentId);
  else if (market) form.append("market", market);
  if (history.length) form.append("history", JSON.stringify(history));

  let bubble = null;
  let answerNode = null;
  let question = "";
  let firstDeltaSeen = false;
  let answered = false;
  let failure = "";

  const ensureBubble = () => {
    if (bubble) return;
    bubble = appendMessage("bot", "");
    answerNode = document.createElement("div");
    answerNode.className = "answer";
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    spinner.setAttribute("aria-label", Lang.t("chat.thinking"));
    answerNode.appendChild(spinner);
    bubble.appendChild(answerNode);
  };

  const handleEvent = (evt) => {
    if (!evt || typeof evt !== "object") return;
    if (evt.type === "transcript") {
      question = evt.text || "";
      appendMessage("user", question);
      ensureBubble();
      setVoiceStatus(Lang.t("voice.answering"));
    } else if (evt.type === "rewrite") {
      ensureBubble();
      if (evt.text) bubble.appendChild(renderRewrite(evt.text));
    } else if (evt.type === "citations") {
      ensureBubble();
      if (Array.isArray(evt.data) && evt.data.length) {
        bubble.appendChild(renderSourceDocs(evt.data));
        bubble.appendChild(renderCitations(evt.data));
      }
    } else if (evt.type === "delta") {
      ensureBubble();
      if (!firstDeltaSeen) {
        answerNode.textContent = "";
        firstDeltaSeen = true;
      }
      answerNode.appendChild(document.createTextNode(evt.text || ""));
      els.chatLog.scrollTop = els.chatLog.scrollHeight;
    } else if (evt.type === "audio") {
      if (evt.b64) enqueueAudio(evt.b64, evt.sample_rate);
    } else if (evt.type === "error") {
      // A failure that arrives mid-stream is shown as itself. The endpoint
      // answers NDJSON precisely so this cannot become an empty success.
      // "no_speech" is the one refusal a person is meant to read and act on,
      // so it is worded here rather than shown in the server's English.
      failure =
        evt.code === "no_speech" ? Lang.t("voice.noSpeech") : evt.message || Lang.t("chat.unknownError");
      ensureBubble();
      bubble.classList.add("error");
      answerNode.textContent = Lang.t("voice.failed", { message: failure });
    } else if (evt.type === "done") {
      ensureBubble();
      if (!firstDeltaSeen) {
        answerNode.textContent = Lang.t("chat.empty");
      } else {
        answerNode.textContent = stripInlineCitations(answerNode.textContent);
        answered = true;
      }
    }
  };

  try {
    const resp = await fetch("/api/voice/ask", {
      method: "POST",
      body: form,
      signal: voice.abort.signal,
    });
    if (bounceIfLoggedOut(resp.status)) return;
    if (!resp.ok || !resp.body) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data?.detail || data?.error || `HTTP ${resp.status}`);
    }
    await readNdjson(resp, handleEvent);
  } catch (err) {
    if (err.name === "AbortError") {
      if (answerNode && firstDeltaSeen) {
        answerNode.textContent = stripInlineCitations(answerNode.textContent);
        answered = true;
      } else if (answerNode) {
        answerNode.textContent = Lang.t("chat.stopped");
      }
    } else {
      failure = err.message;
      ensureBubble();
      bubble.classList.add("error");
      answerNode.textContent = Lang.t("voice.failed", { message: err.message });
    }
  } finally {
    voice.streaming = false;
    voice.abort = null;
    setVoiceStatus("");
    updateMicBtn();
    // Spoken turns join the same history as written ones, so a follow-up can be
    // typed after being asked aloud and the other way round.
    if (answered && question) {
      recordTurn("user", question);
      recordTurn("assistant", answerNode.textContent);
    }
  }
}
