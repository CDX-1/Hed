// Listens in on the Hed island's recognizer and picks out commands.
//
// The island overlay owns the microphone: nothing hears anything until VOICE
// is switched on there. Its hed-speech hub broadcasts every transcript, and
// this module - through the hed-speech native messaging helper, which relays
// to the hub - watches them for the wake word. It never asks for audio itself,
// so there is exactly one place voice gets switched on.
//
// The overlay types what you say into the focused field; it holds back
// anything that looks like a command and waits for us to claim it, so every
// command we run is announced to the hub as "consumed".

import { findWake, isKeyPhrase, parseCommand } from "./commands.js";

export const HOST = "com.hed.speech";

const RETRY_MS = 5000;
const COMMAND_TIMEOUT_MS = 6000;
// A command that has parsed and then held still for this long in the interim
// transcript runs without waiting for the final result, which only arrives
// after a pause.
const STABLE_MS = 350;

const log = (...a) => console.info("[hed-a11y/speech]", ...a);

// Commands whose argument is free text grow as you keep talking ("open git"
// becomes "open github.com"), so they must wait for the final transcript.
const needsFinal = (cmd) => cmd.action === "open" || cmd.action === "search";

export class NativeSpeech {
  /** handlers: onCommand(text), onStatus({ helper, island, listening, awaiting, error }) */
  constructor(handlers) {
    this.h = handlers;
    this.port = null;
    this.retryTimer = 0;
    this.status = { helper: false, island: false, listening: false, awaiting: false, error: "" };
    this.awaiting = false;
    this.awaitingExplicit = false;
    this.awaitTimer = 0;
    this.pendingText = "";
    this.pendingTimer = 0;
    this.suppressUntilFinal = false;
  }

  start() {
    if (this.port) return;
    clearTimeout(this.retryTimer);
    let port;
    try {
      port = chrome.runtime.connectNative(HOST);
    } catch (e) {
      return this.lost(String(e));
    }
    this.port = port;
    port.onMessage.addListener((m) => this.onMessage(m));
    port.onDisconnect.addListener(() => {
      if (this.port !== port) return;
      this.port = null;
      this.lost(chrome.runtime.lastError?.message || "helper exited");
    });
    port.postMessage({ type: "ping" });
    port.postMessage({ type: "monitor" });
  }

  lost(reason) {
    log("helper unavailable:", reason);
    this.resetWake();
    const missing = /not found/i.test(reason);
    this.setStatus({
      helper: false, island: false, listening: false, awaiting: false,
      error: missing ? "helper-not-installed" : reason,
    });
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => this.start(), RETRY_MS);
  }

  send(msg) {
    try { this.port?.postMessage(msg); } catch {}
  }

  setStatus(patch) {
    const next = { ...this.status, ...patch };
    if (JSON.stringify(next) === JSON.stringify(this.status)) return;
    this.status = next;
    this.h.onStatus(next);
  }

  onMessage(m) {
    switch (m?.type) {
      case "pong":
        this.setStatus({ helper: true, error: "" });
        return;
      case "hub":
        this.setStatus({ island: !!m.connected, listening: m.connected ? this.status.listening : false });
        if (!m.connected) this.resetWake();
        return;
      case "status":
        if (m.status === "listening") this.setStatus({ island: true, listening: true, error: "" });
        if (m.status === "stopped") {
          this.resetWake();
          this.setStatus({ listening: false, awaiting: false });
        }
        return;
      case "error":
        this.setStatus({ error: m.error || "error" });
        return;
      case "result":
        this.setStatus({ island: true, listening: true });
        return this.onResult(m);
    }
  }

  onResult(m) {
    if (this.suppressUntilFinal) {
      // This utterance already ran from its interim transcript.
      if (m.final) this.suppressUntilFinal = false;
      return;
    }

    const hit = this.extract([m.text, ...(m.alts || [])]);
    if (!hit) {
      if (m.final) this.clearPending();
      return;
    }

    if (!m.final) {
      if (!hit.cmd || needsFinal(hit.cmd)) {
        this.clearPending();
        return;
      }
      if (hit.rest === this.pendingText) return;
      this.clearPending();
      this.pendingText = hit.rest;
      this.pendingTimer = setTimeout(() => {
        this.pendingText = "";
        this.suppressUntilFinal = true;
        this.fire(hit.rest);
      }, STABLE_MS);
      return;
    }

    this.clearPending();
    if (hit.rest) {
      this.fire(hit.rest);
    } else {
      // Wake word alone: the command is the next thing said.
      this.awaiting = true;
      this.awaitingExplicit = hit.explicit;
      this.setStatus({ awaiting: true });
      clearTimeout(this.awaitTimer);
      this.awaitTimer = setTimeout(() => {
        this.awaiting = false;
        this.setStatus({ awaiting: false });
      }, COMMAND_TIMEOUT_MS);
    }
  }

  /**
   * Look through the transcript and its alternatives for a wake word (or, when
   * awaiting, for anything). Prefer the first candidate that parses as a real
   * command; otherwise the first that at least contained "hey hed".
   */
  extract(candidates) {
    let fallback = null;
    for (const raw of candidates) {
      const c = (raw || "").trim();
      if (!c) continue;
      let rest;
      let explicit;
      if (this.awaiting) {
        rest = c.replace(/[.,!?]+$/, "");
        explicit = this.awaitingExplicit;
      } else {
        const wake = findWake(c);
        if (!wake) continue;
        ({ rest, explicit } = wake);
      }
      // "hed, enter" / "hed, command c" are OS-level keypresses that the
      // overlay handles. Don't parse or claim them, otherwise "hed, down"
      // would scroll the page on top of pressing the arrow key.
      if (rest && isKeyPhrase(rest)) return null;
      const cmd = rest ? parseCommand(rest) : null;
      if (cmd) return { rest, cmd, explicit };
      // A bare "hed" followed by words that are not a command is just
      // somebody saying "head" - the overlay types it as dictation.
      if (rest && !explicit) continue;
      fallback ??= { rest, cmd: null, explicit };
    }
    return fallback;
  }

  fire(text) {
    this.awaiting = false;
    clearTimeout(this.awaitTimer);
    this.setStatus({ awaiting: false });
    // Claim it first, so the overlay does not type the command out.
    this.send({ type: "consumed" });
    this.h.onCommand(text);
  }

  clearPending() {
    clearTimeout(this.pendingTimer);
    this.pendingText = "";
  }

  resetWake() {
    this.clearPending();
    this.awaiting = false;
    clearTimeout(this.awaitTimer);
    this.suppressUntilFinal = false;
  }
}
