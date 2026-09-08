// ux.cpp — buzzer + status LED. See ux.h.
//
// Implementation note: every pattern is a table of (led_on, buzz_on, duration)
// steps that the update() function walks with millis(). That keeps all the
// timing data in one readable place instead of scattered across a switch, and
// makes adding a pattern a one-line change rather than new logic.
//
// BENCH TEST:
//  1. On boot you should get one short confirmation chirp and the LED should
//     start its slow idle breath. If the LED is on but silent and you expected
//     a beep, your buzzer is PASSIVE — set BUZZER_PASSIVE 1 in config.h.
//  2. Send {"t":"ux","buzz":5} (AUTH_REFUSED). Stand at the far side of the room
//     and confirm you can tell it apart from {"t":"ux","buzz":4} (AUTH_OK)
//     without looking. If you can't, make them more different -- this is the
//     moment the judges are watching.
//  3. Put your hand in front of the ultrasonic: the OBSTACLE pattern must take
//     over immediately and stop the instant you withdraw.
//  4. Timing check: patterns must not stutter while the magazine servo is
//     running. If they do, something is blocking the loop.

#include "ux.h"
#include "config.h"

namespace Ux {

struct Step {
  bool led;
  bool buzz;
  uint16_t ms;
};

// Each pattern is a NULL-terminated run of steps. Keep them short and distinct.
static const Step P_IDLE_S[]      = {{1, 0, 60},   {0, 0, 2400}, {0, 0, 0}};
static const Step P_ENROUTE_S[]   = {{1, 0, 400},  {0, 0, 400},  {0, 0, 0}};
static const Step P_WAITAUTH_S[]  = {{1, 0, 120},  {0, 0, 120},  {0, 0, 0}};
static const Step P_DISPENSE_S[]  = {{1, 1, 60},   {0, 0, 90},
                                     {1, 0, 60},   {0, 0, 500},  {0, 0, 0}};
static const Step P_AUTHOK_S[]    = {{1, 1, 120},  {1, 0, 80},
                                     {1, 1, 220},  {1, 0, 300},  {0, 0, 0}};
// Deliberately long, harsh and repetitive. This is the refusal everyone is
// meant to notice.
static const Step P_REFUSED_S[]   = {{1, 1, 400},  {0, 0, 150},
                                     {1, 1, 400},  {0, 0, 150},
                                     {1, 1, 400},  {0, 0, 600},  {0, 0, 0}};
static const Step P_OBSTACLE_S[]  = {{1, 1, 80},   {0, 0, 80},
                                     {1, 0, 80},   {0, 0, 700},  {0, 0, 0}};
static const Step P_ESTOP_S[]     = {{1, 1, 500},  {1, 0, 120},  {0, 0, 0}};
static const Step P_SAFEHOLD_S[]  = {{1, 0, 150},  {0, 0, 150},
                                     {1, 0, 150},  {0, 1, 80},
                                     {0, 0, 1600}, {0, 0, 0}};
static const Step P_LOST_S[]      = {{1, 1, 450},  {0, 0, 140},
                                     {1, 1, 130},  {0, 0, 140},
                                     {1, 1, 450},  {0, 0, 900},  {0, 0, 0}};

static const Step *const PATTERNS[UX_COUNT] = {
    P_IDLE_S, P_ENROUTE_S, P_WAITAUTH_S, P_DISPENSE_S,
    P_AUTHOK_S, P_REFUSED_S, P_OBSTACLE_S, P_ESTOP_S,
    P_SAFEHOLD_S, P_LOST_S,
};

static Pattern s_background = UX_IDLE;
static Pattern s_active = UX_IDLE;
static bool s_oneShot = false;
static uint8_t s_step = 0;
static uint32_t s_stepSince = 0;

static void buzzOn(bool on) {
#if BUZZER_PASSIVE
  // Passive buzzer: it needs a driven waveform, not just a level. tone() on the
  // ESP32 core is non-blocking (it hands the pin to a hardware timer), so this
  // is still safe inside the control loop.
  if (on) tone(PIN_BUZZER, BUZZER_TONE_HZ);
  else noTone(PIN_BUZZER);
#else
  // Active buzzer: has its own oscillator, so a level is all it wants.
  digitalWrite(PIN_BUZZER, on ? HIGH : LOW);
#endif
}

static void applyStep() {
  const Step &st = PATTERNS[s_active][s_step];
  digitalWrite(PIN_STATUS_LED, st.led ? HIGH : LOW);
  buzzOn(st.buzz);
  s_stepSince = millis();
}

static void restart(Pattern p) {
  s_active = p;
  s_step = 0;
  applyStep();
}

void begin() {
  pinMode(PIN_STATUS_LED, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_STATUS_LED, LOW);
  buzzOn(false);
  s_background = UX_IDLE;
  s_oneShot = false;
  restart(UX_IDLE);
}

void setPattern(Pattern p) {
  if (p >= UX_COUNT) return;
  s_background = p;
  // A one-shot in progress keeps the stage until it finishes.
  if (!s_oneShot && p != s_active) restart(p);
}

void oneShot(Pattern p) {
  if (p >= UX_COUNT) return;
  s_oneShot = true;
  restart(p);
}

void setFromSerial(uint8_t led, uint8_t buzz) {
  // The protocol carries two ids; we treat the larger as the intent so a
  // dashboard that only sets one field still gets a sensible result. Unknown
  // ids are ignored, not an error (forward compatibility).
  uint8_t id = buzz > led ? buzz : led;
  if (id >= UX_COUNT) return;
  Pattern p = (Pattern)id;
  // Event-ish patterns play once; everything else is a background state.
  if (p == UX_AUTH_OK || p == UX_AUTH_REFUSED) oneShot(p);
  else setPattern(p);
}

// --- I/O bench --------------------------------------------------------------
static bool s_bench = false, s_benchLed = false, s_benchBuzz = false;

void benchSet(bool active, bool led, bool buzz) {
  s_bench = active;
  s_benchLed = led;
  s_benchBuzz = buzz;
  if (active) {
    digitalWrite(PIN_STATUS_LED, led ? HIGH : LOW);
    buzzOn(buzz);
  } else {
    // Leaving bench: hand the pins straight back to the pattern player rather
    // than leaving whatever the operator last latched burning on the board.
    restart(s_background);
  }
}

bool benchActive() { return s_bench; }
bool benchLed() { return s_benchLed; }
bool benchBuzz() { return s_benchBuzz; }

void update() {
  if (s_bench) return;  // bench owns the pins; do not blink over the operator
  const Step *pat = PATTERNS[s_active];
  const Step &st = pat[s_step];
  if (st.ms == 0) return;  // malformed/terminated pattern: hold
  if (millis() - s_stepSince < st.ms) return;

  s_step++;
  if (pat[s_step].ms == 0) {
    // End of the run.
    if (s_oneShot) {
      s_oneShot = false;
      restart(s_background);  // fall back to the state pattern
      return;
    }
    s_step = 0;  // loop the background pattern
  }
  applyStep();
}

} // namespace Ux
