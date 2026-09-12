import { parseCommand } from "./commands.js";

const OFFSCREEN_URL = "offscreen.html";
const OFFSCREEN_REASONS = ["USER_MEDIA"];
const OFFSCREEN_JUSTIFICATION = "Runs the extension-owned speech recognizer for dictation and the \"hey hed\" wake word.";

let dictation = null; // { tabId, frameId, lang, interim }

async function hasOffscreen() {
  if (!chrome.runtime.getContexts) return false;
  const url = chrome.runtime.getURL(OFFSCREEN_URL);
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [url],
  });
  return contexts.length > 0;
}

async function ensureOffscreen() {
  if (await hasOffscreen()) return true;
  try {
    await chrome.offscreen.createDocument({
      url: OFFSCREEN_URL,
      reasons: OFFSCREEN_REASONS,
      justification: OFFSCREEN_JUSTIFICATION,
    });
    return true;
  } catch (e) {
    if (String(e).includes("Only a single offscreen")) return true;
    console.warn("[hed-a11y] offscreen create failed:", e);
    return false;
  }
}

async function closeOffscreenIfIdle() {
  const { wakeEnabled } = await chrome.storage.sync.get({ wakeEnabled: false });
  if (!wakeEnabled && !dictation && (await hasOffscreen())) {
    try { await chrome.offscreen.closeDocument(); } catch {}
  }
}

async function tellOffscreen(msg) {
  if (!(await hasOffscreen())) return;
  try { await chrome.runtime.sendMessage(msg); } catch {}
}

async function setWakeEnabled(enabled) {
  await chrome.storage.sync.set({ wakeEnabled: enabled });
  if (enabled) {
    const ok = await ensureOffscreen();
    if (!ok) return;
    await tellOffscreen({ type: "wake-config", enabled: true });
  } else {
    await tellOffscreen({ type: "wake-config", enabled: false });
    await closeOffscreenIfIdle();
  }
}

async function startDictation({ tabId, frameId, lang, interim }) {
  console.info("[hed-a11y/bg] startDictation tab=", tabId, "frame=", frameId, "lang=", lang);
  dictation = { tabId, frameId: frameId ?? 0, lang: lang || "en-US", interim: !!interim };
  const ok = await ensureOffscreen();
  if (!ok) {
    console.warn("[hed-a11y/bg] offscreen unavailable");
    forwardToDictationTab({ type: "dictation-error", error: "offscreen-unavailable" });
    dictation = null;
    return;
  }
  await tellOffscreen({
    type: "dictation-config",
    active: true,
    lang: dictation.lang,
    interim: dictation.interim,
  });
}

async function stopDictation() {
  console.info("[hed-a11y/bg] stopDictation");
  await tellOffscreen({ type: "dictation-config", active: false });
  if (dictation) {
    forwardToDictationTab({ type: "dictation-stopped" });
  }
  dictation = null;
  await closeOffscreenIfIdle();
}

function forwardToDictationTab(msg) {
  if (!dictation) return;
  chrome.tabs
    .sendMessage(dictation.tabId, msg, { frameId: dictation.frameId })
    .catch(() => {});
}

chrome.runtime.onStartup.addListener(async () => {
  const { wakeEnabled } = await chrome.storage.sync.get({ wakeEnabled: false });
  if (wakeEnabled) {
    await ensureOffscreen();
    await tellOffscreen({ type: "wake-config", enabled: true });
  }
});

chrome.runtime.onInstalled.addListener(async () => {
  const { wakeEnabled } = await chrome.storage.sync.get({ wakeEnabled: false });
  if (wakeEnabled) {
    await ensureOffscreen();
    await tellOffscreen({ type: "wake-config", enabled: true });
  }
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command === "toggle-listen") {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.id != null) {
      chrome.tabs.sendMessage(tab.id, { type: "toggle" }).catch(() => {});
    }
    return;
  }
  if (command === "toggle-wake") {
    const { wakeEnabled } = await chrome.storage.sync.get({ wakeEnabled: false });
    await setWakeEnabled(!wakeEnabled);
    return;
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (dictation?.tabId === tabId) {
    stopDictation();
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg?.type === "set-wake") {
      await setWakeEnabled(!!msg.enabled);
      sendResponse({ ok: true });
      return;
    }
    if (msg?.type === "dictation-start") {
      const tabId = sender.tab?.id;
      if (tabId == null) { sendResponse({ ok: false, error: "no-tab" }); return; }
      await startDictation({
        tabId,
        frameId: sender.frameId ?? 0,
        lang: msg.lang,
        interim: msg.interim,
      });
      sendResponse({ ok: true });
      return;
    }
    if (msg?.type === "dictation-stop") {
      await stopDictation();
      sendResponse({ ok: true });
      return;
    }
    if (
      msg?.type === "dictation-interim" ||
      msg?.type === "dictation-final" ||
      msg?.type === "dictation-error" ||
      msg?.type === "dictation-started"
    ) {
      forwardToDictationTab(msg);
      sendResponse({ ok: true });
      return;
    }
    if (msg?.type === "voice-command") {
      const cmd = parseCommand(msg.text);
      const result = cmd
        ? await execute(cmd).catch((e) => ({ error: String(e) }))
        : { unrecognized: true };
      await notify(msg.text, cmd, result);
      sendResponse({ cmd, result });
      return;
    }
    if (msg?.type === "wake-status") {
      await broadcastWakeStatus(msg);
      sendResponse({ ok: true });
      return;
    }
    if (msg?.type === "wake-fatal") {
      // Offscreen hit a permission/hardware error; disable wake and update storage.
      await chrome.storage.sync.set({ wakeEnabled: false });
      sendResponse({ ok: true });
      return;
    }
    if (msg?.type === "offscreen-hello") {
      // Offscreen just loaded and does not read storage itself. Push the
      // current wake state (dictation state, if active, is pushed by whoever
      // asked to start it).
      const { wakeEnabled, wakeLang } = await chrome.storage.sync.get({
        wakeEnabled: false, wakeLang: "en-US",
      });
      await tellOffscreen({ type: "wake-config", enabled: wakeEnabled, lang: wakeLang });
      sendResponse({ ok: true });
      return;
    }
  })();
  return true;
});

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

async function broadcastWakeStatus(msg) {
  const t = await activeTab();
  if (!t?.id) return;
  chrome.tabs.sendMessage(t.id, msg).catch(() => {});
}
