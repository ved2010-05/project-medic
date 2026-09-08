# ARCHITECTURE.md — pi/ (Raspberry Pi "planner brain")

Local context for the Pi. Read `ARCHITECTURE.md` first. The Pi is in the **autonomy** path, never the **safety** path — never put obstacle/E-stop/comms-hold logic here.

## Stack
- Python 3, `black`-formatted. OpenCV (`cv2`, `cv2.aruco`) for markers, `pyserial` for the MCU link, `sounddevice`/`pyaudio` for the mic. Keep `requirements.txt` current.
- Three independent scripts launched via tmux/systemd; each posts to the dashboard on its own. One crashing must not take the others down — and if `nav`/`task_bridge` go silent, the MCU safe-holds anyway.

## nav.py — ArUco visual waypoint navigation
Loop: grab frame → detect target marker (dictionary/IDs in `markers/README.md`) → compute a steering goal → send to MCU → detect arrival → confirm marker ID == task destination → hand off.

**Prefer the minimal, calibration-free path first (more reliable for our timeline):**
- Steering = horizontal pixel offset of the marker centre from image centre → proportional heading goal. No camera intrinsics needed.
- Standoff = apparent marker width in pixels vs a target width → stop when "close enough." No pose needed.
- Only move to full `solvePnP` pose (needs a one-time chessboard **camera calibration** producing `cameraMatrix`/`distCoeffs`) if the minimal path proves inadequate. If you do calibrate, save the intrinsics to `pi/calib.npz` and load it — don't hardcode.

Behaviours:
- **Marker in view:** emit `drive` goals (heading + modest speed) toward it.
- **No marker in view:** tell the MCU to coast on the last heading for a bounded odometry distance, then trigger `MARKER_SEARCH` (slow in-place rotate to reacquire). If not reacquired within the angle/time budget → command stop and post a "marker lost" alert. **Never command open-ended wandering.**
- **Arrival:** stop at standoff, verify ID, then signal `AT_WARD_WAIT_AUTH`.

Reliability: 640×480, **lock exposure/white balance** (auto-exposure hunting + glare are the top failure modes), drive slow / detect-creep-detect to beat motion blur.

## ears.py — sound alerting (event-only)
- Rolling amplitude/band-energy over a window; fire when over threshold for a min duration, with a refractory period. Threshold is settable from the dashboard.
- Emit only `{"robot_id":..,"type":"sound","label":"loud","confidence":..,"ts":..}`. **Never write, buffer to disk, or transmit raw audio.**
- Optional upgrade (Days 8–9 only): a small {shout,clap,alarm,background} classifier that emits the *same* schema so the dashboard doesn't change. Ship the threshold version if bench accuracy < ~85% by end of Day 9.
- Gate the mic to standby/patrol and at-station waits (motor noise otherwise).

## task_bridge.py — dashboard ↔ MCU
- Poll the dashboard every 500 ms for the active task; translate to serial commands (`dispense`, `latch`, teleop passthrough); relay MCU telemetry/events (odom, rfid, temp, state, estop) back to the dashboard.
- Auth decision lives here or in the dashboard (not the MCU): compare scanned staff+patient UIDs against the task; only then send `dispense`/`latch`. Fail closed + log on mismatch/timeout.

## Bench tests to state when adding code
ArUco detected + centre/offset printed live; steering goal sign is correct (marker left → turn left); marker-lost path triggers search then stop; a clap fires exactly one event within ~1 s; serial round-trip to MCU works.
