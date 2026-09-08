  // Project MEDIC — ESP32 reflex brain. NAVIGATION SLICE (no encoders yet).
  //
  // Owns motion + SAFETY. Safety reflexes override any Pi command (ARCHITECTURE.md
  // §0.1). Non-blocking; no delay() in states (§0.6).
  //
  // State machine: IDLE_AT_PHARMACY -> EN_ROUTE -> MARKER_SEARCH -> DISPENSING
  //                -> RETURNING -> PATROL
  // Overrides: OBSTACLE_HOLD, SAFEHOLD_COMMS, ESTOPPED, TELEOP
  //
  // There is no RFID reader and no two-scan verification in this build. Arrival
  // at the ward ({"t":"stop","at":"ward","count":N}) IS the dispense trigger —
  // the count rides on that same message. The one gate left is the one that
  // matters most: Safety::motionForbidden() (§0.4, fail-closed) is still checked
  // before Payload::dispense() is ever called, exactly as for every other
  // actuation in this file.
  //
  // See firmware/DESIGN.md and docs/serial-protocol-v1.md.
  //
  // BENCH TEST (whole file):
  //  1. Boot with nothing connected: reports SAFEHOLD_COMMS and refuses to move.
  //     That is correct — no Pi, no autonomy.
  //  2. {"t":"ping"} -> {"t":"pong"}, and the state leaves SAFEHOLD_COMMS.
  //  3. {"t":"drive","hdg":0,"spd":0.3} -> state EN_ROUTE, wheels turn.
  //  4. Hold a book 20 cm from the sensor: state shows ovr OBSTACLE_HOLD, wheels
  //     stop, and further drive commands do NOTHING until you withdraw past 35 cm,
  //     at which point EN_ROUTE resumes by itself (R2).
  //  5. {"t":"search","dir":"cw"} -> MARKER_SEARCH, spins in place, and self-stops
  //     after MARKER_SEARCH_TIMEOUT_MS instead of spinning forever (R15).
  //  6. {"t":"stop","at":"ward","count":2} -> state DISPENSING immediately, servo
  //     sweeps twice, then {"t":"ack","of":"dispense","ok":true,"n":2}.
  //  7. Mid-dispense safety: repeat step 6 with count 5 and put your hand in
  //     front of the ultrasonic. The disk parks at HOME and the ack reports the
  //     SHORT count, not 5.
  //  8. Unplug USB while driving -> stops within ~2 s, SAFEHOLD_COMMS (R10).
  //  9. E-stop while driving -> motors dead in < 0.5 s (R9).
  // 10. With the Pi unplugged, send teleop from a plain serial monitor: the robot
  //     still drives. That is the R14 parachute.

  #include <Arduino.h>
  #include "config.h"
  #include "drive.h"
  #include "safety.h"
  #include "serial_link.h"
  #include "payload.h"
  #include "ux.h"

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  enum RobotState : uint8_t {
    ST_IDLE_AT_PHARMACY = 0,
    ST_EN_ROUTE,
    ST_MARKER_SEARCH,
    ST_DISPENSING,
    ST_RETURNING,
    ST_PATROL,
  };

  static const char *stateName(RobotState s) {
    switch (s) {
    case ST_IDLE_AT_PHARMACY: return "IDLE_AT_PHARMACY";
    case ST_EN_ROUTE: return "EN_ROUTE";
    case ST_MARKER_SEARCH: return "MARKER_SEARCH";
    case ST_DISPENSING: return "DISPENSING";
    case ST_RETURNING: return "RETURNING";
    case ST_PATROL: return "PATROL";
    default: return "UNKNOWN";
    }
  }

  static RobotState s_state = ST_IDLE_AT_PHARMACY;
  static uint32_t s_stateSince = 0;

  // Recomputed at the top of every loop(), before any Pi command is executed, so
  // command handlers can consult the SAME answer the loop is acting on. A handler
  // must never re-derive this independently — that is how the two get out of step
  // and a command slips through during an override.
  static bool s_forbidden = true;  // starts true: nothing moves until we've checked
  static bool forbiddenNow() { return s_forbidden; }

  // How many units the Pi asked for on the dispense currently running. Kept so the
  // ack can compare requested vs actually-dropped — R5 fails on a short count, so
  // "did it finish" is not the same question as "did it succeed".
  static int32_t s_requestedDispense = 0;

  // True while an override is holding us. We resume IN PLACE (the state itself is
  // untouched), so all we need to remember is that we were preempted — that is what
  // tells us to restart the state timeout when the override clears (R2/R10).
  static bool s_preempted = false;

  // TELEOP is sticky: once the operator takes control we stay in teleop until an
  // autonomous command (drive/search) arrives. Otherwise a single nudge would be
  // instantly overwritten by the next autonomous goal.
  static bool s_teleop = false;
  static uint32_t s_lastTeleopMs = 0;
  #define TELEOP_RELEASE_MS 3000

  // BENCH: the dashboard Live tab's I/O panel has direct, LATCHING control of
  // every output. Like TELEOP it takes the wheels from autonomy, but unlike
  // TELEOP it is NOT sticky-with-timeout — a latched output must stay latched, so
  // it is left explicitly by the operator (or by the comms watchdog killing the
  // motors, or by a reboot).
  //
  // It is deliberately NOT an Override enum value: overrides are safety states
  // that FORBID motion, and bench PERMITS it. Adding it there would have put a
  // browser button into the safety precedence chain, which ARCHITECTURE.md §0.1
  // forbids outright. Safety::motionForbidden() still decides, unchanged.
  static bool s_bench = false;
  #define IO_EMIT_MS 300  // full I/O snapshot cadence while bench is active

  // Link health, surfaced in the I/O snapshot so a flaky USB lead is visible as a
  // rising bad-frame count rather than as "the robot feels laggy".
  static uint32_t s_loopCount = 0, s_loopHz = 0, s_lastHzWindow = 0;
  static uint32_t s_lastIoEmit = 0;

  // Last commanded autonomous goal, re-applied each loop while EN_ROUTE/RETURNING.
  static float s_goalHdg = 0.0f, s_goalSpd = 0.0f;
  static int8_t s_searchDir = 1;

  // Telemetry bookkeeping
  static uint32_t s_lastStateEmit = 0, s_lastObsEmit = 0;
  static bool s_lastBlocked = false, s_lastEstop = false;
  static Override s_lastOvr = OVR_NONE;

  static void enterState(RobotState s) {
    if (s == s_state) return;
    s_state = s;
    s_stateSince = millis();

    // ENTRY ACTIONS.
    switch (s) {
    case ST_MARKER_SEARCH:
      Drive::rotate(s_searchDir, SEARCH_SPD);
      break;
    case ST_IDLE_AT_PHARMACY:
      Drive::stop();
      s_goalSpd = 0.0f;
      break;
    default:
      break;
    }
    SerialLink::emitState(stateName(s_state), overrideName(Safety::highest(s_teleop)),
                          0);
    s_lastStateEmit = millis();
  }

  // Attempts to start a dispense right now: checked against the one gate that
  // matters (motion-forbidden, §0.4 fail-closed) plus the mechanical ones
  // (already busy, bad count). Used both for arrival-at-ward (the normal path)
  // and for a manual/retry 'dispense' command from the Pi.
  static void tryDispense(int32_t count) {
    if (forbiddenNow()) {
      SerialLink::emitAck("dispense", false, 0, "safety_override_active");
    } else if (Payload::isBusy()) {
      SerialLink::emitAck("dispense", false, 0, "busy");
    } else if (!Payload::dispense(count)) {
      SerialLink::emitAck("dispense", false, 0, "bad_count");
    } else {
      s_requestedDispense = count;
      enterState(ST_DISPENSING);
      Ux::setPattern(Ux::UX_DISPENSING);
      // No ack yet — it goes out when the sequencer finishes, carrying the
      // count ACTUALLY dropped. R5 grades the exact number, so acking "ok"
      // up front would be lying about something we haven't done.
    }
  }

  // ---------------------------------------------------------------------------
  // Commands
  // ---------------------------------------------------------------------------
  static void handleCommand(const SerialLink::Command &c) {
    // A teleop command from a human always wins over a stale autonomous goal.
    if (c.type == SerialLink::CMD_TELEOP) {
      s_teleop = true;
      s_lastTeleopMs = millis();
    } else if (c.type == SerialLink::CMD_DRIVE || c.type == SerialLink::CMD_SEARCH) {
      s_teleop = false; // autonomy resumes
    }

    switch (c.type) {
    case SerialLink::CMD_PING:
      SerialLink::emitPong();
      break;

    case SerialLink::CMD_DRIVE:
      s_goalHdg = c.hdg;
      s_goalSpd = c.spd;
      // Remember which way to sweep if we lose the marker: toward where it was.
      s_searchDir = (c.hdg >= 0.0f) ? 1 : -1;
      if (c.leg == SerialLink::LEG_RETURN) enterState(ST_RETURNING);
      else if (c.leg == SerialLink::LEG_PATROL) enterState(ST_PATROL);
      else enterState(ST_EN_ROUTE);
      break;

    case SerialLink::CMD_SEARCH:
      s_searchDir = c.dir;
      enterState(ST_MARKER_SEARCH);
      break;

    case SerialLink::CMD_STOP:
      s_goalSpd = 0.0f;
      Drive::stop();
      if (c.at == SerialLink::AT_WARD) {
        // Arrival at the ward is the dispense trigger — no scan/auth step to
        // wait for. If it can't start right now (override active, busy, bad
        // count) tryDispense() acks the failure and we simply hold here; the
        // Pi can retry with a plain {"t":"dispense"} once whatever blocked it
        // clears.
        tryDispense(c.count);
      } else if (c.at == SerialLink::AT_PHARMACY) {
        enterState(ST_IDLE_AT_PHARMACY);
      }
      // AT_NONE / AT_WAYPOINT: just hold position in the current state.
      break;

    case SerialLink::CMD_TELEOP:
      // Motion permission is checked in loop(), not here.
      Drive::teleop(c.teleopCmd, c.spd);
      break;

    case SerialLink::CMD_UX:
      Ux::setFromSerial(c.led, c.buzz);
      break;

    case SerialLink::CMD_DISPENSE:
      // Manual/retry path — e.g. after a short count aborted a run. Normal
      // dispensing happens via CMD_STOP's at=="ward" above; this is the same
      // check, just not gated on any particular state.
      tryDispense(c.count);
      break;

    case SerialLink::CMD_LATCH:
      if (forbiddenNow()) {
        SerialLink::emitAck("latch", false, 0, "safety_override_active");
      } else if (!Payload::latch(c.open)) {
        SerialLink::emitAck("latch", false, 0, "no_latch_hardware");
      } else {
        SerialLink::emitAck("latch", true, 0, "");
      }
      break;

    default:
      break;
    }
  }

  // ---------------------------------------------------------------------------
  static void stepStateMachine(bool motionAllowed) {
    uint32_t now = millis();
    uint32_t inState = now - s_stateSince;

    if (!motionAllowed) return; // overrides already stopped the wheels

    // While a human is driving, autonomy must NOT keep writing motor targets —
    // otherwise the next loop silently overwrites the operator's nudge and the
    // teleop panel feels dead. TELEOP is an override: it takes the wheels.
    bool autonomyDrives = !s_teleop;

    switch (s_state) {
    case ST_EN_ROUTE:
    case ST_RETURNING:
    case ST_PATROL:
      // Re-apply the Pi's last goal every loop. If the Pi stops talking, the comms
      // watchdog takes over long before this matters.
      if (autonomyDrives) Drive::setGoal(s_goalHdg, s_goalSpd);
      if (inState > (s_state == ST_PATROL ? PATROL_TIMEOUT_MS : EN_ROUTE_TIMEOUT_MS)) {
        Drive::stop();
        enterState(ST_IDLE_AT_PHARMACY);
      }
      break;

    case ST_MARKER_SEARCH:
      if (autonomyDrives) Drive::rotate(s_searchDir, SEARCH_SPD);
      // R15: bounded. We never sweep forever — we stop and let the Pi raise the
      // "marker lost" alert. With no encoders this budget is TIME, not angle.
      if (inState > MARKER_SEARCH_TIMEOUT_MS) {
        Drive::stop();
        enterState(ST_IDLE_AT_PHARMACY);
      }
      break;

    case ST_IDLE_AT_PHARMACY:
      if (autonomyDrives) Drive::stop();
      break;

    case ST_DISPENSING:
      if (autonomyDrives) Drive::stop();
      break;
    }
  }

  // The override always wins the LED/buzzer, because "why isn't it moving?" is
  // the question you actually need answered from across the room. Only when
  // nothing is overriding does the pattern reflect the state machine.
  static void updateUxForState(Override ovr) {
    switch (ovr) {
    case OVR_ESTOPPED:       Ux::setPattern(Ux::UX_ESTOP);    return;
    case OVR_OBSTACLE_HOLD:  Ux::setPattern(Ux::UX_OBSTACLE); return;
    case OVR_SAFEHOLD_COMMS: Ux::setPattern(Ux::UX_SAFEHOLD); return;
    default: break;  // TELEOP and NONE fall through to the state pattern
    }

    switch (s_state) {
    case ST_EN_ROUTE:
    case ST_RETURNING:
    case ST_PATROL:           Ux::setPattern(Ux::UX_EN_ROUTE);   break;
    case ST_MARKER_SEARCH:    Ux::setPattern(Ux::UX_MARKER_LOST); break;
    case ST_DISPENSING:       Ux::setPattern(Ux::UX_DISPENSING);  break;
    default:                  Ux::setPattern(Ux::UX_IDLE);        break;
    }
  }

  static void emitTelemetry(Override ovr) {
    uint32_t now = millis();

#if HAS_ULTRASONIC
    bool blocked = Safety::obstacleBlocked();
    if (blocked != s_lastBlocked || now - s_lastObsEmit >= OBSTACLE_EMIT_MS) {
      s_lastBlocked = blocked;
      s_lastObsEmit = now;
      SerialLink::emitObstacle(Safety::obstacleCm(), blocked);
    }

    // A sensor that has gone quiet for a long run of pings is FAULTY, not clear.
    // Without this the failure is invisible: obstacleBlocked() just reads false
    // forever and the robot happily drives with no working obstacle sensor.
    if (Safety::takeUltrasonicFaultNotice()) {
      SerialLink::emitAck("obstacle", false, 0,
                          "ultrasonic returning no valid readings - check ECHO "
                          "wiring/level shifter");
    }
#else
    // No sensor fitted. Emit NOTHING about obstacles rather than a cheerful
    // stream of {"d":-1,"blocked":false} — that reads on the dashboard as a
    // working sensor seeing a clear path, which is the exact false-confidence
    // this build must not create. Silence is honest; the boot banner and the
    // periodic warning below say why it is silent.
    (void)now;
    if (now - s_lastObsEmit >= NO_OBSTACLE_WARN_MS) {
      s_lastObsEmit = now;
      SerialLink::emitAck("obstacle", false, 0,
                          "NO OBSTACLE SENSOR FITTED - nothing will stop this "
                          "robot for an obstacle");
    }
#endif

    bool es = Safety::estopActive();
    if (es != s_lastEstop) {
      s_lastEstop = es;
      SerialLink::emitEstop(es);
    }

    if (ovr != s_lastOvr || now - s_lastStateEmit >= STATE_EMIT_MS) {
      s_lastOvr = ovr;
      s_lastStateEmit = now;
      SerialLink::emitState(stateName(s_state), overrideName(ovr),
                            now - s_stateSince);
    }
  }

  void setup() {
    Serial.begin(SERIAL_BAUD);
    Drive::begin();
    Safety::begin();
    SerialLink::begin();
    Payload::begin();  // parks the magazine disk at HOME = closed
    Ux::begin();
    s_stateSince = millis();

    // Announce a compiled-out obstacle sensor ONCE, at boot, unmistakably. This
    // reaches the Pi, the dashboard and the audit log, so a robot running with no
    // obstacle protection can never look identical to a normal one. Emitted here
    // rather than per state change, which would just spam the trail.
    if (Safety::ultrasonicOverridden()) {
      SerialLink::emitAck("boot", false, 0,
                          "HAS_ULTRASONIC=0 - NO OBSTACLE SAFETY - bench use only");
    }
    // Starts in SAFEHOLD_COMMS by design: the watchdog has never been fed, so the
    // robot cannot move until the Pi actually says something. Fail-closed on boot.
  }

  void loop() {
    // ORDER MATTERS. firmware/DESIGN.md: "Safety checks run every loop, before
    // executing any Pi goal."

    // 1) SAFETY FIRST — always, unconditionally.
    Safety::update();

    if (s_teleop && millis() - s_lastTeleopMs > TELEOP_RELEASE_MS) s_teleop = false;

    Override ovr = Safety::highest(s_teleop);
    bool forbidden = Safety::motionForbidden(s_teleop);
    s_forbidden = forbidden;  // publish it BEFORE any command handler runs

    if (forbidden) {
      if (ovr == OVR_ESTOPPED) {
        Drive::emergencyStop(); // no ramp — R9
      } else {
        Drive::stop();
      }
      if (!s_preempted) {
        // Only on the EDGE into the override, not every loop: lockdown() aborts a
        // dispense in progress and parks the disk, and calling it repeatedly would
        // keep re-triggering that. An interrupted dispense reports the short count
        // through the normal result path below, which becomes a dispense_fail.
        Payload::lockdown();
      }
      s_preempted = true;
    } else if (s_preempted) {
      s_preempted = false;
      s_stateSince = millis(); // don't let the hold burn the state's timeout
    }

    // 2) Parse the Pi. We still parse while an override is active — telemetry and
    //    ping must keep flowing, and refusing to read would strand the watchdog.
    SerialLink::update();
    SerialLink::Command c;
    while (SerialLink::poll(c)) {
      // MOTION commands are IGNORED (not queued) while an override forbids motion.
      bool isMotion = (c.type == SerialLink::CMD_DRIVE ||
                      c.type == SerialLink::CMD_SEARCH ||
                      c.type == SerialLink::CMD_TELEOP);
      if (forbidden && isMotion) continue;
      handleCommand(c);
    }

    // 3) Step the state machine.
    stepStateMachine(!forbidden);

    // 4) Actuators.
    Drive::update();
    Payload::update();

    // 4b) A finished dispense acks with the count ACTUALLY dropped. R5 grades the
    //     exact number, so ok is true only if we delivered what was asked for —
    //     a short count (jam, or a safety override that aborted the run) is a
    //     FAILURE, not a partial success. task_bridge turns that into a red
    //     dispense_fail event.
    int32_t actuated = 0;
    if (Payload::takeResult(actuated)) {
      bool exact = (actuated == s_requestedDispense);
      SerialLink::emitAck("dispense", exact, actuated,
                          exact ? "" : "short_count");
      Ux::oneShot(exact ? Ux::UX_AUTH_OK : Ux::UX_AUTH_REFUSED);
      s_requestedDispense = 0;
      if (s_state == ST_DISPENSING) enterState(ST_IDLE_AT_PHARMACY);
    }

    // 4c) UX follows the winning override, else the state.
    updateUxForState(ovr);
    Ux::update();

    // 5) Telemetry.
    emitTelemetry(ovr);
  }
