// safety.cpp — the reflex layer. See safety.h.
//
// BENCH TEST:
//  1. Ultrasonic: hold a book at 50 cm — obstacleBlocked() false. Move to 20 cm —
//     true, and the robot stops. Withdraw to 30 cm — STILL blocked (hysteresis).
//     Past 35 cm — clears and motion resumes. That is R2's 15-35 cm band.
//  2. Wave a hand through the beam once, fast: ULTRA_AGREE_COUNT should absorb it
//     without a spurious stop.
//  3. E-stop: press it while driving. Motors must be dead in well under 0.5 s and
//     the payload must stay locked (R9). Release — it stays latched until cleared.
//  4. Comms: unplug the Pi's USB cable while driving. Within ~2 s the robot stops
//     and reports SAFEHOLD_COMMS. Replug — it clears and is flagged (R10).
//  5. Loop timing: print micros() around loop(); no iteration should ever spike to
//     tens of ms. If one does, something is blocking and must be fixed.

#include "safety.h"
#include "config.h"

const char *overrideName(Override o) {
  switch (o) {
  case OVR_TELEOP: return "TELEOP";
  case OVR_SAFEHOLD_COMMS: return "SAFEHOLD_COMMS";
  case OVR_OBSTACLE_HOLD: return "OBSTACLE_HOLD";
  case OVR_ESTOPPED: return "ESTOPPED";
  default: return "";
  }
}

namespace Safety {

// --- ultrasonic (interrupt-driven, never blocking) -------------------------
static volatile uint32_t s_echoRiseUs = 0;
static volatile uint32_t s_echoWidthUs = 0;
static volatile bool s_echoReady = false;

static uint32_t s_lastPingMs = 0;
static bool s_pingInFlight = false;
static float s_distCm = -1.0f;
static bool s_blocked = false;
static uint8_t s_agree = 0;
static bool s_pendingBlocked = false;
static uint16_t s_invalidRun = 0;    // consecutive invalid/no-echo reads
static bool s_faultReported = false; // so the fault is announced once, not spammed

// --- e-stop ----------------------------------------------------------------
static bool s_estop = false;
static bool s_estopRaw = false;
static uint32_t s_estopChangeMs = 0;

// --- comms watchdog --------------------------------------------------------
static uint32_t s_lastFrameMs = 0;
// Distinct from "fed a long time ago": at boot millis() is ~0, so a plain
// elapsed-time test would report comms HEALTHY for the first 2 s after power-on,
// before the Pi has said a single word. Fail-closed means starting silent.
static bool s_everFed = false;

// The ISR does the absolute minimum: timestamp the edges. All interpretation
// happens later, in update(), on the main loop.
static void IRAM_ATTR echoIsr() {
  if (digitalRead(PIN_ULTRA_ECHO)) {
    s_echoRiseUs = micros();
  } else {
    s_echoWidthUs = micros() - s_echoRiseUs;
    s_echoReady = true;
  }
}

void begin() {
#if !HAS_ULTRASONIC
  // Obstacle detection is compiled out (config.h HAS_ULTRASONIC 0). Say so
  // loudly and repeatedly enough that nobody can claim they didn't know.
  Serial.println(F("{\"t\":\"state\",\"s\":\"BOOT\",\"ovr\":null,"
                   "\"why\":\"WARNING: HAS_ULTRASONIC=0 - NO OBSTACLE SAFETY\"}"));
#endif
  pinMode(PIN_ULTRA_TRIG, OUTPUT);
  digitalWrite(PIN_ULTRA_TRIG, LOW);
  pinMode(PIN_ULTRA_ECHO, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_ULTRA_ECHO), echoIsr, CHANGE);

#if HAS_ESTOP_SENSE
#if ESTOP_ACTIVE_LOW
  pinMode(PIN_ESTOP_SENSE, INPUT_PULLUP);
#else
  pinMode(PIN_ESTOP_SENSE, INPUT_PULLDOWN);
#endif
#endif
  // With HAS_ESTOP_SENSE 0 we deliberately do NOT configure the pin. Reading an
  // unwired input would float and could report phantom E-stops, which is worse
  // than reporting none: a safety signal that cries wolf gets ignored.

  // Start the watchdog already "silent" so the robot cannot move until the Pi
  // has actually said something. Fail-closed by default.
  s_lastFrameMs = 0;
}

void notifyFrameReceived() {
  s_lastFrameMs = millis();
  s_everFed = true;
}

#if HAS_ULTRASONIC
static void updateUltrasonic() {
  uint32_t now = millis();

  if (s_echoReady) {
    s_echoReady = false;
    uint32_t w = s_echoWidthUs;
    s_pingInFlight = false;
    // Speed of sound ~343 m/s -> 58 us per cm round trip. ULTRA_SCALE is a
    // bench calibration factor and is 1.0 unless someone deliberately set it
    // (see config.h — fix the ECHO wiring before using it).
    float cm = (w == 0 || w > ULTRA_TIMEOUT_US)
                   ? -1.0f
                   : ((float)w / 58.0f) * ULTRA_SCALE;

    // VALIDITY GATE. A reading below the sensor's physical minimum is not a
    // very close obstacle — it is noise (floating ECHO pin, missing level
    // shifter, motor interference). Treating it as an obstacle is how a
    // disconnected sensor pins the robot in place with nothing in front of it.
    bool valid = (cm >= ULTRA_MIN_VALID_CM && cm <= ULTRA_MAX_VALID_CM);
    if (valid) {
      s_distCm = cm;
      s_invalidRun = 0;
      s_faultReported = false;
    } else {
      // Invalid / no echo = NO INFORMATION. Report it as out-of-range rather
      // than inventing a distance.
      s_distCm = -1.0f;
      if (s_invalidRun < 65535) s_invalidRun++;
    }

    // No echo, or an invalid reading, means "nothing measurable in range" —
    // that is CLEAR, not blocked. Only a VALID close reading blocks.
    bool wantBlocked = valid && (cm <= (s_blocked ? OBSTACLE_CLEAR_CM
                                                  : OBSTACLE_STOP_CM));

    // Require N consecutive agreeing reads before flipping, so one bad ping
    // (they are common on HC-SR04) cannot slam the brakes or, worse, release them.
    if (wantBlocked == s_pendingBlocked) {
      if (s_agree < 255) s_agree++;
    } else {
      s_pendingBlocked = wantBlocked;
      s_agree = 1;
    }
    if (s_agree >= ULTRA_AGREE_COUNT) s_blocked = s_pendingBlocked;
  }

  // Fire the next ping on a fixed cadence. A ping that never echoed simply times
  // out here — we never sit and wait for it.
  if (now - s_lastPingMs >= ULTRA_PING_MS) {
    s_lastPingMs = now;
    if (s_pingInFlight) {
      // Previous ping never came back: treat as out of range, keep going.
      s_pingInFlight = false;
      s_distCm = -1.0f;
    }
    s_pingInFlight = true;
    // A 10 us trigger pulse. This is the ONLY busy-wait in the firmware and it is
    // 10 microseconds — four orders of magnitude below pulseIn()'s worst case.
    digitalWrite(PIN_ULTRA_TRIG, HIGH);
    delayMicroseconds(10);
    digitalWrite(PIN_ULTRA_TRIG, LOW);
  }
}
#endif  // HAS_ULTRASONIC

static void updateEstop() {
#if HAS_ESTOP_SENSE
  bool raw = digitalRead(PIN_ESTOP_SENSE);
#if ESTOP_ACTIVE_LOW
  raw = !raw;
#endif
  uint32_t now = millis();
  if (raw != s_estopRaw) {
    s_estopRaw = raw;
    s_estopChangeMs = now;
  }
  if (now - s_estopChangeMs >= ESTOP_DEBOUNCE_MS) s_estop = s_estopRaw;
#else
  // No E-stop sense wire on this build (config.h HAS_ESTOP_SENSE 0).
  //
  // The physical E-stop still cuts MOTOR POWER in hardware, so the robot does
  // stop and R9's "motors halt < 0.5 s" is unaffected — that path never went
  // through software. What we cannot do is NOTICE, so no ESTOPPED override
  // latches and no estop event reaches the audit log.
  //
  // Reporting false here is the honest answer to "did I detect an E-stop?" —
  // we did not, because we cannot. It is NOT a claim that no E-stop happened.
  s_estop = false;
#endif
}

void update() {
  updateEstop();
#if HAS_ULTRASONIC
  updateUltrasonic();
#else
  // Override active: never measure, never block. s_blocked stays false so
  // obstacleBlocked() is always clear and OBSTACLE_HOLD can never fire.
  s_distCm = -1.0f;
  s_blocked = false;
#endif
}

bool estopActive() { return s_estop; }

bool ultrasonicOverridden() {
#if HAS_ULTRASONIC
  return false;
#else
  return true;
#endif
}

// True once the sensor has returned nothing usable for a long run of pings.
// Distinguishes "clear corridor" from "sensor is unplugged/faulty", which look
// identical if you only watch obstacleBlocked().
bool ultrasonicFaulty() {
#if HAS_ULTRASONIC
  return s_invalidRun >= ULTRA_FAULT_AFTER;
#else
  return false;
#endif
}

bool takeUltrasonicFaultNotice() {
#if HAS_ULTRASONIC
  if (s_invalidRun >= ULTRA_FAULT_AFTER && !s_faultReported) {
    s_faultReported = true;
    return true;
  }
#endif
  return false;
}
bool obstacleBlocked() { return s_blocked; }
float obstacleCm() { return s_distCm; }

bool commsSilent() {
  if (!s_everFed) return true; // never heard from the Pi at all
  return (millis() - s_lastFrameMs) > COMMS_TIMEOUT_MS;
}

Override highest(bool teleopActive) {
  if (s_estop) return OVR_ESTOPPED;
  if (s_blocked) return OVR_OBSTACLE_HOLD;
  if (commsSilent()) return OVR_SAFEHOLD_COMMS;
  if (teleopActive) return OVR_TELEOP;
  return OVR_NONE;
}

bool motionForbidden(bool teleopActive) {
  Override o = highest(teleopActive);
  // TELEOP is an override in the sense that it takes control away from autonomy,
  // but it is NOT a motion ban — it is how a human drives the robot (R14).
  return (o == OVR_ESTOPPED || o == OVR_OBSTACLE_HOLD || o == OVR_SAFEHOLD_COMMS);
}

} // namespace Safety
