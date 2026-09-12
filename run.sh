#!/bin/bash
# Start the AirPods head tracker.
#   ./run.sh                  live dashboard
#   ./run.sh --json           one JSON object per sample on stdout
#   ./run.sh --csv run.csv    record every sample to a CSV
#
# The tracker has to run from HeadTrack.app: macOS refuses motion data to a
# process launched straight from a terminal (it blames the terminal, which has
# no NSMotionUsageDescription, and kills us). Launching the bundle through
# LaunchServices makes the app itself the one asking, so the permission prompt
# appears and the data flows. Output is piped back here through a FIFO so the
# dashboard still shows up in your terminal.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CALLER="$PWD"
cd "$HERE"

PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
[ -x "$PY" ] || PY=$(command -v python3)

if [ ! -d .venv ]; then
    echo "  creating .venv ..."
    "$PY" -m venv .venv
    .venv/bin/pip -q install -r requirements.txt
fi
[ -d HeadTrack.app ] || ./build_app.sh

TMP=$(mktemp -d)
FIFO="$TMP/stream"
mkfifo "$FIFO"
APP_PID=""

cleanup() {
    [ -n "$APP_PID" ] && kill -INT "$APP_PID" 2>/dev/null || true
    sleep 0.3
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

open -n -a "$HERE/HeadTrack.app" --stdout "$FIFO" --stderr "$FIFO" \
     --args "$HERE/headtrack.py" --cwd "$CALLER" "$@"

for _ in $(seq 20); do
    APP_PID=$(pgrep -n -f "HeadTrack.app/Contents/MacOS/HeadTrack" || true)
    [ -n "$APP_PID" ] && break
    sleep 0.1
done

# LaunchServices can take several seconds to bring the bundle up the first time,
# and the FIFO stays silent until it does.
echo "  starting HeadTrack.app ..."
cat "$FIFO"
