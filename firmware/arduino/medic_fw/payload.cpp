// payload.cpp — magazine dispenser. See payload.h.
//
// BENCH TEST (do these in order, they build on each other):
//  1. Servo alone, magazine empty, robot on the bench. Send:
//       {"t":"dispense","count":1}
//     The disk should sweep HOME -> DISPENSE -> back to HOME once, and you
//     should get {"t":"ack","of":"dispense","ok":true,"n":1}.
//  2. Angles: if the pocket doesn't line up with the tube, adjust
//     SERVO_HOME_DEG / SERVO_DISPENSE_DEG in config.h. Do NOT reprint the disk
//     for this — it is two numbers.
//  3. Load the magazine. {"t":"dispense","count":3} must drop EXACTLY 3. If it
//     drops 2 or 4, increase SERVO_DWELL_MS (candy needs longer to fall) or
//     SERVO_SETTLE_MS (disk still moving when the next unit is requested).
//  4. THE DAY-5 JAM TEST (R5, needs >= 9/10): run count=1 fifty times and log
//     every result. A jam counts as a failure. Iterate the printed DISK POCKET,
//     not the whole magazine.
//  5. Mid-dispense safety: start count=5 and put your hand in front of the
//     ultrasonic. The disk must stop and return to HOME (lockdown), and the ack
//     must report the SHORT count actually dropped, not 5.
//  6. Timing: print micros() around loop(). No iteration should spike while
//     the servo is moving — if one does, something is blocking.

#include "payload.h"
#include "config.h"

namespace Payload {

// ---------------------------------------------------------------------------
// Servo driven directly by the ESP32's LEDC peripheral — NO external library.
//
// ESP32Servo would work, but it is one more thing to install, and on a locked
// -down network you cannot install it at all. A hobby servo is just a 50 Hz
// PWM signal with a 500-2500 us pulse, which LEDC produces natively, so the
// library buys us nothing here. Zero dependencies = builds offline, in the
// Arduino IDE and PlatformIO alike.
// ---------------------------------------------------------------------------
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
#define MEDIC_LEDC_V3 1
#else
#define MEDIC_LEDC_V3 0
#define SERVO_LEDC_CH 4  // keep clear of drive.cpp's motor channels (0 and 1)
#endif

#define SERVO_FREQ_HZ 50
#define SERVO_BITS 16
#define SERVO_PERIOD_US 20000UL
#define SERVO_MIN_US 500UL   // ~0 deg
#define SERVO_MAX_US 2500UL  // ~180 deg

// Convert an angle to a duty count and emit it. Writing duty 0 stops the pulse
// train entirely, which is how we "detach" (see detachIfIdle).
static void servoWriteDeg(int deg) {
  if (deg < 0) deg = 0;
  if (deg > 180) deg = 180;
  uint32_t us = SERVO_MIN_US + ((SERVO_MAX_US - SERVO_MIN_US) * (uint32_t)deg) / 180UL;
  uint32_t maxDuty = (1UL << SERVO_BITS) - 1UL;
  uint32_t duty = (uint32_t)(((uint64_t)us * maxDuty) / SERVO_PERIOD_US);
#if MEDIC_LEDC_V3
  ledcWrite(PIN_SERVO_MAGAZINE, duty);
#else
  ledcWrite(SERVO_LEDC_CH, duty);
#endif
}

static void servoStopPulses() {
#if MEDIC_LEDC_V3
  ledcWrite(PIN_SERVO_MAGAZINE, 0);
#else
  ledcWrite(SERVO_LEDC_CH, 0);
#endif
}

static void servoSetup() {
#if MEDIC_LEDC_V3
  ledcAttach(PIN_SERVO_MAGAZINE, SERVO_FREQ_HZ, SERVO_BITS);
#else
  ledcSetup(SERVO_LEDC_CH, SERVO_FREQ_HZ, SERVO_BITS);
  ledcAttachPin(PIN_SERVO_MAGAZINE, SERVO_LEDC_CH);
#endif
}

// The sequencer. One unit = TO_POCKET -> DWELL -> TO_HOME -> SETTLE.
enum Phase : uint8_t {
  P_IDLE = 0,
  P_TO_POCKET,
  P_DWELL,
  P_TO_HOME,
  P_SETTLE,
};

static Phase s_phase = P_IDLE;
static uint32_t s_phaseSince = 0;
static int32_t s_remaining = 0;   // units still to drop
static int32_t s_actuated = 0;    // units actually dropped this run
static bool s_resultReady = false;
static int32_t s_resultCount = 0;
static bool s_attached = false;
static bool s_latchOpen = false;

static void attachIfNeeded() {
  if (!s_attached) {
    servoSetup();
    s_attached = true;
  }
}

static void detachIfIdle() {
#if SERVO_DETACH_WHEN_IDLE
  if (s_attached) {
    // Stop the pulse train. A cheap servo left holding an angle hunts and
    // buzzes, wasting current on the shared 5 V rail.
    servoStopPulses();
    s_attached = false;
  }
#endif
}

static void enter(Phase p) {
  s_phase = p;
  s_phaseSince = millis();
  switch (p) {
  case P_TO_POCKET:
    attachIfNeeded();
    servoWriteDeg(SERVO_DISPENSE_DEG);
    break;
  case P_TO_HOME:
    attachIfNeeded();
    servoWriteDeg(SERVO_HOME_DEG);
    break;
  case P_IDLE:
    detachIfIdle();
    break;
  default:
    break;  // DWELL and SETTLE just wait; the servo already holds its angle
  }
}

void begin() {
  // LEDC allocates its own timer. The motors run at 20 kHz and the servo at
  // 50 Hz, so they cannot share a timer -- the core's allocator handles that
  // for us as long as we only attach three pins total.
  servoSetup();
  s_attached = true;
  // Park at HOME so the disk starts in a known position and the magazine is
  // closed. Anything else risks a free-running unit on power-up.
  servoWriteDeg(SERVO_HOME_DEG);
  s_phase = P_IDLE;
  s_phaseSince = millis();
  s_remaining = 0;
  s_actuated = 0;
  s_resultReady = false;
  s_latchOpen = false;
}

bool dispense(int32_t count) {
  if (s_phase != P_IDLE) return false;              // already dispensing
  if (count <= 0 || count > DISPENSE_MAX_COUNT) return false;
  s_remaining = count;
  s_actuated = 0;
  enter(P_TO_POCKET);
  return true;
}

bool isBusy() { return s_phase != P_IDLE; }

// --- I/O bench --------------------------------------------------------------
// Callers must have already checked that BENCH mode is active (main.cpp does).
// Refused mid-sequence so a stray bench click cannot yank the disk out from
// under a dispense that is counting units — that would corrupt the R5 count.
static int s_benchDeg = SERVO_HOME_DEG;

bool benchServo(int deg) {
  if (s_phase != P_IDLE) return false;
  if (deg < 0 || deg > 180) return false;
  s_benchDeg = deg;
  servoWriteDeg(deg);
  return true;
}

int benchServoDeg() { return s_benchDeg; }

bool takeResult(int32_t &actuated) {
  if (!s_resultReady) return false;
  s_resultReady = false;
  actuated = s_resultCount;
  return true;
}

static void finish() {
  s_resultCount = s_actuated;
  s_resultReady = true;
  s_remaining = 0;
  enter(P_IDLE);
}

void update() {
  if (s_phase == P_IDLE) return;
  uint32_t held = millis() - s_phaseSince;

  switch (s_phase) {
  case P_TO_POCKET:
    if (held >= SERVO_TRAVEL_MS) enter(P_DWELL);
    break;

  case P_DWELL:
    if (held >= SERVO_DWELL_MS) enter(P_TO_HOME);
    break;

  case P_TO_HOME:
    if (held >= SERVO_TRAVEL_MS) {
      // The unit is only counted once the disk is back HOME. Counting it at the
      // pocket would over-report if we were interrupted on the way back.
      s_actuated++;
      if (s_remaining > 0) s_remaining--;
      enter(P_SETTLE);
    }
    break;

  case P_SETTLE:
    if (held >= SERVO_SETTLE_MS) {
      if (s_remaining > 0) enter(P_TO_POCKET);
      else finish();
    }
    break;

  default:
    break;
  }
}

bool latch(bool open) {
#if HAS_LATCH
  // TODO(day-5): drive the solenoid/servo here, plus the auto-relock window.
  s_latchOpen = open;
  return true;
#else
  (void)open;
  // No latch hardware. Refuse rather than reporting a success that never
  // happened — a false "cold box opened" in the audit log is worse than a
  // refusal, because it makes the trail lie.
  return false;
#endif
}

bool latchIsOpen() { return s_latchOpen; }

void lockdown() {
  if (s_phase != P_IDLE) {
    // Abort mid-sequence: park the disk at HOME and report the SHORT count. The
    // caller turns that into a dispense_fail, which is correct — an interrupted
    // dispense did not deliver what was asked for.
    attachIfNeeded();
    servoWriteDeg(SERVO_HOME_DEG);
    finish();
  }
  s_latchOpen = false;
}

} // namespace Payload
