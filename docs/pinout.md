# Pinout & wiring — AUTHORITATIVE (fill in real pins at Day-0 wiring)

This file is the single source of truth for pin assignments. `firmware/src/config.h` must mirror it. Values below are a **sensible ESP32 template** — confirm against the actual DevKit and modules on Day 0 and edit here first, code second. Avoid ESP32 input-only pins (34–39) for outputs, and avoid strapping pins (0, 2, 12, 15) for anything that must be a specific level at boot.

## ESP32 (reflex brain) — AS BUILT, 2026-08-03

This is what is **actually wired right now**. `firmware/src/config.h` mirrors it
exactly. The "not wired yet" block below is the remaining plan, not reality.

| Function | Pin | Notes |
|---|---|---|
| Motor A PWM(ENA) / DIR1(IN1) / DIR2(IN2) | 25 / 26 / 27 | via **L298N** |
| Motor B PWM(ENB) / DIR1(IN3) / DIR2(IN4) | 33 / 14 / 32 | |
| Ultrasonic TRIG / ECHO | 13 / 4 | level-shift ECHO to 3.3 V |
| Servo — magazine disk | 18 | better than the old suggestion of 15 (a strapping pin) |
| Buzzer | 22 | |
| Status LED | 23 | |
| Serial to Pi | USB (UART0) | 115200 baud |

### L298N notes (it is not a drop-in TB6612)
- **REMOVE THE ENA/ENB JUMPERS.** Out of the box most L298N boards jumper ENA/ENB
  to +5 V, which pins the motors at full speed and makes every PWM value do
  nothing. This is the single most common "why won't it go slowly" fault.
- **~1.4–2 V drop** across the Darlington output stage. On a 2S LiPo (7.4 V) the
  motors only ever see ~5.5–6 V, so `MIN_DUTY` in `config.h` usually has to be
  raised compared to a TB6612 build.
- L298N logic inputs are 5 V parts driven from 3.3 V GPIO. IN1–IN4 normally
  switch fine (threshold ≈ 2.3 V). If a motor behaves erratically, this is the
  first thing to suspect.
- Give the L298N its own motor supply and a **common ground** with the ESP32.

### NOT WIRED YET — code has capability flags for each
| Function | Planned pin | Consequence while unwired |
|---|---|---|
| **E-stop sense** | 12 (strapping — verify boot level) | **Safety gap.** The physical E-stop still cuts motor power in hardware, so motors do stop; but the MCU cannot *detect* it, so no `estop` event reaches the audit log and `ESTOPPED` never latches in software. Set `HAS_ESTOP_SENSE 1` in `config.h` when wired. |
| RC522 (SPI) SCK/MISO/MOSI/SS/RST | 19 / 21 / 5 / … | No badge/wristband scans, so the two-scan auth can never complete and **nothing will ever be authorised to dispense**. NOTE: the old plan used 18 and 23, which are now the servo and the LED — pick fresh pins. |
| Cold-box latch | 2 | No cold-chain release. |
| DS18B20 (1-Wire) | 17 | No temperature telemetry (R6). |
| Encoders | 34/35, 36/39 | **No encoders in this build** — no PID, no odometry, no `odom` message. See `docs/no-encoder-nav.md`. |

## Raspberry Pi (planner brain)
- Camera → CSI (Pi Camera) or USB.
- USB microphone → USB.
- USB serial to ESP32 → USB (identify the stable `/dev/serial/by-id/...` path and pin it in `task_bridge.py`).
- Dedicated 5 V ≥ 3 A supply (NOT shared with motors).

## Power tree
```
LiPo 2S/3S ──┬── motor driver (motors)
             ├── UBEC #1 5V/3A ── Raspberry Pi   (dedicated rail)
             └── UBEC #2 5V     ── ESP32 + servos + logic
Common ground across all rails. E-stop switch in the motor-power branch.
```
Confirm servo/solenoid current draw; if servos glitch the logic rail, give them their own regulated 5 V or a bulk capacitor.
