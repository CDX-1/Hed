# Hed A11y — Chrome extension

Standalone browser accessibility helper. Two things:

1. **Dictation on focus** - focus any text field and a mic dot appears
   beside it; by default it starts transcribing straight into the field.
2. **"Hey hed" voice commands** - opt-in background listener that turns
   utterances like *hey hed, new tab* or *hey hed, shift two tabs right*
   into real browser actions.

It does **not** need the head-tracker, the Arduino board, `headtrack.py`,
the Python venv, or `HeadTrack.app` running or installed. It never opens a
socket to `localhost` and never imports anything else in this repo - the
only reason it lives in the same repo is that it fills the same
accessibility niche. Load the folder, close everything else, it works.

It depends only on the built-in `SpeechRecognition` (Chrome, Edge - any
Chromium 33+), so no server, no bundled model, no build step.

## Install

1. Open `chrome://extensions`
2. Enable **Developer mode** (top right)
3. **Load unpacked** and pick this `chrome_extension/` folder
4. Open the extension popup and click **Grant microphone access** once

That single grant covers everything the extension does. All speech
recognition runs in the extension's own offscreen document, so no site
ever needs to prompt for mic access on its own behalf, and pages that
disable mic via `Permissions-Policy` still work.

If you already had a tab open when you loaded the extension, reload the
tab so the content script attaches to it.

## Dictation

- Click into any `<input>`, `<textarea>`, or `contenteditable` field
- A mic dot appears next to it; if auto-start is on it turns red and starts
  transcribing straight away
- Speak - interim text floats below the field, final results are inserted at
  the caret with a real `input` event so React/Vue/etc pick them up
- **Ctrl+Shift+M** (**⌘+Shift+M** on macOS) toggles listening
- **Esc** stops listening without moving focus

## "Hey hed" voice commands

1. Make sure microphone access is granted (see Install).
2. Tick **Wake-word listener** in the popup (or hit **Ctrl/⌘+Shift+K**).
3. A small green dot appears top-right of the current tab while the
   background is listening. It turns amber the moment "hey hed" is heard,
   until either the command lands or 6 seconds elapse.

Say the wake phrase followed by the command, either in the same breath
(`hey hed new tab`) or as a separate utterance right after (`hey hed` … then
`new tab`). A toast confirms what fired.

Commands understood right now:

| you say | it does |
|---|---|
| `new tab` / `close tab` / `duplicate tab` | tab open / close / dupe |
| `pin tab` / `unpin tab` / `mute tab` / `unmute tab` | tab flags |
| `next tab` / `previous tab` / `first tab` | shift focus by one, or jump to first |
| `shift two tabs right` / `shift three left` | shift focus by N |
| `go to tab five` / `jump to tab 3` | focus tab N (1-indexed) |
| `move tab two right` / `move tab one left` | reorder the current tab |
| `reload` / `refresh` / `back` / `forward` | history controls |
| `scroll up` / `scroll down` / `page up` / `page down` | soft scroll |
| `top` / `bottom` / `top of page` | hard scroll |
| `new window` / `new incognito window` | window controls |
| `zoom in` / `zoom out` / `reset zoom` | page zoom |
| `search for cats` / `google cats` | new tab with a search |
| `open example.com` / `open weather` | tries a URL, falls back to search |
| `cancel` / `never mind` / `stop` | drop the pending command |

Numbers may be digits or words - `two`, `to`, `too`, `three`, etc. all
resolve to numbers, so speech-to-text quirks don't break commands.

Dictation and the wake listener coordinate: while a field is being
dictated into, the wake listener pauses so it can't misfire on your
dictation. It resumes as soon as you blur the field or press Esc.

## Options (extension popup)

- **Enabled** - master switch for dictation
- **Auto-start on focus** - off if you want to trigger with the mic dot or
  the hotkey only
- **Show interim text** - the live floating preview
- **Language** - passed straight to `SpeechRecognition.lang`
- **Wake-word listener** - the "hey hed" background listener
- **Grant microphone access** - one-shot permission prime for the wake
  listener; shows "granted" once it's set

Settings are stored via `chrome.storage.sync`, so they follow your profile.

## Notes

- Chromium's `SpeechRecognition` sends audio to Google's speech service.
  That is a browser choice, not something this extension controls. Firefox
  has no built-in equivalent yet, so the extension no-ops there.
- The wake listener runs in an MV3 **offscreen document** so it keeps
  listening across tab switches, page navigations, and while the popup is
  closed. Its microphone use shows up in the browser's mic indicator as
  long as it is on.
- The recognizer stops itself after a stretch of silence; the content
  script restarts it while the field is still focused, so long dictation
  sessions do not need re-triggering.
- Password inputs are included by design (some sites use `type=password`
  for API keys people want to dictate). Skip them by unchecking auto-start
  and using the hotkey deliberately if that feels wrong.
- Wake word matching accepts a few common mis-transcriptions ("hey head",
  "hey hedd", etc). Add more in `content.js`/`offscreen.js` if your voice
  keeps landing on something else.
