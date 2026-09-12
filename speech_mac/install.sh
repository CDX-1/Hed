#!/bin/bash
# Build hed-speech and register it as a Chrome native messaging host, so the
# Hed A11y extension uses Apple's on-device SpeechAnalyzer instead of Web Speech.
#
#   ./speech_mac/install.sh               extension loaded unpacked from ../chrome_extension
#   ./speech_mac/install.sh <extension-id> any other install (ID from chrome://extensions)
#   ./speech_mac/install.sh --uninstall
#
# Needs macOS 26+ and the Xcode command line tools. Reload the extension
# afterwards (chrome://extensions -> the circular arrow).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="com.hed.speech"
BIN="$HERE/build/hed-speech"

BROWSER_DIRS=(
    "Google/Chrome"
    "Google/Chrome Beta"
    "Google/Chrome Canary"
    "Chromium"
    "Microsoft Edge"
    "BraveSoftware/Brave-Browser"
    "Arc/User Data"
    "Dia/User Data"
)

if [ "${1:-}" = "--uninstall" ]; then
    for d in "${BROWSER_DIRS[@]}"; do
        rm -f "$HOME/Library/Application Support/$d/NativeMessagingHosts/$NAME.json"
    done
    rm -rf "$HERE/build"
    echo "  removed $NAME"
    exit 0
fi

if [ -n "${1:-}" ]; then
    EXT_ID="$1"
else
    # An unpacked extension's ID is derived from its folder path: the first 32
    # hex digits of SHA-256(path), each mapped 0-f -> a-p.
    EXT_ID="$(/usr/bin/python3 - "$HERE/../chrome_extension" <<'PY'
import hashlib, os, sys
h = hashlib.sha256(os.path.realpath(sys.argv[1]).encode()).hexdigest()[:32]
print("".join(chr(ord("a") + int(c, 16)) for c in h))
PY
)"
fi

echo "  building hed-speech ..."
mkdir -p "$HERE/build"
xcrun swiftc -O -swift-version 5 "$HERE/HedSpeech.swift" -o "$BIN"
codesign --force --sign - "$BIN" >/dev/null 2>&1 || true

MANIFEST="$(cat <<JSON
{
  "name": "$NAME",
  "description": "Hed A11y on-device speech (Apple SpeechAnalyzer)",
  "path": "$BIN",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
JSON
)"

installed=0
for d in "${BROWSER_DIRS[@]}"; do
    base="$HOME/Library/Application Support/$d"
    [ -d "$base" ] || continue
    mkdir -p "$base/NativeMessagingHosts"
    printf '%s\n' "$MANIFEST" > "$base/NativeMessagingHosts/$NAME.json"
    echo "  registered for $d"
    installed=1
done
[ "$installed" = 1 ] || echo "  no Chromium browser profile found; nothing registered"

echo "  extension id: $EXT_ID"
echo "  done - reload the extension in chrome://extensions"
