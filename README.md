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
./run.sh --mouse          # steer the mouse cursor with your head
./run.sh --airpods        # the AirPods/CoreMotion source this started as
```

First run creates `.venv` and installs pyserial.

## Windows quick start

`run.sh` is for macOS. From PowerShell, use the Windows virtual environment
directly. Add `--demo` when an ESP32 is not connected:

```powershell
.\.venv-windows\Scripts\python.exe .\headtrack.py --3d --demo
.\.venv-windows\Scripts\python.exe .\headtrack.py --cube --demo
```

For a standalone Edge web-app window that stays above normal Windows windows,
add `--always-on-top`. To keep the original local cube page in your ordinary
browser and show only an inert, click-through island at the top of the desktop,
use `--overlay` instead:

```powershell
.\.venv-windows\Scripts\python.exe .\headtrack.py --cube --demo --always-on-top
.\.venv-windows\Scripts\python.exe .\headtrack.py --cube --overlay
```

Use the island's Stop button or `Ctrl+C` in PowerShell to close it. Without a
connected ESP32, the tracker needs `--demo`; otherwise it exits after reporting
that no serial port was found.

## Voice typing from the island (macOS 26+)

On a Mac the island is also Hed's voice. Start it on its own or alongside the
tracker:

```bash
.venv/bin/python overlay.py
./run.sh --overlay            # with the tracker (add --3d etc. as usual)
```

Hover the island and click **VOICE** (a head-tilt click works too - the island
never takes keyboard focus). From then on, whatever you say is typed into the
text field that has focus, in any app: Notes, Slack, Mail, a browser, a
terminal. The island shows a live waveform and what you are saying; click
VOICE again, or say *stop listening*, to turn it off. Say *new line* for
Return.

- Speech runs on-device through Apple's `SpeechAnalyzer` (`speech_mac/`,
  built automatically the first time).
- Typing needs **Accessibility** permission for whatever launched the overlay
  (System Settings > Privacy & Security > Accessibility), the same grant
  `--mouse` uses. macOS also asks once for the microphone.
- If focus is on something that is not a text field (a button, a list), the
  text is held for a few seconds and typed as soon as you click into a field,
  rather than firing keyboard shortcuts.
- The island is the only thing that turns the microphone on. The Chrome
  extension (`chrome_extension/`) listens in on the island's transcript and
  runs "hed, new tab"-style browser commands, which are never typed out; with
  VOICE off, nothing anywhere is listening.
- Problems (mic denied, no Accessibility, engine errors) show on the island in
  yellow, and everything is logged to `~/Library/Logs/Hed/voice.log`.

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

Yaw has no such reference - the MPU-6050 has no magnetometer - so it is gyro
integration and nothing else. Two separate things are done about that.

**The filter.** Orientation comes from [x-io Technologies'
Fusion](https://github.com/xioTechnologies/Fusion) (`pip install imufusion`),
by the author of the Madgwick filter. Beyond better accelerometer rejection, it
runs a zero-rate-update bias tracker: any stretch where the gyro reads under
3 deg/s for 3 seconds is taken as "at rest", and at rest the gyro reading *is*
the bias, so it ramps its offset estimate onto it. This matters because the
startup calibration is a single snapshot and gyro bias moves as the board warms
- and on a 6-axis part that stale offset integrates straight into heading with
nothing to check it. It corrects *rate*, not angle, so holding your head at an
angle is never mistaken for bias: a held pose has no rotation rate.

Simulating a stationary sensor whose bias creeps in to 0.8 deg/s over three
minutes:

| | yaw error after 3 min |
|---|---|
| built-in Mahony | 72 deg |
| imufusion + bias tracking | 6 deg |

If `imufusion` is missing the tracker falls back to the old Mahony filter and
says so at startup.

**The application.** `--mouse` does not trust the centre to stay put either; it
lets the origin follow your resting head, so leftover creep never becomes a
cursor that slides on its own. See below.

Neither is a compass, and nothing here can be. If you want yaw that genuinely
does not drift, add a magnetometer (HMC5883L, QMC5883L) on the same I2C bus:
Fusion's full `update()` already takes one, so it is a firmware change plus a
few lines here, not a rewrite.

The AirPods source has exactly the same yaw gap, for a different reason:
CoreMotion's reference-frame entry point that would fix it is private, and
calling it from a client process traps inside CoreMotion.

## Mouse control

```
./run.sh --mouse
```

Your head becomes a d-pad for the cursor, and a tilt towards either shoulder
clicks. Where you were looking when tracking started is the centre; past 6 degrees off it the cursor slides at a steady 350
px/s and keeps sliding until you look back. Turning further does **not** move it
faster or further - it is direction only, so a small glance left parks the
cursor moving left and you steer with how long you hold it, not how far you
crane your neck.

```
./run.sh --mouse --mouse-speed 200      # slower
./run.sh --mouse --mouse-speed 700      # faster
./run.sh --mouse --mouse-recenter 0     # never move the centre
./run.sh --mouse --mouse-deadzone 10    # a wider dead centre
./run.sh --mouse --mouse-invert-y       # look up to go down
./run.sh --mouse --3d                   # with the 3D head alongside
```

Left/right comes from yaw, up/down from pitch. The dashboard grows a `mouse`
line showing which way it is currently pushing, so you can see the deadzone
edges without watching the cursor.

### Clicking

Tip your head over towards a shoulder:

| gesture | button |
|---|---|
| tilt towards the **right** shoulder | **left** click |
| tilt towards the **left** shoulder | **right** click |

Roll is the one axis the cursor does not use, which is what makes it free for
this. The threshold is 20 degrees - deliberate, but an easy movement - and the
button goes **down** as you cross it and stays down until your head comes back
within 10 degrees of upright. A quick tip is still a click; keep the lean and
it is a press-and-hold. That gap is what stops a head resting near the
threshold from machine-gunning clicks as it wobbles across it.

The cursor freezes while your head is over, because head tilt bleeds a little
into yaw and pitch and a click that slides the pointer off its target is a miss.

```
./run.sh --mouse --mouse-click-angle 30   # need a bigger tilt
./run.sh --mouse --mouse-click-angle 0    # no clicking, movement only
./run.sh --mouse --mouse-swap-clicks      # left shoulder left-clicks instead
```

Unlike yaw, roll is held honest by gravity and does not drift, so its centre
only follows a change of posture - settle into a habitual lean and that becomes
upright. It deliberately does *not* follow a held tilt: letting it creep onto
one would re-arm the click while your head was still over, and fire the
opposite button on the way back up.

macOS has to be told to allow it: **System Settings > Privacy & Security >
Accessibility**, tick whichever terminal you launch from. The tracker checks at
startup and says so if the permission is missing - without it the cursor events
are dropped silently. The cursor is nudged from its live position each tick, so
the trackpad still works at the same time and the screen edges clamp normally.

**Drift is handled here, not endured.** A fixed centre plus a heading that
creeps eventually means "the cursor drifts left on its own", so the centre is
not fixed: it leaks towards wherever you are actually looking. Quickly (about 2
seconds) while you are inside the deadzone, where the cursor is parked anyway
and re-centring is free; slowly (about 30 seconds) while you are outside it,
purely as a backstop for drift wider than the deadzone. Simulated:

| | cursor movement |
|---|---|
| 3 deg/min of drift, 5 minutes | never twitches |
| a deliberate 20 deg glance, held 2 s | the full 2 s |

The trade is that holding one pose *indefinitely* decays: a 20 deg hold pushes
for about 36 seconds - some 25,000 px, far wider than any screen - and then
stops. `--mouse-recenter 0` pins the centre if you would rather it never moved.

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
- `mpu.py` - serial reader, gyro calibration, Fusion/Mahony filters, axis remap
- `mouse.py` - head-to-cursor movement and tilt-to-click, CoreGraphics over ctypes
- `firmware/mpu6050_head/` - the Arduino sketch, ~100 lines
- `cube.html` - the MPU cube view, standalone
- `viz.html` - the 3D head (geometry, rotation, and renderer, ~200 lines of plain JS)
- `run.sh` - sets up the venv and launches the tracker
- `build_app.sh` - builds the `HeadTrack.app` wrapper, only needed for `--airpods`
- `chrome_extension/` - MV3 extension: voice-to-text on the focused field, hotkey toggle
