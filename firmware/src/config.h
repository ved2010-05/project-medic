#pragma once
// Project MEDIC — single source of pin truth. MIRRORS docs/pinout.md.
// Edit docs/pinout.md FIRST, this file second. Never scatter pin numbers.
//
// AS BUILT 2026-08-03: L298N motor driver, HC-SR04, magazine servo, buzzer, LED.
// NO encoders, NO E-stop sense, NO RC522, NO cold-box latch, NO DS18B20.
// Each missing subsystem has a HAS_* flag below — flip it to 1 when you wire it
// and the code picks it up. Nothing silently pretends hardware is present.
//
// ESP32 notes:
//   - GPIO 34-39 are INPUT-ONLY. Never drive an output on them.
//   - GPIO 0, 2, 12, 15 are strapping pins — they must sit at a particular level
//     at boot. Avoid them unless you have checked.

#define SERIAL_BAUD 115200

// ===========================================================================
// CAPABILITY FLAGS — what is physically attached right now
// ===========================================================================

// 0 = no E-stop sense wire to the ESP32.
//
// READ THIS BEFORE SETTING IT TO 1 OR LEAVING IT AT 0:
// The physical E-stop cuts MOTOR POWER in hardware, so the motors do stop
// whatever this flag says — R9's "motors halt < 0.5 s" still holds because it
// does not depend on software at all. What is lost at 0 is the MCU's ability to
// KNOW: no ESTOPPED override latches, no {"t":"estop"} goes to the Pi, and
// nothing appears in the audit log. So an E-stop event is invisible to the
// dashboard, and R12 (every event timestamped) cannot be met for E-stops.
// Wire GPIO 12 (or any free input) and set this to 1 to close that gap.
#define HAS_ESTOP_SENSE 0
#define PIN_ESTOP_SENSE 12
#define ESTOP_ACTIVE_LOW 1  // TODO(day-0): confirm against the real switch

// 0 = no cold-box latch, 0 = no DS18B20 temperature probe.
#define HAS_LATCH 0
#define HAS_TEMP 0

// ===========================================================================
// ULTRASONIC OVERRIDE  ***  READ THIS BEFORE SETTING IT TO 0  ***
// ===========================================================================
// 1 = obstacle detection ACTIVE (normal, and what you demo with).
// 0 = obstacle detection COMPILED OUT. The robot will drive into things.
//
// This exists for ONE reason: bench-testing drive/nav while the HC-SR04 is
// broken, unwired, or lying. A sensor stuck reporting a few centimetres pins
// the robot in OBSTACLE_HOLD forever and you cannot test anything else.
//
// It is deliberately COMPILE-TIME, not a command the Pi can send. Root
// ARCHITECTURE.md §0.1: safety reflexes live on the MCU and must override any Pi
// command. If the Pi could switch obstacle detection off over serial, the Pi
// would be IN the safety path — a dashboard bug, a bad packet or a crashed
// script could then disable the robot's brakes remotely. Turning this off has
// to be a deliberate act by a human holding the board, and it costs one
// reflash (~30 s in the Arduino IDE).
//
// When 0 the firmware SHOUTS about it: a banner on every boot and a permanent
// "NO_OBSTACLE_SAFETY" marker in the state message, so the dashboard and the
// audit log both record that the robot ran without obstacle protection. It is
// meant to be impossible to leave off by accident.
//
// R2 CANNOT PASS WITH THIS AT 0. Set it back to 1 before any scored run.
// 0 = HC-SR04 physically removed from this robot (2026-08-08). Obstacle
// detection is compiled out entirely; obstacleBlocked() is always false.
//
// READ THIS BEFORE DRIVING. With HAS_ESTOP_SENSE also 0, the ONLY reflex left
// in firmware is the 2 s comms watchdog. There is no longer anything that
// stops this robot for something in its path — and nav's max_spd is now 1.0,
// so it is the fastest it has ever been with the least protection it has ever
// had. A human must be ready to hit the physical E-stop (which still cuts
// motor power directly in hardware — that path does not depend on this flag).
#define HAS_ULTRASONIC 0

// Readings outside this band are treated as INVALID (no information), not as
// obstacles. An HC-SR04 physically cannot resolve closer than ~2 cm, so
// anything below that is a spurious echo — a floating ECHO pin, a missing
// level shifter, or motor-wiring noise. Before this filter existed, garbage
// 0.1-0.5 cm readings latched OBSTACLE_HOLD and the robot refused to move
// with nothing in front of it.
#define ULTRA_MIN_VALID_CM 2.0f
#define ULTRA_MAX_VALID_CM 400.0f  // HC-SR04 spec max; beyond = no echo

// Calibration for a sensor that reads consistently WRONG but consistently.
// reported_cm * ULTRA_SCALE = real_cm.  1.0 = no correction (correct default).
//
// USE THIS ONLY AFTER FIXING THE WIRING. A systematic under-read is almost
// always the ECHO line: docs/pinout.md requires a level shifter, and a divider
// built from large resistors gives slow edges that truncate the echo pulse —
// short pulse, short distance. Verify with scripts/ultrasonic-check.sh against
// a tape measure, fix the hardware, and only then reach for this. Scaling a
// miswired sensor makes the number look right while the underlying reading
// stays noisy and temperature-dependent, which is how you get a robot that
// brakes correctly on the bench and not on the course.
#define ULTRA_SCALE 1.0f

// If this many reads in a row are invalid, the sensor is not merely quiet —
// it is faulty or unplugged. The MCU says so once instead of failing silently.
#define ULTRA_FAULT_AFTER 40

// There are no encoders on this build: no PID, no odometry, and the protocol's
// "odom" message is never emitted. See docs/no-encoder-nav.md.
#define HAS_ENCODERS 0

// ===========================================================================
// PINS — as built
// ===========================================================================

// --- Drive: L298N (ENA/IN1/IN2, ENB/IN3/IN4) ------------------------------
#define PIN_MOTOR_A_PWM 25   // ENA
#define PIN_MOTOR_A_DIR1 26  // IN1
#define PIN_MOTOR_A_DIR2 27  // IN2
#define PIN_MOTOR_B_PWM 33   // ENB
#define PIN_MOTOR_B_DIR1 14  // IN3
#define PIN_MOTOR_B_DIR2 32  // IN4

// --- Safety ---------------------------------------------------------------
#define PIN_ULTRA_TRIG 13
#define PIN_ULTRA_ECHO 4  // level-shift to 3.3 V

// --- Payload --------------------------------------------------------------
#define PIN_SERVO_MAGAZINE 18

// --- UX -------------------------------------------------------------------
#define PIN_BUZZER 22
#define PIN_STATUS_LED 23

// 1 = passive buzzer (needs a driven tone), 0 = active buzzer (just needs a
// level). Most kit buzzers with a driver board are ACTIVE. If yours only clicks
// once instead of beeping, it is passive — set this to 1.
#define BUZZER_PASSIVE 0
#define BUZZER_TONE_HZ 2400

// ===========================================================================
// TUNING — the numbers you will actually change on the bench
// ===========================================================================

// --- Open-loop drive (no encoders, so these matter a lot) ------------------
// Duty is 0..1023 (10-bit). Below MIN_DUTY a geared motor stalls and buzzes
// instead of turning, so every speed request maps into [MIN_DUTY, MAX_DUTY].
#define PWM_FREQ_HZ 20000  // above hearing, no motor whine
#define PWM_RESOLUTION 10
#define DUTY_MAX 1023
// L298N drops ~1.4-2 V across its output stage, so the motor sees noticeably
// less than the battery voltage and needs MORE duty to start moving than a
// TB6612 build would. Start here and raise until it reliably creeps.
// TODO(day-2): tune on the real chassis, on the real floor, with the payload on.
#define MIN_DUTY 320
// Full scale. Was 750 (~73%), which silently clipped every speed request --
// nav asking for spd=1.0 only ever got 73% duty, and on an L298N (which eats
// ~1.4-2 V before the motor sees anything) that can be under the stall
// threshold, i.e. the wheels just buzz.
//
// The old cap existed to limit motion blur. That trade is now made ONE level
// up instead: nav's max_spd is dashboard-tunable at runtime, so speed can be
// dialled back without a reflash. Capping here as well meant the dashboard
// slider was lying about its top end.
#define MAX_DUTY 900

// With no encoders we cannot detect that one motor is faster, so we correct it
// once, by hand. 1.00 = no correction.
// TODO(day-2): drive straight 2 m, see which way it curves, trim the FAST side DOWN.
#define MOTOR_A_TRIM 1.00f
#define MOTOR_B_TRIM 1.00f

#define STEER_GAIN 0.030f  // heading error (deg) -> differential steering
#define STEER_MAX 0.70f
#define SLEW_PER_LOOP 0.06f
#define DRIVE_UPDATE_MS 20
#define SEARCH_SPD 0.35f  // in-place rotate speed for MARKER_SEARCH

// --- Safety ----------------------------------------------------------------
#define OBSTACLE_STOP_CM 25.0f
#define OBSTACLE_CLEAR_CM 35.0f  // hysteresis: no chattering at the threshold
#define ULTRA_PING_MS 60
#define ULTRA_TIMEOUT_US 25000UL
#define ULTRA_AGREE_COUNT 2
#define ESTOP_DEBOUNCE_MS 20
#define COMMS_TIMEOUT_MS 2000  // protocol: silence > 2 s -> SAFEHOLD_COMMS

// --- Magazine (single-pocket rotating-disk escapement) ---------------------
// One actuation = HOME -> DISPENSE angle -> dwell -> back to HOME -> settle,
// which drops exactly one unit. R5 wants the EXACT commanded count, >= 9/10.
// TODO(day-5): tune all five of these during the 50-cycle jam test.
#define SERVO_HOME_DEG 20
#define SERVO_DISPENSE_DEG 110
#define SERVO_TRAVEL_MS 320   // time allowed to reach the far angle
#define SERVO_DWELL_MS 220    // pause at the pocket so the candy actually drops
#define SERVO_SETTLE_MS 260   // pause back at home before the next unit
#define DISPENSE_MAX_COUNT 12 // sanity clamp; refuse absurd counts
// Detach the servo between dispenses. A cheap servo hunts and buzzes when held
// at an angle, which wastes current on a shared 5 V rail and can brown out the
// logic. Set to 0 if your disk needs to be actively held in place.
#define SERVO_DETACH_WHEN_IDLE 1

// --- Telemetry cadence -----------------------------------------------------
#define STATE_EMIT_MS 1000
// How often to repeat the "no obstacle sensor fitted" warning when
// HAS_ULTRASONIC is 0. Slow enough not to drown the audit log, frequent
// enough that nobody watching the dashboard can miss it.
#define NO_OBSTACLE_WARN_MS 10000UL

#define OBSTACLE_EMIT_MS 250
#define SERIAL_LINE_MAX 200
#define MAX_CMDS_PER_LOOP 4

// --- State timeouts (invariant §0.6: every state has one) ------------------
#define EN_ROUTE_TIMEOUT_MS 90000UL
#define MARKER_SEARCH_TIMEOUT_MS 15000UL
#define PATROL_TIMEOUT_MS 300000UL
