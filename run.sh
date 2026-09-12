#!/bin/bash
# Start the head tracker.
#   ./run.sh                  live dashboard (MPU-6050 over USB serial)
#   ./run.sh --3d             3D head in the browser
#   ./run.sh --json           one JSON object per sample on stdout
#   ./run.sh --csv run.csv    record every sample to a CSV
#   ./run.sh --airpods        the old AirPods/CoreMotion source
#
# The MPU path is an ordinary process reading a serial port, so it just runs.
# --airpods cannot: macOS refuses motion data to a process launched straight
# from a terminal (it blames the terminal, which has no NSMotionUsageDescription,
# and kills us). For that source we launch HeadTrack.app through LaunchServices
# so the app itself is the one asking, and pipe its output back here through a
# FIFO so the dashboard still shows up in your terminal.
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
# Keep an existing .venv from an older checkout up to date (pyserial is new).
.venv/bin/python -c "import serial" 2>/dev/null || \
    .venv/bin/pip -q install -r requirements.txt

AIRPODS=0
for arg in "$@"; do
    [ "$arg" = "--airpods" ] && AIRPODS=1
done

if [ "$AIRPODS" = 0 ]; then
    cd "$CALLER"
    exec "$HERE/.venv/bin/python" "$HERE/headtrack.py" "$@"
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
