# Navigation without encoders — what changes and why

The design doc (§4.1) assumes encoder gear motors: PID speed control on the MCU and
wheel odometry bridging the gaps between markers. **This build has no encoders.**
That is the documented fallback in the lab pull list (§5: "Plain TT motors + L298N —
no PID; homing gets jerkier"). This file records exactly what that costs us.

## The one-line version

**Homing does not need encoders — the camera is the feedback loop.** Every frame
re-measures the heading error, so open-loop motor drift is corrected continuously.
Encoders only ever bought us the *blind stretches between markers*. So the honest
trade is: nav quality is essentially unchanged while a marker is in view, and
degrades in the gaps.

**Therefore: turn the marker-density dial up.** The design doc calls density "the
tunable reliability dial." Without odometry you lean on it harder. Aim for a course
where the robot almost always has a marker in view.

## What we lost, and what replaced it

| Needed encoders | What breaks | Replacement in this build |
|---|---|---|
| PID speed control | Can't hold a commanded speed; real speed varies with battery charge, floor and load | Open-loop PWM. `MIN_DUTY`/`MAX_DUTY` map a 0–1 request into the band where the motor actually turns instead of buzzing. |
| Motor matching | One motor is always faster; robot curves when told to go straight | `MOTOR_A_TRIM`/`MOTOR_B_TRIM` in `config.h`, set once by hand on the bench |
| Odometry (`odom` message) | Pi has **no distance feedback whatsoever** | The MCU never emits `odom`. `nav.py` must not wait for it. |
| "Coast a bounded **distance**" between markers | §4.1's coast step is unimplementable | Bounded **time** (`COAST_S`, default 1.5 s). Kept deliberately short — we cannot measure how far we actually went, so a long blind coast is pure guesswork. |
| `MARKER_SEARCH` within an **angle** budget | Can't measure degrees swept | Bounded **time** (`SEARCH_S` on the Pi, `MARKER_SEARCH_TIMEOUT_MS` on the MCU) |
| Dead reckoning around a corner | Drift is unbounded and unmeasurable | Put a marker at every turn, angled toward the approach, plus the redundant second one §4.1 already calls for |

## Requirements impact

- **R1** (delivery ≥ 8/10) — still reachable, but it now depends more on marker
  density than on tuning. If R1 fails at the Day-10 gate, add markers before you
  touch gains.
- **R15** (marker lost → never wanders) — **still fully satisfied.** The recovery is
  bounded; the bound is just wall-clock instead of distance. Coast → search → stop
  + alert, and the MCU has its own independent `MARKER_SEARCH` timeout underneath
  the Pi's, so a dead Pi cannot leave the robot spinning.
- **R2, R9, R10, R13, R14** — unaffected. None of them ever involved encoders.
- Descope ladder rung 4 ("odometry-only blind short route") is **no longer
  available** — there is no odometry to be blind with. If camera nav fails, the
  ladder now drops straight to rung 6, full teleop. Worth knowing before the gate.

## Retrofit path

`docs/pinout.md` still reserves 34/35 and 36/39 for encoder A/B, and nothing else
uses them. Adding encoders later means: read those pins in `IRAM_ATTR` ISRs, add a
PID inside `Drive::update()` (the fixed-cadence hook is already there), add an
`emitOdom()` to `serial_link`, and switch `nav.py`'s `COAST_S` from a time budget to
a distance budget. No architectural change — the split of responsibilities is
already right.
