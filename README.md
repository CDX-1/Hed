# AirPods head tracking on macOS

Reads live head orientation from AirPods and shows it in the terminal.

```
./run.sh                  # live dashboard
./run.sh --3d             # 3D mannequin head in the browser, moves with yours
./run.sh --json           # one JSON object per sample on stdout
./run.sh --csv run.csv    # record every sample to CSV
./run.sh --duration 30    # stop after 30 seconds
./run.sh --3d --demo      # fake motion, to check the setup without AirPods
```

First run creates `.venv`, installs PyObjC, and builds `HeadTrack.app`.

## What you get

Roughly 25 samples/sec, each with:

| field | meaning |
|---|---|
| `yaw` / `pitch` / `roll` | head angle in degrees - turn / nod / tilt |
| `quat` | the same orientation as a quaternion (w, x, y, z) |
| `rotation_rate` | gyroscope, rad/s |
| `user_accel` | acceleration minus gravity, in g |
| `gravity` | which way is down, in g |
| `sensor` | which earbud is reporting |

Angles are relative to wherever your head pointed when the tracker started -
CoreMotion has no absolute compass reference here. Restart it facing forward
to re-zero.

## The 3D view

`--3d` starts a local server on port 8765 and opens a mannequin head that turns,
nods, and tilts with yours. It is plain canvas 2D with a hand-rolled renderer -
no libraries, no network, nothing to install. Samples reach the page over
server-sent events.

- **re-center** (or `r`) makes the current pose the new zero
- the **invert** checkboxes flip an axis if a movement feels backwards

The stand does not rotate, so it stays as a reference for how far the head has
turned. `--3d --demo` feeds synthetic motion, which is a quick way to confirm the
viewer works before your AirPods are in.

## Requirements

- macOS 14+ (this is `CMHeadphoneMotionManager`, added to macOS in Sonoma)
- AirPods Pro, Pro 2, Max, or 3rd gen - anything that does Spatial Audio.
  Regular AirPods 1/2 have no gyro and will never produce data.
- The AirPods connected **and** selected as the audio output device

## Why the .app bundle

macOS will not hand motion data to a process that has no
`NSMotionUsageDescription` in its bundle - it kills it outright with a TCC
privacy violation. A script run from a terminal has no bundle of its own, and
TCC blames the terminal, which has no such key either.

So `build_app.sh` assembles a minimal `HeadTrack.app` whose executable is a copy
of the real Python interpreter, and `run.sh` starts it through LaunchServices
(`open`) so the app is the one making the request. Output comes back to your
terminal through a FIFO, so the dashboard still works normally. macOS asks for
motion permission once, on the first run.

The tracker also has to hold off App Nap (`beginActivityWithOptions:`). Without
it, macOS throttles the background app the moment you click another window and
CoreMotion stops delivering - the dashboard just freezes, with no error.

The first launch after a rebuild can take ten seconds or so while LaunchServices
registers the bundle; after that it starts quickly.

If you ever deny the prompt, re-allow it in
**System Settings > Privacy & Security > Motion & Fitness**.

## Files

- `headtrack.py` - the tracker: CoreMotion subscription, dashboard, CSV/JSON output, 3D server
- `viz.html` - the 3D head (geometry, rotation, and renderer, ~200 lines of plain JS)
- `run.sh` - sets everything up and launches it the way macOS requires
- `build_app.sh` - builds the `HeadTrack.app` wrapper
