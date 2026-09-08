#pragma once
// serial_link.h — newline-delimited JSON to the Pi. See docs/serial-protocol-v1.md.
//
// Deliberately hand-rolled instead of pulling in ArduinoJson: our messages are
// flat objects with a handful of keys, so a tiny scanner over a fixed char buffer
// is smaller, has zero dynamic allocation, and cannot fragment the heap in the
// control loop (firmware/DESIGN.md).
//
// Forward compatibility is a protocol requirement: unknown message types and
// unknown fields are silently ignored, never an error.

#include <Arduino.h>

namespace SerialLink {

enum CmdType : uint8_t {
  CMD_NONE = 0,
  CMD_DRIVE,
  CMD_STOP,
  CMD_SEARCH,
  CMD_TELEOP,
  CMD_UX,
  CMD_PING,
  CMD_DISPENSE, // manual/retry trigger; see medic_fw.ino for the normal path
  CMD_LATCH,    // parsed and refused by this build (no latch hardware)
  CMD_BENCH,    // enter/leave the I/O bench (dashboard Live tab)
  CMD_IO,       // set one output channel while in bench mode
};

// Optional 'at' field on 'stop' (protocol v1 additive section).
enum AtStation : uint8_t { AT_NONE = 0, AT_PHARMACY, AT_WARD, AT_WAYPOINT };
// Optional 'leg' field on 'drive'.
enum Leg : uint8_t { LEG_OUTBOUND = 0, LEG_RETURN, LEG_PATROL };

struct Command {
  CmdType type;
  float hdg;        // drive: heading error, degrees, + = right
  float spd;        // drive / teleop: 0..1
  int8_t dir;       // search: +1 cw, -1 ccw
  uint8_t teleopCmd; // Drive::TeleopCmd
  uint8_t led, buzz;
  AtStation at;
  Leg leg;
  // dispense: units to drop. Also read on 'stop' when at=="ward" — there is no
  // scan/auth step in this build, so arrival at the ward is itself the
  // dispense trigger, and the count rides along on that same message.
  int32_t count;
  bool open;        // latch
  bool on;          // bench: enter/leave.  io: channel on/off
  char ch[12];      // io: channel name -- motor_l/motor_r/servo/buzzer/led
  float v;          // io: channel value (-1..1 motors, 0..180 servo)
};

void begin();

// Reads available bytes, assembles complete lines, parses at most
// MAX_CMDS_PER_LOOP of them. Never blocks.
void update();

// Pops one parsed command. Returns false when the queue is empty.
bool poll(Command &out);

// --- emitters (MCU -> Pi) --------------------------------------------------
void emitObstacle(float cm, bool blocked);
void emitEstop(bool active);
void emitState(const char *state, const char *ovr, uint32_t sinceMs);
void emitAck(const char *of, bool ok, int32_t n, const char *why);
void emitPong();
void emitTemp(float c); // unused in the nav slice

// Full I/O snapshot for the dashboard Live tab: every output's commanded state
// and every input's reading, in one frame.
void emitIo(bool bench, float motorL, float motorR, int servoDeg, bool buzz,
            bool led, bool latchOpen, float ultraCm, bool ultraValid,
            bool ultraBlocked, bool ultraOverridden, bool ultraFaulty,
            bool estop, bool commsSilent, const char *state, const char *ovr,
            uint32_t uptimeMs, uint32_t loopHz, uint32_t rxFrames,
            uint32_t badFrames);

// NOTE: there is no emitOdom(). This build has no encoders, so the protocol's
// 'odom' message is simply never sent. nav.py must not wait for it.
//
// NOTE: there is no emitRfid(). This build has no RFID reader, and there is
// no two-scan verification step — arrival at the ward is the dispense
// trigger (see the 'count' comment above). If RFID hardware is added back
// later, reintroduce a scan message and the auth gate it implies.

} // namespace SerialLink
