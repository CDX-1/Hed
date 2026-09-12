# Hed A11y — Chrome extension

Browser voice commands for Hed. Say **"hey hed"** or **"hed"** followed by a
command - *hed, new tab*, *hey hed, shift two tabs right* - and it happens.

The extension never listens on its own. Voice is switched on and off from the
**Hed island overlay** (its VOICE button), which runs the one on-device
recognizer on the Mac. While VOICE is on, the island types what you say into
whatever field has focus - in the browser as in any other app - and this
extension watches the same transcript for commands. When the island is not
running, or VOICE is off, nothing is listening.

## Install

1. From the Hed folder, once: `./speech_mac/install.sh` (macOS 26+). This
   builds `hed-speech` and registers it as the extension's native messaging
   helper.
2. `chrome://extensions` → **Developer mode** → **Load unpacked** → this
   `chrome_extension/` folder. (Already loaded? Hit reload.)
3. Start the island: `.venv/bin/python overlay.py`, or `./run.sh --overlay`.
4. Hover the island, click **VOICE**, and talk.

The popup shows whether the helper is connected, whether the island is
running, and whether it is listening - and what to do if not. A green dot at
the top right of the page means the island is listening; it turns amber right
after a bare wake word while it waits for the command.

## How the pieces fit

```
microphone ─▶ hed-speech --hub  (started by the island; the only recognizer)
                 │
                 ├─▶ island overlay   waves, captions, types into the focused app
                 └─▶ hed-speech       native messaging helper, relay only
                        └─▶ this extension   runs "hed …" commands
```

Every command the extension runs is announced back to the hub as "consumed",
so the island does not also type it out. Anything that starts with "hey hed"
is never typed.

## Voice commands

Both **"hey hed"** and a bare **"hed"** work. Because "head" is an ordinary
word, the bare form is stricter: it only counts at the start of an utterance,
and only when what follows is a real command - "head of sales said…" is typed
as dictation. "Hey hed" always gets a toast, even for something it didn't
understand.

Commands:

| you say | it does |
|---|---|
| `new tab` / `close tab` / `duplicate tab` | tab open / close / dupe |
| `pin tab` / `unpin tab` / `mute tab` / `unmute tab` | tab flags |
| `next tab` / `previous tab` / `first tab` / `last tab` | shift focus by one, or jump to an end |
| `shift two tabs right` / `shift three left` | shift focus by N |
| `go to tab five` / `jump to tab 3` / `third tab` | focus tab N (1-indexed) |
| `move tab two right` / `move tab one left` | reorder the current tab |
| `reload` / `refresh` / `hard refresh` / `back` / `forward` | history controls |
| `scroll up` / `scroll down` / `page up` / `page down` | soft scroll |
| `top` / `bottom` / `top of page` | hard scroll |
| `new window` / `new incognito window` | window controls |
| `zoom in` / `zoom out` / `reset zoom` | page zoom |
| `search for cats` / `google cats` / `look up cats` | new tab with a search |
| `open example.com` / `go to youtube dot com` | tries a URL, falls back to search |
| `cancel` / `never mind` / `scratch that` | drop the pending command |

These are examples, not exact phrases - parsing is forgiving
(`commands.js`):

- **Politeness and filler are ignored**: *could you open a new tab for me
  please*, *um, close this tab*.
- **Synonyms and any word order**: *open another tab*, *kill it*, *switch to
  the tab on the right*, *go two tabs to the left*, *make it bigger*, *go
  incognito*, *scroll all the way to the bottom*, *go back a page*.
- **Common mishearings**: *close tap*, *shift to tabs write* (two tabs
  right), *clothes tab*, *scrawl down*.
- **Near-miss spellings** of keywords are corrected (*reloud*, *scrol
  down*), but never inside a search query or a site name.
- Numbers may be digits, words, or ordinals (*2*, *two*, *second*, *2nd*).
- Anything bulk it can't do safely (*close all other tabs*) is ignored
  rather than run as the single-tab version.

