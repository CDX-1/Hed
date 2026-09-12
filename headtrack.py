#!/usr/bin/env python3
"""Read head-tracking motion and show/record it.

The motion comes from an MPU-6050 on an ESP32 over USB serial (see
firmware/mpu6050_head, and mpu.py for the fusion). The AirPods source this
started as is still here behind --airpods: CoreMotion's
CMHeadphoneMotionManager, macOS 14+, via PyObjC.

    ./run.sh                 # live dashboard
    ./run.sh --3d            # 3D head in the browser
    ./run.sh --json          # one JSON object per sample on stdout
    ./run.sh --csv run.csv   # record to CSV (also shows dashboard)
    ./run.sh --airpods       # the old CoreMotion source
"""

import argparse
import csv
import glob
import json
import math
import os
import signal
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The app bundle launches us through LaunchServices, which does not inherit the
# shell's environment - so find the virtualenv ourselves instead of leaning on
# PYTHONPATH.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _sp in glob.glob(os.path.join(_HERE, ".venv/lib/python3.*/site-packages")):
    if _sp not in sys.path:
        sys.path.insert(0, _sp)
sys.path.insert(0, _HERE)

import mpu

AUTH = {0: "not determined", 1: "restricted", 2: "denied", 3: "authorized"}

# AirPods yaw has no absolute reference: it is measured from whatever direction
# you faced at startup. CoreMotion does carry a private entry point that takes a
# CMAttitudeReferenceFrame (magnetic north would remove the drift), but calling
# it from a client process trips a dispatch_assert_queue check inside CoreMotion
# and traps the process - from the main queue too - so the public API is the
# only option. The MPU-6050 has the same gap for a plainer reason: no
# magnetometer on the part at all.
SENSOR = {0: "default", 1: "left earbud", 2: "right earbud"}

DEG = 180.0 / math.pi


class Sample:
    """One motion frame, flattened into plain floats."""

    __slots__ = ("t", "yaw", "pitch", "roll", "quat", "rot", "acc", "grav", "where")

    def __init__(self, t, yaw, pitch, roll, quat, rot, acc, grav, where):
        self.t = t
        self.yaw = yaw
        self.pitch = pitch
        self.roll = roll
        self.quat = quat
        self.rot = rot
        self.acc = acc
        self.grav = grav
        self.where = where

    @classmethod
    def from_device_motion(cls, dm):
        """From a CoreMotion CMDeviceMotion (the --airpods source)."""
        att = dm.attitude()
        q = att.quaternion()
        r = dm.rotationRate()
        a = dm.userAcceleration()
        g = dm.gravity()
        loc = dm.sensorLocation() if dm.respondsToSelector_("sensorLocation") else 0
        return cls(t=dm.timestamp(),
                   yaw=att.yaw() * DEG, pitch=att.pitch() * DEG, roll=att.roll() * DEG,
                   quat=(q.w, q.x, q.y, q.z),
                   rot=(r.x, r.y, r.z),
                   acc=(a.x, a.y, a.z),
                   grav=(g.x, g.y, g.z),
                   where=SENSOR.get(loc, str(loc)))

    @classmethod
    def from_fused(cls, f):
        """From mpu.stream(). Accelerometer minus gravity, to match CoreMotion."""
        return cls(t=f.t, yaw=f.yaw, pitch=f.pitch, roll=f.roll, quat=f.quat,
                   rot=f.rot,
                   acc=tuple(a - g for a, g in zip(f.acc, f.grav)),
                   grav=f.grav,
                   where=f.part)

    @classmethod
    def synthetic(cls, t):
        """A canned head-turn, for checking the plumbing without a sensor."""
        yaw = 65 * math.sin(t * 0.9)
        pitch = 22 * math.sin(t * 0.6 + 1)
        roll = 16 * math.sin(t * 1.3 + 2)
        cy, sy = math.cos(yaw * 0.5 / DEG), math.sin(yaw * 0.5 / DEG)
        cp, sp = math.cos(pitch * 0.5 / DEG), math.sin(pitch * 0.5 / DEG)
        cr, sr = math.cos(roll * 0.5 / DEG), math.sin(roll * 0.5 / DEG)
        return cls(t=t, yaw=yaw, pitch=pitch, roll=roll,
                   quat=(cr*cp*cy + sr*sp*sy, sr*cp*cy - cr*sp*sy,
                         cr*sp*cy + sr*cp*sy, cr*cp*sy - sr*sp*cy),
                   rot=(0.0, 0.0, 0.0), acc=(0.0, 0.0, 0.0), grav=(0.0, 0.0, 1.0),
                   where="demo")

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

    def __init__(self, note=""):
        self.note = note
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
            f"  head tracking  -  {s.where:<12}  {hz}   {self.count} samples",
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
            "  " + self.note,
            "  ctrl-c to stop",
        ]
        if self.started:
            sys.stdout.write(f"\033[{self.LINES}A")
        self.started = True
        sys.stdout.write("".join(f"\033[2K{line}\n" for line in out))
        sys.stdout.flush()


PAGES = {"/": "viz.html", "/index.html": "viz.html",
         "/cube": "cube.html", "/cube.html": "cube.html"}


def serve(port, latest):
    """Serve the viewer pages and push each sample to them over SSE."""
    pages = {route: open(os.path.join(_HERE, f), "rb").read()
             for route, f in PAGES.items()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                sent = -1
                try:
                    while True:
                        s = latest.get("sample")
                        n = latest.get("n", 0)
                        payload = s.as_dict() if s else {"waiting": True}
                        if s is None or n != sent:
                            self.wfile.write(
                                b"data: " + json.dumps(payload).encode() + b"\n\n")
                            self.wfile.flush()
                            sent = n
                        time.sleep(1 / 60)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            if self.path in pages:
                body, ctype = pages[self.path], "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # A browser closing a tab resets the stream socket; that is normal
            # here and should not spray a traceback over the dashboard.
            if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
                super().handle_error(request, client_address)

    srv = Server(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def explain_error(err):
    code = err.code()
    hint = {
        104: "motion access denied - allow it in System Settings > Privacy & Security > Motion & Fitness",
        105: "this process is not entitled to motion data",
        102: "head tracking not available on these headphones",
    }.get(code, err.localizedDescription())
    return f"CoreMotion error {code}: {hint}"


def run_airpods(args, emit, state):
    """The original CoreMotion source. Owns the main thread until it stops."""
    import CoreMotion
    from Foundation import (NSBundle, NSObject, NSOperationQueue, NSProcessInfo,
                            NSRunLoop, NSDate)

    class Delegate(NSObject):
        """Connect/disconnect notices from the headphones."""

        def headphoneMotionManagerDidConnect_(self, mgr):
            print("  [headphones connected]", file=sys.stderr)

        def headphoneMotionManagerDidDisconnect_(self, mgr):
            print("  [headphones disconnected]", file=sys.stderr)

    if NSBundle.mainBundle().objectForInfoDictionaryKey_(
            "NSMotionUsageDescription") is None:
        sys.exit("  Run this through ./run.sh --airpods .\n"
                 "  macOS kills any process that asks for motion data without an app bundle\n"
                 "  declaring NSMotionUsageDescription, so the tracker has to start from\n"
                 "  HeadTrack.app rather than straight from the interpreter.")

    # Without this the sensor stream dies the moment another app takes focus:
    # a background-only process gets napped, and CoreMotion quietly stops
    # delivering. The activity token has to stay referenced to keep working.
    NSActivityUserInitiated = 0x00FFFFFF
    NSActivityLatencyCritical = 0xFF00000000
    activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
        NSActivityUserInitiated | NSActivityLatencyCritical, "head tracking")

    mgr = CoreMotion.CMHeadphoneMotionManager.alloc().init()
    delegate = Delegate.alloc().init()
    mgr.setDelegate_(delegate)

    status = CoreMotion.CMHeadphoneMotionManager.authorizationStatus()
    if not args.json:
        print(f"  motion authorization: {AUTH.get(status, status)}")

    if not mgr.isDeviceMotionAvailable():
        sys.exit("  no head-tracking-capable headphones. Connect AirPods (Pro/Max/3rd gen)\n"
                 "  and make sure they are the active audio output, then try again.")

    def handler(dm, err):
        if err is not None:
            state["fatal"] = explain_error(err)
            mgr.stopDeviceMotionUpdates()
            return
        if dm is not None:
            emit(Sample.from_device_motion(dm))

    mgr.startDeviceMotionUpdatesToQueue_withHandler_(
        NSOperationQueue.mainQueue(), handler)

    if not args.json:
        print("  waiting for motion data... move your head\n")

    loop = NSRunLoop.currentRunLoop()
    deadline = time.monotonic() + args.duration if args.duration else None
    try:
        while state["fatal"] is None:
            loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
            if deadline and time.monotonic() >= deadline:
                break
    finally:
        mgr.stopDeviceMotionUpdates()
        del activity


def main():
    ap = argparse.ArgumentParser(description="Stream head-tracking data from an MPU-6050.")
    ap.add_argument("--json", action="store_true",
                    help="print one JSON object per sample instead of the dashboard")
    ap.add_argument("--csv", metavar="FILE", help="also record every sample to FILE")
    ap.add_argument("--3d", "--serve", dest="serve", nargs="?", type=int,
                    const=8765, metavar="PORT",
                    help="open a live 3D head in the browser (default port 8765)")
    ap.add_argument("--cube", action="store_true",
                    help="open the MPU cube view instead of the head (implies --3d)")
    ap.add_argument("--port", metavar="DEV",
                    help="serial port of the board (default: the one USB serial port found)")
    ap.add_argument("--axes", default="x,y,z", metavar="SPEC",
                    help="which board axes are head X (forward), Y (left), Z (up), "
                         "e.g. '-y,x,z' (default x,y,z)")
    ap.add_argument("--calibrate", type=float, default=1.5, metavar="SEC",
                    help="seconds of stillness used for the gyro bias (default 1.5)")
    ap.add_argument("--list-ports", action="store_true",
                    help="show the serial ports that look like the board and exit")
    ap.add_argument("--duration", type=float, metavar="SEC",
                    help="stop automatically after SEC seconds")
    ap.add_argument("--no-open", action="store_true",
                    help="with --3d, do not launch a browser")
    ap.add_argument("--airpods", action="store_true",
                    help="use AirPods head tracking (CoreMotion) instead of the MPU")
    ap.add_argument("--demo", action="store_true",
                    help="feed fake motion instead of a sensor, to test the setup")
    ap.add_argument("--cwd", metavar="DIR", help=argparse.SUPPRESS)
    args = ap.parse_args()

    # LaunchServices starts the .app in "/", so run.sh hands us the caller's
    # directory to keep relative --csv paths pointing where the user expects.
    if args.cwd:
        os.chdir(args.cwd)

    if args.list_ports:
        ports = mpu.list_ports()
        print("\n".join("  " + p for p in ports) if ports else "  no serial ports found")
        return

    writer = None
    csv_file = None
    if args.csv:
        csv_file = open(args.csv, "w", newline="")
        writer = csv.writer(csv_file)
        writer.writerow(CSV_HEADER)

    note = ("angles are relative to where your head pointed at startup" if args.airpods
            else "yaw drifts (no compass) - restart, or press r in --3d, to re-zero")
    dash = None if args.json else Dashboard(note)
    state = {"fatal": None, "rows": 0}
    latest = {"sample": None, "n": 0}

    if args.cube and not args.serve:
        args.serve = 8765
    if args.serve:
        serve(args.serve, latest)
        url = f"http://127.0.0.1:{args.serve}/" + ("cube" if args.cube else "")
        print(f"  {'cube' if args.cube else '3D'} view: {url}")
        if not args.no_open:
            webbrowser.open(url)

    def emit(s):
        if args.serve:
            latest["sample"] = s
            latest["n"] += 1
        if writer:
            writer.writerow(s.as_row())
            state["rows"] += 1
            if state["rows"] % 50 == 0:   # keep the file usable if we are killed
                csv_file.flush()
        if args.json:
            print(json.dumps(s.as_dict()), flush=True)
        else:
            dash.update(s)

    def background(work):
        """Run a source on a thread, parking its failure in state['fatal']."""
        def wrapped():
            try:
                work()
            except mpu.MPUError as e:
                state["fatal"] = str(e)
            except Exception as e:                       # noqa: BLE001
                state["fatal"] = f"{type(e).__name__}: {e}"
        threading.Thread(target=wrapped, daemon=True).start()

    def wait():
        deadline = time.monotonic() + args.duration if args.duration else None
        while state["fatal"] is None:
            time.sleep(0.05)
            if deadline and time.monotonic() >= deadline:
                break

    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        if args.airpods:
            run_airpods(args, emit, state)
        elif args.demo:
            def fake():
                t0 = time.monotonic()
                while state["fatal"] is None:
                    emit(Sample.synthetic(time.monotonic() - t0))
                    time.sleep(0.04)
            background(fake)
            if not args.json:
                print("  demo mode - synthetic motion\n")
            wait()
        else:
            say = (lambda msg: None) if args.json else print
            def read_mpu():
                for f in mpu.stream(port=args.port, axes=args.axes,
                                    calib_seconds=args.calibrate, status=say):
                    if state["fatal"] is not None:
                        return
                    emit(Sample.from_fused(f))
            background(read_mpu)
            wait()
    except KeyboardInterrupt:
        pass
    finally:
        if csv_file:
            csv_file.close()
            print(f"\n  wrote {args.csv}")

    if state["fatal"]:
        sys.exit("\n  " + state["fatal"])
    if not args.json:
        print("  stopped.")


if __name__ == "__main__":
    main()
