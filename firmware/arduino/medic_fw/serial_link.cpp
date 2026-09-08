// serial_link.cpp — JSON-lines codec. See serial_link.h and docs/serial-protocol-v1.md.
//
// BENCH TEST:
//  1. Open a serial monitor at 115200 and paste:
//       {"t":"ping"}
//     You must get exactly {"t":"pong"} back on its own line.
//  2. Paste garbage: `}}}not json{{{` then a good {"t":"ping"}. The pong must
//     still arrive — one bad line may never wedge the parser.
//  3. Paste a line longer than SERIAL_LINE_MAX, then {"t":"ping"}. The long line
//     is discarded whole and the following line still parses.
//  4. Paste {"t":"drive","hdg":-4,"spd":0.3,"leg":"outbound","future":"ignored"}.
//     The unknown "future" field must be ignored, not rejected (protocol rule).
//  5. Send {"t":"wobble"} — an unknown TYPE must also be silently ignored.
//  6. Stop sending anything for 3 s: the robot reports SAFEHOLD_COMMS. Note that
//     the garbage line in step 2 must NOT have reset that watchdog.

#include "serial_link.h"
#include "safety.h"
#include "config.h"
#include "drive.h"

namespace SerialLink {

static char s_line[SERIAL_LINE_MAX + 1];
static uint16_t s_len = 0;
static bool s_overflow = false; // current line is too long; drop it entirely

static Command s_queue[MAX_CMDS_PER_LOOP];
static uint8_t s_qHead = 0, s_qTail = 0;

// ---------------------------------------------------------------------------
// Minimal JSON field scanners. Our messages are flat, so we look for "key"
// followed by a colon and read the value that follows. Good enough for this
// protocol; deliberately not a general JSON parser.
// ---------------------------------------------------------------------------
static const char *findKey(const char *s, const char *key) {
  char needle[24];
  snprintf(needle, sizeof(needle), "\"%s\"", key);
  const char *p = strstr(s, needle);
  if (!p) return nullptr;
  p += strlen(needle);
  while (*p == ' ') p++;
  if (*p != ':') return nullptr;
  p++;
  while (*p == ' ') p++;
  return p;
}

static bool jsonStr(const char *s, const char *key, char *out, size_t n) {
  const char *p = findKey(s, key);
  if (!p || *p != '"') return false;
  p++;
  size_t i = 0;
  while (*p && *p != '"' && i < n - 1) out[i++] = *p++;
  out[i] = '\0';
  return true;
}

static bool jsonNum(const char *s, const char *key, float *out) {
  const char *p = findKey(s, key);
  if (!p) return false;
  if (*p != '-' && *p != '+' && *p != '.' && !isdigit((unsigned char)*p))
    return false;
  *out = atof(p);
  return true;
}

static bool jsonBool(const char *s, const char *key, bool *out) {
  const char *p = findKey(s, key);
  if (!p) return false;
  if (!strncmp(p, "true", 4)) { *out = true; return true; }
  if (!strncmp(p, "false", 5)) { *out = false; return true; }
  return false;
}

static void push(const Command &c) {
  uint8_t next = (uint8_t)((s_qTail + 1) % MAX_CMDS_PER_LOOP);
  if (next == s_qHead) return; // queue full — drop the oldest-arriving extra
  s_queue[s_qTail] = c;
  s_qTail = next;
}

// Returns true if the line was a message we recognise well enough to count as a
// valid frame for the comms watchdog.
static bool parseLine(const char *line) {
  char t[16];
  if (!jsonStr(line, "t", t, sizeof(t))) return false;

  Command c;
  memset(&c, 0, sizeof(c));
  c.type = CMD_NONE;
  float f;

  if (!strcmp(t, "drive")) {
    c.type = CMD_DRIVE;
    c.hdg = jsonNum(line, "hdg", &f) ? f : 0.0f;
    c.spd = jsonNum(line, "spd", &f) ? f : 0.0f;
    char leg[12];
    c.leg = LEG_OUTBOUND;
    if (jsonStr(line, "leg", leg, sizeof(leg))) {
      if (!strcmp(leg, "return")) c.leg = LEG_RETURN;
      else if (!strcmp(leg, "patrol")) c.leg = LEG_PATROL;
    }
  } else if (!strcmp(t, "stop")) {
    c.type = CMD_STOP;
    char at[12];
    c.at = AT_NONE;
    if (jsonStr(line, "at", at, sizeof(at))) {
      if (!strcmp(at, "pharmacy")) c.at = AT_PHARMACY;
      else if (!strcmp(at, "ward")) c.at = AT_WARD;
      else if (!strcmp(at, "waypoint")) c.at = AT_WAYPOINT;
    }
    // Only meaningful when at=="ward": arrival at the ward is the dispense
    // trigger in this build (no RFID/auth step), so the count rides on the
    // same message instead of waiting for a separate command.
    c.count = jsonNum(line, "count", &f) ? (int32_t)f : 0;
  } else if (!strcmp(t, "search")) {
    c.type = CMD_SEARCH;
    char d[8];
    c.dir = 1;
    if (jsonStr(line, "dir", d, sizeof(d)) && !strcmp(d, "ccw")) c.dir = -1;
  } else if (!strcmp(t, "teleop")) {
    c.type = CMD_TELEOP;
    c.spd = jsonNum(line, "spd", &f) ? f : 0.35f;
    char cmd[10];
    c.teleopCmd = Drive::TELEOP_STOP;
    if (jsonStr(line, "cmd", cmd, sizeof(cmd))) {
      if (!strcmp(cmd, "fwd")) c.teleopCmd = Drive::TELEOP_FWD;
      else if (!strcmp(cmd, "back")) c.teleopCmd = Drive::TELEOP_BACK;
      else if (!strcmp(cmd, "left")) c.teleopCmd = Drive::TELEOP_LEFT;
      else if (!strcmp(cmd, "right")) c.teleopCmd = Drive::TELEOP_RIGHT;
    }
  } else if (!strcmp(t, "ux")) {
    c.type = CMD_UX;
    c.led = jsonNum(line, "led", &f) ? (uint8_t)f : 0;
    c.buzz = jsonNum(line, "buzz", &f) ? (uint8_t)f : 0;
  } else if (!strcmp(t, "ping")) {
    c.type = CMD_PING;
  } else if (!strcmp(t, "dispense")) {
    c.type = CMD_DISPENSE;
    c.count = jsonNum(line, "count", &f) ? (int32_t)f : 0;
  } else if (!strcmp(t, "latch")) {
    c.type = CMD_LATCH;
    bool b;
    c.open = jsonBool(line, "open", &b) ? b : false;
  } else if (!strcmp(t, "bench")) {
    c.type = CMD_BENCH;
    bool b;
    c.on = jsonBool(line, "on", &b) ? b : false;
  } else if (!strcmp(t, "io")) {
    c.type = CMD_IO;
    bool b;
    if (!jsonStr(line, "ch", c.ch, sizeof(c.ch))) c.ch[0] = '\0';
    c.on = jsonBool(line, "on", &b) ? b : false;
    c.v = jsonNum(line, "v", &f) ? f : 0.0f;
  } else {
    // Unknown type. The protocol says ignore it — but it IS well-formed traffic
    // from a live Pi, so it still counts as a frame for the watchdog.
    return true;
  }

  push(c);
  return true;
}

void begin() {
  s_len = 0;
  s_overflow = false;
  s_qHead = s_qTail = 0;
}

void update() {
  uint8_t parsed = 0;
  while (Serial.available() > 0 && parsed < MAX_CMDS_PER_LOOP) {
    char ch = (char)Serial.read();

    if (ch == '\n' || ch == '\r') {
      if (s_len > 0 && !s_overflow) {
        s_line[s_len] = '\0';
        if (parseLine(s_line)) {
          // Only a line we actually understood feeds the 2 s watchdog. Garbage
          // on the wire must never make a dead link look alive.
          Safety::notifyFrameReceived();
        }
        parsed++;
      }
      s_len = 0;
      s_overflow = false;
      continue;
    }

    if (s_len >= SERIAL_LINE_MAX) {
      // Too long: mark it and keep consuming until the newline, so the NEXT
      // line still starts clean instead of inheriting our tail.
      s_overflow = true;
      s_len = 0;
      continue;
    }
    if (!s_overflow) s_line[s_len++] = ch;
  }
}

bool poll(Command &out) {
  if (s_qHead == s_qTail) return false;
  out = s_queue[s_qHead];
  s_qHead = (uint8_t)((s_qHead + 1) % MAX_CMDS_PER_LOOP);
  return true;
}

// --- emitters --------------------------------------------------------------
// snprintf into a fixed buffer, one object per line. No Arduino String anywhere.
static char s_out[160];

void emitIo(bool bench, float motorL, float motorR, int servoDeg, bool buzz,
            bool led, bool latchOpen, float ultraCm, bool ultraValid,
            bool ultraBlocked, bool ultraOverridden, bool ultraFaulty,
            bool estop, bool commsSilent, const char *state, const char *ovr,
            uint32_t uptimeMs, uint32_t loopHz, uint32_t rxFrames,
            uint32_t badFrames) {
  // Built in two halves: one snprintf of this many fields would overrun the
  // fixed s_out buffer, and a truncated JSON line is worse than none — the Pi
  // would log a parse error every single frame.
  snprintf(s_out, sizeof(s_out),
           "{\"t\":\"io\",\"bench\":%s,\"out\":{\"motor_l\":%.2f,\"motor_r\":%.2f,"
           "\"servo\":%d,\"buzzer\":%s,\"led\":%s,\"latch\":%s},",
           bench ? "true" : "false", motorL, motorR, servoDeg,
           buzz ? "true" : "false", led ? "true" : "false",
           latchOpen ? "true" : "false");
  Serial.print(s_out);
  snprintf(s_out, sizeof(s_out),
           "\"in\":{\"ultra_cm\":%.1f,\"ultra_valid\":%s,\"ultra_blocked\":%s,"
           "\"ultra_overridden\":%s,\"ultra_faulty\":%s,\"estop\":%s,"
           "\"comms_silent\":%s},\"state\":\"%s\",\"ovr\":\"%s\","
           "\"up_ms\":%lu,\"loop_hz\":%lu,\"rx\":%lu,\"bad\":%lu}",
           ultraCm, ultraValid ? "true" : "false",
           ultraBlocked ? "true" : "false",
           ultraOverridden ? "true" : "false", ultraFaulty ? "true" : "false",
           estop ? "true" : "false", commsSilent ? "true" : "false", state, ovr,
           (unsigned long)uptimeMs, (unsigned long)loopHz,
           (unsigned long)rxFrames, (unsigned long)badFrames);
  Serial.println(s_out);
}

void emitObstacle(float cm, bool blocked) {
  snprintf(s_out, sizeof(s_out), "{\"t\":\"obstacle\",\"d\":%.1f,\"blocked\":%s}",
           cm, blocked ? "true" : "false");
  Serial.println(s_out);
}

void emitEstop(bool active) {
  snprintf(s_out, sizeof(s_out), "{\"t\":\"estop\",\"active\":%s}",
           active ? "true" : "false");
  Serial.println(s_out);
}

void emitState(const char *state, const char *ovr, uint32_t sinceMs) {
  if (ovr && ovr[0]) {
    snprintf(s_out, sizeof(s_out),
             "{\"t\":\"state\",\"s\":\"%s\",\"ovr\":\"%s\",\"since_ms\":%lu}",
             state, ovr, (unsigned long)sinceMs);
  } else {
    snprintf(s_out, sizeof(s_out),
             "{\"t\":\"state\",\"s\":\"%s\",\"ovr\":null,\"since_ms\":%lu}", state,
             (unsigned long)sinceMs);
  }
  Serial.println(s_out);
}

void emitAck(const char *of, bool ok, int32_t n, const char *why) {
  snprintf(s_out, sizeof(s_out),
           "{\"t\":\"ack\",\"of\":\"%s\",\"ok\":%s,\"n\":%ld,\"why\":\"%s\"}", of,
           ok ? "true" : "false", (long)n, why ? why : "");
  Serial.println(s_out);
}

void emitPong() { Serial.println("{\"t\":\"pong\"}"); }

void emitTemp(float c) {
  snprintf(s_out, sizeof(s_out), "{\"t\":\"temp\",\"c\":%.2f}", c);
  Serial.println(s_out);
}

} // namespace SerialLink
