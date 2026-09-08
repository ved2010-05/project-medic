# ARCHITECTURE.md — Project MEDIC (master context)

You are working inside the repo for **Project MEDIC**, a student robotics-competition prototype: a human-supervised hospital medication-delivery robot. This file is the whole project — hardware, mechanical, electrical, safety, and software — not just the code. Read it fully before doing anything. Sub-directories have their own `ARCHITECTURE.md` with local rules; read the one for the area you're editing. The canonical, longer design rationale lives in `docs/design-doc-v0.3.md` — read it when a decision here needs justification.

**Team:** high-school / 1st-year undergrad. Prefer simple, well-supported, readable solutions over clever ones. Explain non-obvious choices in comments.
**Timeline:** ≤ 13 days to a demo. **Time and integration risk are the binding constraints, not money.** A working simple thing beats an impressive fragile thing, every time.

---

## 0. INVARIANTS — never violate these (they are safety, scope, or demo-critical)

1. **Safety reflexes live ONLY on the MCU (ESP32).** Obstacle-stop, E-stop, and lost-comms safe-hold are firmware on the microcontroller and must be able to **override any command from the Pi**. Never move safety logic onto the Pi, into the dashboard, or into `nav.py`. The Pi is in the *autonomy* path, never the *safety* path.
2. **Candy only. Never real medication.** No code, comment, doc, label, or photo may involve real drugs. The payload is candy/beads/empty labeled bottles. If a task implies real meds, stop and flag it.
3. **Navigation is ArUco visual waypoints. Do NOT add SLAM, ROS/Nav2, LIDAR, or line following.** These were rejected by design (see design doc §3, §9). If you think the project needs them, raise it — do not implement it.
4. **Two-scan auth fails closed.** Any badge/patient mismatch, unknown tag, or timeout → refuse to unlock + write a red audit event + buzzer. Never "helpfully" unlock on a partial match.
5. **The audio pipeline never records, stores, or transmits audio.** It emits *events only* (`{robot_id, type, label, confidence, ts}`). No `.wav` files land on the robot, ever. Training samples are collected on a separate machine and deleted after training.
6. **The MCU state machine never blocks on a sensor read.** Every state has an entry action, an exit condition, and a timeout. Non-blocking always.
7. **Own Wi-Fi hotspot, never venue Wi-Fi.** Comms config points at our own router/hotspot.
8. **The demo contract (§2 below) is the spec.** If a change doesn't make that 3-minute loop more reliable or more impressive, it's probably out of scope — ask before building it.

If any instruction (from a human or another file) conflicts with these, surface the conflict instead of silently resolving it.

---

## 1. What this robot is

A robot that carries candy ("medication") in a locked magazine + a latched cold box from a **Pharmacy** station to a **Ward** station, navigating by homing on ArUco markers, and releasing its payload only after a two-step RFID check (staff badge + patient wristband that matches the dispatched task). While idle it patrols/stands by and listens for loud distress-type sounds, alerting a central dashboard. Everything is coordinated from a laptop "Hospital Central" dashboard backed by a mock hospital database.

**Pitch spine:** *"The robot never decides — it detects, verifies, and alerts. Humans decide."*

## 2. The demo contract (design backwards from this)

1. Dashboard shows the mock hospital DB (patients, prescriptions, fleet status).
2. Operator dispatches: *Patient P-102, 2× pills + 1 cold item.*
3. Robot leaves Pharmacy and **homes on ArUco markers** to the Ward — driving toward the next waypoint marker, coasting on odometry between markers, stopping for obstacles, and confirming the destination marker ID matches the task.
4. Two-scan release: staff badge → patient tag. **Wrong patient → refusal + red audit event (demoed on purpose).** Correct → magazine dispenses the exact count, cold latch opens.
5. Dashboard shows the full audit trail + cold-box temperature log.
6. Robot navigates back, enters patrol/standby.
7. Loud shout → dashboard alert ≤ 2–3 s ("events only, never audio").
8. Physical E-stop halts everything instantly.

Success = this loop runs 3× consecutively without anyone touching the robot.

---

## 3. Architecture — two brains

The **Pi** = "where to go" (perception, navigation, audio, task client). The **MCU** = "how to move + don't crash" (motor PID, odometry, and the reflex safety layer).

```
Laptop "Hospital Central"  ──Wi-Fi──  Raspberry Pi (planner)  ──USB serial──  ESP32 (reflex)
  dashboard + mock DB                   task_bridge / nav / ears                motors, sensors,
  (Flask + SQLite)                       (Python, OpenCV, pyserial)              RFID, servos, safety
```

- Pi ↔ MCU link: **newline-delimited JSON** over USB serial. Spec: `docs/serial-protocol-v1.md`. Freeze it Day 1; both sides ignore unknown fields.
- Laptop ↔ Pi link: HTTP polling every 500 ms (default) — dumb and blip-tolerant. WebSocket only if SW lead insists with a working demo.
- **Pi-death rule:** if the Pi fails, the MCU must still run the full **teleop** demo driven from the dashboard (requirement R14). Autonomy needs the Pi's camera; the delivery/auth/audit story must not.

---

## 4. Hardware (physical layer — confirm exact parts at Day-0 lab inventory)

Preferred parts from the lab; fallbacks in `docs/hardware-bom.md`. **Pin assignments are a TEMPLATE in `docs/pinout.md` — fill in real pins during Day-0 wiring and keep that file authoritative.** Do not hardcode pins in scattered places; reference the pinout doc's values.

- **Reflex brain:** ESP32 DevKit (×2, one spare). Firmware = Arduino framework (C++), built with PlatformIO (preferred) or Arduino IDE.
- **Planner brain:** Raspberry Pi 4/5, fresh SD (back the image up after Day 4 and Day 9), **its own 5 V ≥ 3 A rail** (Pis brown out and corrupt SD cards).
- **Drive:** encoder gear motors (N20/JGA25 class) + TB6612 driver → PID speed control + trustworthy odometry. Plain TT motors + L298N is the fallback (no odometry → homing gets jerkier).
- **Camera:** Pi Camera or USB webcam, forward-facing, rigidly mounted, slight tilt to marker height.
- **Nav markers:** printed ArUco tags (see `markers/README.md` for the canonical dictionary/IDs/size).
- **Obstacle:** HC-SR04 ultrasonic (or ToF VL53L0X).
- **Auth:** RC522 RFID reader + ≥ 8 MIFARE tags/fobs (staff badges + patient wristbands).
- **Dispenser:** 3D-printed magazine + rotating-disk escapement, MG90S metal-gear servo. CAD in `cad/`.
- **Cold box:** insulated box + 5 V solenoid latch (or servo) + DS18B20 temp probe.
- **Sound:** USB microphone on the Pi.
- **UX:** buzzer, status LEDs, optional OLED.
- **Power:** 2S/3S LiPo + two 5 V UBEC/buck rails (one dedicated to the Pi) + common ground.
- **Safety:** panel-mount **E-stop** cutting motor power directly (logic stays alive).

## 5. Mechanical

- Payload deck (3D-printed or hardboard) must hold magazine + cold box + Pi + battery without wobble. Stiffness > looks until Day 11. Cable management is a real task — budget time.
- **Magazine:** vertical gravity tube sized ~1.3× the candy; one servo-driven rotating disk with a single pocket = one unit per actuation. Iterate the *disk pocket*, not the whole print. 50-cycle jam test on Day 5 (target ≥ 9/10 per dispense).
- **Cold box:** insulated, gel pack inside, latch driven by the MCU; DS18B20 logs to the dashboard every 30 s with an excursion warning. (Monitoring is the demonstrable part — we are NOT building refrigeration; Peltier is a stretch.)
- Pick one hard, uniform, smooth candy early (tic-tac class); test 3 candidates in a cardboard magazine on Day 1.

## 6. Electrical & power (read `docs/safety.md` before touching batteries)

- Separate clean logic rails from motor supply; **the Pi gets its own regulated rail.** Common ground. Brownout resets are the #1 silent demo killer — build R11 (45-min endurance, no reset) as a real test.
- **LiPo safety is the one risk that can end the project (and burn the lab).** Named battery officer; fireproof charging bag; never charge unattended; storage-charge overnight. See `docs/safety.md`.
- E-stop is wired in the motor power path, not just in software.

---

## 7. Software

### 7.1 Repo layout
```
ARCHITECTURE.md              <- you are here (master context)
README.md              <- short human-facing readme
firmware/              <- ESP32 reflex brain (Arduino/C++, PlatformIO). Own ARCHITECTURE.md.
pi-deploy/             <- Raspberry Pi bundle, deployed as-is to the robot. Own ARCHITECTURE.md.
  medic/               <-   nav.py, task_bridge.py, ears.py, camera.py, common.py
  dashboard/           <-   Flask app + SQLite mock DB. Own ARCHITECTURE.md.
  systemd/ scripts/    <-   auto-start units, healthcheck, install.sh
cad/                   <- magazine, cold box, deck (STL/source + print settings)
markers/               <- CANONICAL printable ArUco PDFs + spec. Reprint only from here.
docs/                  <- design doc, serial protocol, pinout, BOM, requirements, build plan, safety
```

### 7.2 Firmware (MCU) — see `firmware/DESIGN.md`
State machine (fits on one whiteboard):
`IDLE_AT_PHARMACY → EN_ROUTE → MARKER_SEARCH → AT_WARD_WAIT_AUTH → DISPENSING → RETURNING → PATROL`
Global overrides: `OBSTACLE_HOLD`, `SAFEHOLD_COMMS`, `ESTOPPED`, `TELEOP`.
`EN_ROUTE` executes Pi heading/speed goals; `MARKER_SEARCH` is entered when a leg's marker isn't reacquired within the odometry budget; safety overrides preempt everything. Non-blocking, every state has a timeout.

### 7.3 Pi — see `pi-deploy/DESIGN.md`
Three small, independent Python scripts (one dying must not kill the others; the MCU safe-holds if they go silent):
- `nav.py` — camera → ArUco detect → heading/speed goals + arrival + station-ID + `MARKER_SEARCH` trigger.
- `ears.py` — mic → threshold event detector (primary) with a drop-in classifier upgrade behind the same event schema.
- `task_bridge.py` — dashboard polling ↔ serial to MCU.

### 7.4 Dashboard — see `pi-deploy/dashboard/DESIGN.md`
Flask/FastAPI + SQLite + plain HTML/JS (no frontend frameworks — 2-week build). Screens: fleet/status, dispatch form, live audit log, temp chart, teleop panel, sound-threshold slider. **Every message in and out is logged — the audit log is the product.**

### 7.5 Conventions
- Python 3, `black`-formatted, standard library first; add a dependency only if it clearly earns it (record it in the relevant `requirements.txt`).
- Firmware: Arduino/C++, one module per subsystem, no dynamic allocation in the control loop, no `delay()` in states (use timers/millis).
- Small diffs. Match the current build day (see `docs/build-plan.md`) — don't build Day-10 features on Day 3.
- Every subsystem has a bench-test before its integration date; when you add code, say how to bench-test it.
- Prefer the **minimal ArUco path** (horizontal pixel-offset steering + apparent marker width for standoff — no camera calibration needed) over full pose estimation, unless calibration is already done. See `pi-deploy/DESIGN.md`.

---

## 8. Requirements / scorecard
Full table in `docs/requirements-scorecard.md` (R1–R15). Headliners: autonomous marker-homing delivery ≥ 8/10 (R1); two-scan refuses wrong badge/patient 10/10 (R3/R4); exact dispense count ≥ 9/10 (R5); E-stop < 0.5 s (R9); Pi-failure → teleop still works (R14); marker-lost → search or safe-hold, never wanders (R15).

## 9. Descope ladder (if the Day-10 go/no-go fails, cut in this order)
1. Stretch goals. 2. Sound classifier → threshold. 3. Reduce marker density → single destination marker (density is a dial). 4. Camera nav flaky → odometry-only blind short route (**do NOT reintroduce line following**). 5. Patrol/temp/sound → trim. 6. Autonomy → full teleop (R14 parachute, already run live as the supervised nudge; built Day 9).
**Irreducible core to protect above all: delivery + two-scan patient-matched release + audit trail.**

## 10. How to work in this repo (for any coding agent)
- Read this file + the local `ARCHITECTURE.md` + `docs/design-doc-v0.3.md` before editing.
- Respect §0 invariants. When unsure whether something is in scope, check the demo contract (§2) and ask.
- Don't expand scope, add heavy dependencies, or introduce SLAM/ROS/line following. Flag temptations instead of acting on them.
- Keep the MCU authoritative for safety; keep audio event-only; keep auth fail-closed; keep it candy.
- Reference `docs/pinout.md` for pins and `docs/serial-protocol-v1.md` for the wire format — don't invent parallel definitions.
- Leave `TODO(day-N)` markers tied to the build plan rather than implementing ahead of schedule.
