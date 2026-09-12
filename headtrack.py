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
    ./run.sh --mouse         # steer the cursor, and tilt to click
    ./run.sh --tongue        # hold W in focused Minecraft when your tongue is out
    ./run.sh --airpods       # the old CoreMotion source
"""

import argparse
import csv
import glob
import json
import math
import os
import signal
import subprocess
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

import gamekeys
import hedstate
import mouse
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


def arrows(direction):
    """Which way the cursor is being pushed, as a one-liner."""
    x, y = direction
    names = []
    if x < 0:
        names.append("left")
    elif x > 0:
        names.append("right")
    if y < 0:
        names.append("up")
    elif y > 0:
        names.append("down")
    return " + ".join(names) if names else "centred"


class Dashboard:
    """In-place terminal readout."""

    def __init__(self, note="", cursor=None, tongue=None):
        self.note = note
        self.cursor = cursor
        self.tongue = tongue
        self.lines = 14 + bool(cursor) + bool(tongue)
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
        if self.cursor:
            out.insert(5, f"  mouse   {arrows(self.cursor.direction):<16}"
                          f"  tilt: {self.cursor.click_state}")
        if self.tongue:
            out.insert(6 if self.cursor else 5,
                       f"  tongue  {self.tongue.state}")
        if self.started:
            sys.stdout.write(f"\033[{self.lines}A")
        self.started = True
        sys.stdout.write("".join(f"\033[2K{line}\n" for line in out))
        sys.stdout.flush()


PAGES = {"/": "viz.html", "/index.html": "viz.html",
         "/cube": "cube.html", "/cube.html": "cube.html"}


def open_topmost_web_app(url):
    """Open the local viewer in Edge app mode and pin its window on Windows."""
    if os.name != "nt":
        raise RuntimeError("--always-on-top is currently available on Windows only")

    edge = next((p for p in (
        os.path.join(os.environ.get("ProgramFiles(x86)", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
    ) if os.path.isfile(p)), None)
    if not edge:
        raise RuntimeError("Microsoft Edge was not found; install Edge or omit --always-on-top")

    subprocess.Popen([edge, f"--app={url}", "--new-window"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def pin_foreground_edge():
        # A page cannot elevate itself over other desktop apps. Edge hosts this
        # same local page; Windows gives its app window the topmost flag.
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        get_class_name = user32.GetClassNameW
        get_class_name.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        set_window_pos = user32.SetWindowPos
        set_window_pos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.UINT)
        set_window_pos.restype = wintypes.BOOL
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            hwnd = user32.GetForegroundWindow()
            name = ctypes.create_unicode_buffer(256)
            get_class_name(hwnd, name, len(name))
            if name.value == "Chrome_WidgetWin_1":
                # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
                set_window_pos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0,
                               0x0001 | 0x0002 | 0x0040)
                return
            time.sleep(.1)

    threading.Thread(target=pin_foreground_edge, daemon=True).start()


def open_overlay():
    """Launch the independent, click-through native island (Windows or macOS)."""
    if os.name != "nt" and sys.platform != "darwin":
        raise RuntimeError("--overlay needs Windows or macOS; use --3d instead")
    return subprocess.Popen([sys.executable, os.path.join(_HERE, "overlay.py")])


def serve(port, latest, stop):
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

        def do_POST(self):
            if self.path != "/stop":
                self.send_error(404)
                return
            stop.set()
            self.send_response(204)
            self.end_headers()

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


def run_airpods(args, emit, state, stop):
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
        while state["fatal"] is None and not stop.is_set():
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
    ap.add_argument("--mouse", action="store_true",
                    help="move the mouse cursor with your head (macOS/Windows)")
    ap.add_argument("--mouse-speed", type=float, default=350.0, metavar="PXS",
                    help="cursor speed in pixels per second (default 350)")
    ap.add_argument("--mouse-deadzone", type=float, default=6.0, metavar="DEG",
                    help="degrees off centre before the cursor starts moving "
                         "(default 6)")
    ap.add_argument("--mouse-invert-y", action="store_true",
                    help="look up to move the cursor down")
    ap.add_argument("--mouse-recenter", type=float, default=2.0, metavar="SEC",
                    help="how fast centre follows your resting head, to soak up "
                         "yaw drift; 0 pins it (default 2)")
    ap.add_argument("--mouse-click-angle", type=float, default=20.0, metavar="DEG",
                    help="head tilt that presses a button (held until you "
                         "straighten) - right shoulder left-clicks, left "
                         "shoulder right-clicks; 0 turns clicking off "
                         "(default 20)")
    ap.add_argument("--mouse-swap-clicks", action="store_true",
                    help="tilt left to left-click and right to right-click instead")
    ap.add_argument("--tongue", action="store_true",
                    help="hold W while your tongue is out and Minecraft is focused")
    ap.add_argument("--camera", type=int, default=0, metavar="INDEX",
                    help="camera index for --tongue (default 0)")
    ap.add_argument("--tongue-threshold", type=float, default=0.08, metavar="RATIO",
                    help="pink pixel ratio needed for tongue detection (default 0.08)")
    ap.add_argument("--tongue-preview", action="store_true",
                    help="show the camera, face/mouth boxes, score, and tongue state")
    ap.add_argument("--always-on-top", action="store_true",
                    help="Windows: open the viewer as an always-on-top Edge web app")
    ap.add_argument("--overlay", action="store_true",
                    help="show the island overlay (Windows and macOS; on macOS "
                         "its VOICE button types what you say into any app)")
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

    cursor = None
    if args.mouse:
        try:
            cursor = mouse.Cursor(speed=args.mouse_speed,
                                  deadzone=args.mouse_deadzone,
                                  invert_y=args.mouse_invert_y,
                                  recenter=args.mouse_recenter,
                                  click_angle=args.mouse_click_angle,
                                  swap_clicks=args.mouse_swap_clicks)
        except mouse.MouseError as e:
            sys.exit(f"  {e}")
        if not cursor.trusted():
            print("  ! this terminal is not allowed to move the cursor.\n"
                  "    System Settings > Privacy & Security > Accessibility,\n"
                  "    tick your terminal app, then start the tracker again.")
        cursor.start()
        if not args.json:
            print(f"  mouse control on - {args.mouse_speed:.0f} px/s past "
                  f"{args.mouse_deadzone:.0f} deg off centre")
            if args.mouse_click_angle > 0:
                lo, hi = cursor.buttons
                print(f"  tilt {args.mouse_click_angle:.0f} deg to press "
                      f"(hold while tilted) - right shoulder {lo}-clicks, "
                      f"left shoulder {hi}-clicks")

    tongue = None
    if args.tongue:
        try:
            import tongue as tongue_module
            tongue = tongue_module.TongueDetector(
                camera=args.camera, threshold=args.tongue_threshold,
                preview=args.tongue_preview,
                on_error=lambda message: print(f"  ! tongue camera: {message}"))
        except ImportError:
            sys.exit("  --tongue needs opencv-python and numpy; install requirements.txt")
        except tongue_module.TongueError as e:
            sys.exit(f"  {e}")
        tongue.start()

    if args.airpods:
        note = "angles are relative to where your head pointed at startup"
    elif cursor and args.mouse_recenter > 0:
        note = "yaw still creeps (no compass) - centre follows your resting head"
    else:
        note = "yaw drifts (no compass) - restart, or press r in --3d, to re-zero"
    # State the voice overlay writes ("hed, game mode", "hed, max") and the
    # tracker reads. The watcher polls the shared state file and pushes
    # changes into game (arrow-key mode) and cursor (mouse speed scale).
    game = gamekeys.GameKeys()

    def apply_state(s):
        game.set_mode(s.get("mode", "casual"))
        if cursor is not None:
            cursor.set_scale(hedstate.clamp_mouse_scale(s.get("mouse_scale", 1.0)))

    state = hedstate.Watcher(on_change=apply_state)
    dash = None if args.json else Dashboard(note, cursor, tongue)
    state = {"fatal": None, "rows": 0}
    stop = threading.Event()
    latest = {"sample": None, "n": 0}
    overlay = None

    # The island is also where voice lives, so it stands on its own rather
    # than riding along with the 3D view.
    if args.overlay:
        overlay = open_overlay()

    if args.cube and not args.serve:
        args.serve = 8765
    if args.serve:
        serve(args.serve, latest, stop)
        url = f"http://127.0.0.1:{args.serve}/" + ("cube" if args.cube else "")
        print(f"  {'cube' if args.cube else '3D'} view: {url}")
        if args.always_on_top:
            open_topmost_web_app(url)
        elif not args.no_open:
            webbrowser.open(url)

    def emit(s):
        state.refresh()
        if cursor:
            cursor.aim(s.yaw, s.pitch, s.roll)
        game.aim(s.yaw, s.pitch, s.roll)
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
        while state["fatal"] is None and not stop.is_set():
            time.sleep(0.05)
            if deadline and time.monotonic() >= deadline:
                break

    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        if args.airpods:
            run_airpods(args, emit, state, stop)
        elif args.demo:
            def fake():
                t0 = time.monotonic()
                while state["fatal"] is None and not stop.is_set():
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
                    if state["fatal"] is not None or stop.is_set():
                        return
                    emit(Sample.from_fused(f))
            background(read_mpu)
            wait()
    except KeyboardInterrupt:
        pass
    finally:
        if cursor:
            cursor.stop()
        if tongue:
            tongue.stop()
        game.stop()
        if overlay:
            overlay.terminate()
        if csv_file:
            csv_file.close()
            print(f"\n  wrote {args.csv}")

    if state["fatal"]:
        sys.exit("\n  " + state["fatal"])
    if not args.json:
        print("  stopped.")


if __name__ == "__main__":
    main()
