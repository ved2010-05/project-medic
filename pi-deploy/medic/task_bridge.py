"""task_bridge.py — dashboard <-> MCU bridge (Raspberry Pi planner brain).

This is the ONLY place the two-scan auth decision gets acted on, so it is the
most safety-sensitive file in the Pi bundle even though it never touches a
motor. Read ARCHITECTURE.md §0.4 and §0.1 before changing anything here:

  - §0.4 Two-scan auth FAILS CLOSED. This file may only ever send a
    "dispense" or "latch" command to the MCU after Central.verify_auth()
    (medic.common) has come back with authorized=True. Every other outcome
    -- mismatch, unknown tag, HTTP error, timeout, malformed reply, or the
    30 s auth window running out -- results in NO command, a RED audit
    event, and the refusal buzzer. There is no default-allow path.
  - §0.1 This file only REPORTS what the MCU decided (obstacle hold, estop,
    comms safe-hold). It never decides to hold or stop anything itself --
    that logic lives only in the ESP32 firmware.

What this script does, in one paragraph: it polls the dashboard for this
robot's active task, forwards MCU telemetry (state, obstacle, estop, temp,
rfid, ack) up to the dashboard as telemetry + audit events, runs the
two-scan-auth state machine while the MCU is in AT_WARD_WAIT_AUTH, forwards
teleop nudges from the dashboard down to the MCU, and keeps the MCU's 2 s
comms watchdog fed with a heartbeat whenever nothing else was just sent.

Three independent scripts share the Pi (nav.py, ears.py, task_bridge.py --
see pi-deploy/DESIGN.md). If this one crashes, nav still drives and the MCU still
safe-holds on its own 2 s watchdog -- but the loop above builds every effort
into never crashing, because a live task_bridge is what keeps that watchdog
from ever needing to fire.

Both the dashboard and the MCU are allowed to be absent. Neither missing
peer may crash this process, block it, or cause it to unlock anything --
see medic.common.SerialLink and medic.common.Central, which this module
relies on for that behaviour rather than reimplementing it.

BENCH TEST:
  1. No MCU, no dashboard yet -- the "everything unplugged" case:
       cd pi-deploy
       python -m medic.task_bridge --log-level DEBUG
     You should see ONE warning that no serial port was found and the loop
     keep ticking forever (heartbeats attempted, dashboard polls retried).
     Ctrl-C must exit cleanly and print "task_bridge stopped cleanly".

  2. With the dashboard up (`python dashboard/app.py` in another terminal)
     but still no MCU: dispatch a task from the dashboard UI and confirm
     task_bridge's log shows "active task changed: None -> <id>". Nothing
     else should happen (no MCU means no state/rfid messages arrive).

  3. Exercising the auth path without real hardware -- use a virtual serial
     port pair so you can hand-feed MCU-shaped JSON lines:
       socat -d -d pty,raw,echo=0,link=/tmp/mcu_a pty,raw,echo=0,link=/tmp/mcu_b
       python -m medic.task_bridge --serial-port /tmp/mcu_a
     Then, in a third terminal, feed lines into /tmp/mcu_b one at a time
     (each `echo` is one MCU->Pi message) and watch task_bridge's log plus
     the dashboard's audit page:
       exec 3>/tmp/mcu_b
       echo '{"t":"state","s":"EN_ROUTE"}' >&3
       echo '{"t":"state","s":"AT_WARD_WAIT_AUTH"}' >&3
       echo '{"t":"rfid","uid":"04A1B2C3","reader":"scan1"}' >&3
       echo '{"t":"rfid","uid":"04D4E5F6","reader":"scan2"}' >&3
     If the staff/patient UIDs match a real dispatched task in the mock DB
     you should see a "dispense" command echoed in the log (DEBUG level)
     and a dispense_ok event once you also feed a matching ack:
       echo '{"t":"ack","of":"dispense","ok":true,"n":2}' >&3
     Feed a WRONG patient UID instead and confirm you get auth_refused, a
     buzz command, and NO dispense line -- that refusal is the R3/R4 demo.

  4. Timeout path: open the auth window (steps above through
     AT_WARD_WAIT_AUTH) and feed no rfid at all. After 30 s you should see
     an auth_timeout red event and a buzz command, with no dispense.
"""

import argparse
import signal
import sys
import time

from medic.common import (
    INFO,
    RED,
    WARN,
    Central,
    SerialLink,
    add_common_args,
    load_config,
    setup_logging,
)

# ---------------------------------------------------------------------------
# Tuning constants.
# ---------------------------------------------------------------------------
# Main loop rate. This just needs to be fast enough that the heartbeat and
# the 30 s auth timeout are checked with a fine enough grain -- it is NOT a
# camera loop, so 10 Hz is plenty and keeps the Pi's CPU budget for nav.py.
LOOP_HZ = 10.0

# docs/serial-protocol-v1.md: "The window resets ... or on the 30 s auth
# timeout." This is OUR side of that timeout -- the MCU has its own separate
# state timeout underneath us, so a dead task_bridge cannot leave the robot
# stuck waiting forever either.
AUTH_WINDOW_S = 30.0

# Buzzer pattern id sent on a refusal. The actual pattern (its length/rhythm)
# is decided when the buzzer is wired to the MCU and its "ux" handler is
# written -- this Pi-side code only needs a stable id.
# TODO(day-6): confirm this id against firmware's ux pattern table once the
# buzzer is wired (docs/build-plan.md Day 6 = integration begins).
BUZZ_REFUSAL = 3


class Bridge(object):
    """Holds all task_bridge state and runs the poll/relay loop.

    Everything that is NOT a safety decision lives here: reading MCU
    telemetry and turning it into dashboard events, running the two-scan
    auth handshake, and passing teleop nudges through. Nothing here ever
    decides to stop or hold the robot -- see the module docstring.
    """

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log

        self.link = SerialLink(cfg.serial_port, cfg.serial_baud)
        self.central = Central(cfg.central_url, cfg.robot_id, cfg.http_timeout)

        # The dashboard's idea of "what should this robot be doing right now".
        self.task = None

        # -- heartbeat / link bookkeeping ------------------------------------
        self.last_send_ts = 0.0
        self.link_was_connected = False  # start "offline"; see _check_link()

        # -- last-seen MCU telemetry, for edge-detection ---------------------
        self.last_state = None
        self.last_ovr = None
        self.last_blocked = False
        self.last_estop = False
        self._departed_task_id = None  # last task_id we posted "depart" for

        # -- two-scan auth window --------------------------------------------
        self.auth_task_id = None
        self.auth_opened_at = None
        self.auth_staff_uid = None
        self.auth_patient_uid = None
        self.auth_decided = False
        # Task ids we have already ruled on, ever. "One task, one decision" --
        # even if the MCU re-enters AT_WARD_WAIT_AUTH for the same task_id
        # (e.g. a state flap), we must not give it a second chance to turn a
        # refusal into an approval.
        self.decided_task_ids = set()

        # Commands we sent to the MCU and are waiting on an ack for.
        self._pending_dispense = None  # {"task_id", "count", "open_latch"}
        self._pending_latch = None  # {"task_id"}

        self.last_teleop_seq = None

        self._stop = False
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError, AttributeError):
                # Not available on this platform/thread. Harmless on a Pi;
                # this only matters on odd dev setups.
                pass

    # -- lifecycle ------------------------------------------------------------
    def _on_signal(self, signum, _frame):
        self.log.info("signal %s received -- shutting down", signum)
        self._stop = True

    def run(self):
        self.log.info(
            "task_bridge up: robot_id=%s central=%s serial=%s",
            self.cfg.robot_id,
            self.cfg.central_url,
            self.cfg.serial_port,
        )

        # Baseline the teleop cursor BEFORE reacting to anything. The
        # dashboard's teleop mailbox (docs/http-api-v1.md GET /api/v1/teleop)
        # is "latest command wins" with no expiry, and self.last_teleop_seq
        # only lives in this process's memory -- it resets to None on every
        # restart (a crash, `systemctl restart`, or systemd's own
        # Restart=on-failure). Without this, whatever command was already
        # sitting in the mailbox at start-up (e.g. one delivered in a
        # previous life of this process, long before this restart) would
        # look "new" and get replayed straight to the MCU. Reading it once
        # here and treating it as already-seen closes that hole without
        # touching the frozen HTTP contract or the dashboard's display of
        # "what's queued". If the dashboard is unreachable this just leaves
        # last_teleop_seq at None, same as before -- no worse than today.
        self.last_teleop_seq = self.central.get_teleop().get("seq")

        period = 1.0 / LOOP_HZ
        last_task_poll = 0.0
        last_teleop_poll = 0.0

        while not self._stop:
            t0 = time.time()
            try:
                for msg in self.link.poll():
                    self._handle_mcu_message(msg)

                self._check_link_transition()

                now = time.time()
                if now - last_task_poll >= self.cfg.poll_interval:
                    self._refresh_task()
                    last_task_poll = now
                if now - last_teleop_poll >= self.cfg.poll_interval:
                    self._refresh_teleop()
                    last_teleop_poll = now

                self._check_auth_timeout()
                self._heartbeat_if_idle()
            except Exception:
                # A single bad iteration must never take the bridge down: if
                # task_bridge dies, heartbeats stop and the MCU safe-holds
                # after 2 s (safe, but it ends the demo). Log and keep going.
                self.log.exception("unhandled error in task_bridge loop -- continuing")

            time.sleep(max(0.0, period - (time.time() - t0)))

        self._shutdown()
        return 0

    def _shutdown(self):
        # systemd sends SIGTERM before killing the process -- stop the robot
        # first. The MCU would safe-hold on its own after 2 s of silence
        # anyway, but say it explicitly rather than relying on the watchdog.
        self._send({"t": "stop"})
        self.link.close()
        self.log.info("task_bridge stopped cleanly")

    # -- small helpers ---------------------------------------------------------
    def _current_task_id(self):
        return self.task.get("task_id") if self.task else None

    def _send(self, obj):
        """Write one command to the MCU and log it. Never raises (SerialLink
        is a dumb pipe that no-ops when nothing is connected)."""
        ok = self.link.send(obj)
        self.last_send_ts = time.time()
        self.log.debug("TX -> MCU: %s (sent=%s)", obj, ok)
        return ok

    def _heartbeat_if_idle(self):
        """Feed the MCU's 2 s comms watchdog. cfg.heartbeat_interval (750 ms
        default) is comfortably inside that budget."""
        if time.time() - self.last_send_ts >= self.cfg.heartbeat_interval:
            self._send({"t": "ping"})

    def _check_link_transition(self):
        """robot_online / robot_offline on MCU serial link transitions.

        We start assuming "offline" and only ever announce a transition, so
        a Pi that boots before the ESP32 is wired up does not spam an
        offline event for a state that was never actually online.
        """
        connected = self.link.connected
        if connected and not self.link_was_connected:
            self.central.post_event(
                "robot_online",
                detail="MCU serial link up on %s" % (self.link.path or "?"),
                severity=INFO,
            )
        elif not connected and self.link_was_connected:
            self.central.post_event(
                "robot_offline", detail="MCU serial link lost", severity=WARN
            )
        self.link_was_connected = connected

    # -- dashboard polling ------------------------------------------------------
    def _refresh_task(self):
        task = self.central.get_active_task()
        prev_id = self._current_task_id()
        self.task = task
        new_id = self._current_task_id()
        if new_id != prev_id:
            self.log.info("active task changed: %s -> %s", prev_id, new_id)

    def _refresh_teleop(self):
        """R14 parachute + the live supervised nudge. Only acts on a NEW
        seq so a slow poll can never replay a stale nudge."""
        data = self.central.get_teleop()
        cmd = data.get("cmd")
        if cmd is None:
            return
        seq = data.get("seq")
        if seq == self.last_teleop_seq:
            return
        self.last_teleop_seq = seq
        spd = data.get("spd", 0.0)
        self._send({"t": "teleop", "cmd": cmd, "spd": spd})
        self.central.post_event(
            "teleop_nudge",
            detail="cmd=%s spd=%s (seq=%s)" % (cmd, spd, seq),
            task_id=self._current_task_id(),
        )

    # -- MCU message dispatch ----------------------------------------------------
    def _handle_mcu_message(self, msg):
        self.log.debug("RX <- MCU: %s", msg)
        t = msg.get("t")
        if t == "state":
            self._handle_state(msg)
        elif t == "obstacle":
            self._handle_obstacle(msg)
        elif t == "estop":
            self._handle_estop(msg)
        elif t == "temp":
            self._handle_temp(msg)
        elif t == "ack":
            self._handle_ack(msg)
        elif t == "pong":
            pass  # heartbeat reply -- the serial link being alive is enough
        else:
            self.log.debug("unhandled MCU message type: %r", t)

    def _handle_state(self, msg):
        s = msg.get("s")
        ovr = msg.get("ovr")
        if not s:
            return

        # Always relay raw telemetry for the fleet view, regardless of
        # whether anything "interesting" (an edge) happened.
        self.central.post_telemetry(state=s, ovr=ovr)

        # SAFEHOLD_COMMS has no dedicated MCU->Pi message of its own -- the
        # override field on 'state' is the only place it is visible. §0.1:
        # we are reporting the MCU's decision, never making it.
        if ovr != self.last_ovr:
            task_id = self._current_task_id()
            if ovr == "SAFEHOLD_COMMS":
                self.central.post_event(
                    "safehold_comms",
                    detail="MCU lost comms with the Pi and is safe-holding",
                    task_id=task_id,
                    severity=RED,
                )
            elif self.last_ovr == "SAFEHOLD_COMMS":
                # R10: "flagged on reconnect" -- this IS that flag.
                self.central.post_event(
                    "safehold_comms",
                    detail="comms restored, override cleared",
                    task_id=task_id,
                    severity=INFO,
                )
            self.last_ovr = ovr

        if s != self.last_state:
            prev = self.last_state
            self.log.info("MCU state: %s -> %s", prev, s)

            if s == "EN_ROUTE" and self.task:
                tid = self.task["task_id"]
                if self._departed_task_id != tid:
                    self._departed_task_id = tid
                    self.central.post_event(
                        "depart", detail="leaving pharmacy for task %s" % tid, task_id=tid
                    )
                    self.central.set_task_state(tid, "en_route")

            # No AT_WARD_WAIT_AUTH in this build: there is no RFID reader and
            # no two-scan step, so the MCU dispenses on arrival instead of
            # parking in an auth window. See _handle_dispense_ack().

            self.last_state = s

    def _handle_obstacle(self, msg):
        d = msg.get("d")
        blocked = bool(msg.get("blocked"))
        self.central.post_telemetry(obstacle_cm=d, blocked=blocked)
        if blocked != self.last_blocked:
            if blocked:
                self.central.post_event(
                    "obstacle_hold",
                    detail="MCU holding, obstacle at %s cm" % (d if d is not None else "?"),
                    task_id=self._current_task_id(),
                    severity=WARN,
                )
            else:
                self.central.post_event(
                    "obstacle_hold",
                    detail="obstacle cleared, MCU resuming",
                    task_id=self._current_task_id(),
                    severity=INFO,
                )
            self.last_blocked = blocked

    def _handle_estop(self, msg):
        active = bool(msg.get("active"))
        self.central.post_telemetry(estop=active)
        if active != self.last_estop:
            if active:
                self.central.post_event(
                    "estop",
                    detail="physical E-stop engaged",
                    task_id=self._current_task_id(),
                    severity=RED,
                )
            else:
                self.central.post_event(
                    "estop",
                    detail="E-stop cleared",
                    task_id=self._current_task_id(),
                    severity=INFO,
                )
            self.last_estop = active

    def _handle_temp(self, msg):
        c = msg.get("c")
        if c is None:
            return
        # Relayed as soon as it arrives -- no throttling here, so we cannot
        # be the cause of a gap in the R6 gap-free temperature log. The DS18B20
        # read cadence (~30 s, see ARCHITECTURE.md §5) is the MCU's job.
        self.central.post_telemetry(temp_c=c)
        self.central.post_event("temp_reading", detail="%.1f C" % float(c))

    # -- two-scan auth ------------------------------------------------------------
    def _open_auth_window(self):
        task_id = self._current_task_id()
        self.auth_task_id = task_id
        # time.monotonic(), not time.time(): a Pi has no RTC and commonly
        # boots with a stale saved clock that steps once NTP catches up. A
        # backward step here would make `elapsed` in _check_auth_timeout()
        # negative and let the 30 s auth window run indefinitely long --
        # the client-side half of the fail-closed auth timeout (§0.4) would
        # silently stop protecting anything. Matches ears.py's convention.
        self.auth_opened_at = time.monotonic()
        self.auth_staff_uid = None
        self.auth_patient_uid = None
        self.auth_decided = False
        self.log.info("auth window opened for task %s", task_id)

        if task_id is None:
            # The MCU thinks it is waiting for a badge but the dashboard has
            # no active task for this robot. Fail closed and loud -- never
            # guess which task a scan might belong to (§0.4).
            self._refuse("auth_refused", "no_active_task")
            return

        if task_id in self.decided_task_ids:
            # Re-entered the wait state for a task we already ruled on.
            # "One task, one decision" -- do not collect scans again.
            self.auth_decided = True
            self.log.info("task %s already decided -- ignoring re-entry", task_id)
            return

        self.central.set_task_state(task_id, "awaiting_auth")

    def _close_auth_window(self, reason):
        self.log.info("auth window closed: %s", reason)
        self.auth_task_id = None
        self.auth_opened_at = None
        self.auth_staff_uid = None
        self.auth_patient_uid = None
        self.auth_decided = False

    def _check_auth_timeout(self):
        if self.auth_opened_at is None or self.auth_decided:
            return
        elapsed = time.monotonic() - self.auth_opened_at
        if elapsed > AUTH_WINDOW_S:
            self._refuse(
                "auth_timeout",
                "auth window expired after %.0fs with no decision" % elapsed,
            )

    def _handle_rfid(self, msg):
        uid = msg.get("uid")
        reader = msg.get("reader")
        if not uid or reader not in ("scan1", "scan2"):
            return

        # Fail closed: a tag read outside the open auth window is not part
        # of any decision. The MCU only reports UIDs (docs/serial-protocol
        # -v1.md) -- matching them to a task is entirely our job, and we
        # only do that inside AT_WARD_WAIT_AUTH.
        if self.last_state != "AT_WARD_WAIT_AUTH":
            self.log.warning(
                "rfid %s (%s) received outside AT_WARD_WAIT_AUTH -- ignored", uid, reader
            )
            return
        if self.auth_decided:
            self.log.info(
                "rfid %s (%s) ignored -- task %s already decided", uid, reader, self.auth_task_id
            )
            return

        if reader == "scan1" and uid != self.auth_staff_uid:
            self.auth_staff_uid = uid
            self.central.post_event(
                "scan_staff", detail="staff badge %s" % uid, task_id=self.auth_task_id
            )
        elif reader == "scan2" and uid != self.auth_patient_uid:
            self.auth_patient_uid = uid
            self.central.post_event(
                "scan_patient", detail="patient tag %s" % uid, task_id=self.auth_task_id
            )

        if self.auth_staff_uid and self.auth_patient_uid:
            self._attempt_auth()

    def _attempt_auth(self):
        if self.auth_decided or self.auth_task_id is None:
            return
        # Mark decided BEFORE the network call returns, not after -- this is
        # a synchronous call in a single-threaded loop, so this also closes
        # the (tiny) window where a duplicate rfid message could trigger a
        # second verify_auth call for the same pair of scans.
        self.auth_decided = True
        self.decided_task_ids.add(self.auth_task_id)

        result = self.central.verify_auth(
            self.auth_task_id, self.auth_staff_uid, self.auth_patient_uid
        )
        # medic.common.Central.verify_auth() is the fail-closed gate: every
        # transport error, timeout, or malformed reply already comes back as
        # authorized=False here. There is no branch below that can unlock
        # anything without an explicit True.
        if result.get("authorized") is True:
            self._approve(result)
        else:
            self._refuse("auth_refused", result.get("reason", "refused"))

    def _approve(self, result):
        task_id = self.auth_task_id
        count = result["dispense_count"]
        # Only open the latch if BOTH the dashboard says so (open_latch) AND
        # our local copy of the task agrees it has a cold item -- two
        # independent signals have to agree before we unlock the cold box.
        open_latch = bool(result.get("open_latch")) and bool(
            self.task and self.task.get("cold_item")
        )
        self.log.info("AUTH OK task %s -> dispense %d (latch=%s)", task_id, count, open_latch)
        self._pending_dispense = {
            "task_id": task_id,
            "count": count,
            "open_latch": open_latch,
        }
        self._send({"t": "dispense", "count": count})
        self.central.set_task_state(task_id, "dispensing")

    def _refuse(self, kind, reason):
        """The one path that ends an auth window without unlocking anything.
        kind is "auth_refused" (an explicit refusal) or "auth_timeout" (our
        own 30 s window ran out). Both send NO dispense and NO latch."""
        task_id = self.auth_task_id
        self.log.error("AUTH REFUSED task %s: %s", task_id, reason)
        self.central.post_event(kind, detail=reason, task_id=task_id, severity=RED)
        self._send({"t": "ux", "buzz": BUZZ_REFUSAL})
        if task_id is not None:
            self.central.set_task_state(task_id, "refused")
            self.decided_task_ids.add(task_id)
        self.auth_decided = True

    # -- acks -----------------------------------------------------------------------
    def _handle_ack(self, msg):
        of = msg.get("of")
        ok = bool(msg.get("ok"))
        n = msg.get("n")
        why = msg.get("why")
        if of == "dispense":
            self._handle_dispense_ack(ok, n, why)
        elif of == "latch":
            self._handle_latch_ack(ok, why)
        else:
            self.log.debug("ack of=%s ok=%s -- not tracked on the dashboard", of, ok)

    def _handle_dispense_ack(self, ok, n, why):
        pending = self._pending_dispense
        if pending is None:
            # NORMAL PATH in this build. There is no RFID/two-scan step, so the
            # MCU starts the dispense ITSELF the moment nav parks it at the
            # ward ({"t":"stop","at":"ward","count":N}) -- this bridge never
            # sent a 'dispense' command and so has nothing "pending".
            #
            # Before this branch existed the ack was dropped with a warning and
            # the task sat in 'arrived' forever: delivered candy, no delivered
            # state, nothing in the audit trail. Reconstruct the pending record
            # from the active task instead.
            task = self.central.get_active_task()
            if not task or task.get("task_id") is None:
                self.log.warning(
                    "dispense ack (ok=%s n=%r) with no pending dispense and no "
                    "active task -- ignored", ok, n)
                return
            pending = {
                "task_id": task.get("task_id"),
                # Trust the MCU's own count as the request: it was handed the
                # number on the stop message, and it is the thing that counted.
                "count": int(n) if (ok and n is not None) else 0,
                "open_latch": False,  # no latch hardware in this build
            }
            self.log.info("dispense ack for task %s (MCU-triggered on arrival)",
                          pending["task_id"])
        self._pending_dispense = None
        task_id = pending["task_id"]
        requested = pending["count"]

        if not ok:
            self.central.post_event(
                "dispense_fail",
                detail="MCU refused: %s" % (why or "not ok"),
                task_id=task_id,
                severity=RED,
            )
            self.central.set_task_state(task_id, "aborted")
            return

        # R5 cares about the EXACT count, so "ok" alone is not enough -- a
        # missing n is treated the same as a wrong n, because we then have
        # no way to confirm the count that actually happened.
        n_matches = False
        try:
            n_matches = n is not None and int(n) == requested
        except (TypeError, ValueError):
            n_matches = False

        if not n_matches:
            self.central.post_event(
                "dispense_fail",
                detail="requested %d, MCU ack n=%r -- exact count not confirmed" % (requested, n),
                task_id=task_id,
                severity=RED,
            )
            self.central.set_task_state(task_id, "aborted")
            return

        self.central.post_event(
            "dispense_ok", detail="%d unit(s) dispensed" % requested, task_id=task_id
        )
        if pending["open_latch"]:
            self._send({"t": "latch", "open": True})
            self._pending_latch = {"task_id": task_id}
        else:
            self._complete_task(task_id)

    def _handle_latch_ack(self, ok, why):
        pending = self._pending_latch
        if pending is None:
            self.log.debug("latch ack received with nothing pending -- ignored")
            return
        self._pending_latch = None
        task_id = pending["task_id"]

        if ok:
            self.central.post_event(
                "latch_open", detail="cold-box latch opened", task_id=task_id
            )
            self._complete_task(task_id)
        else:
            self.central.post_event(
                "latch_open",
                detail="MCU refused to open the latch: %s" % (why or "not ok"),
                task_id=task_id,
                severity=RED,
            )
            # Mirror _handle_dispense_ack's failure path: without this the
            # task is left in "dispensing" forever -- that state is not in
            # db.TERMINAL_STATES, so get_active_task() keeps handing this
            # same wedged task back on every future poll and the run never
            # reaches a terminal audit outcome (R12).
            self.central.set_task_state(task_id, "aborted")

    def _complete_task(self, task_id):
        self.central.post_event("task_complete", detail="delivery complete", task_id=task_id)
        self.central.set_task_state(task_id, "complete")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="MEDIC dashboard <-> MCU bridge")
    add_common_args(ap)
    args = ap.parse_args()
    cfg = load_config(args)
    log = setup_logging(cfg.log_level, "task_bridge")

    bridge = Bridge(cfg, log)
    return bridge.run()


if __name__ == "__main__":
    sys.exit(main())
