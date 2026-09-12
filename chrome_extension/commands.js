// Parse a natural-language utterance into a structured command.
// Shared between the recognizers (which strip the wake word) and anything
// else that wants to dispatch a command from text.
//
// Speech-to-text never hands us the phrase we documented. People say "could
// you open a new tab for me", recognizers write "close tap" and "shift to tabs
// write", and word order wanders. So instead of one exact regex per command,
// parsing is soft:
//
//   1. tokenize, fix soundalikes ("tap" -> "tab"), drop politeness at the edges
//   2. free-text commands (search, open) by their leading verb
//   3. fixed commands by the keywords present, in any order, with synonyms
//   4. failing that, correct near-miss spellings ("reloud") and try again
//
// Anything that could destroy work ("close other tabs") and has no matching
// action returns null rather than guessing.

// MARK: wake word

// "Hed" is not a dictionary word, so recognizers spell it every which way, and
// Apple's transcriber punctuates the greeting ("Hey, head,").
const HEY = "(?:hey|hay|hi|okay|ok)";
const NAME_AFTER_HEY = "(?:hed|head|hedd|hedge|heads|heddy|hedy|hedz|edd|ed|ted|had)";
// Without "hey" the name has to carry the whole wake on its own, so only
// spellings that are unlikely to open an ordinary sentence count.
const NAME_BARE = "(?:hed|head|hedd|heddy|hedy|hedz)";
const HEY_WAKE_RE = new RegExp(`\\b${HEY}[\\s,.!]+${NAME_AFTER_HEY}\\b[\\s,.:;!?-]*`, "i");
const BARE_WAKE_RE = new RegExp(`^[\\s,.!?-]*${NAME_BARE}\\b[\\s,.:;!?-]*`, "i");

/**
 * Find the wake word in a transcript. Returns null, or
 *   { rest, explicit }
 * where `rest` is whatever followed it and `explicit` says the wake was the
 * full "hey hed". A bare "hed" only counts at the very start of an utterance,
 * and callers should only act on it when `rest` parses as a real command -
 * "head" is an everyday word and must not trigger toasts mid-conversation.
 */
export function findWake(text) {
  if (!text) return null;
  let m = text.match(HEY_WAKE_RE);
  if (m) return { rest: clean(text.slice(m.index + m[0].length)), explicit: true };
  m = text.match(BARE_WAKE_RE);
  if (m) return { rest: clean(text.slice(m[0].length)), explicit: false };
  return null;
}

const clean = (s) => s.trim().replace(/[\s.,!?;:]+$/, "");

// MARK: key phrases

// "hed, enter" / "hed, command c" press a key at the OS level. The overlay
// (voicekeys.py) does the pressing; the extension has to recognize the same
// phrases and step aside, because "hed, down" would otherwise scroll the page
// on top of pressing the arrow key. Keep the vocabulary in sync with
// voicekeys.py (KEYS, MODIFIERS, LETTER_NAMES, DIGITS, COUNTS).
const KEY_MODIFIERS = new Set([
  "command", "cmd", "comand", "control", "ctrl", "option", "alt", "shift",
]);
const KEY_NAMES = new Set([
  "enter", "return", "tab", "space", "spacebar", "backspace", "delete",
  "escape", "esc", "up", "down", "left", "right", "home", "end",
  "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
]);
// Multi-word names, checked as suffixes after the modifiers are peeled off.
const KEY_PHRASES = [
  "space bar", "back space", "forward delete", "up arrow", "arrow up",
  "down arrow", "arrow down", "left arrow", "arrow left", "right arrow",
  "arrow right", "page up", "page down",
];
const LETTER_KEYS = new Set([
  "a", "ay", "b", "be", "bee", "c", "see", "sea", "d", "dee", "e", "ee",
  "f", "ef", "g", "gee", "h", "aitch", "i", "eye", "j", "jay", "k", "kay",
  "l", "el", "m", "em", "n", "en", "o", "oh", "p", "pee", "q", "queue", "cue",
  "r", "are", "s", "es", "t", "tee", "tea", "u", "you", "v", "vee", "w",
  "x", "ex", "y", "why", "z", "zee", "zed",
]);
const DIGIT_KEYS = new Set([
  "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
  "nine", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
]);
const COUNT_WORDS = new Set([
  "once", "twice", "thrice", "one", "two", "to", "too", "three", "four",
  "for", "five", "six", "seven", "eight", "nine", "ten",
]);
const KEY_LEADS = new Set(["press", "hit", "tap", "push", "type", "key"]);

/**
 * True if `rest` (the words after the wake) is a keypress request the overlay
 * will execute at the OS level. The extension must not run these as browser
 * commands - "hed, down" is an arrow key, not a page scroll.
 */
export function isKeyPhrase(rest) {
  if (!rest) return false;
  let words = rest.toLowerCase().replace(/[^\w\s]/g, " ").split(/\s+/).filter(Boolean);
  while (words.length && KEY_LEADS.has(words[0])) words.shift();
  if (words.length && words[words.length - 1] === "key") words.pop();

  // Trailing repeat count ("three times", "twice", "x2").
  if (words.length >= 2 && (words[words.length - 1] === "times" || words[words.length - 1] === "time")) {
    const n = words[words.length - 2];
    if (!/^\d+$/.test(n) && !COUNT_WORDS.has(n)) return false;
    words = words.slice(0, -2);
    if (words.length && words[words.length - 1] === "x") words.pop();
  } else if (words.length && (words[words.length - 1] === "once" ||
             words[words.length - 1] === "twice" || words[words.length - 1] === "thrice")) {
    words = words.slice(0, -1);
  }

  const mods = [];
  while (words.length && KEY_MODIFIERS.has(words[0])) {
    mods.push(words.shift());
    if (words.length && (words[0] === "plus" || words[0] === "and")) words.shift();
  }
  if (words.length && words[words.length - 1] === "key") words.pop();
  if (!words.length) return false;

  const name = words.join(" ");
  if (KEY_NAMES.has(name) || KEY_PHRASES.includes(name)) return true;
  // Letters and digits are only key requests when a modifier makes them one
  // ("command c"); "hed, a" alone is far more likely a misheard sentence.
  if (mods.length && (LETTER_KEYS.has(name) || DIGIT_KEYS.has(name))) return true;
  return false;
}

// MARK: mode phrases

// "hed, game mode" / "hed, casual mode" toggle the tracker's arrow-key mode;
// the overlay handles them. The extension steps aside. Mirror voicekeys.py's
// MODES/_MODE_LEAD/_MODE_GLUE - change one, change both.
const MODE_NAMES = new Set([
  "game mode", "gamer mode", "gaming mode", "gaming", "arrow mode", "arrows",
  "game", "casual mode", "casual", "normal mode", "regular mode", "chat mode",
  "chill mode", "normal",
]);
const MODE_LEADS = new Set([
  "switch", "go", "enter", "enable", "turn", "activate", "start",
]);
const MODE_GLUE = new Set(["into", "to", "in", "on", "off", "the", "a"]);

export function isModePhrase(rest) {
  if (!rest) return false;
  let words = rest.toLowerCase().replace(/[^\w\s]/g, " ").split(/\s+/).filter(Boolean);
  while (words.length && MODE_LEADS.has(words[0])) words.shift();
  while (words.length && MODE_GLUE.has(words[0])) words.shift();
  while (words.length && (words[words.length - 1] === "please" ||
         words[words.length - 1] === "now")) {
    words.pop();
  }
  return MODE_NAMES.has(words.join(" "));
}

// MARK: shortcut phrases

// OS-level clipboard/window macros. The overlay presses cmd+C / cmd+V /
// cmd+A+Backspace / etc.; the extension steps aside so "hed, copy" doesn't
// duplicate the current tab (parseCommand("copy") -> duplicateTab). Mirror
// voicekeys.py's SHORTCUTS - change one, change both.
const SHORTCUT_NAMES = new Set([
  "copy", "cut", "paste", "select all", "delete all", "clear all", "clear",
  "undo", "redo", "save", "save all", "print", "minimize",
  "bold", "italic", "italics", "underline",
]);
const SHORTCUT_LEADS = new Set(["please", "do"]);
const SHORTCUT_GLUE = new Set(["a", "an", "the"]);

export function isShortcutPhrase(rest) {
  if (!rest) return false;
  let words = rest.toLowerCase().replace(/[^\w\s]/g, " ").split(/\s+/).filter(Boolean);
  while (words.length && SHORTCUT_LEADS.has(words[0])) {
    words.shift();
    while (words.length && SHORTCUT_GLUE.has(words[0])) words.shift();
  }
  while (words.length && (words[words.length - 1] === "please" ||
         words[words.length - 1] === "now")) {
    words.pop();
  }
  return SHORTCUT_NAMES.has(words.join(" "));
}

// MARK: sensitivity phrases

// Mouse sensitivity presets ("hed, min" / "hed, max" / "hed, reset"). The
// overlay writes the choice to the state file; the tracker scales the cursor.
// Mirror voicekeys.py's SENSITIVITY.
const SENSITIVITY_NAMES = new Set([
  "min", "minimum", "slow", "slower", "slowest", "precise", "precision",
  "fine", "tiny", "small", "low",
  "max", "maximum", "fast", "faster", "fastest", "quick", "quicker",
  "big", "large", "high",
  "reset", "default", "normal speed", "medium", "middle", "regular",
  "reset mouse", "reset sensitivity", "reset speed",
]);
const SENS_FILLER = new Set([
  "set", "make", "mouse", "cursor", "sensitivity", "speed", "the", "a",
  "to", "at", "go", "please", "now",
]);

export function isSensitivityPhrase(rest) {
  if (!rest) return false;
  const words = rest.toLowerCase().replace(/[^\w\s]/g, " ").split(/\s+/).filter(Boolean);
  if (SENSITIVITY_NAMES.has(words.join(" "))) return true;
  const core = words.filter((w) => !SENS_FILLER.has(w));
  return SENSITIVITY_NAMES.has(core.join(" "));
}

// MARK: vocabulary

const NUMBER_WORDS = {
  one: 1, won: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7,
  eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, thirteen: 13,
  fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18,
  nineteen: 19, twenty: 20,
};

// Only numbers when they sit right before "tab(s)" - "to" and "for" are far
// more often just words.
const NUMBER_SOUNDALIKES = { a: 1, an: 1, to: 2, too: 2, tu: 2, for: 4, fore: 4, ate: 8 };

const ORDINALS = {
  first: 1, second: 2, third: 3, fourth: 4, fifth: 5, sixth: 6, seventh: 7,
  eighth: 8, ninth: 9, tenth: 10, eleventh: 11, twelfth: 12,
};
const LAST_WORDS = new Set(["last", "final", "rightmost", "end"]);

// Recognizer mishearings, mapped before any rule looks at the words.
const SOUNDALIKES = {
  tap: "tab", tabb: "tab", tob: "tab", tub: "tab", tad: "tab", dab: "tab",
  taps: "tabs", tubs: "tabs", tads: "tabs",
  write: "right", rite: "right", wright: "right",
  knew: "new", nu: "new",
  clothes: "close", cloths: "close", closed: "close", closing: "close",
  scrawl: "scroll", scroller: "scroll", scrolled: "scroll", scrolling: "scroll", scrool: "scroll",
  reloaded: "reload", refreshed: "refresh", reloading: "reload", refreshing: "refresh",
  foreword: "forward", forwards: "forward", ford: "forward",
  backwards: "backward",
  mewt: "mute", muted: "mute", unmuted: "unmute",
  pinned: "pin", unpinned: "unpin",
  duplicated: "duplicate", dupe: "duplicate",
  incognita: "incognito", cognito: "incognito",
  googled: "google", googol: "google",
  zoomed: "zoom", zooming: "zoom",
  windows: "window",
  nevermind: "nevermind",
};

// Politeness and hesitation that carries no meaning at either end.
const LEADING_FILLER = [
  ["can", "you"], ["could", "you"], ["would", "you"], ["will", "you"],
  ["i", "want", "to"], ["i", "wanna"], ["i", "would", "like", "to"], ["id", "like", "to"],
  ["i", "need", "to"], ["go", "ahead", "and"], ["lets"], ["let", "us"], ["try", "to"],
  ["please"], ["just"], ["now"], ["so"], ["and"], ["then"], ["okay"], ["ok"],
  ["um"], ["uh"], ["uhm"], ["er"], ["hmm"], ["hey"], ["yo"], ["quickly"],
];
const TRAILING_FILLER = [
  ["please"], ["for", "me"], ["thanks"], ["thank", "you"], ["now"], ["real", "quick"],
  ["quickly"], ["um"], ["uh"], ["okay"], ["ok"],
];

// Words the fixed rules can safely ignore anywhere.
const STOPWORDS = new Set([
  "the", "this", "that", "current", "my", "our", "it", "to", "of", "on", "in",
  "by", "one", "ones", "over", "please", "a", "an", "some", "bit", "little",
  "active", "open", "opened",
]);

const RIGHT = new Set(["right", "forward", "next", "ahead", "along"]);
const LEFT = new Set(["left", "back", "backward", "previous", "prev", "prior", "before"]);

// Every keyword the rules react to - the dictionary for spelling correction.
const VOCAB = [
  "tab", "tabs", "new", "another", "blank", "fresh", "empty", "create", "add",
  "close", "kill", "shut", "exit", "remove", "delete", "dismiss", "quit",
  "duplicate", "copy", "clone", "pin", "unpin", "mute", "unmute", "silence",
  "move", "drag", "push", "slide", "shift", "switch", "jump", "skip", "focus",
  "select", "right", "left", "forward", "backward", "back", "next", "previous",
  "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
  "ninth", "tenth", "last", "final", "window", "incognito", "private",
  "reload", "refresh", "hard", "force", "scroll", "down", "up", "page", "top",
  "bottom", "beginning", "zoom", "bigger", "smaller", "larger", "shrink",
  "enlarge", "reset", "normal", "default", "actual", "find", "search", "google",
  "cancel", "never", "mind", "abort", "stop", "forget", "ignore", "navigate",
  "visit", "number", ...Object.keys(NUMBER_WORDS),
];
const VOCAB_SET = new Set(VOCAB);

// MARK: text handling

function tokenize(raw) {
  return raw
    .toLowerCase()
    .replace(/[’']/g, "")
    .replace(/\s+dot\s+(?=[a-z])/g, ".")         // "youtube dot com"
    .replace(/[^\p{L}\p{N}\s./:-]/gu, " ")
    .replace(/(^|\s)[.:/-]+|[.:/-]+(?=\s|$)/g, " ") // stray punctuation, keep "a.com"
    .split(/\s+/)
    .filter(Boolean);
}

function startsWith(tokens, phrase, at = 0) {
  if (tokens.length - at < phrase.length) return false;
  return phrase.every((w, i) => tokens[at + i] === w);
}

function stripFiller(tokens) {
  let t = tokens;
  for (let changed = true; changed && t.length;) {
    changed = false;
    for (const p of LEADING_FILLER) {
      if (startsWith(t, p) && t.length > p.length) { t = t.slice(p.length); changed = true; break; }
    }
    for (const p of TRAILING_FILLER) {
      if (t.length > p.length && startsWith(t, p, t.length - p.length)) {
        t = t.slice(0, t.length - p.length); changed = true; break;
      }
    }
  }
  return t;
}

function editDistance(a, b) {
  if (Math.abs(a.length - b.length) > 2) return 3;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1,
        prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    prev = cur;
  }
  return prev[b.length];
}

/** Nearest keyword to a misheard word, if exactly one is close enough. */
function correct(word) {
  if (VOCAB_SET.has(word) || word.length < 4 || /\d/.test(word)) return word;
  const limit = word.length >= 7 ? 2 : 1;
  let best = null;
  let bestDist = limit + 1;
  let tie = false;
  for (const v of VOCAB) {
    const d = editDistance(word, v);
    if (d < bestDist) { best = v; bestDist = d; tie = false; }
    else if (d === bestDist && v !== best) tie = true;
  }
  return best && !tie ? best : word;
}

// MARK: parsing

/** Returns { action, ... } or null if nothing matched. */
export function parseCommand(raw) {
  if (!raw) return null;
  const tokens = stripFiller(tokenize(raw).map((w) => SOUNDALIKES[w] ?? w));
  if (!tokens.length) return null;

  const direct = parseSearch(tokens) ?? parseFixed(tokens) ?? parseOpen(tokens);
  if (direct) return direct;

  // Second chance with near-miss spellings pulled onto known keywords. Only
  // for the keyword rules: correcting a search query or a site name would
  // mangle it.
  const corrected = tokens.map(correct);
  if (corrected.some((w, i) => w !== tokens[i])) {
    return parseSearch(corrected, tokens) ?? parseFixed(corrected);
  }
  return null;
}

const SEARCH_PREFIXES = [
  ["search", "google", "for"], ["search", "the", "web", "for"], ["search", "web", "for"],
  ["search", "for"], ["google", "search", "for"], ["google", "search"], ["google", "for"],
  ["look", "up"], ["lookup"], ["search"], ["google"],
];

// `original` supplies the query words when `tokens` has been spell-corrected.
function parseSearch(tokens, original = tokens) {
  for (const p of SEARCH_PREFIXES) {
    if (!startsWith(tokens, p)) continue;
    const query = original.slice(p.length);
    // "search this page" is find, "search" alone is nothing.
    if (!query.length || /^(this |the |on )?page$/.test(query.join(" "))) return null;
    return { action: "search", query: query.join(" ") };
  }
  return null;
}

const OPEN_PREFIXES = [
  ["navigate", "to"], ["take", "me", "to"], ["bring", "me", "to"], ["go", "to"],
  ["open", "up"], ["pull", "up"], ["open"], ["visit"], ["launch"], ["load"],
];

function parseOpen(tokens) {
  for (const p of OPEN_PREFIXES) {
    if (!startsWith(tokens, p)) continue;
    const target = tokens.slice(p.length).filter((w, i) => i > 0 || !["the", "a"].includes(w));
    if (!target.length) return null;
    return { action: "open", target: target.join(" ") };
  }
  return null;
}

function findNumber(tokens) {
  for (const t of tokens) {
    if (/^\d+$/.test(t)) return Number(t);
    if (t in NUMBER_WORDS) return NUMBER_WORDS[t];
  }
  // "shift to tabs right" is two tabs, but "go to tab five" is not - so a
  // soundalike only counts before the plural (or "a tab", which is one).
  for (let i = 0; i < tokens.length - 1; i++) {
    const t = tokens[i];
    const next = tokens[i + 1];
    if (!(t in NUMBER_SOUNDALIKES)) continue;
    if (next === "tabs" || (next === "tab" && (t === "a" || t === "an"))) {
      return NUMBER_SOUNDALIKES[t];
    }
  }
  return null;
}

function findOrdinal(tokens) {
  for (const t of tokens) {
    if (t in ORDINALS) return ORDINALS[t];
    const m = t.match(/^(\d+)(st|nd|rd|th)$/);
    if (m) return Number(m[1]);
  }
  return null;
}

function parseFixed(tokens) {
  const phrase = tokens.join(" ");
  const has = (...words) => words.some((w) => tokens.includes(w));
  const core = tokens.filter((w) => !STOPWORDS.has(w));
  const only = (...words) => core.length > 0 && core.every((w) => words.includes(w));

  if (/^(cancel|never ?mind|abort|stop|forget (it|that|about it)|ignore( that| it)?|scratch that|no|nothing|oops|wait)$/.test(phrase)) {
    return { action: "cancel" };
  }

  const tabWord = has("tab", "tabs");

  // Nothing here closes or moves more than the current tab; bail out rather
  // than run the single-tab version of a bulk request.
  if (tabWord && has("all", "other", "others", "every", "everything")) return null;

  if (has("window")) {
    if (has("incognito", "private", "secret")) return { action: "newWindow", incognito: true };
    if (has("new", "another", "open", "create", "fresh")) return { action: "newWindow" };
    if (has("close", "kill", "shut", "exit", "quit")) return null;
  }
  if (has("incognito", "private")) return { action: "newWindow", incognito: true };

  if (tabWord || only("close", "kill", "duplicate", "copy", "clone", "pin", "unpin",
    "mute", "unmute", "silence", "un", "sound", "audio", "volume")) {
    const tab = parseTab(tokens, has, core);
    if (tab) return tab;
  }

  // "shift three left", "skip right" - focus moves without saying "tab".
  if (has("shift", "skip", "jump", "switch", "hop") && !has("page", "scroll", "up", "down")) {
    const right = tokens.some((w) => RIGHT.has(w));
    const left = tokens.some((w) => LEFT.has(w));
    if (right !== left) return { action: "shiftTab", delta: (findNumber(tokens) ?? 1) * (right ? 1 : -1) };
  }

  // Scroll before history: "scroll back up" is not "go back".
  const scroll = parseScroll(tokens, has, only);
  if (scroll) return scroll;

  if (has("reload", "refresh", "rerender")) {
    return has("hard", "force", "forced", "cache", "full")
      ? { action: "reload", bypassCache: true }
      : { action: "reload" };
  }

  if (only("back", "go", "page", "navigate", "previous", "last", "backward", "a") &&
      has("back", "backward") || /^(previous|last) page$/.test(phrase)) {
    return { action: "back" };
  }
  if (only("forward", "go", "page", "navigate", "next", "a") && has("forward") ||
      /^next page$/.test(phrase)) {
    return { action: "forward" };
  }

  const zoom = parseZoom(tokens, has);
  if (zoom) return zoom;

  if (/^(find|find on page|find in page|find text|search (on )?(this )?page)$/.test(phrase)) {
    return { action: "find" };
  }

  return null;
}

function parseTab(tokens, has, core) {
  if (has("reopen", "restore", "undo")) return null; // no action for these yet

  if (has("new", "another", "blank", "fresh", "empty", "create", "add")) {
    return { action: "newTab" };
  }
  if (has("duplicate", "copy", "clone")) return { action: "duplicateTab" };
  if (has("close", "kill", "shut", "exit", "remove", "delete", "dismiss", "quit")) {
    return { action: "closeTab" };
  }
  if (has("unpin") || (has("un") && has("pin"))) return { action: "pinTab", pinned: false };
  if (has("pin")) return { action: "pinTab", pinned: true };
  if (has("unmute", "unsilence") || (has("un") && has("mute"))) {
    return { action: "muteTab", muted: false };
  }
  if (has("mute", "silence", "quiet", "hush")) return { action: "muteTab", muted: true };

  const right = tokens.some((w) => RIGHT.has(w));
  const left = tokens.some((w) => LEFT.has(w));
  const dir = right && !left ? 1 : left && !right ? -1 : 0;
  const n = findNumber(tokens);
  const ordinal = findOrdinal(tokens);

  // Reordering the tab itself, as opposed to moving focus to another one.
  if (has("move", "drag", "push", "slide", "shove") && !has("focus") && dir) {
    return { action: "moveTab", delta: (n ?? 1) * dir };
  }

  if (ordinal != null) return { action: "gotoTab", index: ordinal - 1 };
  if (tokens.some((w) => LAST_WORDS.has(w)) && !dir) return { action: "gotoTab", index: -1 };

  if (dir) return { action: "shiftTab", delta: (n ?? 1) * dir };

  // "tab 3", "go to tab number five", "switch to 2nd tab"
  if (n != null) return { action: "gotoTab", index: n - 1 };

  if (core.length && core.every((w) => ["switch", "change", "tab", "tabs", "cycle"].includes(w)) &&
      core.some((w) => w === "switch" || w === "change" || w === "cycle")) {
    return { action: "shiftTab", delta: 1 };
  }
  return null;
}

function parseScroll(tokens, has, only) {
  const phrase = tokens.join(" ");
  const scrollish = ["scroll", "go", "move", "jump", "page", "way", "all", "the",
    "to", "of", "a", "bit", "little", "more", "lot", "lots", "further", "far",
    "back", "very", "please", "down", "up", "top", "bottom", "beginning", "start",
    "end", "some", "bunch", "whole", "screen", "page"];
  if (!tokens.every((w) => scrollish.includes(w))) return null;

  if (has("top", "beginning", "start")) return { action: "scroll", dir: "top" };
  if (has("bottom", "end")) return { action: "scroll", dir: "bottom" };
  // "all the way down" means the end of the page, not one screen.
  if (has("all") && has("way")) {
    if (has("down")) return { action: "scroll", dir: "bottom" };
    if (has("up")) return { action: "scroll", dir: "top" };
  }

  const big = has("page", "lot", "lots", "far", "bunch", "whole", "screen", "way");
  if (has("down")) return { action: "scroll", dir: big ? "pageDown" : "down" };
  if (has("up")) return { action: "scroll", dir: big ? "pageUp" : "up" };
  if (phrase === "scroll" || only("scroll", "more")) return { action: "scroll", dir: "down" };
  return null;
}

function parseZoom(tokens, has) {
  const makeBigger = has("bigger", "larger", "enlarge", "magnify");
  const makeSmaller = has("smaller", "shrink", "tinier");
  if (!has("zoom") && !makeBigger && !makeSmaller) return null;
  if (has("reset", "normal", "default", "actual", "original", "100")) {
    return { action: "zoom", reset: true };
  }
  if (makeBigger || (has("zoom") && has("in", "closer", "more"))) return { action: "zoom", delta: 0.1 };
  if (makeSmaller || (has("zoom") && has("out", "away", "less"))) return { action: "zoom", delta: -0.1 };
  return null;
}
