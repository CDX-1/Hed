// Parse a natural-language utterance into a structured command.
// Shared between the offscreen recognizer (which strips the wake word) and
// anything else that wants to dispatch a command from text.

const NUMBER_WORDS = {
  a: 1, an: 1, one: 1, two: 2, to: 2, too: 2, three: 3, four: 4, for: 4,
  five: 5, six: 6, seven: 7, eight: 8, ate: 8, nine: 9, ten: 10, eleven: 11,
  twelve: 12,
};

const ORDINAL_WORDS = {
  first: 1, second: 2, third: 3, fourth: 4, fifth: 5, sixth: 6, seventh: 7,
  eighth: 8, ninth: 9, tenth: 10, last: -1,
};

function parseNum(token) {
  if (token == null) return NaN;
  const n = Number(token);
  if (Number.isFinite(n)) return n;
  return NUMBER_WORDS[token.toLowerCase()] ?? NaN;
}

function normalize(text) {
  return text
    .toLowerCase()
    .replace(/["']/g, "")
    .replace(/[!?;:]/g, "")
    .replace(/[.,](\s|$)/g, "$1")
    .replace(/\bto the\b/g, "")
    .replace(/\bthe\b/g, "")
    .replace(/\bplease\b/g, "")
    .replace(/\bcan you\b/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

// Returns { action, ... } or null if nothing matched.
export function parseCommand(raw) {
  if (!raw) return null;
  const text = normalize(raw);
  if (!text) return null;

  if (/^(cancel|never mind|nevermind|abort|stop|forget it)$/.test(text)) {
    return { action: "cancel" };
  }

  if (/^new tab$/.test(text)) return { action: "newTab" };
  if (/^(close|kill) (this |that |current )?tab$/.test(text)) return { action: "closeTab" };
  if (/^duplicate (this |that |current )?tab$/.test(text)) return { action: "duplicateTab" };
  if (/^pin (this |that |current )?tab$/.test(text)) return { action: "pinTab", pinned: true };
  if (/^unpin (this |that |current )?tab$/.test(text)) return { action: "pinTab", pinned: false };
  if (/^mute (this |that |current )?tab$/.test(text)) return { action: "muteTab", muted: true };
  if (/^unmute (this |that |current )?tab$/.test(text)) return { action: "muteTab", muted: false };

  if (/^next tab$/.test(text)) return { action: "shiftTab", delta: 1 };
  if (/^(previous|prev|last) tab$/.test(text)) return { action: "shiftTab", delta: -1 };
  if (/^first tab$/.test(text)) return { action: "gotoTab", index: 0 };

  let m;
  if ((m = text.match(/^(?:shift|move focus|jump|switch) (\S+) tabs? (right|forward|left|back|backward)$/))) {
    const n = parseNum(m[1]);
    if (!Number.isFinite(n)) return null;
    const sign = /^r|^f/.test(m[2]) ? 1 : -1;
    return { action: "shiftTab", delta: n * sign };
  }
  if ((m = text.match(/^(?:shift|jump) (right|forward|left|back|backward) (\S+) tabs?$/))) {
    const n = parseNum(m[2]);
    if (!Number.isFinite(n)) return null;
    const sign = /^r|^f/.test(m[1]) ? 1 : -1;
    return { action: "shiftTab", delta: n * sign };
  }
  if ((m = text.match(/^move (?:this |that |current )?tab (\S+) (right|forward|left|back|backward)$/))) {
    const n = parseNum(m[1]);
    if (!Number.isFinite(n)) return null;
    const sign = /^r|^f/.test(m[2]) ? 1 : -1;
    return { action: "moveTab", delta: n * sign };
  }
  if ((m = text.match(/^(?:go to|switch to|open|jump to|focus) tab (\S+)$/))) {
    if (m[1] === "last") return { action: "gotoTab", index: -1 };
    const n = parseNum(m[1]) ?? ORDINAL_WORDS[m[1]];
    if (!Number.isFinite(n)) return null;
    return { action: "gotoTab", index: n > 0 ? n - 1 : n };
  }

  if (/^(reload|refresh)( page)?$/.test(text)) return { action: "reload" };
  if (/^(hard )?(reload|refresh) hard$/.test(text)) return { action: "reload", bypassCache: true };
  if (/^(go )?back$/.test(text)) return { action: "back" };
  if (/^(go )?forward$/.test(text)) return { action: "forward" };

  if (/^(scroll )?down$/.test(text)) return { action: "scroll", dir: "down" };
  if (/^(scroll )?up$/.test(text)) return { action: "scroll", dir: "up" };
  if (/^page down$/.test(text)) return { action: "scroll", dir: "pageDown" };
  if (/^page up$/.test(text)) return { action: "scroll", dir: "pageUp" };
  if ((m = text.match(/^(?:scroll to |go to )?(top|bottom)( of page)?$/))) {
    return { action: "scroll", dir: m[1] };
  }

  if (/^new window$/.test(text)) return { action: "newWindow" };
  if (/^new incognito( window)?$/.test(text)) return { action: "newWindow", incognito: true };

  if (/^zoom in$/.test(text)) return { action: "zoom", delta: 0.1 };
  if (/^zoom out$/.test(text)) return { action: "zoom", delta: -0.1 };
  if (/^(reset zoom|zoom reset|actual size)$/.test(text)) return { action: "zoom", reset: true };

  if (/^find( on page)?$/.test(text)) return { action: "find" };

  if ((m = text.match(/^search (?:for )?(.+)$/))) {
    return { action: "search", query: m[1] };
  }
  if ((m = text.match(/^google (?:for )?(.+)$/))) {
    return { action: "search", query: m[1] };
  }
  if ((m = text.match(/^open (.+)$/))) {
    return { action: "open", target: m[1] };
  }

  return null;
}
