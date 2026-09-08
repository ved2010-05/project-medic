# Project MEDIC — Internal Design & Build Doc (v0.3, pre-build)

**Status:** DRAFT — internal team use only. Purpose: lock the idea, kill scope creep, agree exactly what we build before anyone touches hardware.
**Timeline:** ≤ 13 days from kickoff to demo-ready.
**Constraints:** Budget is NOT a constraint (well-stocked lab). Binding constraints are **time, integration risk, and our skill ceiling** (HS / 1st-year UG). A bigger parts bin buys reliability, not extra days.
**Rule zero:** If a feature is not in "Prototype scope," we do not build it. We argue about the doc, not mid-build.
**Rule zero-point-five:** Having a part in the lab is not a reason to use it. Every component must earn its place by making the §2 demo more reliable or more impressive per integration-hour it costs.

---

## 1. One-liner

A human-supervised hospital delivery robot that carries medication in locked, authenticated compartments from a "pharmacy" station to a "ward" station, navigating by homing on visual markers, dispensing only after a two-step ID check, and passively listening for distress-type sounds while idle — coordinated from a live central dashboard playing the role of the hospital's system.

Pitch spine for judges: **"The robot never decides — it detects, verifies, and alerts. Humans decide."**

---

## 2. The demo story (design backwards from this)

Three-minute loop — this is the contract everything serves:

1. Dashboard shows mock hospital DB (patients, prescriptions, fleet status).
2. Operator dispatches: *Patient P-102, 2× pills + 1 cold item.*
3. Robot leaves Pharmacy and **navigates by homing on ArUco markers** — driving toward the marker for its next waypoint, coasting on odometry between markers, stopping for obstacles en route. It docks at the Ward marker and confirms the marker ID matches the task's destination.
4. Two-scan release: staff RFID badge, then patient wristband tag. **Wrong patient → refusal + red audit event (we demo this on purpose).** Correct → magazine dispenses exact count, cold-latch opens.
5. Dashboard shows full audit trail + cold-box temperature log.
6. Robot navigates back to Pharmacy, enters patrol/standby.
7. Loud shout near robot → alert on dashboard ≤ 2–3 s, with the line: *"It never records audio — events only."*
8. Physical E-stop halts it instantly.

If this loop runs 3× consecutively untouched, we're done. Everything else is garnish.

---

## 3. Vision → prototype mapping (the honest table)

| Full vision | Prototype (≤2 weeks, lab-grade) | Framing |
|---|---|---|
| Magazine-style pill dispensing | 3D-printed gravity magazine + servo disk escapement, candy rounds, count-on-command | "Same mechanism as pharma cassettes, scaled" |
| Non-pill meds + cold chain | Latched insulated box, gel pack, DS18B20 logging + excursion warning (Peltier = stretch) | "Monitored cold chain with excursion audit" |
| Hospital database connection | SQLite mock DB behind a REST API on the lab PC | "Same integration pattern as real EHR APIs" |
| Central dashboard, deliver anywhere | Web dashboard ↔ robot over our own Wi-Fi; **stations = ArUco waypoints, robot homes on markers, odometry between** | "Add a station = add a marker + a route entry" |
| Navigate the building | **Visual waypoint navigation:** camera homes on ArUco markers, wheel odometry bridges gaps, marker re-localizes and corrects drift each leg | "Real localization concept, honestly scoped — no line on the floor" |
| Human supervised, little autonomy | Literally true: executes dispatched tasks only; teleop override always available and run live as a supervised nudge | Core selling point |
| Patrol / stay / follow a person | Patrol = slow idle loop between markers; stay = idle at station; **follow = stretch (robot homes on an ArUco tag worn by an authorized escort)** | "Follow mode tracks a badge marker" |
| Always listening, react + alert | On-Pi audio pipeline: threshold detector (primary) with drop-in classifier upgrade (same event schema). No recording, no streaming, ever | "Privacy by design — labels, never audio" |
| Multiple robots per hospital | ONE robot; protocol carries `robot_id`; dashboard renders a fleet list. Second cloned robot = top stretch goal | "Fleet-ready protocol; unit 2 is a clone" |
| Five-rights verification | Two-scan RFID: staff badge + task's patient tag, else refuse + log | Our best feature; say it clearly |

**Explicitly NOT building, even though the lab can:** SLAM / free autonomous navigation, LIDAR anything, ROS/Nav2, line following (rejected by design), elevator anything, robotic arm/manipulation, real UV-C disinfection (genuine hazard — a labeled mock "sanitize" indicator LED only), real medication (NEVER — candy only), actual EHR integration, multi-floor.

---

## 4. System architecture (two-brain split)

The Pi owns **perception and navigation** ("where to go"). The MCU owns **motion and safety reflexes** ("how to move, and don't crash"). Safety never depends on the Pi.

```
            Our own Wi-Fi router / hotspot (never venue Wi-Fi)
   ┌──────────────────────────┴──────────────────────────┐
LAB PC / LAPTOP — "Hospital Central"                ROBOT
- Dashboard (Flask/FastAPI + plain HTML/JS)   ┌─ RASPBERRY PI 4/5 — "planner brain"
- Mock DB (SQLite): patients, scripts,        │   - Task client (HTTP polling, 500 ms)
  staff, tasks, events                        │   - NAV: camera → ArUco detect →
- Audit log, temp/event charts                │     pixel-offset heading/width → goals
- Teleop override panel                       │   - Audio pipeline (USB mic): threshold
- Fleet view (renders N robots; N=1)          │     detector → [upgrade: classifier]
                                              │   - Serial to MCU (USB, JSON lines)
                                              └─ ESP32 (or lab Arduino Mega) — "reflex brain"
                                                  - Motor PID (encoder gear motors + TB6612)
                                                  - Executes Pi heading/speed goals
                                                  - Odometry from encoders → reported to Pi
                                                  - Ultrasonic stop/resume (OVERRIDES Pi)
                                                  - RFID RC522, servos (dispenser + latch)
                                                  - DS18B20, buzzer, status LEDs
                                                  - E-stop sensing; watchdog: serial silence
                                                    > 2 s → stop, hold locks
```

Design rules for the split:
- **The MCU alone must be able to run the teleop fallback demo.** Autonomous navigation now needs the Pi's camera, so if the Pi dies on stage we drive the robot from the dashboard and the delivery/auth/audit story survives intact (see R14). This is the honest cost of visual nav: the Pi is now in the *autonomy* path — but never in the *safety* path.
- Safety reflexes (obstacle stop, E-stop, lost-comms hold) live entirely on the MCU and **override** any Pi command.
- Serial = newline-delimited JSON, ≤ 10 message types, versioned in the repo Day 1. Both sides ignore unknown fields.

### 4.1 Navigation — ArUco visual waypoints (the subsystem that changed)

**Course.** Lay the demo route as an L-shape at most. Mark it with ArUco tags on small stands at roughly camera height: Pharmacy (ID 10), Ward (ID 20), and a **corner marker** (ID 15) at any turn — angled toward the approach heading, with a redundant second marker, because a grazing-angle miss is worst exactly where the correction is needed. The corner marker re-localizes the robot mid-route so odometry drift never accumulates across a bend. **Marker density per leg is the tunable reliability dial** (corners-only up to one every 1–2 m), set empirically once real drift and detection range are measured (Day 5–7) — not fixed up front; a straight course can use just the two station markers.

**Division of labor.**
- *Pi ("where to go"):* grab a frame, detect the target marker, and compute a steering goal straight from the image — horizontal pixel-offset of the marker centre → heading, apparent marker width → standoff (calibration-free, no camera intrinsics). Emit a high-level goal to the MCU: "steer to heading θ, creep forward" or "you've arrived, stop." (`solvePnP` pose is a fallback only if the minimal path proves inadequate — it needs the printed marker size plus a camera calibration.)
- *MCU ("how to move"):* closed-loop PID drive executing those goals, reporting odometry back, and running the reflex safety layer that can override the Pi at any instant.

**Homing loop.** Detect target marker → compute heading error (pixel offset) + range proxy (apparent width) → send heading/speed → robot centers the marker and closes distance → stop at a set standoff (~20 cm) → confirm detected ID == task destination (this *is* the station-identity check, now fused into navigation) → hand off to auth.

**Between markers (none in view).** Coast on odometry along the last commanded heading for a bounded distance, then enter `MARKER_SEARCH`.

**`MARKER_SEARCH`.** Rotate slowly in place to reacquire the expected marker. If not found within a set angle/time budget → `SAFEHOLD` + dashboard alert "marker lost." **The robot never wanders looking for markers** — lost means stop and flag, always.

**Reliability measures (bake these in from day one, not at the end):**
- Big, matte-printed markers (~12–15 cm; larger for longer legs) — detection range scales with size.
- Camera at 640×480 with **locked exposure/white balance** — auto-exposure hunting and glare are the top killers of ArUco.
- Drive slowly (our standing principle) to avoid motion blur; when in doubt, use short detect–creep–detect steps rather than continuous fast motion.
- Camera rigidly mounted (no wobble = no false marker-position jumps), slight tilt matched to marker height.

**Obstacle interaction.** Ultrasonic stop always overrides (safety). An obstacle that also occludes the marker is fine: the MCU stops; when it clears, the Pi reacquires (or `MARKER_SEARCH` handles it). No special case needed.

**Consequences to internalize:** navigation is now our **#1 technical integration risk** and the Pi is mission-critical for autonomy. Basic single-marker homing must work by Day 3. Pi image backups and its dedicated power rail matter more than ever.

### 4.2 Medication payload
- **Magazine:** 3D-printed vertical magazine + single-servo rotating-disk escapement (candy-machine mechanism), printed to fit the chosen candy with ~1.3× clearance. Print v1 by Day 3; iterate the disk pocket, not the whole part. Metal-gear micro servo (MG90S class).
- **Cold box:** insulated box, servo or 5 V solenoid latch (solenoid = crisper lock, if the lab has them), gel pack, DS18B20 logging every 30 s with excursion warning on the dashboard. Peltier active cooling = stretch only.
- Locked except after successful two-scan auth at a verified station.

### 4.3 Authentication (two-scan flow)
RC522 + MIFARE tags. Staff badge scan → patient wristband scan → must match the active task → dispense/unlock. Any mismatch or 30 s timeout → relock, red audit event, buzzer pattern. Every scan logged either way. The audit log IS the product.

### 4.4 Sound alerting (scoped)
- USB microphone into the Pi (better SNR than an ADC mic module; DSP in Python where we're fastest).
- **Primary (must ship):** rolling amplitude/band-energy threshold with minimum-duration + refractory logic; threshold adjustable from the dashboard.
- **Upgrade (drop-in, attempt Days 8–9 only):** small classifier for {shout, clap, alarm-beep, background} (Edge Impulse or MFCC + scikit-learn on 15 min of our own samples). Must emit the same event schema `{robot_id, type, label, confidence, ts}` so the dashboard is pipeline-agnostic. If bench accuracy < ~85% by end of Day 9, ship the threshold version and stop.
- Active only in standby/patrol and while waiting at a station (motor-noise gate). **No audio recorded, stored, or transmitted** — training samples collected on a separate machine and deleted after training.

### 4.5 Dashboard & mock DB
Flask/FastAPI + plain HTML/JS on the lab PC. Screens: fleet/status, dispatch form, live audit log, temp chart, teleop panel, sound-threshold slider. SQLite seeded with fake patients. Every message logged. No frontend frameworks — this is a 2-week build.

### 4.6 Power & safety
- **Battery:** 2S/3S LiPo from the lab + a dedicated 5 V ≥ 3 A UBEC/buck for the **Pi alone** (Pis brown out and corrupt SD cards — its own rail), a second 5 V rail for MCU/logic/servos, motors straight off battery through the driver. Common ground. LiPo rules: fireproof bag, never charge unattended, storage-charge overnight, a named **battery officer**.
- **E-stop:** big physical switch cutting motor power directly; logic stays alive; mounted grabbable.
- **Watchdogs:** MCU stops + holds locks if Pi serial silent > 2 s; Pi flags dashboard if robot unreachable > 5 s; robot safe-holds if dashboard unreachable > 10 s while en route.
- **No real medication ever**, including photos. Candy/beads/empty labeled bottles only.

---

## 5. Lab pull list (Day 0 inventory, not a purchase order)

Walk the lab Day 0 with this; tick preferred, else fallback. Anything in neither column → order same-day (lead time, not cost, is the risk).

| Subsystem | Preferred (use if lab has it) | Fallback |
|---|---|---|
| High-level brain | Raspberry Pi 4/5 + fresh SD + USB mic + Pi/USB camera | (No autonomous nav without it — teleop-only build) |
| Reflex brain | ESP32 DevKit ×2 (one spare) | Arduino Mega + Wi-Fi via Pi |
| Drive | Encoder gear motors (N20/JGA25) + TB6612 | Plain TT motors + L298N (no PID; homing gets jerkier) |
| Chassis | Aluminum kit + 3D-printed payload deck | Acrylic kit + cardboard deck |
| Camera | Pi Camera v2/v3 or a decent USB webcam | phone-as-webcam over USB (last resort) |
| Nav markers | ArUco tags printed matte, ~12–15 cm, on stands | — (markers are the whole nav plan) |
| Obstacle | HC-SR04 (or lab ToF VL53L0X) | HC-SR04 |
| Auth | RC522 + ≥8 MIFARE tags/fobs | same |
| Dispenser | 3D-printed magazine + MG90S metal-gear servo | PET-bottle tube + SG90 |
| Cold box | Insulated box + 5 V solenoid latch + DS18B20 | Plastic box + SG90 latch + DS18B20 |
| Sound | USB microphone (Pi) | MAX9814 → ESP32 ADC |
| Power | 2S/3S LiPo + 5V/3A UBEC ×2 + charger + LiPo bag | 18650 ×2 + buck |
| Safety | Panel-mount E-stop / big rocker in motor path | same |
| Course | Matte marker prints + stands + tape measure | same |
| Spares kit (Day 12) | Spare ESP32, servo, motor, fuses, printed markers, charged batteries, USB cables | — |

---

## 6. Requirements (testable — Day 11 scorecard)

| ID | Requirement | Pass criterion |
|---|---|---|
| R1 | Dispatch → autonomous marker-homing delivery Pharmacy→Ward, untouched | ≥ 8/10 consecutive |
| R2 | Obstacle stop + resume | 10/10, stop at 15–35 cm |
| R3 | Refuse on wrong staff badge | 10/10, logged |
| R4 | Refuse on wrong patient tag | 10/10, logged |
| R5 | Correct two-scan → exact commanded count dispensed | ≥ 9/10 (jams = fail) |
| R6 | Cold-box temp logged | ≤ 60 s intervals, gap-free over 10 min |
| R7 | Sound alert latency | ≤ 3 s dashboard-visible, ≥ 8/10 |
| R8 | False alerts in quiet standby | ≤ 1 per 10 min |
| R9 | E-stop | Motors halt < 0.5 s, payload stays locked |
| R10 | Lost comms en route | Safe-hold ≤ 10 s, locks held, flagged on reconnect |
| R11 | Endurance | ≥ 45 min continuous loop, no brownout, no Pi reset |
| R12 | Audit completeness | Every scan/dispense/alert/command timestamped for a full run |
| R13 | Arrive at correct station (marker ID == task) | 10/10; wrong-station arrival → no auth, exception event |
| R14 | Pi-failure fallback | Pi unplugged → teleop demo runs end-to-end (delivery, auth, dispense, audit) |
| R15 | Marker lost / occluded | Coast-then-`MARKER_SEARCH`; reacquire or SAFEHOLD+alert; never wanders. 10/10 |

---

## 7. 13-day plan

4–6 people; overlap intentional; **integration starts Day 6**; **navigation proven early**. Daily 10-min standup, non-negotiable.

| Day | Milestone | Owner |
|---|---|---|
| 0 | Kickoff: read this doc, settle §12, **lab inventory vs §5**, order gaps, repo + group, flash Pi, appoint battery officer + integration lead | All |
| 1 | Arena/route laid out; markers printed + mounted; **camera detects ArUco + prints centre-offset/width on the bench** (calibration-free, no pose); mock DB schema + seed; serial protocol spec v1 committed; magazine v0 (cardboard) validates candy | SW + FW + Mech |
| 2 | Chassis rolling under MCU PID; odometry reports to Pi; Flask serves fleet page with fake data; Pi↔MCU serial echo passes | EE + FW + SW |
| 3 | **Basic single-marker homing works:** robot drives to one visible marker and stops at standoff; magazine v1 printed | FW + EE |
| 4 | **Multi-marker route** (Pharmacy→corner→Ward) with odometry coast + `MARKER_SEARCH` reacquire; RFID two-scan passes bench; dispatch form creates tasks; Pi forwards goals | FW + SW |
| 5 | Magazine v1 on robot: 50-cycle jam test ≥ 9/10 per dispense; cold box latched + DS18B20 streaming | Mech + EE |
| 6 | **Integration begins:** dispatch → navigate → arrive → auth → dispense → return, on the real course | All |
| 7 | Full loop end-to-end at least once; failure list on the whiteboard | All |
| 8 | Audio primary (threshold on Pi) → dashboard alert + slider; start classifier sample collection | SW + FW |
| 9 | **Nav robustness pass:** lighting/exposure lock, motion-blur/creep tuning, marker-lost (R15), obstacle tuning; lost-comms + **Pi-fail→teleop fallback (R14)**; teleop panel run live as the supervised nudge; classifier go/no-go (≥85% or ship threshold) | All |
| 10 | **GATE (go/no-go):** informal run of R1–R15, including a **blind unassisted nav block** (supervised nudge disabled, a non-driver logging): ≥7/10 → supervised autonomy is the headline, else teleop-primary with autonomy as one leg. Any red → descope ladder (§9); stretch stays locked. All green → pick ONE stretch | All |
| 11 | Dress rehearsals ×5 with metrics recorded; perfboard/loom wiring; Pi image backup; battery rotation plan | All |
| 12 | **FEATURE FREEZE.** Demo script practiced until boring; poster/slides use measured numbers; pack spares kit | All |
| 13 | Buffer / travel / sleep | All |

Roles: **Mech** (chassis, magazine, cold box, mounting), **EE** (wiring, power, sensors), **FW** (MCU PID + serial + state machine), **SW** (dashboard, DB, Pi-side nav/audio), **Integration/Demo lead** (owns §2, runs gates, scope veto). People hold two hats; the lead's veto is real.

---

## 8. Firmware & software shape

**MCU state machine (fits on one whiteboard):** `IDLE_AT_PHARMACY → EN_ROUTE → MARKER_SEARCH → AT_WARD_WAIT_AUTH → DISPENSING → RETURNING → PATROL`, overrides `OBSTACLE_HOLD`, `SAFEHOLD_COMMS`, `ESTOPPED`, `TELEOP`. `EN_ROUTE` executes Pi heading/speed goals; `MARKER_SEARCH` is entered when a leg's marker isn't reacquired within the odometry budget. Every state: entry action, exit condition, timeout. No blocking sensor reads.

**Pi processes (small, independent Python scripts):** `task_bridge.py` (dashboard ↔ serial), `nav.py` (camera → ArUco detect [pixel-offset/width] → heading/speed goals + arrival/station-ID), `ears.py` (audio events). Any one can die without killing the others; the MCU safe-holds if `nav`/`task_bridge` go silent. Launch via tmux/systemd, decided Day 2.

**Repo Day 0:** `/firmware`, `/pi`, `/dashboard`, `/cad`, `/markers` (the canonical printable ArUco PDFs — reprint from here, never from a phone photo), `/docs` (this file, versioned).

---

## 9. Risks & the descope ladder

1. **ArUco navigation reliability (top technical risk).** Glare/lighting, motion blur, detection range, marker occlusion, odometry drift between markers. Mitigations: big matte markers, locked exposure, slow detect-creep motion, angled + redundant corner markers at every turn, marker density tuned to the leg, bounded search-then-SAFEHOLD, a live supervised heading-nudge (bounded, logged, disclosed) that catches drift before SAFEHOLD fires, and an on-site arena calibration slot in the demo-day plan. Measure it honestly: report unassisted per-leg success (R1) and the supervised run separately.
2. **Shiny-parts scope creep.** Now that we're doing "real" vision nav, someone will say "we're already doing ArUco — let's just add SLAM/ROS." Standing answer: *"Does it make the §2 demo more reliable per integration-hour? No → not in this build."* SLAM is a two-week trap unless a member ships ROS daily, and it adds nothing marker homing doesn't.
3. **Pi is mission-critical for autonomy.** SD corruption / brownout / "worked yesterday." Mitigations: dedicated 5 V rail, image backups after Day 4 and Day 9, and R14 guarantees the show goes on via teleop without it.
4. **Integration underestimated** → Day 6 start + Day 10 gate.
5. **Magazine jams** → hard uniform candy, 1.3× clearance, 50-cycle test Day 5, iterate the printed disk only.
6. **Venue lighting vs camera** (elevated — now affects navigation, not just station ID) → own markers, exposure lock, calibration slot on demo day.
7. **Venue RF** → own router/hotspot; polling tolerates blips; safe-hold covers outages.
8. **LiPo handling** → named battery officer, charge bag, storage charge, no unattended charging. The one risk that can end the project (and singe the lab).
9. **Member's part slips** → bench-test date precedes integration date for every subsystem; slips surface at standup.

**Descope ladder** (Day 10 gate fails → cut in order, no debate):
1. Stretch goals (never promised).
2. Sound classifier → threshold version (same schema, zero dashboard change).
3. Reduce marker density → **single straight leg with one destination marker** (simplest reliable homing; density is a dial, and dropping the corner is its first turn).
4. Camera nav flaky → **odometry-only "blind" fixed short route** (accept lower reliability; a very short, taped-off course). *We do not reintroduce line following — rejected by design.*
5. Patrol → idle at station; temp logging → plain locked box; sound → mention-only.
6. Autonomy → **full teleop from the dashboard** (R14 path; parachute built Day 9). The teleop channel is already run live as the supervised nudge, so this rung just widens the human's role — not a new system. Dispatch, auth, dispensing, and audit all survive, and "human supervised, little autonomy" stays true.

Irreducible core to protect above all: **delivery + two-scan patient-matched release + audit trail.**

---

## 10. Stretch goals (locked until the Day 10 gate is all-green; pick ONE)

Ranked by demo value per hour:
1. **Second cloned robot** — fleet view with two live `robot_id`s and the dashboard assigning a task to whichever is free. Viable only if a second chassis clones from our repo in ≤ 1.5 days (itself a great test of build quality).
2. **Follow-mode** — robot homes on an ArUco tag worn by an authorized "escort" (reuses the exact nav pipeline; nearly free once nav is solid).
3. **Sound classifier** (if not already shipped Day 9).
4. Peltier-cooled cold box on its own battery budget.
5. OLED/touch status face + buzzer "voice" patterns.

---

## 11. Pitch notes

- Problem numbers first: nurses lose a large share of shifts to fetch-and-carry; real deployments (Aethon TUG ~160 hospitals; Diligent Moxi, 1M+ deliveries) prove the pattern and its measured time savings — we're a student-scale proof with a security-first twist.
- Differentiators vs "a delivery robot": (1) two-scan patient-matched release, (2) tamper-evident audit trail, (3) privacy-preserving sound alerting (labels, never audio), (4) two-brain architecture with **perception/navigation on the Pi and safety reflexes isolated on the MCU**, (5) **visual waypoint navigation with per-leg drift correction** — a real localization concept, honestly scoped, with no line on the floor.
- Show the §3 gap table honestly; judges reward teams who know what they didn't build.
- Demo failures on purpose: wrong badge, wrong patient, E-stop, comms loss, marker occlusion (R15), Pi unplug (R14). Grace under failure beats features.

---

## 12. Open decisions — settle at Day 0, then freeze

1. Pi 4 vs Pi 5 vs whichever is spare (default: whichever has a known-good SD + supply).
2. ESP32 vs Arduino Mega as reflex brain (default: ESP32).
3. Encoder motors available? → PID + trustworthy odometry (strongly preferred for homing/coast).
4. Camera: Pi Camera vs USB webcam (default: whichever OpenCV sees first).
5. **Marker plan:** size (default 12–15 cm), placement height (≈ camera height), and **density per leg as the tunable dial** (default 2 stations + 1 corner — corner angled toward the approach plus a redundant second; add more only if measured drift needs it). Print from `/markers`.
6. **Camera settings:** resolution (default 640×480), locked exposure/WB (default: lock).
7. **Docking standoff distance** (default ~20 cm from station marker).
8. Solenoid vs servo latch for cold box (default: solenoid if 5 V units exist).
9. HTTP polling vs WebSocket (default: polling, 500 ms).
10. Candy candidate ×3, tested in cardboard magazine Day 1.
11. Robot name. (Non-blocking. Will take longest.)
12. Who is Integration/Demo lead (scope veto) and who is Battery officer.

---

*Change log:*
*v0.3 — navigation rebuilt around ArUco visual waypoints (line following removed by design). Nav logic moved to the Pi (camera + markers), safety reflexes stay on the MCU; MCU-only fallback is now teleop (R14 reworded). Added `MARKER_SEARCH` state, R13 arrival-at-correct-station made core, R15 marker-lost behavior. Schedule front-loads marker homing (Day 3). Risk #1 is now ArUco nav reliability; descope ladder swaps in single-marker → odometry-blind → teleop (no line-following fallback). `/markers` added to repo.*
*v0.2 — budget removed; two-brain Pi+MCU architecture, USB-mic audio with classifier path, encoder/PID drive, LiPo power + battery officer, lab pull list, second robot promoted.*
*v0.1 — initial budget-constrained draft.*
