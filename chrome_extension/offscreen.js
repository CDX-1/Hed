// The extension-owned speech recognizer. One instance handles two modes:
//
//   - wake:      continuous listening for "hey hed <command>"
//   - dictation: continuous listening streaming interim + final transcripts
//                to the tab that requested it
//
// Both run out of the extension's own origin, so one mic grant on the
// extension covers everything. We also hold a persistent MediaStream
// while either mode is active, so the OS-level microphone stays open and
// the mic indicator does not flicker on each recognizer restart.

const WAKE_RE = /\bhey\s+(hed|head|hedd|hedge|heads|hedd?y|hedz|edd)\b[\s,.:]*/i;
const COMMAND_TIMEOUT_MS = 6000;
const FLAP_WINDOW_MS = 800;    // ends faster than this count as flapping
const FLAP_MAX = 4;             // this many fast ends in a row = surrender

const state = {
  wakeEnabled: false,
  wakeLang: "en-US",
  dictationActive: false,
  dictationLang: "en-US",
  dictationInterim: true,
  awaiting: false,
  awaitTimer: 0,
  running: false,
  lastStartTs: 0,
  fastEnds: 0,
  reconfigureBusy: false,
  reconfigureQueued: false,
};

// Prefer the webkit-prefixed constructor: on some Chromium forks the
// unprefixed SpeechRecognition points to a placeholder that never
// actually connects.
const SR = self.webkitSpeechRecognition || self.SpeechRecognition;
let recog = null;
let keepAliveStream = null;

const log = (...a) => console.info("[hed-a11y/offscreen]", ...a);
const warn = (...a) => console.warn("[hed-a11y/offscreen]", ...a);

function mode() {
  if (state.dictationActive) return "dictation";
  if (state.wakeEnabled) return "wake";
  return "idle";
}

function desiredLang() {
  return state.dictationActive ? state.dictationLang : state.wakeLang;
}

function post(msg) {
  chrome.runtime.sendMessage(msg).catch(() => {});
}

function postStatus(status) {
  post({ type: "wake-status", status, awaiting: state.awaiting });
}

async function acquireMic() {
  if (keepAliveStream) return true;
  try {
    keepAliveStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    log("mic acquired");
    return true;
  } catch (e) {
    warn("mic acquire failed:", e?.name, e?.message);
    return false;
  }
}

function releaseMic() {
  if (!keepAliveStream) return;
  keepAliveStream.getTracks().forEach((t) => t.stop());
  keepAliveStream = null;
  log("mic released");
}

function build() {
  if (!SR) return null;
  const r = new SR();
  r.lang = desiredLang();
  r.continuous = true;
  r.interimResults = state.dictationActive ? state.dictationInterim : true;
  // Recent Chromium exposes on-device speech via processLocally. This
  // bypasses Google's cloud backend, which some Chromium forks (Dia,
  // ungoogled-chromium) cannot reach for lack of an API key. Setting it
  // is a no-op on browsers that ignore the property.
  try { r.processLocally = true; } catch {}
  r.onresult = onResult;
  r.onerror = onError;
  r.onend = onEnd;
  // Diagnostic hooks: log every stage so we can see what fires (or doesn't)
  // when the recognizer flaps. If none of these fire between start and end,
  // Chromium's speech backend is refusing to connect.
  r.onstart = () => log("sr event: start");
  r.onaudiostart = () => log("sr event: audiostart");
  r.onsoundstart = () => log("sr event: soundstart");
  r.onspeechstart = () => log("sr event: speechstart");
  r.onspeechend = () => log("sr event: speechend");
  r.onsoundend = () => log("sr event: soundend");
  r.onaudioend = () => log("sr event: audioend");
  r.onnomatch = () => log("sr event: nomatch");
  return r;
}

function destroy() {
  state.awaiting = false;
  clearTimeout(state.awaitTimer);
  if (recog) {
    try { recog.onresult = null; recog.onerror = null; recog.onend = null; recog.stop(); } catch {}
    recog = null;
  }
  state.running = false;
}

async function reconfigure() {
  if (state.reconfigureBusy) { state.reconfigureQueued = true; return; }
  state.reconfigureBusy = true;
  try {
    const m = mode();
    log("reconfigure ->", m, "lang=", desiredLang());
    destroy();
    if (m === "idle") {
      releaseMic();
      postStatus("idle");
      return;
    }
    const ok = await acquireMic();
    if (!ok) {
      if (state.dictationActive) post({ type: "dictation-error", error: "no-mic-permission" });
      if (state.wakeEnabled) {
        state.wakeEnabled = false;
        post({ type: "wake-fatal" });
        postStatus("permission-denied");
      }
      state.dictationActive = false;
      return;
    }
    if (!SR) {
      if (state.dictationActive) post({ type: "dictation-error", error: "no-speech-api" });
      return;
    }
    state.fastEnds = 0;
    startRecog(m);
  } finally {
    state.reconfigureBusy = false;
    if (state.reconfigureQueued) {
      state.reconfigureQueued = false;
      reconfigure();
    }
  }
}

function startRecog(m) {
  recog = build();
  if (!recog) return;
  try {
    recog.start();
    state.lastStartTs = Date.now();
    state.running = true;
    if (m === "dictation") post({ type: "dictation-started" });
    postStatus(m === "dictation" ? "dictating" : "listening");
    log("started", m);
  } catch (e) {
    warn("recog.start() threw:", e);
    state.running = false;
    if (state.dictationActive) post({ type: "dictation-error", error: "start-failed" });
  }
}

function onEnd() {
  state.running = false;
  const m = mode();
  log("recog ended, mode=", m);
  if (m === "idle") { postStatus("idle"); return; }

  const elapsed = Date.now() - state.lastStartTs;
  if (elapsed < FLAP_WINDOW_MS) {
    state.fastEnds++;
    if (state.fastEnds >= FLAP_MAX) {
      warn(`recognizer flapping (${state.fastEnds} fast ends), giving up`);
      if (state.dictationActive) {
        post({ type: "dictation-error", error: "recognizer-flapping" });
        state.dictationActive = false;
      }
      if (state.wakeEnabled) {
        state.wakeEnabled = false;
        post({ type: "wake-fatal" });
        postStatus("permission-denied");
      }
      destroy();
      releaseMic();
      return;
    }
  } else {
    state.fastEnds = 0;
  }
  startRecog(m);
}

function onError(e) {
  state.running = false;
  const kind = e.error;
  warn("recog error:", kind, e?.message || "");
  if (kind === "not-allowed" || kind === "service-not-allowed") {
    if (state.dictationActive) post({ type: "dictation-error", error: kind });
    if (state.wakeEnabled) {
      state.wakeEnabled = false;
      post({ type: "wake-fatal" });
      postStatus("permission-denied");
    }
    state.dictationActive = false;
    destroy();
    releaseMic();
  } else if (kind === "audio-capture") {
    if (state.dictationActive) post({ type: "dictation-error", error: kind });
    state.dictationActive = false;
    destroy();
    releaseMic();
  } else if (kind === "network") {
    postStatus("network-error");
    // onend will retry
  } else if (kind === "no-speech" || kind === "aborted") {
    // benign — onend restarts
  } else {
    if (state.dictationActive) post({ type: "dictation-error", error: kind });
    postStatus("error:" + kind);
  }
}

function onResult(e) {
  if (state.dictationActive) return handleDictation(e);
  if (state.wakeEnabled) return handleWake(e);
}

function handleDictation(e) {
  let finalText = "";
  let interim = "";
  for (let i = e.resultIndex; i < e.results.length; i++) {
    const r = e.results[i];
    if (r.isFinal) finalText += r[0].transcript;
    else interim += r[0].transcript;
  }
  if (finalText) {
    log("dictation-final:", JSON.stringify(finalText));
    post({ type: "dictation-final", text: finalText });
  }
  if (interim && state.dictationInterim) {
    post({ type: "dictation-interim", text: interim });
  }
}

function handleWake(e) {
  for (let i = e.resultIndex; i < e.results.length; i++) {
    const res = e.results[i];
    if (!res.isFinal) continue;
    const raw = res[0].transcript.trim();
    if (!raw) continue;

    if (state.awaiting) {
      clearTimeout(state.awaitTimer);
      state.awaiting = false;
      postStatus("dispatching");
      post({ type: "voice-command", text: raw });
      continue;
    }

    const m = raw.match(WAKE_RE);
    if (!m) continue;
    const rest = raw.slice(m.index + m[0].length).trim();
    if (rest) {
      postStatus("dispatching");
      post({ type: "voice-command", text: rest });
    } else {
      state.awaiting = true;
      postStatus("awaiting-command");
      state.awaitTimer = setTimeout(() => {
        state.awaiting = false;
        postStatus("wake-timeout");
      }, COMMAND_TIMEOUT_MS);
    }
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "wake-config") {
    state.wakeEnabled = !!msg.enabled;
    if (msg.lang) state.wakeLang = msg.lang;
    log("wake-config:", state.wakeEnabled);
    reconfigure();
  } else if (msg?.type === "dictation-config") {
    const wasActive = state.dictationActive;
    state.dictationActive = !!msg.active;
    if (msg.lang) state.dictationLang = msg.lang;
    if (msg.interim != null) state.dictationInterim = !!msg.interim;
    log("dictation-config:", state.dictationActive, "lang=", state.dictationLang);
    reconfigure();
    if (wasActive && !state.dictationActive) post({ type: "dictation-stopped" });
  } else if (msg?.type === "wake-ping") {
    postStatus(state.running ? mode() : "idle");
  }
});

// On load: ask the service worker for the current wake state. We don't
// touch chrome.storage from here — some Chromium builds do not expose it
// to offscreen documents.
log("offscreen loaded, SR=", !!SR);
post({ type: "offscreen-hello" });
