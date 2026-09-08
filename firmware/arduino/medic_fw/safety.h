#pragma once
// safety.h — the reflex layer. THE most important file in this repo.
//
// Root ARCHITECTURE.md §0.1: obstacle-stop, E-stop and lost-comms safe-hold live ONLY
// here, on the MCU, and they override any command from the Pi. Nothing in this
// module knows what a task, a patient or an auth check is. It is pure reflex.
//
// Everything here is non-blocking (invariant §0.6). In particular the ultrasonic
// is measured with an interrupt on the echo pin — NEVER pulseIn(), which can
// stall the control loop for tens of milliseconds and would blow the R9 budget.

#include <Arduino.h>

// Overrides, in ascending priority. The numeric order IS the priority order.
enum Override : uint8_t {
  OVR_NONE = 0,
  OVR_TELEOP,
  OVR_SAFEHOLD_COMMS,
  OVR_OBSTACLE_HOLD,
  OVR_ESTOPPED,
};

const char *overrideName(Override o);

namespace Safety {

void begin();
void update();

// serial_link calls this on every VALID parsed frame. Malformed lines must NOT
// feed the watchdog, or garbage on the wire would keep a dead link looking alive.
void notifyFrameReceived();

bool estopActive();
bool obstacleBlocked();
float obstacleCm(); // last accepted distance; negative = no echo / out of range
bool commsSilent();

// True when config.h has HAS_ULTRASONIC 0, i.e. obstacle detection is compiled
// out and the robot has NO obstacle protection. main.cpp reports this in every
// state message so the dashboard and audit log both record it.
bool ultrasonicOverridden();

// True when the sensor has returned nothing usable for a long run of pings —
// unplugged, mis-wired, or dead. Distinct from "clear corridor", which looks
// identical if you only watch obstacleBlocked().
bool ultrasonicFaulty();

// One-shot edge: true the first time the fault latches, so the fault is
// announced to the Pi once instead of spamming the audit log every loop.
bool takeUltrasonicFaultNotice();

// TELEOP is reported by main.cpp, not sensed here, so it is passed in.
Override highest(bool teleopActive);

// True when an override forbids motion. main.cpp checks this BEFORE executing
// any Pi goal, every single loop.
bool motionForbidden(bool teleopActive);

} // namespace Safety
