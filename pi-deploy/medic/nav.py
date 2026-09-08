"""nav.py — ArUco visual waypoint navigation (Raspberry Pi).

Calibration-free path (primary): steer by the marker's horizontal pixel
offset, stop by apparent marker width; solvePnP pose would be a fallback
only (not implemented here -- the calibration-free path has been enough).
Arrival = at standoff AND detected marker ID == task destination. Marker
density per leg is a tuned dial, not a fixed route. See pi-deploy/DESIGN.md and
markers/README.md.

NEVER put obstacle/E-stop/comms-hold or the supervised nudge here -- safety
and teleop live on the MCU (TELEOP override); nav only ever emits
heading/speed goals and LOGS what the MCU reports back (invariant §0.1).

THIS BUILD HAS NO ENCODERS.
    The robot sends no 'odom', so there is no distance feedback at all (see
    docs/no-encoder-nav.md). Two consequences, both handled below:
      * The "coast between markers" step is bounded by TIME (coast_s), not
        by odometry distance as docs/design-doc-v0.3.md §4.1 assumes.
      * MARKER_SEARCH is bounded by TIME (search_s), not by a swept angle.
    R15 ("never wanders") still holds -- the bound is just wall-clock.
    Because blind stretches are now the weakest link, keep marker density
    HIGH: density is the tunable reliability dial, and without encoders you
    lean on it harder.

    Homing itself does NOT need encoders: the camera is the feedback loop.
    Every frame re-measures the heading error, so open-loop motor drift is
    corrected continuously. Encoders only ever bought us the blind gaps.

THIS PROCESS DEGRADES GRACEFULLY:
    - No MCU wired up yet -> medic.common.SerialLink logs it once and every
      send() becomes a no-op. Nav keeps computing and printing goals.
    - No dashboard reachable -> medic.common.Central returns None/{} from
      every call. Nav falls back to --route/--destination (or a hardcoded
      bench default) and file-constant tuning, and just logs alerts locally
      instead of also posting them.
    Neither missing dependency ever crashes or blocks this loop.

BENCH TEST (run from the pi-deploy/ directory):
  1. `python -m medic.nav --dry-run --debug` -- point the camera at marker
     20 and watch it print id / centre-offset px / apparent width px live.
     Needs no MCU, no dashboard, and no --route/--destination (falls back to
     a single-marker bench route). This is the Day-1 milestone.
  2. Sign check (do this before ever driving on the floor): hold the marker
     to the RIGHT of centre. hdg must be POSITIVE and the printed goal must
     say "steer right". Left of centre -> negative. A flipped sign makes the
     robot run away from every marker.
  3. Standoff: walk the marker toward the camera until it prints ARRIVED,
     then measure the real distance with a tape. Tune target_w_px (either
     the file constant TARGET_W_PX or the dashboard's nav.target_w_px) until
     that reads ~20 cm (open-decision #7).
  4. Marker-lost path: while homing, cover the marker with your hand. You
     must see COAST (brief, straight on) -> SEARCH (in-place rotate) -> LOST
     (stop + alert). It must NEVER keep driving forward. That is R15.
  5. Wrong station: `--destination 20` but show it marker 10. It must refuse
     to arrive and print a station mismatch (R13).
  6. Task pull: with the dashboard running and a task dispatched to this
     robot_id, run `python -m medic.nav --debug` with NO --route/--destination
     at all -- it should log "pulled active task ..." and home on the task's
     real route. Kill the dashboard mid-run and confirm nav keeps navigating
     off the tuning/route it already has (it only degrades to logging).
  7. Leg continuation: after step 6 reaches ARRIVED, dispatch a SECOND task
     to the same robot_id (e.g. destination_marker=10, the return leg) from
     the dashboard while nav.py is still running. Within TASK_POLL_S (2 s)
     it should log "picked up new task ..." and resume HOMING toward the
     new route -- it must NOT need a restart. This only fires while idle
     (ARRIVED/LOST); it must not interrupt a leg already in progress.
  8. Camera backend: `python -m medic.camera --camera-backend auto` first
     (see camera.py) to confirm which backend wins on this Pi before
     debugging nav itself -- a camera problem is easy to mistake for a nav
     bug.
"""

import argparse
import json
import logging
import os
import sys
import time

import cv2
import numpy as np

from medic import camera
from medic import common

# ---------------------------------------------------------------------------
# Tuning. Everything here is a bench-tunable dial, not a magic number.
# max_spd / target_w_px / coast_s / search_s can also be retuned live from
# the dashboard (GET /api/v1/config -> "nav": {...}); these module constants
# are only the fallback used until the dashboard says otherwise. See Tuning
# below.
# ---------------------------------------------------------------------------

# The camera's approximate horizontal field of view, in degrees. This is the
# ONE rough optical constant we need, and it only sets the SCALE of the
# heading error -- it is not a calibration. A wrong-ish value just means
# STEER_GAIN on the MCU needs a different number. Typical USB webcam ~60,
# Pi Camera v2 ~62.
# TODO(day-3): if steering feels mis-scaled, measure this properly.
HFOV_DEG = 60.0

# Standoff: we stop when the marker's apparent width reaches this many
# pixels. Bigger printed marker or closer standoff -> bigger number.
# TODO(day-3): measure on the real course with the real printed marker size.
TARGET_W_PX = 150.0
SLOW_W_PX = 70.0  # start slowing down once the marker is this wide

ARRIVE_HDG_DEG = 6.0  # must also be roughly centred to count as arrived

# Full speed, by explicit request. This is the CRUISE ceiling only — the
# approach taper (SLOW_W_PX) and the turn taper still scale it down, and they
# have to: without them the robot cannot stop at the right standoff or steer.
#
# Known cost, measured on this robot: at the camera's current 66 ms exposure a
# marker stops decoding past roughly 10 px of motion smear. Faster travel means
# more smear, so expect more lost markers -> more COAST -> more MARKER_SEARCH.
# The fix for that is LIGHT, not a lower number: at ~300 lux the exposure drops
# to ~10 ms and full speed becomes genuinely usable. See docs/no-encoder-nav.md.
MAX_SPD = 1.0

MIN_SPD = 0.18  # below this an open-loop geared motor barely moves
COAST_SPD = 0.20  # blind coast is slower still

# Above this heading error we stop translating and just pivot. Sending
# spd=0 with a non-zero hdg makes the MCU counter-rotate the wheels, i.e.
# turn in place.
TURN_ONLY_DEG = 28.0

HDG_SMOOTH = 0.5  # 0..1 exponential smoothing on the heading goal

COAST_S = 1.5  # blind straight-on time after losing the marker (NO encoders)
SEARCH_S = 8.0  # in-place sweep budget before we give up

LOOP_HZ = 12.0
HEARTBEAT_S = 0.75  # MCU safe-holds after 2 s of silence; stay well inside that

CONFIG_POLL_S = 3.0  # how often to re-pull dashboard nav tuning while running
MARKER_SEEN_POST_S = 5.0  # rate-limit for the marker_seen audit event
TASK_POLL_S = 2.0  # how often to check for a newly-dispatched task while idle

# Human-readable name for the serial protocol's `stop.at` field
# (docs/serial-protocol-v1.md) and for log lines.
STATION_NAMES = {10: "pharmacy", 20: "ward"}
PHARMACY_MARKER = 10  # home/return marker; must match the dashboard's

# --- Autonomous mapping (--map) --------------------------------------------
# The survey turn. With NO ENCODERS we cannot measure angle, so a "full turn"
# is a DURATION, not 360 degrees. MAP_SWEEP_S must be calibrated by eye:
# start the sweep, watch the robot, and set it to however long one complete
# revolution actually takes on your floor at MAP_ROT_SPD.
#
# Rotation speed is deliberately modest. Too fast and the 66 ms camera
# exposure smears every marker past the ~10 px the detector tolerates, so the
# robot spins blind and "finds" nothing; too slow and a survey of a dozen
# markers takes all afternoon.
MAP_ROT_SPD = 0.30
MAP_ROT_HDG = 25.0     # heading error that produces an in-place pivot
MAP_SWEEP_S = 14.0     # one full revolution at MAP_ROT_SPD -- CALIBRATE THIS
MAP_SETTLE_S = 0.45    # pause after each rotation step so the frame is sharp
MAP_STEP_S = 0.9       # rotate in short bursts, looking between them
MAP_APPROACH_TIMEOUT_S = 90.0  # give up on one marker rather than hunt forever

# Ceiling on a single dispense. The magazine is a one-pocket escapement, so a
# runaway count would just keep cycling the disk until the tube empties.
MAX_DISPENSE_UNITS = 10

ARUCO_DICT = cv2.aruco.DICT_4X4_50  # markers/README.md -- frozen

log = logging.getLogger("nav")


# ---------------------------------------------------------------------------
# Serial link to the ESP32. Wraps medic.common.SerialLink (the actual pipe,
# reconnect logic, and "no port yet" handling all live there -- we do not
# duplicate any of that here per medic/common.py's own rules). This wrapper
# only adds two bench-test/nav-local conveniences: an explicit --dry-run
# mode that never touches serial even if a port is present, and a heartbeat
# timer so the MCU's 2 s comms watchdog never trips while nav is thinking.
# ---------------------------------------------------------------------------
class GoalLink(object):
    def __init__(self, serial_link, dry_run=False):
        self.serial_link = serial_link
        self.dry_run = dry_run
        self._last_tx = 0.0
        self._poll_warned = False

    def send(self, obj):
        # monotonic, not time.time(): the Pi has no RTC, so an NTP step early in
        # the run could otherwise push the next heartbeat past the MCU's 2 s
        # watchdog and trip a spurious SAFEHOLD_COMMS mid-delivery.
        self._last_tx = time.monotonic()
        if self.dry_run:
            log.debug("TX (dry-run, not sent) %s", json.dumps(obj))
            return False
        return self.serial_link.send(obj)

    def heartbeat(self):
        """Keep the MCU's 2 s comms watchdog fed when we have nothing else
        to say (e.g. while ARRIVED or LOST, where no drive/search goals are
        being sent every loop tick)."""
        if time.monotonic() - self._last_tx >= HEARTBEAT_S:
            self.send({"t": "ping"})

    def poll(self):
        """Kept for bench/debug use (e.g. --dry-run tooling), but main()
        deliberately does NOT call this in the live loop -- see the comment
        there. Multiple processes each opening their own SerialLink on the
        SAME physical MCU device (nav.py, task_bridge.py, and formerly
        ears.py) is fine for short atomic writes, but pyserial does not
        give a tty exclusive-lock semantics: reads from more than one fd on
        the same device split the incoming byte stream non-deterministically
        between whichever process's read() runs first, corrupting the
        newline-JSON framing for BOTH readers. task_bridge.py needs full,
        uncorrupted reads (ack/estop/temp/state drive the arrival-dispense
        auth path), so it is the one process that actually consumes MCU
        replies; nav only ever used this for a debug log line it can live
        without.

        It warns loudly if you ever call it against a real port. This method is
        a loaded gun: nothing calls it today, and if someone wires it back into
        the live loop it will quietly start stealing bytes out of
        task_bridge.py's auth-path reads, which is exactly the kind of bug that
        only shows up on demo day."""
        if self.dry_run:
            return []
        if not self._poll_warned:
            self._poll_warned = True
            log.warning(
                "nav.Link.poll() called against a REAL serial port — this "
                "competes with task_bridge.py for the MCU byte stream and can "
                "corrupt its ack/estop reads. Debug use only; never leave "
                "this in the live loop."
            )
        return self.serial_link.poll()


# ---------------------------------------------------------------------------
# Live-tunable nav constants, pulled from the dashboard.
# ---------------------------------------------------------------------------
class Tuning(object):
    """Holds the four nav numbers the dashboard can retune live via
    GET /api/v1/config ("nav": {max_spd, target_w_px, coast_s, search_s}).
    Starts from the file constants above and only changes when the
    dashboard is reachable and its config_rev has actually moved -- an
    unreachable dashboard just means we keep using whatever we had (the
    file constants, at first)."""

    def __init__(self):
        self.max_spd = MAX_SPD
        self.target_w_px = TARGET_W_PX
        self.coast_s = COAST_S
        self.search_s = SEARCH_S
        self._rev = None

    def refresh(self, central):
        cfg = central.get_config()  # {} on any dashboard error -- safe default
        rev = cfg.get("config_rev")
        if rev is not None and rev == self._rev:
            return  # nothing changed since last poll
        nav_cfg = cfg.get("nav") or {}
        self.max_spd = float(nav_cfg.get("max_spd", MAX_SPD))
        self.target_w_px = float(nav_cfg.get("target_w_px", TARGET_W_PX))
        self.coast_s = float(nav_cfg.get("coast_s", COAST_S))
        self.search_s = float(nav_cfg.get("search_s", SEARCH_S))
        if rev != self._rev:
            log.info("nav tuning updated (config_rev=%s): max_spd=%.2f "
                     "target_w_px=%.0f coast_s=%.1f search_s=%.1f",
                     rev, self.max_spd, self.target_w_px, self.coast_s,
                     self.search_s)
        self._rev = rev


# ---------------------------------------------------------------------------
# ArUco detection
# ---------------------------------------------------------------------------
def make_detector():
    """Works on both the modern and the legacy cv2.aruco APIs."""
    if hasattr(cv2.aruco, "getPredefinedDictionary"):
        dictionary = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
    else:  # OpenCV < 4.7
        dictionary = cv2.aruco.Dictionary_get(ARUCO_DICT)

    if hasattr(cv2.aruco, "ArucoDetector"):
        params = cv2.aruco.DetectorParameters()
        det = cv2.aruco.ArucoDetector(dictionary, params)
        return lambda gray: det.detectMarkers(gray)

    params = cv2.aruco.DetectorParameters_create()
    return lambda gray: cv2.aruco.detectMarkers(gray, dictionary, parameters=params)


def marker_metrics(corner):
    """Return (centre_x, centre_y, apparent_width_px) for one marker.

    Apparent width is the mean of the four edge lengths -- more stable than
    a bounding box when the marker is seen at a slight angle.
    """
    pts = corner.reshape(4, 2)
    cx, cy = float(pts[:, 0].mean()), float(pts[:, 1].mean())
    edges = [float(np.linalg.norm(pts[i] - pts[(i + 1) % 4])) for i in range(4)]
    return cx, cy, sum(edges) / 4.0


# ---------------------------------------------------------------------------
# Navigator
# ---------------------------------------------------------------------------
HOMING, COAST, SEARCH, LOST, ARRIVED = "HOMING", "COAST", "SEARCH", "LOST", "ARRIVED"


def _units_of(task):
    """Units to dispense on arrival, clamped to something the magazine can
    actually do. There is no RFID/two-scan step in this build, so arrival is
    the dispense trigger and this count rides on the 'stop' message.

    Defaults to 1 rather than 0: a task with no units field still represents a
    real delivery, and dropping nothing at the ward looks identical to a jam.
    """
    try:
        n = int((task or {}).get("units", 1))
    except (TypeError, ValueError):
        n = 1
    return max(1, min(n, MAX_DISPENSE_UNITS))


# ---------------------------------------------------------------------------
# Live view for the dashboard's /live page.
# ---------------------------------------------------------------------------
# nav OWNS the camera -- only one process can hold the Pi Camera at a time
# ("Pipeline handler in use by another process"), so the dashboard cannot open
# its own stream. nav therefore publishes what it already has. That is also the
# more useful picture: you see exactly what the DETECTOR sees, marker overlay
# and all, not a separate prettier stream that might disagree with it.
#
# NOT A RECORDING. Frames go to /dev/shm -- a RAM-backed tmpfs -- as a single
# file that is overwritten several times a second and vanishes on reboot.
# Nothing is ever written to the SD card: no wear, no history, no footage to
# explain to anyone. This matches the spirit of the audio rule (ARCHITECTURE.md
# §0.5, "events only, never recordings"); §0.5 itself governs AUDIO, and the
# microphone pipeline is untouched by any of this.
FRAME_PATH = os.environ.get("MEDIC_NAV_FRAME_PATH", "/dev/shm/medic-nav.jpg")
FRAME_FPS = float(os.environ.get("MEDIC_NAV_FRAME_FPS", "4"))
FRAME_QUALITY = int(os.environ.get("MEDIC_NAV_FRAME_QUALITY", "70"))


class FramePublisher(object):
    """Writes the annotated frame to tmpfs at a low, fixed rate.

    Rate-limited on purpose: encoding every frame at the full loop rate would
    burn CPU the navigation loop needs, and nobody can see 12 fps of difference
    on a status page anyway. Failures are swallowed -- a broken preview must
    never take down navigation.
    """

    def __init__(self, path=FRAME_PATH, fps=FRAME_FPS, quality=FRAME_QUALITY):
        self.path = path
        self.tmp = path + ".tmp"
        self.enabled = fps > 0
        self.interval = (1.0 / fps) if fps > 0 else 0.0
        self.quality = max(10, min(95, quality))
        self._next = 0.0
        self._warned = False

    def due(self):
        return self.enabled and time.monotonic() >= self._next

    def publish(self, frame_bgr):
        if not self.due():
            return
        self._next = time.monotonic() + self.interval
        try:
            ok, buf = cv2.imencode(
                ".jpg", frame_bgr,
                [int(cv2.IMWRITE_JPEG_QUALITY), self.quality],
            )
            if not ok:
                return
            # Write to a temp name then rename. rename() is atomic on the same
            # filesystem, so the dashboard can never read a half-written JPEG
            # and show a torn frame.
            with open(self.tmp, "wb") as fh:
                fh.write(buf.tobytes())
            os.replace(self.tmp, self.path)
        except Exception as exc:
            if not self._warned:
                self._warned = True
                log.warning("frame publish failed (preview only, nav continues): %s",
                            exc)


class Navigator(object):
    def __init__(self, link, route, destination, leg, tuning, central=None,
                 task_id=None):
        self.link = link
        self.route = list(route)  # e.g. [15, 20] -- waypoints then destination
        self.destination = destination
        self.leg = leg
        self.tuning = tuning
        self.central = central  # medic.common.Central, or None to skip posting
        self.task_id = task_id
        self.units = 1  # replaced by adopt_task(); see _units_of()
        # Mapping reuses this class purely for its homing/coast/search logic.
        # It must NOT drop candy every time it docks on a marker it is only
        # surveying, so arrival-triggered dispense is switchable.
        self.dispense_on_arrival = True
        self.idx = 0
        self.phase = HOMING
        self.hdg = 0.0
        # time.monotonic(), not time.time(): this feeds the R15 COAST->SEARCH
        # ->LOST bound (on_no_marker() below), which must hold even across an
        # NTP clock step. A Pi has no RTC and commonly boots with a stale
        # saved time that jumps once the network/NTP comes up -- a backward
        # step here would make `gone` go negative and keep the robot
        # blind-coasting well past coast_s, i.e. "wander" (see ears.py, which
        # already uses time.monotonic() throughout for the same reason).
        self.last_seen = time.monotonic()
        self.search_dir = "cw"
        self._last_marker_seen_post = 0.0

    @property
    def target(self):
        return self.route[self.idx]

    def _drive(self, hdg, spd):
        self.link.send(
            {"t": "drive", "hdg": round(hdg, 1), "spd": round(spd, 2), "leg": self.leg}
        )

    def on_marker(self, cx, width, debug=False):
        """Target marker is in view: steer toward it."""
        self.last_seen = time.monotonic()

        # --- calibration-free steering -------------------------------------
        # Horizontal offset from image centre, normalised to [-1, +1], then
        # scaled by half the field of view to get degrees. Positive = marker
        # is to the RIGHT = steer right, matching the protocol's "+right".
        err_norm = (cx - camera.FRAME_W / 2.0) / (camera.FRAME_W / 2.0)
        raw_hdg = err_norm * (HFOV_DEG / 2.0)
        self.hdg = HDG_SMOOTH * self.hdg + (1.0 - HDG_SMOOTH) * raw_hdg
        self.search_dir = "cw" if self.hdg >= 0 else "ccw"

        # marker_seen is rate-limited -- posting it every frame at 12 Hz
        # would flood the audit log for no benefit (pi-deploy/DESIGN.md: the audit
        # log is the product, so it has to stay readable).
        if (self.central is not None
                and time.time() - self._last_marker_seen_post >= MARKER_SEEN_POST_S):
            self._last_marker_seen_post = time.time()
            self.central.post_event(
                "marker_seen",
                detail="id=%d off=%+.0fpx w=%.0fpx"
                       % (self.target, cx - camera.FRAME_W / 2.0, width),
                task_id=self.task_id,
            )

        # --- calibration-free standoff -------------------------------------
        # Apparent width grows as we close in. No pose estimation needed.
        target_w_px = self.tuning.target_w_px
        if width >= target_w_px and abs(self.hdg) <= ARRIVE_HDG_DEG:
            self._arrive()
            return

        approach = (target_w_px - width) / max(1.0, target_w_px - SLOW_W_PX)
        approach = max(0.0, min(1.0, approach))

        # Large heading error -> stop translating and pivot. Sending spd=0
        # with a non-zero hdg makes the MCU counter-rotate the wheels: a
        # clean in-place turn, with no extra message type needed.
        turn_factor = max(0.0, 1.0 - abs(self.hdg) / TURN_ONLY_DEG)

        spd = self.tuning.max_spd * approach * turn_factor
        if 0.0 < spd < MIN_SPD:
            spd = MIN_SPD

        self.phase = HOMING
        self._drive(self.hdg, spd)
        if debug:
            side = "right" if self.hdg > 0 else "left"
            log.info(
                "id=%d off=%+.0fpx w=%.0fpx -> steer %s hdg=%+.1f spd=%.2f",
                self.target,
                cx - camera.FRAME_W / 2.0,
                width,
                side,
                self.hdg,
                spd,
            )

    def _arrive(self):
        """Reached standoff on the current waypoint."""
        last_leg = self.idx >= len(self.route) - 1
        if not last_leg:
            log.info("waypoint %d reached, advancing", self.target)
            self.idx += 1
            self.hdg = 0.0
            self.phase = HOMING
            return

        # R13: the marker we docked on must BE the task's destination. This
        # is the station-identity check, fused into navigation.
        if self.target != self.destination:
            log.error(
                "STATION MISMATCH: docked on marker %d, task destination is %d",
                self.target,
                self.destination,
            )
            self.link.send({"t": "stop"})
            self._alert("station_mismatch",
                       "docked marker %d != task destination %d"
                       % (self.target, self.destination))
            self.phase = LOST  # refuse to proceed to auth
            return

        # Which station this is, for the MCU. Derived from the LEG, not from
        # STATION_NAMES: that table only knows the two hardcoded demo markers
        # (10/20), so a delivery to a marker learned during mapping (Room 4B,
        # 4C, ...) would fall through to "waypoint" and silently never
        # dispense. Any outbound destination is "the ward" as far as the
        # firmware's arrival trigger is concerned.
        if self.leg == "return" or self.destination == PHARMACY_MARKER:
            at = "pharmacy"
        else:
            at = "ward"
        stop = {"t": "stop", "at": at}
        # Arrival IS the dispense trigger in this build (no RFID, no two-scan).
        # The count rides on the same message so there is no window where the
        # robot is parked at the ward waiting for a second command that may
        # never come. Only on a delivery leg: coming home to the pharmacy must
        # obviously not drop candy on the floor.
        if at == "ward" and self.dispense_on_arrival:
            stop["count"] = self.units
        self.link.send(stop)
        self.phase = ARRIVED
        log.info("ARRIVED at marker %d (%s), standoff reached%s", self.target, at,
                 (", dispensing %d unit(s)" % self.units) if at == "ward" else "")

        if self.central is not None:
            self.central.post_event(
                "arrive", detail="marker %d (%s)" % (self.target, at),
                task_id=self.task_id,
            )
            if self.task_id is not None:
                self.central.set_task_state(
                    self.task_id, "arrived", detail="marker %d (%s)" % (self.target, at)
                )

    def adopt_task(self, task):
        """Swap in a newly-dispatched task while idle (ARRIVED or LOST only
        -- see main()'s call site, which never calls this mid-leg). Without
        this, reaching ARRIVED was a permanent dead end: nav pulled the
        active task exactly once at startup and then just heartbeated
        forever, so the robot could never autonomously run the RETURNING
        leg or hand off to PATROL (ARCHITECTURE.md demo contract step 6).
        Mirrors the startup route/destination/leg resolution in
        _resolve_route() so a freshly-picked-up task behaves identically to
        one nav was launched with."""
        route = [int(m) for m in task["route"]] if task.get("route") else [self.destination]
        destination = (
            int(task["destination_marker"])
            if task.get("destination_marker") is not None
            else route[-1]
        )
        # Same inference _resolve_route() uses: docking back at the pharmacy
        # marker is the return leg, anything else is an outbound delivery.
        # "patrol" has no marker-route signal to infer from -- a patrol
        # task, if the dashboard ever dispatches one, must carry leg
        # information some other way; out of scope for this fix.
        leg = "return" if destination == 10 else "outbound"

        self.route = route
        self.destination = destination
        self.leg = leg
        self.idx = 0
        self.phase = HOMING
        self.hdg = 0.0
        self.last_seen = time.monotonic()
        self.task_id = task.get("task_id")
        self.units = _units_of(task)

        log.info("picked up new task %s: route=%s destination=%s leg=%s units=%d",
                 self.task_id, route, destination, leg, self.units)
        if self.central is not None and self.task_id is not None:
            self.central.set_task_state(
                self.task_id, "en_route",
                detail="leg=%s target=%d" % (leg, route[0]),
            )

    def on_no_marker(self):
        """Target marker is not in view. Bounded recovery only -- never
        wander."""
        if self.phase in (ARRIVED, LOST):
            self.link.heartbeat()
            return

        gone = time.monotonic() - self.last_seen
        coast_s = self.tuning.coast_s
        search_s = self.tuning.search_s

        if gone < coast_s:
            # Blind coast, straight on, briefly. With no encoders this is a
            # TIME budget, not a distance (docs/no-encoder-nav.md). Keep it
            # short -- we cannot measure how far we actually travelled, so a
            # long coast is pure guesswork.
            if self.phase != COAST:
                log.info("marker %d lost -> COAST %.1fs", self.target, coast_s)
                self.phase = COAST
            self._drive(0.0, COAST_SPD)

        elif gone < coast_s + search_s:
            # Rotate in place toward where the marker last was.
            if self.phase != SEARCH:
                log.info("COAST expired -> MARKER_SEARCH (%s)", self.search_dir)
                self.phase = SEARCH
            self.link.send({"t": "search", "dir": self.search_dir})

        else:
            # R15: budget spent. STOP and flag. We never keep hunting.
            if self.phase != LOST:
                self.phase = LOST
                self.link.send({"t": "stop"})
                log.error("MARKER LOST: %d not reacquired -- stopping", self.target)
                self._alert("marker_lost", "marker %d not reacquired" % self.target)
            self.link.heartbeat()

    def _alert(self, kind, detail):
        """Nav-level red/warn alert. Always printed locally as a structured
        line (so `--dry-run`/bench runs show it with zero dashboard at all),
        and also posted to the dashboard's audit log when Central is
        configured. Central.post_event already swallows any network failure
        and returns quietly -- a slow or dead dashboard must never block
        navigation (invariant: nav is autonomy, never safety, and it can't
        be allowed to hang either)."""
        print(
            json.dumps({"type": "nav_alert", "kind": kind, "detail": detail,
                       "ts": time.time()}),
            flush=True,
        )
        if self.central is not None:
            self.central.post_event(kind, detail=detail, task_id=self.task_id)


# ---------------------------------------------------------------------------
def _resolve_route(args, task):
    """--route/--destination are overrides for bench testing; otherwise take
    them from the dispatched task; otherwise fall back to a single-marker
    bench default so `--dry-run` still works with zero setup."""
    if args.route is not None:
        route = [int(x) for x in args.route.split(",") if x.strip()]
    elif task and task.get("route"):
        route = [int(m) for m in task["route"]]
    else:
        route = [20]  # bench default: home straight on the Ward marker

    if args.destination is not None:
        destination = args.destination
    elif task and task.get("destination_marker") is not None:
        destination = int(task["destination_marker"])
    else:
        destination = route[-1]

    if destination not in route:
        log.warning("destination %d is not in route %s", destination, route)

    if args.leg is not None:
        leg = args.leg
    else:
        # Infer the obvious case (10=pharmacy => returning, anything else
        # => outbound); "patrol" has no marker-route signal to infer from,
        # so it must always be requested explicitly.
        leg = "return" if destination == 10 else "outbound"

    return route, destination, leg


# ---------------------------------------------------------------------------
# TEACH MODE — learn the marker map without driving anything.
# ---------------------------------------------------------------------------
def run_teach(cam, detect, central, link, args):
    """Watch, learn, and name. NAV SENDS NO DRIVE COMMANDS IN THIS MODE.

    You drive the robot with the dashboard teleop panel (or just carry it) and
    walk it past every marker. Whenever two or more markers land in the SAME
    frame, that pair becomes an edge in the map: "from around here, you can see
    both of these". Chain enough of those together and the dashboard can plan a
    route between any two markers it has been shown.

    Deliberately NOT autonomous exploration. Root ARCHITECTURE.md §0.3 and design doc
    §3 rule out free autonomous navigation, and it would also contradict the
    whole pitch — the robot observes and asks, a human decides where it goes and
    what each place is called. It is also why this works before the motors do:
    carry the robot round by hand and it learns just the same.

    Name the markers as they appear on the dashboard's /map page.
    """
    log.info("TEACH MODE — nav will NOT drive. Move the robot yourself.")
    log.info("Name each new marker at %s/map", args.central_url or "the dashboard")

    seen_ids = set()
    pair_posts = 0
    last_post = 0.0
    period = 1.0 / LOOP_HZ

    try:
        while True:
            t0 = time.time()
            try:
                gray = cam.read_gray()
                if gray is None:
                    link.heartbeat()
                    time.sleep(period)
                    continue

                corners, ids, _ = detect(gray)
                visible = sorted({int(i) for i in ids.flatten()}) if ids is not None else []

                for mid in visible:
                    if mid not in seen_ids:
                        seen_ids.add(mid)
                        log.info("NEW MARKER %d  — name it on the dashboard /map page", mid)
                        central.post_event(
                            "marker_seen",
                            detail="teach: first sighting of marker %d" % mid,
                        )

                # Only PAIRS teach connectivity, and only post a few times a
                # second — the map needs repeated observations, not every frame
                # at 12 Hz flooding the dashboard with the same edge.
                if len(visible) >= 2 and (time.time() - last_post) >= 0.5:
                    last_post = time.time()
                    if central.post_map_sighting(visible):
                        pair_posts += 1
                    log.info("co-visible: %s   (edges posted: %d)", visible, pair_posts)

                # Keep the MCU's comms watchdog fed. We send no motion, but if
                # nav went silent the MCU would SAFEHOLD and your teleop driving
                # would stop working mid-teach.
                link.heartbeat()

            except Exception:
                log.exception("teach loop iteration failed")
                time.sleep(0.5)

            time.sleep(max(0.0, period - (time.time() - t0)))

    except KeyboardInterrupt:
        log.info("teach mode ended — %d markers seen: %s",
                 len(seen_ids), sorted(seen_ids))
        log.info("Check the map at %s/map", args.central_url or "the dashboard")
    return 0



def _sweep(cam, detect, link, central, anchor, log_prefix=""):
    """Rotate roughly one full turn on the spot, collecting every marker seen.

    This is the heart of autonomous mapping. Standing still, usually only ONE
    marker is in view; turn around and others come into frame. Everything found
    during one sweep is reachable from this spot, so all of it becomes an edge.

    Note the edge semantics are LOOSER than teach mode's: teach mode required
    two markers in the SAME frame, whereas a sweep links markers seen at
    different moments from the same standing position. That is the right
    relation for routing -- the robot can always turn on the spot -- but it
    does mean a sweep edge is only as good as the robot's ability to return to
    this exact spot, which with no encoders is approximate.

    Rotation happens in short bursts with a pause between them: a marker has to
    be sharp in at least one frame to decode, and rotating continuously at the
    current camera exposure smears it out. Stop-look-stop beats a smooth spin.
    """
    seen = set()
    if anchor is not None:
        seen.add(anchor)
    t_end = time.monotonic() + MAP_SWEEP_S
    period = 1.0 / LOOP_HZ

    while time.monotonic() < t_end:
        # --- rotate a burst ---
        t_burst = time.monotonic() + MAP_STEP_S
        while time.monotonic() < t_burst:
            # spd=0 with a non-zero hdg = in-place pivot (see drive.cpp). Kept
            # on the normal 'drive' path so every MCU safety override still
            # preempts it exactly as it would during a delivery.
            link.send({"t": "drive", "hdg": MAP_ROT_HDG, "spd": 0.0})
            time.sleep(period)

        # --- stop and look ---
        link.send({"t": "stop"})
        time.sleep(MAP_SETTLE_S)
        try:
            gray = cam.read_gray()
            if gray is not None:
                _, ids, _ = detect(gray)
                if ids is not None:
                    for i in ids.flatten():
                        mid = int(i)
                        if mid not in seen:
                            seen.add(mid)
                            log.info("%s  spotted marker %d", log_prefix, mid)
        except Exception:
            log.exception("sweep frame failed")
        link.heartbeat()

    link.send({"t": "stop"})

    # One posting for the whole sweep. record_sighting() links every pair in
    # the list, which is what we want: all of these are mutually reachable
    # from this spot, not just each one back to the anchor.
    if len(seen) >= 2 and central is not None:
        central.post_map_sighting(sorted(seen))
    return seen


def run_map(cam, detect, central, link, tuning, args):
    """AUTONOMOUS mapping: drive to a marker, survey, drive to the next.

    Loop: home on a target marker -> stop -> sweep a full turn recording
    everything visible from there -> pick the first marker not yet visited ->
    repeat. Ends when no unvisited marker remains.

    SCOPE NOTE (ARCHITECTURE.md §0.3): this is still pure ArUco waypoint
    navigation -- no SLAM, no LIDAR, no occupancy grid, no line following. The
    output is a topological graph of marker IDs, exactly what teach mode built;
    only the operator has been automated out of the loop. Obstacle-stop,
    E-stop and the comms watchdog are untouched and still live on the MCU.
    """
    log.info("AUTONOMOUS MAPPING — the robot will drive itself.")
    log.info("  sweep=%.1fs at spd=%.2f (CALIBRATE MAP_SWEEP_S to one real turn)",
             MAP_SWEEP_S, MAP_ROT_SPD)
    log.info("  name markers as they appear at %s/map",
             args.central_url or "the dashboard")

    visited = set()
    # Opening survey from wherever the robot is standing, so we have somewhere
    # to go before any marker has been approached.
    frontier = [m for m in sorted(_sweep(cam, detect, link, central, None, "start:"))]
    if not frontier:
        log.error("no markers visible from the start position — "
                  "point the robot at one and rerun")
        return 1

    while True:
        nxt = next((m for m in frontier if m not in visited), None)
        if nxt is None:
            break

        log.info("--> heading for marker %d  (visited %d, frontier %s)",
                 nxt, len(visited), sorted(set(frontier) - visited))

        nav = Navigator(link, [nxt], nxt, "patrol", tuning, central=central,
                        task_id=None)
        nav.dispense_on_arrival = False  # surveying, not delivering
        deadline = time.monotonic() + MAP_APPROACH_TIMEOUT_S
        period = 1.0 / LOOP_HZ

        while nav.phase not in (ARRIVED, LOST) and time.monotonic() < deadline:
            t0 = time.time()
            try:
                gray = cam.read_gray()
                if gray is not None:
                    corners, ids, _ = detect(gray)
                    nav.step(corners, ids)
                else:
                    link.heartbeat()
            except Exception:
                log.exception("approach iteration failed")
                time.sleep(0.3)
            time.sleep(max(0.0, period - (time.time() - t0)))

        if nav.phase != ARRIVED:
            # Not fatal and not a retry loop: mark it visited so the survey
            # moves on instead of grinding on one unreachable marker forever.
            log.warning("could not reach marker %d (%s) — skipping it", nxt,
                        nav.phase)
            visited.add(nxt)
            continue

        visited.add(nxt)
        found = _sweep(cam, detect, link, central, nxt, "at %d:" % nxt)
        log.info("marker %d surveyed: sees %s", nxt, sorted(found - {nxt}))
        for m in sorted(found):
            if m not in frontier:
                frontier.append(m)

    link.send({"t": "stop"})
    log.info("MAPPING COMPLETE — %d markers visited: %s", len(visited),
             sorted(visited))
    log.info("Name them and check the graph at %s/map",
             args.central_url or "the dashboard")
    return 0


def main():
    ap = argparse.ArgumentParser(description="ArUco waypoint navigation (MEDIC)")
    common.add_common_args(ap)  # --robot-id --central-url --serial-port --log-level
    camera.add_camera_args(ap)  # --camera-index --camera-backend --lock-exposure
    ap.add_argument("--route", default=None,
                    help="comma-separated marker IDs to home on, in order "
                         "(override; normally comes from the dispatched task)")
    ap.add_argument("--destination", type=int, default=None,
                    help="task destination marker ID for the R13 check "
                         "(override; normally comes from the dispatched task)")
    ap.add_argument("--leg", default=None, choices=["outbound", "return", "patrol"],
                    help="which MCU state this leg selects (default: "
                         "inferred from the destination marker)")
    ap.add_argument("--teach", action="store_true",
                    help="TEACH MODE: drive the robot yourself with the teleop "
                         "panel while nav watches and learns which markers are "
                         "visible together. Sends NO drive commands of its own "
                         "-- you are the navigation. Name each new marker on "
                         "the dashboard's /map page as it appears.")
    ap.add_argument("--map", action="store_true", dest="automap",
                    help="AUTONOMOUS MAPPING: drive to a marker, spin a full "
                         "turn recording every other marker visible from "
                         "there, then drive to the first one not yet visited "
                         "and repeat. Builds the same topological map --teach "
                         "does, without a human driving. Calibrate "
                         "MAP_SWEEP_S to one real revolution first.")
    ap.add_argument("--dry-run", action="store_true",
                    help="never touch serial, even if a port is present -- "
                         "goals are only printed (Day-1 bench test)")
    ap.add_argument("--debug", action="store_true", help="print offset/width live")
    ap.add_argument("--show", action="store_true",
                    help="annotated preview window (needs a display -- not "
                         "available on a headless robot run)")
    args = ap.parse_args()

    cfg = common.load_config(args)
    common.setup_logging("DEBUG" if args.debug else cfg.log_level, "nav")

    central = common.Central(cfg.central_url, cfg.robot_id, cfg.http_timeout)
    serial_link = common.SerialLink(cfg.serial_port, cfg.serial_baud,
                                    enabled=not args.dry_run)
    link = GoalLink(serial_link, dry_run=args.dry_run)

    task = None
    if args.route is None or args.destination is None:
        task = central.get_active_task()
        if task:
            log.info("pulled active task %s from dashboard: route=%s destination=%s",
                     task.get("task_id"), task.get("route"),
                     task.get("destination_marker"))
        else:
            log.warning("no active task from the dashboard (offline, or none "
                       "dispatched yet) -- falling back to --route/--destination "
                       "for a bench run")

    route, destination, leg = _resolve_route(args, task)
    task_id = task.get("task_id") if task else None

    tuning = Tuning()
    tuning.refresh(central)  # picks up dashboard nav config if reachable now

    cam = camera.Camera(index=args.camera_index, backend=args.camera_backend,
                        lock_exposure=args.lock_exposure)
    try:
        cam.open()
    except camera.CameraError as exc:
        log.error("%s", exc)
        return 1

    detect = make_detector()

    if args.teach:
        # Teach mode needs no task, no route and no destination — it is not
        # going anywhere on its own.
        return run_teach(cam, detect, central, link, args)

    if args.automap:
        # Same: mapping picks its own targets, so the task/route resolved
        # above is irrelevant to it.
        return run_map(cam, detect, central, link, tuning, args)
    nav = Navigator(link, route, destination, leg, tuning, central=central,
                    task_id=task_id)
    # A task nav was LAUNCHED with needs its unit count too, not just one
    # adopted later via adopt_task() — otherwise a delivery that was already
    # in flight when nav restarted would dispense the default 1 instead of
    # what the operator dispatched.
    nav.units = _units_of(task)

    # Publishes the annotated frame to tmpfs for the dashboard's /live page.
    # Set MEDIC_NAV_FRAME_FPS=0 to turn the feed off entirely.
    publisher = FramePublisher()
    if publisher.enabled:
        log.info("live frame feed -> %s at %.1f fps (RAM only, never the SD card)",
                 publisher.path, FRAME_FPS)

    if task_id is not None:
        central.set_task_state(task_id, "en_route",
                               detail="leg=%s target=%d" % (leg, route[0]))

    # Only auto-continue onto whatever the dashboard dispatches next when
    # nothing was pinned on the command line -- a bench run with an explicit
    # --route/--destination should stay on that single leg, not get swapped
    # mid-test the moment some other task becomes active for this robot_id.
    auto_task = args.route is None and args.destination is None

    period = 1.0 / LOOP_HZ
    next_config_poll = time.time() + CONFIG_POLL_S
    next_task_poll = time.time() + TASK_POLL_S
    log.info("nav up: route=%s destination=%d leg=%s task_id=%s backend=%s",
             route, destination, leg, task_id, cam.backend)

    # A single unexpected exception must not end the run. pi-deploy/DESIGN.md: the three
    # Pi scripts are independent and "one dying must not kill the others" -- and
    # nav dying mid-delivery means the robot coasts to a SAFEHOLD stop in front of
    # the judges. systemd would restart it, but that costs seconds and re-pulls
    # the task; surviving the frame is strictly better. Counts consecutive
    # failures so a persistent fault backs off instead of log-flooding the
    # journal or pegging a core.
    consecutive_errors = 0

    try:
        while True:
            t0 = time.time()

            try:
                if t0 >= next_config_poll:
                    tuning.refresh(central)
                    next_config_poll = t0 + CONFIG_POLL_S

            # Without this, ARRIVED was a permanent dead end: nav pulled the
            # active task exactly once at startup and then just heartbeated
            # forever once docked, so it could never autonomously run the
            # RETURNING leg (ARCHITECTURE.md demo contract step 6) even after
            # task_bridge finished dispensing and the dashboard had a new
            # task waiting. Only check while idle (ARRIVED/LOST) so this
            # never interrupts an in-progress leg.
                if auto_task and t0 >= next_task_poll:
                    next_task_poll = t0 + TASK_POLL_S
                    if nav.phase in (ARRIVED, LOST):
                        next_task = central.get_active_task()
                        next_task_id = (next_task.get("task_id")
                                        if next_task else None)
                        if next_task_id is not None and next_task_id != nav.task_id:
                            nav.adopt_task(next_task)

            # NOTE: nav deliberately does not read MCU serial telemetry here
            # (see GoalLink.poll()'s docstring) -- task_bridge.py is the sole
            # reader of the shared MCU link. Obstacle handling and safe-hold
            # are the MCU's job either way (invariant §0.1); nav only ever
            # used this feed for a debug log line.

                gray = cam.read_gray()
                if gray is None:
                    # A dropped frame must not kill nav -- log, feed the
                    # watchdog, retry.
                    log.warning("frame grab failed")
                    link.heartbeat()
                    time.sleep(period)
                    continue

                corners, ids, _ = detect(gray)

                seen = None
                if ids is not None:
                    for corner, mid in zip(corners, ids.flatten()):
                        if int(mid) == nav.target:
                            seen = marker_metrics(corner)
                            break

                if seen is not None:
                    cx, _cy, width = seen
                    nav.on_marker(cx, width, debug=args.debug)
                else:
                    nav.on_no_marker()

                # Build the annotated frame once and reuse it for both the local
                # preview window and the dashboard feed.
                annotated = None
                if args.show or publisher.due():
                    annotated = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                    if ids is not None:
                        cv2.aruco.drawDetectedMarkers(annotated, corners, ids)
                    cv2.line(annotated, (camera.FRAME_W // 2, 0),
                            (camera.FRAME_W // 2, camera.FRAME_H), (0, 255, 0), 1)
                    cv2.putText(annotated, "%s target=%d hdg=%+.1f"
                               % (nav.phase, nav.target, nav.hdg),
                               (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                    if seen is not None:
                        cv2.putText(annotated, "w=%.0fpx (need %.0f)"
                                   % (seen[2], nav.tuning.target_w_px),
                                   (8, 46), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                                   (0, 255, 255), 1)

                if annotated is not None:
                    publisher.publish(annotated)

                if args.show:
                    cv2.imshow("nav", annotated)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break

                consecutive_errors = 0

            except Exception:
                # KeyboardInterrupt is a BaseException, so Ctrl-C still exits
                # cleanly through the outer handler -- this only catches real
                # faults (a camera hiccup, a malformed task dict, an OpenCV
                # edge case).
                consecutive_errors += 1
                log.exception("nav loop iteration failed (%d in a row)",
                              consecutive_errors)
                # Keep feeding the MCU watchdog even while faulting, so a
                # transient error does not also trip SAFEHOLD_COMMS.
                link.heartbeat()
                time.sleep(min(2.0, period * consecutive_errors))
                continue

            time.sleep(max(0.0, period - (time.time() - t0)))

    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        # Leave the robot stopped. The MCU would safe-hold anyway after 2 s
        # of silence, but say it explicitly rather than relying on the
        # watchdog.
        link.send({"t": "stop"})
        cam.close()
        # Remove the last frame so the dashboard shows "no feed" instead of a
        # frozen image that looks like a live camera which simply stopped moving.
        try:
            os.remove(FRAME_PATH)
        except OSError:
            pass
        if args.show:
            cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    sys.exit(main())
