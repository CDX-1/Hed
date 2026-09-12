#!/usr/bin/env python3
"""Turn the MPU-6050's raw serial stream into head orientation.

The board (see firmware/mpu6050_head) only ships raw accelerometer and gyro
counts; everything that makes them an orientation happens here:

    counts -> physical units -> axis remap -> gyro bias -> Mahony filter

The filter integrates the gyro for the fast motion and leans on gravity to pull
pitch and roll back, which is what keeps them from walking away over minutes.

Two filters live here. The real one is x-io Technologies' Fusion (`imufusion`,
by the author of the Madgwick filter): it brings adaptive accelerometer
rejection, and - the part that matters most - a zero-rate-update bias tracker
that keeps re-estimating the gyro offset whenever the sensor sits still. A
one-shot calibration at startup goes stale as the board warms, and on a 6-axis
part nothing ever corrects yaw, so that stale offset integrates straight into
heading. Letting the tracker chase it cut simulated three-minute yaw drift from
72 deg to 6 deg. Mahony below is the fallback for when imufusion is missing.

Yaw still has no absolute reference - there is no magnetometer on a 6050 - so
what is left is a slow creep rather than a march. Re-centre in the 3D view (r),
or let --mouse re-zero itself as you rest at centre.
"""

import glob
import math
import time
from collections import namedtuple

# Frame this module reports in: X forward (nose), Y left, Z up. Sitting still
# and level, the accelerometer reads +1 g on Z.
Fused = namedtuple("Fused", "t yaw pitch roll quat rot acc grav part")

DEG = 180.0 / math.pi

PORT_GLOBS = ["/dev/cu.usbserial*", "/dev/cu.usbmodem*", "/dev/cu.wchusbserial*",
              "/dev/cu.SLAB_USBtoUART*", "/dev/ttyUSB*", "/dev/ttyACM*"]


class MPUError(Exception):
    pass


def list_ports():
    seen = []
    for pattern in PORT_GLOBS:
        for p in sorted(glob.glob(pattern)):
            if p not in seen:
                seen.append(p)
    return seen


def find_port():
    ports = list_ports()
    if not ports:
        raise MPUError("no serial port found - plug the ESP32 in, or pass --port.\n"
                       "  (a bare /dev/cu.Bluetooth-Incoming-Port does not count)")
    if len(ports) > 1:
        raise MPUError("several serial ports look plausible, pick one with --port:\n  "
                       + "\n  ".join(ports))
    return ports[0]


def parse_axes(spec):
    """'-y,x,z' -> the board axes that supply head X, Y, Z, with signs."""
    parts = [p.strip().lower() for p in spec.split(",")]
    if len(parts) != 3:
        raise MPUError(f"--axes wants three entries, got {spec!r}")
    out = []
    for p in parts:
        sign = -1.0 if p.startswith("-") else 1.0
        name = p.lstrip("+-")
        if name not in "xyz" or len(name) != 1:
            raise MPUError(f"--axes entry {p!r} is not one of x, y, z (optionally signed)")
        out.append(("xyz".index(name), sign))
    if len({i for i, _ in out}) != 3:
        raise MPUError(f"--axes uses an axis twice: {spec!r}")
    return out


def remap(v, axes):
    return tuple(v[i] * s for i, s in axes)


def attitude_from_accel(acc):
    """A quaternion with the same pitch/roll as this accelerometer reading.

    Seeds a filter so it starts settled instead of swinging into place.
    """
    ax, ay, az = acc
    n = math.sqrt(ax * ax + ay * ay + az * az)
    if n < 1e-6:
        return [1.0, 0.0, 0.0, 0.0]
    ax, ay, az = ax / n, ay / n, az / n
    roll = math.atan2(ay, az)
    pitch = math.atan2(-ax, math.sqrt(ay * ay + az * az))
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    return [cr * cp, sr * cp, cr * sp, -sr * sp]


class Fusion:
    """x-io's Fusion (the `imufusion` package), wrapped to look like Mahony.

    Takes gyro in deg/s, accelerometer in g, and reports the same frame the
    rest of this module uses: X forward, Y left, Z up - which is exactly
    Fusion's NWU convention, so no remap is needed on the way in or out.

    The bias tracker is the reason this class exists. It watches for stretches
    where the gyro reads below `stationary_threshold` deg/s for
    `stationary_period` seconds, decides the sensor is at rest, and ramps its
    offset estimate towards whatever the gyro is reporting - which, at rest,
    is bias by definition. It corrects rate, not angle, so holding your head
    at an angle never gets absorbed: a held pose has no rotation rate, and the
    bias it converges on is the same one it would find sitting on the desk.
    """

    def __init__(self, sample_rate=100.0, gyro_range=500.0):
        import numpy                      # imufusion speaks numpy arrays
        import imufusion
        self.np = numpy
        self.imufusion = imufusion

        self.ahrs = imufusion.Ahrs()
        settings = imufusion.AhrsSettings()
        settings.convention = imufusion.CONVENTION_NWU
        settings.gain = 0.5
        # Telling it the real range lets it spot a saturated gyro (a fast head
        # snap can peg 500 deg/s) and recover instead of integrating garbage.
        settings.gyroscope_range = gyro_range
        # Degrees of tilt error tolerated before a reading is dismissed as
        # something other than gravity. Replaces the old hand-rolled
        # "is the magnitude near 1 g" test, which passes plenty of readings
        # that are 1 g of something that is not down.
        settings.acceleration_rejection = 10.0
        settings.rejection_timeout = int(5 * sample_rate)
        settings.sample_rate = int(sample_rate)
        self.ahrs.set_settings(settings)
        self.ahrs.set_sample_period(1.0 / sample_rate)

        self.bias = imufusion.Bias()
        bias_settings = imufusion.BiasSettings()
        bias_settings.sample_rate = int(sample_rate)
        bias_settings.stationary_period = 3.0
        bias_settings.stationary_threshold = 3.0
        self.bias.set_settings(bias_settings)

    name = "imufusion"

    def align(self, acc):
        self.ahrs.set_quaternion(self.np.array(attitude_from_accel(acc),
                                               dtype=self.np.float32))
        # Fusion normally spends its first 3 seconds converging from an unknown
        # attitude, and holds heading at zero while it does - which would eat
        # the first three seconds of head movement. We have just handed it an
        # attitude averaged over the whole calibration hold, so there is
        # nothing left for that phase to work out.
        self.ahrs.skip_startup()

    def update(self, gyro_dps, acc, dt):
        self.ahrs.set_sample_period(dt)
        g = self.bias.update(self.np.array(gyro_dps, dtype=self.np.float32))
        self.ahrs.update_no_magnetometer(g, self.np.array(acc, dtype=self.np.float32))
        return self.quat()

    def quat(self):
        return tuple(float(v) for v in self.ahrs.get_quaternion())

    def euler(self):
        roll, pitch, yaw = self.imufusion.quaternion_to_euler(
            self.ahrs.get_quaternion())
        return float(yaw), float(pitch), float(roll)

    def gravity(self):
        return tuple(float(v) for v in self.ahrs.get_gravity())

    def offset(self):
        """What the bias tracker has taken out, deg/s - worth showing."""
        return tuple(float(v) for v in self.bias.get_offset())


class Mahony:
    """Complementary filter on a quaternion, with gravity as the only reference.

    kp sets how hard gravity pulls the estimate back - too high and every head
    bob shows up as a tilt, too low and pitch/roll take seconds to settle. ki
    absorbs whatever gyro bias the startup calibration missed, and the drift it
    picks up as the board warms.
    """

    def __init__(self, kp=1.2, ki=0.05):
        self.kp = kp
        self.ki = ki
        self.q = [1.0, 0.0, 0.0, 0.0]     # w, x, y, z - body to earth
        self.bias = [0.0, 0.0, 0.0]

    name = "mahony"

    def align(self, acc):
        """Seed from one accelerometer reading, so pitch/roll start settled."""
        self.q = attitude_from_accel(acc)

    def update(self, gyro_dps, acc, dt):
        gx, gy, gz = (v / DEG for v in gyro_dps)     # deg/s in, rad/s here
        q0, q1, q2, q3 = self.q

        # Where the filter currently thinks "up" is, in board axes.
        vx = 2.0 * (q1 * q3 - q0 * q2)
        vy = 2.0 * (q0 * q1 + q2 * q3)
        vz = q0 * q0 - q1 * q1 - q2 * q2 + q3 * q3

        ax, ay, az = acc
        n = math.sqrt(ax * ax + ay * ay + az * az)
        # Only trust the accelerometer when it is reading close to plain
        # gravity. Mid-turn it is reading the turn as well, and correcting
        # towards that tips the estimate.
        if 0.75 < n < 1.25:
            ax, ay, az = ax / n, ay / n, az / n
            ex = ay * vz - az * vy
            ey = az * vx - ax * vz
            ez = ax * vy - ay * vx
            self.bias[0] += self.ki * ex * dt
            self.bias[1] += self.ki * ey * dt
            self.bias[2] += self.ki * ez * dt
            gx += self.kp * ex + self.bias[0]
            gy += self.kp * ey + self.bias[1]
            gz += self.kp * ez + self.bias[2]

        # qa/qb/qc hold the pre-update values: every line below wants the
        # quaternion as it was at the start of the step, not as the line above
        # left it.
        h = 0.5 * dt
        qa, qb, qc = q0, q1, q2
        q0 += (-qb * gx - qc * gy - q3 * gz) * h
        q1 += (qa * gx + qc * gz - q3 * gy) * h
        q2 += (qa * gy - qb * gz + q3 * gx) * h
        q3 += (qa * gz + qb * gy - qc * gx) * h

        n = math.sqrt(q0 * q0 + q1 * q1 + q2 * q2 + q3 * q3) or 1.0
        self.q = [q0 / n, q1 / n, q2 / n, q3 / n]
        return self.quat()

    def quat(self):
        return tuple(self.q)

    def offset(self):
        return tuple(self.bias)

    def euler(self):
        q0, q1, q2, q3 = self.q
        roll = math.atan2(2 * (q0 * q1 + q2 * q3), 1 - 2 * (q1 * q1 + q2 * q2))
        s = max(-1.0, min(1.0, 2 * (q0 * q2 - q3 * q1)))
        pitch = math.asin(s)
        yaw = math.atan2(2 * (q0 * q3 + q1 * q2), 1 - 2 * (q2 * q2 + q3 * q3))
        return yaw * DEG, pitch * DEG, roll * DEG

    def gravity(self):
        """Down-vector in board axes, in g - the same shape CoreMotion reports."""
        q0, q1, q2, q3 = self.q
        return (2.0 * (q1 * q3 - q0 * q2),
                2.0 * (q0 * q1 + q2 * q3),
                q0 * q0 - q1 * q1 - q2 * q2 + q3 * q3)


def make_filter(sample_rate=100.0, gyro_range=500.0, say=None):
    """Fusion if it is installed, Mahony if not."""
    say = say or (lambda msg: None)
    try:
        return Fusion(sample_rate=sample_rate, gyro_range=gyro_range)
    except ImportError:
        say("  imufusion not installed - falling back to the built-in Mahony\n"
            "    filter, which drifts noticeably more. Fix with:\n"
            "    .venv/bin/pip install imufusion numpy")
        return Mahony()


def open_serial(port, baud=115200):
    try:
        import serial
    except ImportError:
        raise MPUError("pyserial is missing - run ./run.sh, which installs it, "
                       "or: .venv/bin/pip install pyserial")
    # Opening the port normally toggles DTR/RTS, which resets the ESP32 (and on
    # some boards drops it into the bootloader). Clear both before opening so
    # the sketch keeps running and we do not lose the header line.
    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = 1.0
    s.dtr = False
    s.rts = False
    try:
        s.open()
    except Exception as e:
        hint = ""
        if "usy" in str(e):      # EBUSY - almost always the IDE's serial monitor
            hint = "\n  Something else is holding the port - close the Arduino serial monitor."
        raise MPUError(f"cannot open {port}: {e}{hint}")
    return s


def stream(port=None, axes="x,y,z", calib_seconds=1.5, status=None):
    """Yield a Fused sample per frame from the board, forever.

    `status` is called with human-readable progress (port, calibration) so the
    caller can print it wherever its output belongs.
    """
    say = status or (lambda msg: None)
    axes = parse_axes(axes) if isinstance(axes, str) else axes
    port = port or find_port()
    ser = open_serial(port)
    say(f"  reading {port}")
    ser.reset_input_buffer()
    ser.write(b"?\n")            # ask the board to repeat its header
    asked = time.monotonic()

    accel_lsb = 32768.0 / 2       # overridden by the board's header line
    gyro_fs = 500.0
    gyro_lsb = 32768.0 / gyro_fs
    rate = 100.0
    part = "mpu"                  # the board names the part it actually found
    fil = None                    # built once the header has named the ranges

    bias = [0.0, 0.0, 0.0]
    calib = []                    # raw gyro triples collected while stationary
    acc_sum = [0.0, 0.0, 0.0]
    calibrated = False
    said_calib = False

    last_us = None
    t = 0.0
    deadline = time.monotonic() + 8.0

    while True:
        raw = ser.readline()
        if not raw:
            if time.monotonic() > deadline:
                raise MPUError(
                    f"no data on {port}. Is the mpu6050_head sketch flashed, and is the\n"
                    "  Arduino serial monitor closed? (only one program can hold the port)")
            continue
        line = raw.decode("ascii", "replace").strip()
        if not line:
            continue

        # The header may be mid-flight, or the board may be an older build that
        # only prints it at power-up. Keep asking until it turns up.
        if part == "mpu" and time.monotonic() - asked > 1.0:
            ser.write(b"?\n")
            asked = time.monotonic()

        if line.startswith("#"):
            if line.startswith("#error"):
                raise MPUError(f"the board cannot reach the sensor ({line[6:].strip()}).\n"
                               "  Check SDA/SCL wiring and that AD0 is tied low.")
            for tok in line.split():
                if tok.startswith("accel_fs="):
                    accel_lsb = 32768.0 / float(tok.split("=")[1])
                elif tok.startswith("gyro_fs="):
                    gyro_fs = float(tok.split("=")[1])
                    gyro_lsb = 32768.0 / gyro_fs
                elif tok.startswith("rate="):
                    rate = float(tok.split("=")[1])
                elif tok.startswith("part="):
                    part = tok.split("=")[1].lower()
                    # Plenty of boards sold as MPU-6050 are really a 6500 or a
                    # 9250. Same registers, same scales, different WHO_AM_I -
                    # so say which one turned up rather than treating it as a
                    # failure.
                    if part == "unknown":
                        say(f"  unrecognised part ({line.strip()}) - "
                            "continuing, the registers are compatible")
            continue

        if line.startswith("Accel") or "Gyro" in line:
            raise MPUError(
                "the board is running the old print-style sketch, not the streaming one.\n"
                "  Flash firmware/mpu6050_head/mpu6050_head.ino and try again:\n"
                f"  the tracker needs 'ax,ay,az,gx,gy,gz,micros' at 100 Hz, and got:\n"
                f"  {line[:70]}")

        parts = line.split(",")
        if len(parts) != 7:
            continue              # bootloader noise, or a line we caught mid-write
        try:
            ax, ay, az, gx, gy, gz, us = (int(p) for p in parts)
        except ValueError:
            continue
        deadline = time.monotonic() + 3.0

        acc = remap((ax / accel_lsb, ay / accel_lsb, az / accel_lsb), axes)
        gyro_dps = remap((gx / gyro_lsb, gy / gyro_lsb, gz / gyro_lsb), axes)

        if fil is None:
            fil = make_filter(sample_rate=rate, gyro_range=gyro_fs, say=say)
            if fil.name == "imufusion":
                say("  filter: imufusion (Fusion) with zero-rate bias tracking")

        if not calibrated:
            if not said_calib:
                say(f"  calibrating gyro - hold the sensor still ({calib_seconds:.1f}s)")
                said_calib = True
            calib.append(gyro_dps)
            for i in range(3):
                acc_sum[i] += acc[i]
            if len(calib) >= max(10, int(calib_seconds * 100)):
                n = len(calib)
                bias = [sum(c[i] for c in calib) / n for i in range(3)]
                # A wobble during calibration bakes itself into the bias, and
                # then yaw walks off at that rate forever. Worth saying so.
                swing = max(max(abs(c[i] - bias[i]) for c in calib) for i in range(3))
                if swing > 3.0:
                    say(f"  ! sensor moved while calibrating ({swing:.1f} deg/s) - "
                        "yaw will drift; restart while holding it still")
                fil.align([v / n for v in acc_sum])
                say(f"  calibrated - gyro bias {bias[0]:+.2f} {bias[1]:+.2f} "
                    f"{bias[2]:+.2f} deg/s")
                calibrated = True
            continue

        # The board's clock, not ours: USB serial buffering bunches arrivals up,
        # and integrating against arrival times smears every fast turn.
        if last_us is None:
            dt = 0.01
        else:
            dt = ((us - last_us) & 0xFFFFFFFF) / 1e6   # micros() wraps at ~71 min
        last_us = us
        if not (0.0002 < dt < 0.2):
            dt = 0.01
        t += dt

        # The startup calibration takes out the bulk of the offset; the filter's
        # own bias tracker chases whatever is left as the board warms.
        debiased = tuple(g - b for g, b in zip(gyro_dps, bias))      # deg/s
        fil.update(debiased, acc, dt)
        yaw, pitch, roll = fil.euler()
        grav = fil.gravity()
        q = fil.quat()

        yield Fused(t=t, yaw=yaw, pitch=pitch, roll=roll,
                    quat=q, rot=tuple(v / DEG for v in debiased), acc=acc,
                    grav=grav, part=part)
