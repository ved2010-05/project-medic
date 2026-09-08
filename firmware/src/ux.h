#pragma once
// ux.h — buzzer + status LED. Non-blocking millis() patterns, no delay(), no
// tone() blocking (invariant §0.6 — this runs in the same loop as the safety
// checks, so it must never hold anything up).
//
// The patterns exist to be recognised across a noisy room from behind a table.
// AUTH_REFUSED especially: the wrong-patient refusal is demoed ON PURPOSE
// (ARCHITECTURE.md §2 step 4), so it has to be unmistakable, and clearly
// different from every "normal" sound.

#include <Arduino.h>

namespace Ux {

enum Pattern : uint8_t {
  UX_IDLE = 0,       // slow LED breath, silent
  UX_EN_ROUTE,       // steady slow blink, silent
  UX_WAIT_AUTH,      // fast blink + single short chirp on entry
  UX_DISPENSING,     // double blink, short tick per unit
  UX_AUTH_OK,        // solid LED + one rising double-beep
  UX_AUTH_REFUSED,   // LED strobe + three long harsh beeps -- demoed on purpose
  UX_OBSTACLE,       // LED double-flash + short repeated chirp
  UX_ESTOP,          // LED solid + continuous urgent beep
  UX_SAFEHOLD,       // LED slow double-flash + occasional low beep
  UX_MARKER_LOST,    // LED long-short-long + two beeps
  UX_COUNT
};

void begin();


void update();

void setPattern(Pattern p);

// Fire a ONE-SHOT pattern that plays to completion, then falls back to whatever
// setPattern() last requested. Used for auth ok / refused, which are events
// rather than states.
void oneShot(Pattern p);

// Raw pattern ids from the protocol's 'ux' message ({"t":"ux","led":N,"buzz":N}).
// Out-of-range ids are ignored rather than treated as an error, per the
// protocol's forward-compatibility rule.
void setFromSerial(uint8_t led, uint8_t buzz);

// --- I/O bench (dashboard Live tab) ----------------------------------------
// Raw pin control, bypassing the pattern player. setFromSerial() above picks a
// *pattern*, which is the right thing for the demo but useless for "is this LED
// wired to the pin I think it is". While bench is active the pattern player
// stops writing the pins, so a latched output really does stay latched instead
// of being blinked back off a few milliseconds later.
void benchSet(bool active, bool led, bool buzz);
bool benchActive();
bool benchLed();
bool benchBuzz();

} // namespace Ux
