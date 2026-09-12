const controls = {
  enabled: document.getElementById("enabled"),
  autoStart: document.getElementById("autoStart"),
  interim: document.getElementById("interim"),
  lang: document.getElementById("lang"),
  wakeEnabled: document.getElementById("wakeEnabled"),
};

const grantBtn = document.getElementById("grantMic");
const micState = document.getElementById("micState");

const defaults = {
  enabled: true,
  autoStart: true,
  interim: true,
  lang: "en-US",
  wakeEnabled: false,
};

chrome.storage.sync.get(defaults, (v) => {
  controls.enabled.checked = v.enabled;
  controls.autoStart.checked = v.autoStart;
  controls.interim.checked = v.interim;
  controls.lang.value = v.lang;
  controls.wakeEnabled.checked = v.wakeEnabled;
  refreshMicState();
});

for (const [key, el] of Object.entries(controls)) {
  el.addEventListener("change", () => {
    const value = el.type === "checkbox" ? el.checked : el.value;
    if (key === "wakeEnabled") {
      chrome.runtime.sendMessage({ type: "set-wake", enabled: value });
    } else {
      chrome.storage.sync.set({ [key]: value });
    }
  });
}

async function refreshMicState() {
  if (!navigator.permissions?.query) return;
  try {
    const p = await navigator.permissions.query({ name: "microphone" });
    setMic(p.state);
    p.onchange = () => setMic(p.state);
  } catch {}
}

function setMic(state) {
  if (state === "granted") {
    micState.textContent = "Microphone: granted for the extension.";
    grantBtn.disabled = true;
  } else if (state === "denied") {
    micState.textContent = "Microphone: denied. Re-allow it in browser settings.";
    grantBtn.disabled = false;
  } else {
    micState.textContent = "Grant once so the background listener can hear you.";
    grantBtn.disabled = false;
  }
}

grantBtn.addEventListener("click", async () => {
  grantBtn.disabled = true;
  grantBtn.textContent = "Requesting…";
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
    grantBtn.textContent = "Granted";
    setMic("granted");
  } catch (e) {
    grantBtn.textContent = "Blocked — check browser settings";
    setMic("denied");
  }
});

const testBtn = document.getElementById("testSpeech");
const testResult = document.getElementById("testResult");

testBtn.addEventListener("click", async () => {
  const SR = window.webkitSpeechRecognition || window.SpeechRecognition;
  if (!SR) {
    testResult.textContent = "No SpeechRecognition API in this browser.";
    return;
  }

  const lines = [];
  const push = (s) => { lines.push(s); testResult.textContent = lines.join("\n"); };

  // Probe on-device availability if the browser exposes it.
  if (typeof SR.available === "function") {
    try {
      const status = await SR.available({ langs: ["en-US"] });
      push(`on-device available: ${JSON.stringify(status)}`);
      if (status === "downloadable" && typeof SR.install === "function") {
        push("attempting install…");
        try {
          await SR.install({ langs: ["en-US"] });
          push("install ok");
        } catch (e) { push(`install failed: ${e.message}`); }
      }
    } catch (e) { push(`available() threw: ${e.message}`); }
  } else {
    push("on-device API not exposed (no SR.available)");
  }

  push("starting recognizer…");
  const r = new SR();
  r.lang = "en-US";
  r.continuous = false;
  r.interimResults = true;
  try { r.processLocally = true; push("processLocally set"); } catch {}
  const stamp = () => `${Math.round(performance.now())}ms`;
  const event = (name, extra) => push(`[${stamp()}] ${name}${extra ? " " + extra : ""}`);
  r.onstart = () => event("start");
  r.onaudiostart = () => event("audiostart");
  r.onspeechstart = () => event("speechstart");
  r.onresult = (e) => {
    let t = "";
    for (let i = 0; i < e.results.length; i++) t += e.results[i][0].transcript;
    event("result", JSON.stringify(t));
  };
  r.onerror = (e) => event("ERROR", e.error + (e.message ? " " + e.message : ""));
  r.onspeechend = () => event("speechend");
  r.onaudioend = () => event("audioend");
  r.onend = () => event("end");
  try {
    r.start();
    event("(called .start)");
  } catch (e) {
    event("(start threw)", e.message);
  }
  setTimeout(() => { try { r.stop(); } catch {} }, 6000);
});
