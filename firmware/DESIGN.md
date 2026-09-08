# ARCHITECTURE.md — firmware/ (ESP32 "reflex brain")

Local context for the microcontroller. Read the `ARCHITECTURE.md` first; §0 invariants apply here with full force — **this is where safety lives.**

## Role
The MCU owns motion and safety. It executes heading/speed goals from the Pi, runs motor PID, produces odometry, and enforces the reflex safety layer that **overrides any Pi command**. It must be able to run the entire teleop demo with the Pi absent (R14).

## Stack
- Arduino framework (C++), PlatformIO preferred (`platformio.ini` present) or Arduino IDE.
- Board: ESP32 DevKit. Serial to Pi over USB at a fixed baud (set in `platformio.ini` / a `config.h`; default 115200).
- No dynamic allocation in the control loop. No `delay()` inside states — use `millis()`/timers. Non-blocking always.

## State machine (authoritative)
`IDLE_AT_PHARMACY → EN_ROUTE → MARKER_SEARCH → AT_WARD_WAIT_AUTH → DISPENSING → RETURNING → PATROL`
Overrides (can fire from any state): `OBSTACLE_HOLD`, `SAFEHOLD_COMMS`, `ESTOPPED`, `TELEOP`.
Rules: every state has entry action + exit condition + timeout. `OBSTACLE_HOLD` (ultrasonic < ~25 cm) and `ESTOPPED` and `SAFEHOLD_COMMS` (Pi serial silent > 2 s) **preempt** `EN_ROUTE`/`RETURNING` and hold the payload locked.

## Hard rules (in addition to root §0)
- Safety checks run every loop, before executing any Pi goal. A Pi `drive` command is ignored while `OBSTACLE_HOLD`/`ESTOPPED` is active.
- Auth is fail-closed: the compartment servos/solenoid only actuate on an explicit `dispense`/`latch` command that the Pi issues *after* a verified two-scan match. The MCU never unlocks on its own.
- Watchdog: no valid serial frame from the Pi for > 2 s → stop, hold locks, report `SAFEHOLD_COMMS`.
- Read pins from a single `config.h`/pinout source that mirrors `docs/pinout.md`. Don't scatter pin numbers.

## Files (create as you go — keep one module per subsystem)
```
src/
  main.cpp        <- setup/loop, state machine dispatcher
  config.h        <- pins (mirror docs/pinout.md), baud, thresholds
  drive.*         <- motor PID + odometry
  serial_link.*   <- JSON-lines parse/emit (docs/serial-protocol-v1.md)
  safety.*        <- estop, obstacle, comms watchdog (highest priority)
  payload.*       <- magazine dispense, cold-box latch
  rfid.*          <- RC522 read → emit rfid events (MCU reads tags, Pi/dashboard decides match)
  ux.*            <- buzzer, LEDs
```

## Bench tests to state whenever you add code
Motors spin + PID holds speed; odometry counts correct direction/magnitude; ultrasonic stop/resume at 15–35 cm; E-stop halts < 0.5 s with locks held; serial round-trips a `ping`/`pong`; dispense actuates exactly N times.
