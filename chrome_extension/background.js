// The extension never listens on its own. Voice is switched on from the Hed
// island overlay (VOICE), which runs the one on-device recognizer through the
// hed-speech hub; this worker attaches to that hub through the native
// messaging helper and turns "hey hed, ..." / "hed, ..." into browser actions.
// Typing into fields is the overlay's job, in the browser as everywhere else.

import { parseCommand } from "./commands.js";
import { NativeSpeech } from "./native-speech.js";

const speech = new NativeSpeech({
  onCommand: (text) => runVoiceCommand(text),
  onStatus: (status) => {
    session.set({ speechStatus: { ...status, at: Date.now() } }).catch(() => {});
    broadcastStatus(status);
  },
});

const session = chrome.storage.session ?? chrome.storage.local;

// Connecting keeps the worker alive for as long as the helper runs, and the
// helper keeps retrying the hub, so this is the only place that needs to.
speech.start();

chrome.tabs.onActivated.addListener(() => broadcastStatus(speech.status));

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "speech-status") {
    sendResponse(speech.status);
    return;
  }
  if (msg?.type === "voice-command") {
    runVoiceCommand(msg.text).then(sendResponse);
    return true;
  }
});

async function runVoiceCommand(text) {
  const cmd = parseCommand(text);
  const result = cmd
    ? await execute(cmd).catch((e) => ({ error: String(e) }))
    : { unrecognized: true };
  await notify(text, cmd, result);
  return { cmd, result };
}

async function broadcastStatus(status) {
  const t = await activeTab();
  if (!t?.id) return;
  chrome.tabs.sendMessage(t.id, { type: "speech-status", ...status }).catch(() => {});
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab ?? null;
}

async function execute(cmd) {
  switch (cmd.action) {
    case "newTab":
      return await chrome.tabs.create({});
    case "newWindow":
      return await chrome.windows.create(cmd.incognito ? { incognito: true } : {});
    case "closeTab": {
      const t = await activeTab();
      if (t) return await chrome.tabs.remove(t.id);
      return null;
    }
    case "duplicateTab": {
      const t = await activeTab();
      if (t) return await chrome.tabs.duplicate(t.id);
      return null;
    }
    case "pinTab": {
      const t = await activeTab();
      if (t) return await chrome.tabs.update(t.id, { pinned: cmd.pinned });
      return null;
    }
    case "muteTab": {
      const t = await activeTab();
      if (t) return await chrome.tabs.update(t.id, { muted: cmd.muted });
      return null;
    }
    case "shiftTab": {
      const tabs = await chrome.tabs.query({ currentWindow: true });
      const idx = tabs.findIndex((t) => t.active);
      if (idx < 0 || tabs.length < 2) return null;
      const n = tabs.length;
      const next = ((idx + cmd.delta) % n + n) % n;
      return await chrome.tabs.update(tabs[next].id, { active: true });
    }
    case "gotoTab": {
      const tabs = await chrome.tabs.query({ currentWindow: true });
      if (!tabs.length) return null;
      let i = cmd.index;
      if (i < 0) i = tabs.length + i;
      i = Math.max(0, Math.min(tabs.length - 1, i));
      return await chrome.tabs.update(tabs[i].id, { active: true });
    }
    case "moveTab": {
      const t = await activeTab();
      if (!t) return null;
      const tabs = await chrome.tabs.query({ currentWindow: true });
      const cur = tabs.findIndex((x) => x.id === t.id);
      const target = Math.max(0, Math.min(tabs.length - 1, cur + cmd.delta));
      return await chrome.tabs.move(t.id, { index: target });
    }
    case "reload": {
      const t = await activeTab();
      if (t) return await chrome.tabs.reload(t.id, { bypassCache: !!cmd.bypassCache });
      return null;
    }
    case "back": {
      const t = await activeTab();
      if (t) return await chrome.tabs.goBack(t.id).catch(() => null);
      return null;
    }
    case "forward": {
      const t = await activeTab();
      if (t) return await chrome.tabs.goForward(t.id).catch(() => null);
      return null;
    }
    case "zoom": {
      const t = await activeTab();
      if (!t) return null;
      if (cmd.reset) return await chrome.tabs.setZoom(t.id, 0);
      const z = await chrome.tabs.getZoom(t.id);
      return await chrome.tabs.setZoom(t.id, Math.max(0.25, Math.min(5, z + cmd.delta)));
    }
    case "search":
      return await chrome.tabs.create({
        url: "https://www.google.com/search?q=" + encodeURIComponent(cmd.query),
      });
    case "open": {
      const target = cmd.target.trim();
      const looksLikeUrl = /^(https?:\/\/|www\.)/i.test(target) || /\.[a-z]{2,}(\/|$)/i.test(target);
      const url = looksLikeUrl
        ? (target.startsWith("http") ? target : "https://" + target)
        : "https://www.google.com/search?q=" + encodeURIComponent(target);
      return await chrome.tabs.create({ url });
    }
    case "scroll": {
      const t = await activeTab();
      if (!t) return null;
      return await chrome.scripting.executeScript({
        target: { tabId: t.id },
        func: (dir) => {
          const h = window.innerHeight || 800;
          const opts = { behavior: "smooth" };
          switch (dir) {
            case "up": window.scrollBy({ top: -h * 0.4, ...opts }); break;
            case "down": window.scrollBy({ top: h * 0.4, ...opts }); break;
            case "pageUp": window.scrollBy({ top: -h * 0.9, ...opts }); break;
            case "pageDown": window.scrollBy({ top: h * 0.9, ...opts }); break;
            case "top": window.scrollTo({ top: 0, ...opts }); break;
            case "bottom": window.scrollTo({ top: document.documentElement.scrollHeight, ...opts }); break;
          }
        },
        args: [cmd.dir],
      });
    }
    case "find": {
      const t = await activeTab();
      if (!t) return null;
      return await chrome.scripting.executeScript({
        target: { tabId: t.id },
        func: () => {
          const el = document.querySelector(
            'input[type=search], input[name*=q i], input[aria-label*=search i]'
          );
          if (el) el.focus();
        },
      });
    }
    case "cancel":
      return null;
  }
  return null;
}

async function notify(text, cmd, result) {
  const t = await activeTab();
  if (!t?.id) return;
  chrome.tabs
    .sendMessage(t.id, { type: "voice-feedback", text, cmd, ok: !result?.error && !result?.unrecognized })
    .catch(() => {});
}
