const $ = (id) => document.getElementById(id);

function paint(s) {
  if (!s) return;
  const set = (id, cls, text) => {
    $(id + "Dot").className = "dot " + cls;
    $(id).textContent = text;
  };

  if (s.helper) {
    set("helper", "ok", "Speech helper connected");
  } else if (s.error === "helper-not-installed") {
    set("helper", "bad", "Speech helper not installed");
  } else {
    set("helper", "bad", "Speech helper not running");
  }

  if (!s.helper) set("island", "", "Island: unknown");
  else if (s.listening) set("island", s.awaiting ? "warn" : "ok",
    s.awaiting ? "Island listening - waiting for a command" : "Island listening");
  else if (s.island) set("island", "warn", "Island running, VOICE is off");
  else set("island", "bad", "Island overlay not running");

  $("help").textContent = !s.helper
    ? "Run ./speech_mac/install.sh from the Hed folder, then reload this extension."
    : !s.island
      ? "Start the island: .venv/bin/python overlay.py (or ./run.sh --overlay)."
      : !s.listening
        ? "Click VOICE on the island to start listening."
        : "";
}

chrome.runtime.sendMessage({ type: "speech-status" }).then(paint).catch(() => {});
chrome.storage.onChanged.addListener((changes) => {
  if (changes.speechStatus) paint(changes.speechStatus.newValue);
});
