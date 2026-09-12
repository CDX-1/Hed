// Voice command feedback. The page never listens or dictates itself: voice is
// switched on from the Hed island overlay, which also types into fields. This
// only shows what a spoken command did, and a dot while the island is hearing.
(() => {
  chrome.runtime?.onMessage?.addListener?.((msg) => {
    if (msg?.type === "voice-feedback") showToast(msg);
    else if (msg?.type === "speech-status") showStatus(msg);
  });

  let toastEl = null;
  let toastTimer = 0;
  let dot = null;

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

  const buildDot = () => {
    if (dot) return dot;
    dot = document.createElement("div");
    dot.setAttribute("data-heda11y-listening", "");
    dot.style.cssText = [
      "position:fixed",
      "top:14px",
      "right:14px",
      "z-index:2147483646",
      "width:10px",
      "height:10px",
      "border-radius:50%",
      "background:transparent",
      "transition:background 160ms ease, box-shadow 160ms ease",
      "pointer-events:none",
    ].join(";");
    document.documentElement.appendChild(dot);
    return dot;
  };

  // Green while the island is listening, amber right after the wake word.
  const showStatus = ({ listening, awaiting }) => {
    if (!listening && !dot) return;
    buildDot();
    if (awaiting) {
      dot.style.background = "#ffa726";
      dot.style.boxShadow = "0 0 0 5px rgba(255,167,38,0.28)";
    } else if (listening) {
      dot.style.background = "#4caf50";
      dot.style.boxShadow = "0 0 0 3px rgba(76,175,80,0.18)";
    } else {
      dot.style.background = "transparent";
      dot.style.boxShadow = "none";
    }
  };
})();
