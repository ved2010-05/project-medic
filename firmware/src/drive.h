#pragma once
// drive.h — open-loop differential drive. NO ENCODERS in this build.
//
// Because there is no encoder feedback there is no PID and no odometry: we
// convert a requested speed straight into a PWM duty. The robot's *real* speed
// will vary with battery charge, floor surface and load. That is acceptable
// here because the CAMERA closes the loop — nav.py re-measures the heading
// error every frame and corrects continuously (see docs/no-encoder-nav.md).
//
// This module NEVER decides whether it is safe to move. main.cpp asks safety.h
// first and simply does not call these functions when an override is active
// (ARCHITECTURE.md §0.1).

#include <Arduino.h>

namespace Drive {

enum TeleopCmd : uint8_t {
  TELEOP_STOP = 0,
  TELEOP_FWD,
  TELEOP_BACK,
  TELEOP_LEFT,
  TELEOP_RIGHT,
};

void begin();

// Call every loop. Applies slew limiting and writes the motor outputs.
void update();

// Pi 'drive': hdgErrDeg is the heading error in degrees, POSITIVE = marker is to
// the RIGHT = steer right. spd is 0..1.
void setGoal(float hdgErrDeg, float spd);

// Pi 'search': rotate in place. dir = +1 clockwise, -1 counter-clockwise.
void rotate(int8_t dir, float spd);

// Pi 'teleop': the R14 parachute and the live supervised nudge.
void teleop(uint8_t cmd, float spd);

// Controlled stop — ramps down through the slew limiter.
void stop();

// Immediate zero output. BYPASSES the slew limiter and the trim. This is the
// R9 path: motors must reach zero in well under 0.5 s.
void emergencyStop();

bool isMoving();

// --- I/O bench (dashboard Live tab) ----------------------------------------
// Direct per-side control, for wiring diagnosis and motor-polarity checks.
// LATCHING: a side keeps running at the commanded value until it is set again,
// so these are toggles rather than nudges.
//
// Safety is NOT weakened by this. main.cpp still gates every motor write on
// Safety::motionForbidden(), so E-stop, OBSTACLE_HOLD and the comms watchdog
// cut the wheels here exactly as they do in autonomy — including the case that
// matters most for a latching control: if the browser or the Pi goes away, the
// 2 s comms watchdog stops the motors on its own (ARCHITECTURE.md §0.1).
//
// side: 0 = A (left), 1 = B (right).  v: -1..+1.
void benchSide(uint8_t side, float v);
float benchValue(uint8_t side);

} // namespace Drive
