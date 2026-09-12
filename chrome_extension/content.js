(() => {
  const state = {
    enabled: true,
    autoStart: true,
    lang: "en-US",
    interim: true,
    active: false,
  };

  let field = null;
  let indicator = null;
  let interimNode = null;
  let interimText = "";
  let errorNode = null;
  let errorTimer = 0;

  const EDITABLE_INPUT_TYPES = new Set([
    "text", "search", "email", "url", "tel", "password", "number",
  ]);

  const isEditable = (el) => {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.tagName === "TEXTAREA") return true;
    if (el.tagName === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      return EDITABLE_INPUT_TYPES.has(t);
    }
    return !!el.isContentEditable;
  };

  const loadState = () => {
    try {
      chrome.storage?.sync.get(
        { enabled: true, autoStart: true, lang: "en-US", interim: true },
        (v) => Object.assign(state, v),
      );
    } catch {}
  };

  chrome.storage?.onChanged?.addListener?.((changes) => {
    for (const k in changes) if (k in state) state[k] = changes[k].newValue;
    if (!state.enabled && state.active) stop();
  });

  chrome.runtime?.onMessage?.addListener?.((msg) => {
    if (!msg?.type) return;
    switch (msg.type) {
      case "toggle": toggle(); break;
      case "voice-feedback": showToast(msg); break;
      case "wake-status": showWakeStatus(msg); break;
      case "dictation-interim": showInterim(msg.text || ""); break;
      case "dictation-final":
        if (field && msg.text) insertFinal(field, msg.text);
        clearInterim();
        break;
      case "dictation-error":
        state.active = false;
        paintIndicator();
        clearInterim();
        showError(msg.error || "speech error");
        break;
      case "dictation-started":
        state.active = true;
        paintIndicator();
        break;
      case "dictation-stopped":
        state.active = false;
        paintIndicator();
        clearInterim();
        break;
    }
  });

  const sendBg = (msg) => {
    try {
      const p = chrome.runtime?.sendMessage?.(msg);
      if (p?.catch) p.catch(() => {});
    } catch {}
  };

  const buildIndicator = () => {
    if (indicator) return indicator;
    indicator = document.createElement("div");
    indicator.setAttribute("data-heda11y-indicator", "");
    indicator.setAttribute("aria-hidden", "true");
    indicator.style.cssText = [
      "position:absolute",
      "z-index:2147483647",
      "width:28px",
      "height:28px",
      "border-radius:50%",
      "background:#424242",
      "color:#fff",
      "font:16px/1 system-ui,-apple-system,sans-serif",
      "display:none",
      "align-items:center",
      "justify-content:center",
      "cursor:pointer",
      "box-shadow:0 2px 6px rgba(0,0,0,0.25)",
      "user-select:none",
      "transition:background 120ms ease",
      "pointer-events:auto",
    ].join(";");
    indicator.textContent = "\u{1F3A4}";
    indicator.title = "Dictate into this field (Ctrl/Cmd+Shift+M)";
    indicator.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggle();
    });
    document.documentElement.appendChild(indicator);
    return indicator;
  };

  const positionIndicator = () => {
    if (!field || !indicator) return;
    const r = field.getBoundingClientRect();
    indicator.style.top = `${r.top + window.scrollY + r.height / 2 - 14}px`;
    indicator.style.left = `${r.right + window.scrollX + 6}px`;
  };

  const paintIndicator = () => {
    if (!indicator) return;
    indicator.style.background = state.active ? "#e53935" : "#424242";
    indicator.style.boxShadow = state.active
      ? "0 0 0 4px rgba(229,57,53,0.25), 0 2px 6px rgba(0,0,0,0.25)"
      : "0 2px 6px rgba(0,0,0,0.25)";
  };

  const insertFinal = (el, text) => {
    if (!text) return;
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      const start = el.selectionStart ?? el.value.length;
      const end = el.selectionEnd ?? start;
      const before = el.value.slice(0, start);
      const after = el.value.slice(end);
      const setter = Object.getOwnPropertyDescriptor(
        el.tagName === "INPUT" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype,
        "value",
      )?.set;
      if (setter) setter.call(el, before + text + after);
      else el.value = before + text + after;
      const caret = start + text.length;
      try { el.setSelectionRange(caret, caret); } catch {}
      el.dispatchEvent(new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" }));
    } else if (el.isContentEditable) {
      el.focus();
      let inserted = false;
      try { inserted = document.execCommand("insertText", false, text); } catch {}
      if (!inserted) {
        const sel = window.getSelection();
        if (sel && sel.rangeCount) {
          const range = sel.getRangeAt(0);
          range.deleteContents();
          const node = document.createTextNode(text);
          range.insertNode(node);
          range.setStartAfter(node);
          range.setEndAfter(node);
          sel.removeAllRanges();
          sel.addRange(range);
        } else {
          el.appendChild(document.createTextNode(text));
        }
        el.dispatchEvent(new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" }));
      }
    }
  };

  const clearInterim = () => {
    if (!interimNode) return;
    if (interimNode.parentNode) interimNode.parentNode.removeChild(interimNode);
    interimNode = null;
    interimText = "";
  };

  const showInterim = (text) => {
    if (!field || !state.interim) return;
    if (text === interimText) return;
    interimText = text;
    if (!interimNode) {
      interimNode = document.createElement("div");
      interimNode.setAttribute("data-heda11y-interim", "");
      interimNode.style.cssText = [
        "position:absolute",
        "z-index:2147483646",
        "max-width:min(60ch,60vw)",
        "padding:6px 8px",
        "border-radius:6px",
        "background:rgba(0,0,0,0.75)",
        "color:#fff",
        "font:13px/1.3 system-ui,-apple-system,sans-serif",
        "pointer-events:none",
        "white-space:pre-wrap",
      ].join(";");
      document.documentElement.appendChild(interimNode);
    }
    interimNode.textContent = text;
    const r = field.getBoundingClientRect();
    interimNode.style.top = `${r.bottom + window.scrollY + 6}px`;
    interimNode.style.left = `${r.left + window.scrollX}px`;
  };

  const showError = (kind) => {
    const messages = {
      "not-allowed": "Microphone blocked. Click Grant microphone access in the Hed A11y popup.",
      "service-not-allowed": "Speech service blocked by the browser.",
      "audio-capture": "No microphone found.",
      "network": "Speech service unreachable — try again.",
      "no-mic-permission": "Microphone not granted for the extension. Click Grant microphone access in the popup.",
      "offscreen-unavailable": "Extension offscreen document unavailable in this build.",
      "no-speech-api": "This browser has no Web Speech API.",
    };
    const text = messages[kind] || `Speech error: ${kind}`;
    if (!errorNode) {
      errorNode = document.createElement("div");
      errorNode.setAttribute("data-heda11y-error", "");
      errorNode.style.cssText = [
        "position:absolute",
        "z-index:2147483647",
        "max-width:min(360px,60vw)",
        "padding:6px 10px",
        "border-radius:6px",
        "background:rgba(160,30,30,0.94)",
        "color:#fff",
        "font:12px/1.35 system-ui,-apple-system,sans-serif",
        "box-shadow:0 4px 14px rgba(0,0,0,0.3)",
        "pointer-events:none",
        "white-space:pre-wrap",
      ].join(";");
      document.documentElement.appendChild(errorNode);
    }
    errorNode.textContent = text;
    if (field) {
      const r = field.getBoundingClientRect();
      errorNode.style.top = `${r.bottom + window.scrollY + 6}px`;
      errorNode.style.left = `${r.left + window.scrollX}px`;
    } else {
      errorNode.style.top = "14px";
      errorNode.style.left = "14px";
    }
    clearTimeout(errorTimer);
    errorTimer = setTimeout(() => {
      errorNode?.remove();
      errorNode = null;
    }, 4500);
  };

  const start = () => {
    if (!state.enabled || !field || state.active) return;
    sendBg({
      type: "dictation-start",
      lang: state.lang,
      interim: state.interim,
    });
  };

  const stop = () => {
    if (!state.active) {
      clearInterim();
      paintIndicator();
      return;
    }
    state.active = false;
    paintIndicator();
    clearInterim();
    sendBg({ type: "dictation-stop" });
  };

  const toggle = () => {
    if (!field) {
      const a = document.activeElement;
      if (isEditable(a)) attach(a);
      else return;
    }
    if (state.active) stop();
    else start();
  };

  const attach = (el) => {
    field = el;
    buildIndicator();
    indicator.style.display = "flex";
    positionIndicator();
    paintIndicator();
    if (state.autoStart) start();
  };

  const detach = () => {
    stop();
    field = null;
    if (indicator) indicator.style.display = "none";
    clearInterim();
  };

  document.addEventListener("focusin", (e) => {
    if (!state.enabled) return;
    if (isEditable(e.target)) attach(e.target);
  }, true);

  document.addEventListener("focusout", (e) => {
    if (e.target === field) {
      queueMicrotask(() => {
        if (!isEditable(document.activeElement)) detach();
      });
    }
  }, true);

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "M" || e.key === "m")) {
      e.preventDefault();
      toggle();
    } else if (e.key === "Escape" && state.active) {
      stop();
    }
  }, true);

  const reflow = () => {
    if (field && indicator?.style.display === "flex") positionIndicator();
    if (field && interimNode) {
      const r = field.getBoundingClientRect();
      interimNode.style.top = `${r.bottom + window.scrollY + 6}px`;
      interimNode.style.left = `${r.left + window.scrollX}px`;
    }
    if (field && errorNode) {
      const r = field.getBoundingClientRect();
      errorNode.style.top = `${r.bottom + window.scrollY + 6}px`;
      errorNode.style.left = `${r.left + window.scrollX}px`;
    }
  };
  window.addEventListener("scroll", reflow, true);
  window.addEventListener("resize", reflow);

  // --- Voice command feedback UI (top-right toast / listening dot) -----------
  let toastEl = null;
  let toastTimer = 0;
  let wakeDot = null;

  const buildToast = () => {
    if (toastEl) return toastEl;
    toastEl = document.createElement("div");
    toastEl.setAttribute("data-heda11y-toast", "");
    toastEl.style.cssText = [
      "position:fixed",
      "top:14px",
      "right:14px",
      "z-index:2147483647",
      "max-width:min(340px,60vw)",
      "padding:8px 12px",
      "border-radius:8px",
      "background:rgba(30,30,30,0.92)",
      "color:#fff",
      "font:13px/1.35 system-ui,-apple-system,sans-serif",
      "box-shadow:0 4px 16px rgba(0,0,0,0.35)",
      "opacity:0",
      "transform:translateY(-6px)",
      "transition:opacity 160ms ease, transform 160ms ease",
      "pointer-events:none",
      "white-space:pre-wrap",
    ].join(";");
    document.documentElement.appendChild(toastEl);
    return toastEl;
  };

  const showToast = ({ text, cmd, ok }) => {
    buildToast();
    const label = cmd
      ? `✓ ${describe(cmd)}`
      : `– "${text}" (didn't match a command)`;
    toastEl.textContent = label;
    toastEl.style.background = ok
      ? "rgba(30,90,50,0.94)"
      : "rgba(90,30,30,0.94)";
    toastEl.style.opacity = "1";
    toastEl.style.transform = "translateY(0)";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toastEl.style.opacity = "0";
      toastEl.style.transform = "translateY(-6px)";
    }, ok ? 1600 : 2800);
  };

  const describe = (cmd) => {
    switch (cmd.action) {
      case "newTab": return "new tab";
      case "closeTab": return "closed tab";
      case "duplicateTab": return "duplicated tab";
      case "pinTab": return cmd.pinned ? "pinned tab" : "unpinned tab";
      case "muteTab": return cmd.muted ? "muted tab" : "unmuted tab";
      case "shiftTab": return `shifted ${Math.abs(cmd.delta)} ${cmd.delta > 0 ? "right" : "left"}`;
      case "moveTab": return `moved tab ${Math.abs(cmd.delta)} ${cmd.delta > 0 ? "right" : "left"}`;
      case "gotoTab": return `tab ${cmd.index < 0 ? "last" : cmd.index + 1}`;
      case "reload": return "reloaded";
      case "back": return "back";
      case "forward": return "forward";
      case "scroll": return `scrolled ${cmd.dir}`;
      case "newWindow": return cmd.incognito ? "new incognito window" : "new window";
      case "zoom": return cmd.reset ? "zoom reset" : cmd.delta > 0 ? "zoomed in" : "zoomed out";
      case "search": return `search: ${cmd.query}`;
      case "open": return `open: ${cmd.target}`;
      case "find": return "find on page";
      case "cancel": return "cancelled";
      default: return cmd.action;
    }
  };

  const buildWakeDot = () => {
    if (wakeDot) return wakeDot;
    wakeDot = document.createElement("div");
    wakeDot.setAttribute("data-heda11y-wake", "");
    wakeDot.style.cssText = [
      "position:fixed",
      "top:14px",
      "right:14px",
      "z-index:2147483646",
      "width:10px",
      "height:10px",
      "border-radius:50%",
      "background:transparent",
      "box-shadow:0 0 0 0 rgba(76,175,80,0)",
      "transition:background 160ms ease, box-shadow 160ms ease",
      "pointer-events:none",
    ].join(";");
    document.documentElement.appendChild(wakeDot);
    return wakeDot;
  };

  const showWakeStatus = ({ status, awaiting }) => {
    buildWakeDot();
    if (status === "listening" || status === "dictating") {
      wakeDot.style.background = awaiting ? "#ffa726" : "#4caf50";
      wakeDot.style.boxShadow = awaiting
        ? "0 0 0 4px rgba(255,167,38,0.22)"
        : "0 0 0 3px rgba(76,175,80,0.18)";
    } else if (status === "awaiting-command" || status === "dispatching") {
      wakeDot.style.background = "#ffa726";
      wakeDot.style.boxShadow = "0 0 0 5px rgba(255,167,38,0.28)";
    } else if (status === "permission-denied") {
      wakeDot.style.background = "#e53935";
      wakeDot.style.boxShadow = "0 0 0 3px rgba(229,57,53,0.22)";
    } else {
      wakeDot.style.background = "transparent";
      wakeDot.style.boxShadow = "0 0 0 0 rgba(0,0,0,0)";
    }
  };

  loadState();
})();
