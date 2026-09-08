// drive.cpp — open-loop differential drive (no encoders). See drive.h.
//
// BENCH TEST:
//  1. Wheels off the ground. Send {"t":"drive","hdg":0,"spd":0.4} — both wheels
//     forward at the same speed. Send {"t":"stop"} — both stop smoothly.
//  2. Send {"t":"drive","hdg":20,"spd":0.4} — LEFT wheel speeds up (steer RIGHT).
//     Send hdg:-20 — RIGHT wheel speeds up. Getting this sign wrong makes the
//     robot run AWAY from every marker, so check it before driving on the floor.
//  3. Send {"t":"search","dir":"cw"} — wheels counter-rotate, robot spins in place.
//  4. Robot on the floor, drive straight 2 m at spd 0.4. It will curve, because
//     there is no encoder to correct it. Note which way, then lower the TRIM of
//     the faster side in config.h and repeat until it tracks acceptably straight.
//  5. While driving, assert E-stop: output must reach zero immediately, with no
//     ramp-down at all (R9 < 0.5 s).

#include "drive.h"
#include "config.h"

namespace Drive {

// ESP32 Arduino core 3.x replaced ledcSetup/ledcAttachPin with ledcAttach.
// Support both so this builds on whatever core the team's PlatformIO pulls.
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
#define MEDIC_LEDC_V3 1
#else
#define MEDIC_LEDC_V3 0
#define LEDC_CH_A 0
#define LEDC_CH_B 1
#endif

// Commanded wheel outputs, -1..+1. Target is what we want, current is what we
// are actually writing (slew-limited toward target).
static float s_targetL = 0.0f, s_targetR = 0.0f;
static float s_curL = 0.0f, s_curR = 0.0f;
static uint32_t s_lastUpdate = 0;

static float clampf(float v, float lo, float hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

// Map a signed -1..+1 command onto the motor pins. |v| is expanded into the
// [MIN_DUTY, MAX_DUTY] band because anything below MIN_DUTY just stalls and hums.
static void writeMotor(uint8_t pwmPin, uint8_t dir1, uint8_t dir2, float v,
                       float trim) {
  bool forward = (v >= 0.0f);
  float mag = clampf(fabsf(v) * trim, 0.0f, 1.0f);

  uint32_t duty;
  if (mag < 0.02f) {
    duty = 0; // true zero — coast/brake, not a stalled hum
    digitalWrite(dir1, LOW);
    digitalWrite(dir2, LOW);
  } else {
    duty = (uint32_t)(MIN_DUTY + mag * (float)(MAX_DUTY - MIN_DUTY));
    digitalWrite(dir1, forward ? HIGH : LOW);
    digitalWrite(dir2, forward ? LOW : HIGH);
  }

#if MEDIC_LEDC_V3
  ledcWrite(pwmPin, duty);
#else
  ledcWrite(pwmPin == PIN_MOTOR_A_PWM ? LEDC_CH_A : LEDC_CH_B, duty);
#endif
}

void begin() {
  pinMode(PIN_MOTOR_A_DIR1, OUTPUT);
  pinMode(PIN_MOTOR_A_DIR2, OUTPUT);
  pinMode(PIN_MOTOR_B_DIR1, OUTPUT);
  pinMode(PIN_MOTOR_B_DIR2, OUTPUT);

#if MEDIC_LEDC_V3
  ledcAttach(PIN_MOTOR_A_PWM, PWM_FREQ_HZ, PWM_RESOLUTION);
  ledcAttach(PIN_MOTOR_B_PWM, PWM_FREQ_HZ, PWM_RESOLUTION);
#else
  ledcSetup(LEDC_CH_A, PWM_FREQ_HZ, PWM_RESOLUTION);
  ledcSetup(LEDC_CH_B, PWM_FREQ_HZ, PWM_RESOLUTION);
  ledcAttachPin(PIN_MOTOR_A_PWM, LEDC_CH_A);
  ledcAttachPin(PIN_MOTOR_B_PWM, LEDC_CH_B);
#endif

  emergencyStop();
}

void setGoal(float hdgErrDeg, float spd) {
  spd = clampf(spd, 0.0f, 1.0f);
  // Positive hdg = marker is to the RIGHT = steer right = speed the LEFT wheel up.
  float turn = clampf(hdgErrDeg * STEER_GAIN, -STEER_MAX, STEER_MAX);

  float l = spd + turn;
  float r = spd - turn;

  // If steering pushed a wheel past full scale, scale BOTH down rather than
  // clipping one. Clipping would quietly change the turn rate we asked for.
  float peak = fmaxf(fabsf(l), fabsf(r));
  if (peak > 1.0f) {
    l /= peak;
    r /= peak;
  }
  s_targetL = l;
  s_targetR = r;
}

void rotate(int8_t dir, float spd) {
  spd = clampf(spd, 0.0f, 1.0f);
  // dir +1 = clockwise seen from above = left wheel forward, right wheel back.
  s_targetL = (dir >= 0) ? spd : -spd;
  s_targetR = (dir >= 0) ? -spd : spd;
}

void teleop(uint8_t cmd, float spd) {
  spd = clampf(spd, 0.0f, 1.0f);
  switch (cmd) {
  case TELEOP_FWD:
    s_targetL = spd;
    s_targetR = spd;
    break;
  case TELEOP_BACK:
    s_targetL = -spd;
    s_targetR = -spd;
    break;
  case TELEOP_LEFT:
    s_targetL = -spd;
    s_targetR = spd;
    break;
  case TELEOP_RIGHT:
    s_targetL = spd;
    s_targetR = -spd;
    break;
  default:
    s_targetL = 0.0f;
    s_targetR = 0.0f;
    break;
  }
}

void stop() {
  s_targetL = 0.0f;
  s_targetR = 0.0f;
}

void emergencyStop() {
  // No ramp, no trim, no slew — straight to zero. R9 depends on this path.
  s_targetL = s_targetR = s_curL = s_curR = 0.0f;
  digitalWrite(PIN_MOTOR_A_DIR1, LOW);
  digitalWrite(PIN_MOTOR_A_DIR2, LOW);
  digitalWrite(PIN_MOTOR_B_DIR1, LOW);
  digitalWrite(PIN_MOTOR_B_DIR2, LOW);
#if MEDIC_LEDC_V3
  ledcWrite(PIN_MOTOR_A_PWM, 0);
  ledcWrite(PIN_MOTOR_B_PWM, 0);
#else
  ledcWrite(LEDC_CH_A, 0);
  ledcWrite(LEDC_CH_B, 0);
#endif
}

bool isMoving() { return fabsf(s_curL) > 0.02f || fabsf(s_curR) > 0.02f; }

void benchSide(uint8_t side, float v) {
  v = clampf(v, -1.0f, 1.0f);
  // Writes the same target the autonomy path writes, so the slew limiter and
  // the per-side trim still apply. Deliberate: bench control should behave like
  // the real drive, otherwise "it works on the bench" proves nothing about the
  // demo. It also keeps the L298N from being slammed between full reverse and
  // full forward by an impatient double-click.
  if (side == 0) s_targetL = v;
  else s_targetR = v;
}

float benchValue(uint8_t side) { return side == 0 ? s_targetL : s_targetR; }

static float slew(float cur, float target) {
  float d = target - cur;
  if (d > SLEW_PER_LOOP) d = SLEW_PER_LOOP;
  if (d < -SLEW_PER_LOOP) d = -SLEW_PER_LOOP;
  return cur + d;
}

void update() {
  uint32_t now = millis();
  if (now - s_lastUpdate < DRIVE_UPDATE_MS) return; // fixed cadence, non-blocking
  s_lastUpdate = now;

  s_curL = slew(s_curL, s_targetL);
  s_curR = slew(s_curR, s_targetR);

  writeMotor(PIN_MOTOR_A_PWM, PIN_MOTOR_A_DIR1, PIN_MOTOR_A_DIR2, s_curL,
             MOTOR_A_TRIM);
  writeMotor(PIN_MOTOR_B_PWM, PIN_MOTOR_B_DIR1, PIN_MOTOR_B_DIR2, s_curR,
             MOTOR_B_TRIM);
}

} // namespace Drive
