#!/usr/bin/env python3
"""Read AirPods head-tracking motion on macOS and show/record it.

Uses CoreMotion's CMHeadphoneMotionManager (macOS 14+) via PyObjC. Works with
AirPods Pro / Pro 2 / Max / 3rd gen - anything that supports Spatial Audio.

Launch it with ./run.sh, not directly: macOS only releases motion data to a
process whose bundle declares NSMotionUsageDescription, and kills anything else
that asks.

    ./run.sh                 # live dashboard
    ./run.sh --json          # one JSON object per sample on stdout
    ./run.sh --csv run.csv   # record to CSV (also shows dashboard)
"""

import argparse
import csv
import glob
import json
import math
import os
import signal
import sys
import time

# The app bundle launches us through LaunchServices, which does not inherit the
# shell's environment - so find the virtualenv ourselves instead of leaning on
# PYTHONPATH.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _sp in glob.glob(os.path.join(_HERE, ".venv/lib/python3.*/site-packages")):
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

import CoreMotion
from Foundation import NSBundle, NSObject, NSOperationQueue, NSRunLoop, NSDate
import objc

AUTH = {0: "not determined", 1: "restricted", 2: "denied", 3: "authorized"}
SENSOR = {0: "default", 1: "left earbud", 2: "right earbud"}

DEG = 180.0 / math.pi


class Sample:
    """One motion frame, flattened into plain floats."""

    __slots__ = ("t", "yaw", "pitch", "roll", "quat", "rot", "acc", "grav", "where")

    def __init__(self, dm):
        att = dm.attitude()
        q = att.quaternion()
        r = dm.rotationRate()
        a = dm.userAcceleration()
        g = dm.gravity()
        self.t = dm.timestamp()
        self.yaw = att.yaw() * DEG
        self.pitch = att.pitch() * DEG
        self.roll = att.roll() * DEG
        self.quat = (q.w, q.x, q.y, q.z)
        self.rot = (r.x, r.y, r.z)
        self.acc = (a.x, a.y, a.z)
        self.grav = (g.x, g.y, g.z)
        loc = dm.sensorLocation() if dm.respondsToSelector_("sensorLocation") else 0
        self.where = SENSOR.get(loc, str(loc))

    def as_dict(self):
        return {
            "t": self.t,
            "yaw": self.yaw,
            "pitch": self.pitch,
            "roll": self.roll,
            "quat": {"w": self.quat[0], "x": self.quat[1], "y": self.quat[2], "z": self.quat[3]},
            "rotation_rate": dict(zip("xyz", self.rot)),
            "user_accel": dict(zip("xyz", self.acc)),
            "gravity": dict(zip("xyz", self.grav)),
            "sensor": self.where,
        }

    def as_row(self):
        return [f"{v:.6f}" if isinstance(v, float) else v for v in
                (self.t, self.yaw, self.pitch, self.roll, *self.quat,
                 *self.rot, *self.acc, *self.grav, self.where)]


CSV_HEADER = ["timestamp", "yaw_deg", "pitch_deg", "roll_deg",
              "qw", "qx", "qy", "qz",
              "rot_x", "rot_y", "rot_z",
              "acc_x", "acc_y", "acc_z",
              "grav_x", "grav_y", "grav_z", "sensor"]


def bar(value, span, width=41):
    """A centred gauge: value in [-span, span] -> a marker on a track."""
    half = width // 2
    pos = half + int(round(max(-1.0, min(1.0, value / span)) * half))
    track = ["-"] * width
    track[half] = "|"
    track[pos] = "@" if pos != half else "+"
    return "".join(track)


class Dashboard:
    """In-place terminal readout."""

    LINES = 14

    def __init__(self):
        self.started = False
        self.count = 0
        self.t0 = time.monotonic()
        self.last_draw = 0.0

    def update(self, s):
        self.count += 1
        now = time.monotonic()
        if now - self.last_draw < 1 / 30:   # cap redraws, sensor runs faster
            return
        self.last_draw = now
        # the first few samples arrive in a burst, so the rate is meaningless
        # until the window has some width to it
        elapsed = now - self.t0
        hz = f"{self.count / elapsed:5.1f} Hz" if elapsed > 0.5 else "  -- Hz"

        out = [
            f"  AirPods head tracking  -  {s.where:<12}  {hz}   {self.count} samples",
            "  " + "=" * 62,
            f"  yaw    {s.yaw:+8.2f} deg  {bar(s.yaw, 90)}",
            f"  pitch  {s.pitch:+8.2f} deg  {bar(s.pitch, 90)}",
            f"  roll   {s.roll:+8.2f} deg  {bar(s.roll, 90)}",
            "",
            "  quaternion      w {:+.4f}  x {:+.4f}  y {:+.4f}  z {:+.4f}".format(*s.quat),
            "  rotation rate   x {:+.4f}  y {:+.4f}  z {:+.4f}   rad/s".format(*s.rot),
            "  user accel      x {:+.4f}  y {:+.4f}  z {:+.4f}   g".format(*s.acc),
            "  gravity         x {:+.4f}  y {:+.4f}  z {:+.4f}   g".format(*s.grav),
            "",
            "  yaw = turn left/right   pitch = nod   roll = tilt",
            "  angles are relative to where your head pointed at startup",
            "  ctrl-c to stop",
        ]
        if self.started:
            sys.stdout.write(f"\033[{self.LINES}A")
        self.started = True
        sys.stdout.write("".join(f"\033[2K{line}\n" for line in out))
        sys.stdout.flush()


class Delegate(NSObject):
    """Connect/disconnect notices from the headphones."""

    def headphoneMotionManagerDidConnect_(self, mgr):
        print("  [headphones connected]", file=sys.stderr)

    def headphoneMotionManagerDidDisconnect_(self, mgr):
        print("  [headphones disconnected]", file=sys.stderr)


def explain_error(err):
    code = err.code()
    hint = {
        104: "motion access denied - allow it in System Settings > Privacy & Security > Motion & Fitness",
        105: "this process is not entitled to motion data",
        102: "head tracking not available on these headphones",
    }.get(code, err.localizedDescription())
    return f"CoreMotion error {code}: {hint}"


def main():
    ap = argparse.ArgumentParser(description="Stream AirPods head-tracking data.")
    ap.add_argument("--json", action="store_true",
                    help="print one JSON object per sample instead of the dashboard")
    ap.add_argument("--csv", metavar="FILE", help="also record every sample to FILE")
    ap.add_argument("--duration", type=float, metavar="SEC",
                    help="stop automatically after SEC seconds")
    ap.add_argument("--cwd", metavar="DIR", help=argparse.SUPPRESS)
    args = ap.parse_args()

    # LaunchServices starts us in "/", so run.sh hands us the caller's directory
    # to keep relative --csv paths pointing where the user expects.
    if args.cwd:
        os.chdir(args.cwd)

    if NSBundle.mainBundle().objectForInfoDictionaryKey_("NSMotionUsageDescription") is None:
        sys.exit("  Run this through ./run.sh .\n"
                 "  macOS kills any process that asks for motion data without an app bundle\n"
                 "  declaring NSMotionUsageDescription, so the tracker has to start from\n"
                 "  HeadTrack.app rather than straight from the interpreter.")

    mgr = CoreMotion.CMHeadphoneMotionManager.alloc().init()
    delegate = Delegate.alloc().init()
    mgr.setDelegate_(delegate)

    status = CoreMotion.CMHeadphoneMotionManager.authorizationStatus()
    if not args.json:
        print(f"  motion authorization: {AUTH.get(status, status)}")

    if not mgr.isDeviceMotionAvailable():
        sys.exit("  no head-tracking-capable headphones. Connect AirPods (Pro/Max/3rd gen)\n"
                 "  and make sure they are the active audio output, then try again.")

    writer = None
    csv_file = None
    if args.csv:
        csv_file = open(args.csv, "w", newline="")
        writer = csv.writer(csv_file)
        writer.writerow(CSV_HEADER)

    dash = None if args.json else Dashboard()
    state = {"fatal": None, "rows": 0}

    def handler(dm, err):
        if err is not None:
            state["fatal"] = explain_error(err)
            mgr.stopDeviceMotionUpdates()
            return
        if dm is None:
            return
        s = Sample(dm)
        if writer:
            writer.writerow(s.as_row())
            state["rows"] += 1
            if state["rows"] % 50 == 0:   # keep the file usable if we are killed
                csv_file.flush()
        if args.json:
            print(json.dumps(s.as_dict()), flush=True)
        else:
            dash.update(s)

    mgr.startDeviceMotionUpdatesToQueue_withHandler_(
        NSOperationQueue.mainQueue(), handler)

    if not args.json:
        print("  waiting for motion data... move your head\n")

    signal.signal(signal.SIGINT, signal.default_int_handler)
    loop = NSRunLoop.currentRunLoop()
    deadline = time.monotonic() + args.duration if args.duration else None
    try:
        while state["fatal"] is None:
            loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
            if deadline and time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        mgr.stopDeviceMotionUpdates()
        if csv_file:
            csv_file.close()
            print(f"\n  wrote {args.csv}")

    if state["fatal"]:
        sys.exit("\n  " + state["fatal"])
    if not args.json:
        print("  stopped.")


if __name__ == "__main__":
    main()
