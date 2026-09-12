#!/bin/bash
# Wraps the tracker in a minimal .app bundle.
#
# macOS only hands out motion data to a process whose bundle declares
# NSMotionUsageDescription - that string is what the permission prompt shows.
# A bare `python headtrack.py` has no bundle, so the request can be denied
# without ever prompting. Running the python binary that lives *inside* this
# bundle makes CoreMotion see the Info.plist, while stdout stays in your
# terminal so the dashboard still works.
set -euo pipefail
cd "$(dirname "$0")"

APP="HeadTrack.app"
# Framework builds ship a stub at bin/python3.x that re-execs into
# Resources/Python.app - copying that stub would make NSBundle resolve to
# Python.app instead of ours, so reach for the real interpreter binary.
PYBIN="$(.venv/bin/python - <<'PY'
import os, sys
base = os.path.realpath(sys._base_executable)
inner = os.path.join(sys.base_prefix, "Resources/Python.app/Contents/MacOS/Python")
print(inner if os.path.exists(inner) else base)
PY
)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>HeadTrack</string>
    <key>CFBundleDisplayName</key>       <string>HeadTrack</string>
    <key>CFBundleExecutable</key>        <string>HeadTrack</string>
    <key>CFBundleIdentifier</key>        <string>local.headtrack</string>
    <key>CFBundleVersion</key>           <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>LSBackgroundOnly</key>          <true/>
    <key>NSMotionUsageDescription</key>
    <string>HeadTrack reads head orientation from your AirPods so you can see the motion data.</string>
    <key>NSBluetoothAlwaysUsageDescription</key>
    <string>HeadTrack talks to your AirPods over Bluetooth to read head orientation.</string>
</dict>
</plist>
PLIST

cp "$PYBIN" "$APP/Contents/MacOS/HeadTrack"
codesign --force --sign - "$APP" >/dev/null 2>&1 || echo "  (ad-hoc signing failed; continuing)"
echo "  built $APP  (python: $PYBIN)"
