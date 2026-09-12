# Head tracking from an MPU-6050

An ESP32 with an MPU-6050 strapped to your head streams raw motion over USB
serial; this reads it, fuses it into an orientation, and shows it in the
terminal or as a 3D head in the browser.

```
./run.sh                  # live dashboard
./run.sh --3d             # 3D mannequin head in the browser, moves with yours
./run.sh --cube           # just the MPU: a cube in the sensor's own orientation
./run.sh --json           # one JSON object per sample on stdout
./run.sh --csv run.csv    # record every sample to CSV
./run.sh --duration 30    # stop after 30 seconds
./run.sh --3d --demo      # fake motion, to check the setup without a sensor
./run.sh --airpods        # the AirPods/CoreMotion source this started as
```

First run creates `.venv` and installs pyserial.

## Flashing the board

Open `firmware/mpu6050_head/mpu6050_head.ino` in the Arduino IDE, pick your
ESP32 board, and upload. Wiring is the same as before - SDA on GPIO 33, SCL on
GPIO 32, AD0 to GND (change `SDA_PIN`/`SCL_PIN` at the top if yours differs).

Plenty of boards sold as MPU-6050 are really an MPU-6500 or MPU-9250: same
registers, same scales, a different `WHO_AM_I`. The sketch reads that register
to name the part, not to refuse it - it only gives up when nothing answers on
the bus at all. Whatever it finds shows up in the dashboard and in the CSV's
`sensor` column.

The sketch does no fusion. It sets the MPU to 100 Hz with the DLPF on,
±2 g / ±500 °/s, and prints raw counts:

```
#mpu part=MPU-6500 whoami=0x70 rate=100 accel_fs=2 gyro_fs=500
#fields ax,ay,az,gx,gy,gz,micros
-412,183,16522,-38,12,-7,1043210
```

The board repeats that header every five seconds, and on demand when the host
sends `?`. It has to: connecting does not reset the board (that would make
every start cost a reboot), and the serial library throws away anything already
buffered when it opens the port - so a header printed once at power-up is one
the host would usually never see.

Everything else - bias calibration, the Mahony filter, the axis remap - is in
`mpu.py`, so a different mounting or a retune never needs a reflash.

**Close the Arduino serial monitor before running.** Only one program can hold
the port; the tracker will tell you if something else has it.

## Start-up

```
  reading /dev/cu.usbserial-10
  calibrating gyro - hold the sensor still (1.5s)
  calibrated - gyro bias -1.83 +0.44 +2.06 deg/s
```

Hold the sensor still for that first second and a half. The gyro's zero-rate
offset is a few °/s and differs per axis; whatever is measured there gets
subtracted from every later reading. Move during calibration and the movement
is baked in as bias, so yaw walks off at that rate forever - the tracker warns
when it sees that happen.

## Mounting and axes

The code reports in a head frame of **X forward, Y left, Z up**. If the board is
mounted in some other orientation, say which board axis feeds which head axis
instead of rewiring or reflashing:

```
./run.sh --axes "-y,x,z"      # board -Y points forward, board X points left
```

Quick way to work it out: run `./run.sh`, nod, and see which number moves. If
the right angles move but in the wrong direction, either flip the sign in
`--axes` or tick the invert box in the 3D view.

## What you get

100 samples/sec, each with:

| field | meaning |
|---|---|
| `yaw` / `pitch` / `roll` | head angle in degrees - turn / nod / tilt |
| `quat` | the same orientation as a quaternion (w, x, y, z) |
| `rotation_rate` | gyroscope, bias removed, rad/s |
| `user_accel` | acceleration minus gravity, in g |
| `gravity` | which way is down, in g |
| `sensor` | which source produced the frame |

## Drift

Pitch and roll are held steady by gravity: the filter always knows which way is
down, so a tilt error corrects itself within a second or so.

Yaw has no such reference - the MPU-6050 has no magnetometer - so it is pure
gyro integration and creeps, typically a few degrees a minute after
calibration. Warm the board up for a minute before calibrating if you care;
otherwise re-centre (`r` in the 3D view) or restart. Adding a magnetometer
(HMC5883L, QMC5883L) on the same I2C bus is the real fix, and the filter has
the shape for it - it would take a second correction term against magnetic
north.

The AirPods source has exactly the same yaw gap, for a different reason:
CoreMotion's reference-frame entry point that would fix it is private, and
calling it from a client process traps inside CoreMotion.

## The cube view

```
./run.sh --cube
```

A page of its own at `/cube` - just the sensor. A cube in the board's own
orientation, driven straight off the quaternion rather than the euler angles,
with the three sensor axes drawn and labelled: **X red** (forward), **Y green**
(left), **Z blue** (up). The panel beside it carries the angles, the raw
quaternion, gyro in deg/s and what the accelerometer actually reads.

`r` (or the button) zeroes it: the current attitude becomes the new identity and
the cube snaps back square. That is the quickest way to check the mounting -
zero it, turn your head left, and see which axis the cube turns about.

It is a fixed camera looking slightly down and across, so a cube at rest shows
front, right and top rather than one flat face.

## The 3D view

`--3d` starts a local server on port 8765 and opens a mannequin head that turns,
nods, and tilts with yours. It is plain canvas 2D with a hand-rolled renderer -
no libraries, no network, nothing to install. Samples reach the page over
server-sent events.

- **re-center** (or `r`) makes the current pose the new zero
- the **invert** checkboxes flip an axis if a movement feels backwards

`--3d --demo` feeds synthetic motion, a quick way to confirm the viewer works
before the board is wired up.

## The AirPods source

`--airpods` reads `CMHeadphoneMotionManager` instead, and needs:

- macOS 14+ (that class arrived in Sonoma)
- AirPods Pro, Pro 2, Max, or 3rd gen - anything that does Spatial Audio.
  Regular AirPods 1/2 have no gyro and will never produce data.
- The AirPods connected **and** selected as the audio output device

It also needs the `HeadTrack.app` wrapper, which the MPU path does not. macOS
will not hand motion data to a process with no `NSMotionUsageDescription` in
its bundle - it kills it outright with a TCC privacy violation, and a script in
a terminal has no bundle of its own. So `build_app.sh` assembles a minimal app
whose executable is a copy of the real Python interpreter, and `run.sh` starts
it through LaunchServices (`open`) so the app is the one asking; output comes
back to your terminal through a FIFO. That path also has to hold off App Nap,
or macOS throttles the background app the moment you click another window and
CoreMotion silently stops delivering.

If you ever deny the motion prompt, re-allow it in
**System Settings > Privacy & Security > Motion & Fitness**.

## The Chrome extension

`chrome_extension/` is a standalone browser accessibility helper. Load it
unpacked from `chrome://extensions`: focusing any text field pops up a mic
dot beside it and dictates into the field using `SpeechRecognition`.
**Ctrl/⌘+Shift+M** toggles listening, **Esc** stops.

It does **not** need the Arduino board, `headtrack.py`, the Python venv, or
`HeadTrack.app`. It never opens a socket to the tracker and never imports
anything else in this repo - use it on its own with nothing else running.

See `chrome_extension/README.md` for install and options.

## Files

- `headtrack.py` - dashboard, CSV/JSON output, 3D server, source selection
- `mpu.py` - serial reader, gyro calibration, Mahony filter, axis remap
- `firmware/mpu6050_head/` - the Arduino sketch, ~100 lines
- `cube.html` - the MPU cube view, standalone
- `viz.html` - the 3D head (geometry, rotation, and renderer, ~200 lines of plain JS)
- `run.sh` - sets up the venv and launches the tracker
- `build_app.sh` - builds the `HeadTrack.app` wrapper, only needed for `--airpods`
- `chrome_extension/` - MV3 extension: voice-to-text on the focused field, hotkey toggle
