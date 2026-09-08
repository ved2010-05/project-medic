# Project MEDIC

> ### ⚠️ Not a medical device. Not for use with medication.
>
> This is a **student robotics prototype**. It carries **candy** as a stand-in
> payload. It has not been validated, certified, or reviewed by anyone, and it
> must never be used to handle, store, transport, or dispense real
> pharmaceuticals — or anything a person would ingest on the basis of it
> having been delivered "correctly".
>
> The hospital framing is a **design exercise**: it exists to make the
> engineering problem concrete (why an audit trail matters, why a release
> should fail closed). Nothing here is clinical guidance, and no part of it
> was designed to a medical-device standard.
>
> It also drives motors and runs on lithium-polymer batteries. If you build
> from it, read [`docs/safety.md`](docs/safety.md) first — LiPo mishandling is
> a fire risk, not a theoretical one.

**A human-supervised hospital delivery robot that navigates by looking at printed squares — and writes down everything it does.**

MEDIC carries a payload from a pharmacy bench to a ward bench, homing on printed
ArUco markers one at a time, and logs every event to a dashboard. It is a
student competition prototype built on a hard constraint: *no wheel encoders, no
LIDAR, no SLAM.*

> The robot never decides — it detects, verifies, and alerts. Humans decide.

The payload is **candy**. Never medication — not in the hardware, not in the
code, not in the seed data.

---

## The loop it is built around

1. Operator dispatches a delivery from the dashboard — patient, destination, count.
2. The dashboard plans a route across a marker map the robot learned by driving around.
3. The robot homes on each marker in turn, camera correcting its heading every frame.
4. On arrival it **confirms the marker it docked on is the destination the task named**, then dispenses exactly the requested count.
5. Every step lands in a timestamped audit trail.

Everything below exists to make that loop reliable.

---

## Architecture: two brains, one rule

```
Browser                  Raspberry Pi 4                ESP32
dashboard UI    --HTTP--  "where to go"    --USB--   "how to move"
                          camera, routing   serial     motors, safety
                          may crash                    must not crash
```

**The Pi plans. The ESP32 moves and enforces safety — and the ESP32 always wins.**

That split is the most important decision in the project. Python on Linux is
convenient but not punctual: the OS can pause it, the garbage collector can
pause it, a network call can block it. That is fine for deciding *where to
drive* and unacceptable for deciding *when to stop*.

So the microcontroller owns every safety reflex, and a motion command from the
Pi is **discarded, not queued**, while an override is active. Queuing would mean
the robot lurches forward the instant the override clears, acting on a goal
computed seconds ago from a position it is no longer in.

The consequence worth stating plainly: **the Pi is allowed to die.** If Python
crashes or Wi-Fi drops, the ESP32 notices the silence and stops the robot within
two seconds, on its own.

The two halves speak newline-delimited JSON over USB serial — chosen because you
can debug it with your eyes:

```json
{"t":"drive","hdg":-4.2,"spd":0.55}
{"t":"stop","at":"ward","count":3}
{"t":"ack","of":"dispense","ok":true,"n":3,"why":""}
```

Both sides ignore fields they do not recognise, so the protocol can grow without
a flag-day. See [`docs/serial-protocol-v1.md`](docs/serial-protocol-v1.md).

---

## Navigation: two numbers, no calibration

An ArUco marker is a printed square encoding a number. From each frame the robot
extracts exactly two measurements:

| Measurement | Used for |
|---|---|
| **Horizontal pixel offset** of the marker's centre | Heading error — right of centre means steer right |
| **Apparent width** in pixels | Distance — wider is closer; at 150 px it has arrived |

No camera calibration, no lens distortion model, no 3D pose estimation, no
coordinate frames. Full `solvePnP` pose was available and deliberately not used:
it needs an intrinsics calibration, and that afternoon buys nothing the simpler
method does not already deliver.

**The camera is the feedback loop.** Every frame re-measures the error and
corrects it, so open-loop motor drift is cancelled continuously rather than
accumulating. That is what makes the next section survivable.

---

## Designing around a sensor that isn't there

The robot has **no wheel encoders**. No odometry, no PID, no distance feedback
of any kind. That single absence shapes more of the design than anything else:

| With encoders | What this robot does instead |
|---|---|
| "Drive forward 2 metres" | "Drive forward for 1.5 **seconds**" |
| "Rotate 90 degrees" | "Rotate for 8 **seconds**" |
| "Spin one full turn" | "Spin for 14 **seconds**" |
| Position estimate | None. It knows only "I can see marker 15" |

Everything blind is bounded by wall-clock time. That is fine while a marker is
visible and fragile in the gaps between them — which makes **marker density**
the reliability dial, and the thing worth spending effort on.

Losing a marker follows a strict escalation that never ends in wandering:

```
marker visible  ->  HOMING    steer toward it, every frame
marker lost     ->  COAST     straight on, blind, 1.5 s
still lost      ->  SEARCH    rotate in place, up to 8 s
still lost      ->  LOST      stop. raise an alert. do not hunt.
```

Full reasoning in [`docs/no-encoder-nav.md`](docs/no-encoder-nav.md).

---

## The map is a graph, not a floor plan

Markers alone say "drive at that square". They do not say that the pharmacy
connects to Room 4C through the corridor. So the robot builds a **topological
graph** — nodes and links, no coordinates, no walls, no occupancy grid:

```
10 -- 15 -- 20          10 -> 23?
       |                breadth-first search
       22 -- 23         10 -> 15 -> 22 -> 23   (3 hops)
```

An edge means *"standing at one, the robot could see the other"* — a good proxy
for "it can drive between them", and something it can observe for itself rather
than be told. Because co-visibility is only a proxy (you can see a marker across
a railing you cannot drive through), **an edge is not trusted until it has been
observed three separate times.**

Two ways to build it:

- **Teach mode** — you drive or carry the robot; it records links and asks you to name each marker.
- **Autonomous mapping** — drive to a marker, stop, spin a full turn recording everything visible from there, drive to the first unvisited one, repeat.

The spin matters: standing still you usually see one marker; turn around and
others appear. It rotates in **short bursts with pauses**, not smoothly — see
the measurement below for why.

---

## Measured, not assumed

Numbers taken from the actual hardware, not a datasheet:

| Finding | Value | Why it mattered |
|---|---|---|
| Marker still decodes down to | **12 px** wide | Detection range was never the bottleneck |
| Detection fails past | **~10 px** of motion smear | This *was* the bottleneck |
| Room illumination | **46.9 lux** | Forces a 66 ms exposure (an office is 300-500) |
| Resulting rotation limit | **~13 deg/s** | Firmware was set to spin at roughly 3x that |
| Cold boot to working dashboard | **40 s** | Verified by rebooting, not by reading a config flag |
| Deploy integrity | **29/29 files** byte-identical | A checksum sweep caught one file that had never been copied |

The fourth row is the one worth pointing at. While searching for a lost marker
the robot would have rotated too fast for its own camera to resolve anything,
then reported "marker lost" — a failure that looks like a detection bug and is
really a lighting problem. It surfaced by measuring the smear threshold
directly rather than by guessing at it.

---

## Failing honestly

Mid-test, the serial link was cut while the magazine was dispensing. The
firmware's response:

```json
{"t":"ack","of":"dispense","ok":false,"n":2,"why":"short_count"}
```

Two of three, reported **as a failure**. It did not claim success and it did not
silently lose a unit.

That is by construction: a unit is counted only when the escapement disk returns
to home, never when it sets off. So the number in the audit log is the number
that actually landed. A system that admits a short count is worth more than one
that always says OK — especially one pretending to move medication.

---

## Safety model

Every reflex lives on the microcontroller. None on the Pi, none in the dashboard.

| Layer | Mechanism | Status |
|---|---|---|
| Physical E-stop | Cuts motor power in hardware, independent of software | Working |
| Comms watchdog | No valid frame from the Pi for 2 s, stop and hold | Working |
| State timeouts | Every state has a maximum duration; nothing runs forever | Working |
| Obstacle stop | Ultrasonic, stop at 25 cm / resume at 35 cm | **Sensor removed** |
| E-stop sense | Lets the MCU *log* an E-stop press | Not wired |

Two details that took thought:

**The obstacle switch is compile-time, not a command.** Disabling obstacle
detection requires editing `config.h` and reflashing. It is deliberately not
something the Pi can request over serial — otherwise a dashboard bug or a
malformed packet could disable the robot's brakes remotely, which would put the
Pi in the safety path.

**Absent hardware reports as absent, never as fine.** When the ultrasonic was
first switched off, the firmware kept emitting `{"d":-1.0,"blocked":false}`
several times a second. On a dashboard that reads as *a working sensor seeing a
clear path* — the most dangerous message it could send. It now emits nothing
about obstacles, plus an explicit warning every ten seconds. Silence is honest.

---

## Current status

Being straight about this, because a robot demo that overclaims is exactly the
failure mode the project is built to avoid.

**Working, verified on hardware**

- Marker homing with bounded coast / search / stop-and-alert behaviour
- Learned marker map with any-to-any breadth-first routing
- Autonomous rotate-and-survey mapping
- Dispense-on-arrival, confirmed end to end (`ok:true, n:3`)
- Safety preemption mid-dispense, reporting the true short count
- Flask dashboard, live camera feed, full audit trail
- Auto-start on boot: 40 s, no login, Wi-Fi reconnects itself

**Not working / not fitted**

- **Drive motors have not been confirmed turning** under load
- Obstacle sensor removed, so the requirement it covers cannot pass
- RFID reader removed, so the two-scan patient check is gone (see below)
- No microphone, cold-box latch, or temperature probe
- The full demo loop has **not** yet run start to finish

---

## Decisions, including the ones that hurt

| Considered | Chosen | Why |
|---|---|---|
| SLAM / LIDAR / ROS | ArUco waypoints | A short build cannot absorb a mapping stack. Markers are debuggable by eye. |
| Line following | Marker homing | Rejected even as a fallback: it solves an easier, different problem and would not demonstrate visual navigation. |
| `solvePnP` pose estimation | Pixel offset + apparent width | Needs calibration; the simple method proved sufficient. |
| Encoders + PID | Open loop + camera feedback | Not available. The camera closes the loop instead. |
| Two-scan RFID release | Arrival triggers dispense | **A capability loss, not a simplification.** |

That last row deserves the honesty. The original design refused to release a
payload until a staff badge *and* a patient wristband both matched the dispatched
task — wrong patient meant refusal, a red audit event, and a buzzer. When the
RFID reader came out of the build, that check went with it, and with it two
scored requirements and the most interesting thing the robot did. The
fail-closed *structure* survives in the firmware; the input to it does not.

---

## Layout

```
firmware/     ESP32 reflex brain: motion, safety, dispenser (Arduino/C++)
  src/        canonical source; config.h is the single source of pin truth
  arduino/    generated sketch folder for the Arduino IDE
pi-deploy/    what actually runs on the robot
  medic/      nav.py, task_bridge.py, ears.py, camera.py, common.py
  dashboard/  Flask app + SQLite schema + seed data
  systemd/    unit files for auto-start on boot
  scripts/    healthcheck, ultrasonic check, run-all
markers/      canonical ArUco generator + printable PDFs; reprint ONLY from here
docs/         design doc, serial protocol, pinout, BOM, requirements, safety
cad/          magazine, cold box, deck
```

Start with [`ARCHITECTURE.md`](ARCHITECTURE.md) for full project context, or
[`docs/design-doc-v0.3.md`](docs/design-doc-v0.3.md) for the rationale behind
each decision.

---

## Running it

**Print the markers** — from the canonical generator only. A rescaled marker
still *looks* right and detects badly at range, which then gets blamed on the
software.

```bash
python markers/generate_markers.py --check
```

A4, one per page, **100% scale, never "fit to page"**, matte paper. Distance is
measured from apparent size, so a rescaled print makes the robot stop in the
wrong place.

**Deploy to the Pi**

```bash
cd pi-deploy && ./install.sh
```

**Teach the floor** (or `--map` to let it drive itself)

```bash
sudo systemctl stop medic-nav
.venv/bin/python -m medic.nav --teach
```

**Flash the firmware** — open `firmware/arduino/medic_fw/medic_fw.ino`, board
"ESP32 Dev Module", upload speed 115200.

---

## What I would do differently

- **Fit encoders.** Every blind manoeuvre is timed rather than measured, and every timing constant needs recalibrating when the battery sags.
- **Fix the lighting before tuning anything.** Hours went into detection reliability that were really an illumination problem: 47 lux forces a 66 ms exposure, and the resulting motion blur set the speed limit for both driving and searching.
- **Give the robot an out-of-band console on day one.** It knew exactly one Wi-Fi network, so every change of room made it unreachable. A USB-gadget login would have cost twenty minutes and saved many hours.
- **Keep the RFID reader.** Losing it cost the most compelling behaviour in the build.

---

## Scope, and what this is not

Restating this at the end because the framing invites the wrong reading:

- **Not a medical device.** No regulatory review, no clinical validation, no testing against any standard. The word "medication" appears in this repo only to describe what the candy is *pretending* to be.
- **Not a safety-critical system.** The safety architecture here is a design exercise in where reflexes should live. Real safety-critical work needs hazard analysis, redundancy, and formal verification — none of which this has.
- **Not deployable.** It navigates a room with printed paper markers under supervision. It has no concept of people, doors, lifts, or anything else in a real building.
- **Not medical advice.** Nothing in this repository is guidance about medication, dosing, storage, or handling.

If you are here for the engineering — the two-brain split, calibration-free
visual homing, or designing around a missing sensor — that all transfers. The
hospital story does not.

---

## License

MIT — see [LICENSE](LICENSE). The license includes an explicit
"not a medical device" clause, and the MIT warranty disclaimer applies in full:
this is provided as is, with no fitness for any particular purpose.
