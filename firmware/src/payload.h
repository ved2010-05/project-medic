#pragma once
// payload.h — magazine dispenser (single-pocket rotating-disk escapement).
//
// FAIL-CLOSED (ARCHITECTURE.md §0.4). This module never actuates on its own. It
// only moves when main.cpp calls dispense(), and main.cpp only does that once
// it has confirmed motion is not forbidden (Safety::motionForbidden()) — the
// robot arriving/stopping at the ward is what triggers the call. There is no
// RFID or other scan step in this build; the safety-override check is the gate.
//
// Non-blocking (§0.6): a dispense is a small step sequencer driven by millis().
// There is no delay() anywhere, because the control loop has to keep servicing
// the ultrasonic and the comms watchdog while candy is dropping.
//
// This build has NO cold-box latch and NO temperature probe (see config.h's
// HAS_LATCH / HAS_TEMP). The latch API is declared so main.cpp compiles
// unchanged when you wire one; it refuses until then rather than pretending.

#include <Arduino.h>

namespace Payload {

void begin();

// Call every loop. Advances the dispense sequencer.
void update();

// Start dispensing `count` units. Returns false if busy, or if count is
// non-positive or above DISPENSE_MAX_COUNT. One unit per disk actuation.
bool dispense(int32_t count);

bool isBusy();

// One-shot result: returns true exactly once after a dispense finishes, and
// writes how many units were ACTUALLY actuated into `actuated`. R5 grades the
// exact count, so the caller reports this back to the Pi in ack's "n".
bool takeResult(int32_t &actuated);

// Cold-box latch. Refuses (returns false) while HAS_LATCH is 0.
bool latch(bool open);
bool latchIsOpen();

// Force everything closed and abort any dispense in progress. Called by the
// safety overrides — an E-stop or obstacle hold must never leave the magazine
// mid-actuation with a half-dropped unit.
void lockdown();

// --- I/O bench (dashboard Live tab) ----------------------------------------
// Raw servo angle, for checking the escapement geometry and the disk pocket
// without staging a full delivery run.
//
// ** THIS IS NOT A SAFETY BYPASS. ** benchServo() is refused unless the MCU is
// in explicit BENCH mode, which itself is refused while a delivery task is in
// flight — so there is no reachable path from a live demo to an unauthorised
// payload release. It is a workshop tool, not a demo feature.
bool benchServo(int deg);
int benchServoDeg();

} // namespace Payload
