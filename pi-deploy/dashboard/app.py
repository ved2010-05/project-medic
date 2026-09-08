"""app.py — "Hospital Central" dashboard (Flask + SQLite mock hospital DB).

Implements docs/http-api-v1.md EXACTLY: every path, method, JSON field
name and status code in that doc. Two other things are built against the
SAME frozen doc in parallel and both depend on this file matching it to
the letter: dashboard/templates/*.html + dashboard/static/app.js (the
browser side) and medic/nav.py + medic/task_bridge.py + medic/ears.py
(the Pi side, three doors down in this same pi-deploy bundle).

THE CRITICAL ENDPOINT is POST /api/v1/auth/verify (ARCHITECTURE.md S0.4):
authorize ONLY when both scanned UIDs resolve AND the patient tag belongs
to THIS task's patient AND the task is in a releasable state AND the auth
window has not expired. Every other outcome is a refusal, and the audit
event for that decision is written BEFORE the HTTP response is built —
see api_auth_verify() below. There is no code path through it that
returns authorized=True without every one of those checks passing.

WHY POST /api/v1/teleop DOES NOT ALSO WRITE A teleop_nudge EVENT: it looks
like it should (docs/http-api-v1.md even says "every command handed out is
logged as teleop_nudge" under the GET endpoint), but medic/task_bridge.py
already does exactly that — it polls GET /api/v1/teleop and calls
Central.post_event("teleop_nudge", ...) itself the moment it detects a NEW
seq and actually relays the command to the MCU. If this file also logged
one at enqueue time, every nudge would appear twice in the audit trail.
See api_teleop_get()/api_teleop_post() below for the full reasoning.

BENCH TEST:
    cd pi-deploy
    python -m dashboard.seed         # first time only — creates + seeds medic.db
    python -m dashboard.app
  Then, from another terminal (or a browser on the same machine):
    curl http://127.0.0.1:5000/api/v1/fleet
    curl "http://127.0.0.1:5000/api/v1/tasks/active?robot_id=medic-01"
  Both should return HTTP 200 JSON with no robot wired up yet. Open
  http://127.0.0.1:5000/ in a browser — the fleet page should load (it
  will say "No robots reporting yet" until a Pi script or curl call
  registers one). See README.md / dashboard/DESIGN.md for the full R-series
  bench script (dispatch -> auth refuse -> auth ok -> audit -> temp).
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# Make `dashboard` and `medic` importable regardless of how this file was
# started. The canonical way — matching systemd/medic-dashboard.service's
# ExecStart, scripts/run_all.sh, README.md and medic/task_bridge.py's bench
# test — is the plain script path `cd pi-deploy && python dashboard/app.py`.
# That puts dashboard/ (not pi-deploy/) on sys.path, so the insert below is
# what actually makes `from dashboard import db` and `from medic...` resolve.
# It is also a harmless no-op under `python -m dashboard.app`, which some
# students will try; both forms work.
# ---------------------------------------------------------------------------
_PI_DEPLOY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PI_DEPLOY_DIR not in sys.path:
    sys.path.insert(0, _PI_DEPLOY_DIR)

from dashboard import db  # noqa: E402
from medic.common import INFO, RED, SEVERITY, WARN, now_ts, setup_logging  # noqa: E402

log = setup_logging(os.environ.get("MEDIC_LOG_LEVEL", "INFO"), "dashboard")

app = Flask(__name__)
db.init_app(app)  # creates dashboard/medic.db + tables on first import, every time


def _recover_stale_tasks():
    """Abort any task left mid-flight by a power cut, BEFORE nav can pick it up.

    Why this exists: the four services autostart at boot. Without this, a task
    still in 'dispatched'/'en_route'/'arrived'/'awaiting_auth'/'dispensing' when
    the power dropped is still the ACTIVE task on the next boot — so
    GET /api/v1/tasks/active hands it straight back to medic/nav.py, which
    starts emitting drive goals seconds after you plug the robot in.

    That is unsafe and it is also wrong. After a reboot the robot's physical
    position is unknown: there are no encoders (docs/no-encoder-nav.md), so
    there is no odometry to recover from, and the marker it was homing on may
    now be behind it. Resuming blind is exactly the open-ended wandering R15
    forbids. A delivery interrupted by power loss must be re-dispatched by a
    human, which is also the whole "human supervised, the robot never decides"
    pitch (ARCHITECTURE.md S1).

    Fails soft: if this cannot run, the dashboard still starts. Never let
    housekeeping stop the audit log from being available.
    """
    try:
        conn = db.connect()
    except Exception as exc:  # pragma: no cover - only on a broken/locked DB
        log.error("stale-task recovery could not open the DB: %s", exc)
        return
    try:
        rows = conn.execute(
            "SELECT id, robot_id, state FROM tasks WHERE state NOT IN (?,?,?)",
            db.TERMINAL_STATES,
        ).fetchall()
        ts = now_ts()
        for row in rows:
            detail = (
                "aborted at dashboard startup: task was still '%s' when the "
                "robot lost power. Position unknown after reboot (no encoders) "
                "- re-dispatch manually rather than resuming." % row["state"]
            )
            db.set_task_state(conn, row["id"], "aborted", detail, None, ts)
            # The audit log is the product: a task that silently vanished
            # between boots would be a hole in it.
            db.append_event(
                conn,
                robot_id=row["robot_id"],
                kind="task_complete",
                severity="warn",
                detail="task %s aborted on boot (was '%s')" % (row["id"], row["state"]),
                task_id=row["id"],
                ts=ts,
                received_ts=ts,
            )
            log.warning("aborted stale task %s (was '%s') on startup",
                        row["id"], row["state"])
        if not rows:
            log.info("stale-task recovery: nothing in flight, clean start")
    except Exception as exc:  # pragma: no cover
        log.error("stale-task recovery failed (continuing anyway): %s", exc)
    finally:
        conn.close()


_recover_stale_tasks()

# ---------------------------------------------------------------------------
# Tuning constants — every one of these is a bench-tunable dial, called out
# with the doc/design section it comes from. None of them are secrets.
# ---------------------------------------------------------------------------

# docs/design-doc-v0.3.md S4.3: "Any mismatch or 30 s timeout." Matches
# medic/task_bridge.py's own AUTH_WINDOW_S exactly (that file's timer is the
# primary one in practice; this one is the server-side backstop — see
# api_auth_verify()'s awaiting_auth_expires_ts check).
AUTH_WINDOW_S = 30.0

# A robot counts as "online" on the fleet page if telemetry arrived within
# this many seconds. docs/design-doc-v0.3.md S4.6: "Pi flags dashboard if
# robot unreachable > 5 s" — reusing that same number here for consistency
# rather than inventing a new one.
ONLINE_TIMEOUT_S = 5.0

# Cold-box safe band for the R6 excursion check (ARCHITECTURE.md S5: "an
# excursion warning"). A gel-pack cold box, not real refrigeration — this
# is a plausible cold-chain band, not a measured one.
# TODO(day-5): confirm against real DS18B20 + gel-pack bench data once the
# cold box is built (ARCHITECTURE.md S5 / docs/build-plan.md Day 5).
TEMP_MIN_C = 2.0
TEMP_MAX_C = 8.0

VALID_TELEOP_CMDS = ("fwd", "back", "left", "right", "stop")

# markers/README.md is the canonical marker layout for this build's one
# L-shaped course: Pharmacy (10) -> corner (15) -> Ward (20). Every leg
# starts at the Pharmacy, so a "route" here means "how to get from the
# Pharmacy to this destination." If the team ever adds waypoints, extend
# this table — it's the one place that knows the course layout. An unknown
# destination_marker falls back to a direct single-leg route rather than
# rejecting the dispatch outright (matches descope ladder rung 3: "single
# destination marker" must keep working with no dashboard code change).
PHARMACY_MARKER = 10
CORNER_MARKER = 15
WARD_MARKER = 20
# How many times two markers must be seen together before we will plan a route
# through that link. 1 makes teaching instant but trusts a single frame, and a
# single frame can catch a marker through a doorway you cannot actually drive
# through. 3 means "seen together repeatedly from a moving robot", which is
# much better evidence that the hop is really drivable.
EDGE_MIN_SIGHTINGS = int(os.environ.get("MEDIC_MAP_MIN_EDGE_SIGHTINGS", "3"))

ROUTE_TABLE = {
    WARD_MARKER: [CORNER_MARKER, WARD_MARKER],
    PHARMACY_MARKER: [CORNER_MARKER, PHARMACY_MARKER],
}

TELEMETRY_KEYS = ("state", "ovr", "obstacle_cm", "blocked", "estop", "temp_c")

CONFIG_FIELD_MAP = {
    # body key -> (column, caster)
    "sound_threshold": ("sound_threshold", float),
    "sound_min_ms": ("sound_min_ms", int),
    "sound_refractory_s": ("sound_refractory_s", float),
    "teleop_enabled": ("teleop_enabled", lambda v: 1 if _to_bool_early(v) else 0),
}
NAV_FIELD_MAP = {
    "max_spd": ("nav_max_spd", float),
    "target_w_px": ("nav_target_w_px", int),
    "coast_s": ("nav_coast_s", float),
    "search_s": ("nav_search_s", float),
}


# ---------------------------------------------------------------------------
# Small parsing helpers. Every one of these fails soft (returns a default
# instead of raising) — malformed input from a query string or a JSON body
# must never 500 this process; it should read as "that field was bad" and
# get handled by the caller as a normal 400, not a crash.
# ---------------------------------------------------------------------------
def _to_bool_early(v, default=False):
    # Named _early because CONFIG_FIELD_MAP above (module-level) needs it
    # before the "real" _to_bool below is defined; they're the same
    # function — see the alias right after _to_bool's definition.
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no")
    return default


def _to_bool(v, default=False):
    return _to_bool_early(v, default)


def _to_int(v, default=None):
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_float(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _body():
    """Parsed JSON body, or {} for anything malformed/missing/wrong content
    type. Never raises — see the module docstring on blip tolerance."""
    return request.get_json(silent=True) or {}


def _route_for(destination_marker, origin_marker=None, conn=None):
    """Plan the marker chain to a destination.

    Prefers the LEARNED map (teach mode) so the robot can go anywhere it has
    been taught, and falls back to the hardcoded ROUTE_TABLE so a robot that
    has never been taught still runs the original Pharmacy<->Ward demo exactly
    as before. Order matters: the taught map describes the room you are
    actually standing in; ROUTE_TABLE is a guess baked in months earlier.

    An unknown destination degrades to a single-hop [destination] — drive
    straight at it and let nav's own coast/search/lost bounds (R15) handle it
    if it is not there. That is the same behaviour as before this function
    learned anything.
    """
    origin = PHARMACY_MARKER if origin_marker is None else int(origin_marker)
    if conn is not None and origin != destination_marker:
        try:
            path = db.find_route(conn, origin, destination_marker, EDGE_MIN_SIGHTINGS)
            if path:
                # find_route includes the origin; the robot is already there, so
                # the route it drives is everything after it.
                return path[1:] or [destination_marker]
        except Exception as exc:  # never let map trouble block a dispatch
            log.warning("map route lookup failed, using ROUTE_TABLE: %s", exc)
    return ROUTE_TABLE.get(destination_marker, [destination_marker])


def _build_payload_desc(units, cold_item):
    """Candy-only description text (ARCHITECTURE.md S0.2 — never a real
    drug name, anywhere). Mirrors the exact wording used in
    docs/http-api-v1.md's worked example."""
    bits = []
    if units and units > 0:
        bits.append("%dx round candy 'tablet' stand-in" % units)
    if cold_item:
        bits.append("1 chilled bead pouch")
    return " + ".join(bits) if bits else "no payload"


def _auth_window_expiry(base_ts):
    """base_ts is an ISO-8601 string from now_ts() — always UTC, always
    "+00:00", always millisecond precision, so both parsing it back and
    later comparing two such strings lexically (see api_auth_verify) are
    safe as long as every writer keeps using now_ts()."""
    dt = datetime.fromisoformat(base_ts)
    return (dt + timedelta(seconds=AUTH_WINDOW_S)).isoformat(timespec="milliseconds")


def _serialize_task(conn, row):
    """Shared by GET /api/v1/tasks/active, GET /api/v1/tasks and the
    dispatch response's live-tracking poll — one place that knows the
    task JSON shape docs/http-api-v1.md defines."""
    return {
        "task_id": row["id"],
        "robot_id": row["robot_id"],
        "patient_id": row["patient_id"],
        "patient_name": row["patient_name"],
        "destination_marker": row["destination_marker"],
        "route": json.loads(row["route"]),
        "units": row["units"],
        "cold_item": bool(row["cold_item"]),
        "payload_desc": row["payload_desc"],
        "state": row["state"],
        # Informational only — see db.get_expected_staff_uid()'s docstring.
        # The robot never compares these itself (docs/http-api-v1.md).
        "expected_staff_uid": db.get_expected_staff_uid(conn),
        "expected_patient_uid": db.get_expected_patient_uid(conn, row["patient_id"]),
        "created_ts": row["created_ts"],
    }


def _config_json(row):
    return {
        "sound_threshold": row["sound_threshold"],
        "sound_min_ms": row["sound_min_ms"],
        "sound_refractory_s": row["sound_refractory_s"],
        "nav": {
            "max_spd": row["nav_max_spd"],
            "target_w_px": row["nav_target_w_px"],
            "coast_s": row["nav_coast_s"],
            "search_s": row["nav_search_s"],
        },
        "teleop_enabled": bool(row["teleop_enabled"]),
        "config_rev": row["config_rev"],
    }


# ---------------------------------------------------------------------------
# Browser pages — every one of these takes NO template context on purpose
# (see templates/base.html's own comment: it asks nothing of app.py so the
# frontend and backend agents could build in parallel against the frozen
# JSON API alone).
# ---------------------------------------------------------------------------
@app.route("/")
def page_fleet():
    return render_template("fleet.html")


@app.route("/dispatch")
def page_dispatch():
    return render_template("dispatch.html")


@app.route("/audit")
def page_audit():
    return render_template("audit.html")


@app.route("/temp")
def page_temp():
    return render_template("temp.html")


@app.route("/teleop")
def page_teleop():
    return render_template("teleop.html")


# ---------------------------------------------------------------------------
# Pi-facing: GET /api/v1/tasks/active
# ---------------------------------------------------------------------------
@app.route("/api/v1/tasks/active", methods=["GET"])
def api_tasks_active():
    robot_id = request.args.get("robot_id")
    if not robot_id:
        return jsonify({"task": None})

    conn = db.get_db()
    ts = now_ts()
    db.ensure_robot(conn, robot_id, ts)
    row = db.get_active_task(conn, robot_id)
    if row is None:
        return jsonify({"task": None})
    # Unwrapped at the top level when found — see docs/http-api-v1.md's
    # worked example. Deliberately asymmetric with the "not found" shape;
    # medic/common.py's Central.get_active_task() parses both.
    return jsonify(_serialize_task(conn, row))


# ---------------------------------------------------------------------------
# Pi-facing: POST /api/v1/tasks/<task_id>/state
# ---------------------------------------------------------------------------
@app.route("/api/v1/tasks/<int:task_id>/state", methods=["POST"])
def api_task_state(task_id):
    body = _body()
    robot_id = body.get("robot_id")
    new_state = body.get("state")
    detail = body.get("detail") or ""

    if not robot_id:
        return jsonify({"ok": False, "error": "robot_id is required"}), 400
    if new_state not in db.VALID_TASK_STATES:
        return jsonify({"ok": False, "error": "unknown state %r" % (new_state,)}), 400

    conn = db.get_db()
    row = db.get_task(conn, task_id)
    if row is None:
        return jsonify({"ok": False, "error": "task not found"}), 404
    if row["robot_id"] != robot_id:
        return jsonify({"ok": False, "error": "robot_id does not match this task"}), 400

    current = row["state"]
    if current in db.TERMINAL_STATES:
        # "One task, one decision" (docs/http-api-v1.md). A refused/
        # complete/aborted task is permanently closed — see api_auth_verify,
        # which relies on this being true to keep its own guarantee.
        return (
            jsonify({
                "ok": False,
                "error": "task %d is already %s (terminal) — cannot change" % (task_id, current),
            }),
            409,
        )
    if new_state == "awaiting_auth" and current == "dispensing":
        # Closes a real reopening loophole: medic/task_bridge.py tracks
        # "already decided" task ids only in an in-process set
        # (decided_task_ids), which resets if that process restarts. If the
        # MCU then re-enters AT_WARD_WAIT_AUTH for a task that already got
        # an approved ("dispensing") verdict, task_bridge would otherwise
        # be able to reopen the auth window and collect a second pair of
        # scans for it. Every other state is reachable from "dispensing"
        # (e.g. -> complete, -> aborted) — only the reopen-to-awaiting_auth
        # path is blocked.
        return (
            jsonify({
                "ok": False,
                "error": "task %d already has an approved auth decision — cannot reopen" % task_id,
            }),
            409,
        )

    ts = now_ts()
    expires = _auth_window_expiry(ts) if new_state == "awaiting_auth" else None
    db.set_task_state(conn, task_id, new_state, detail, expires, ts)
    return jsonify({"ok": True, "state": new_state})


# ---------------------------------------------------------------------------
# Pi-facing: POST /api/v1/events (single or batch)
# ---------------------------------------------------------------------------
@app.route("/api/v1/events", methods=["POST"])
def api_events_post():
    body = _body()
    items = body["events"] if isinstance(body.get("events"), list) else [body]

    # Validate the whole batch before writing anything, so a caller never
    # gets back a partial "ids" list it can't line up with what it sent.
    for item in items:
        if not isinstance(item, dict) or not item.get("robot_id") or not item.get("kind"):
            return jsonify({"ok": False, "error": "each event needs robot_id and kind"}), 400

    conn = db.get_db()
    received = now_ts()
    ids = []
    for item in items:
        robot_id = item["robot_id"]
        kind = item["kind"]
        db.ensure_robot(conn, robot_id, received)
        severity = item.get("severity") or SEVERITY.get(kind, INFO)
        ids.append(
            db.append_event(
                conn,
                robot_id=robot_id,
                kind=kind,
                severity=severity,
                detail=item.get("detail") or "",
                task_id=item.get("task_id"),
                ts=item.get("ts") or received,
                received_ts=received,
            )
        )
    return jsonify({"ok": True, "ids": ids})


# ---------------------------------------------------------------------------
# Pi-facing: POST /api/v1/telemetry (single or batch)
# ---------------------------------------------------------------------------
def _telemetry_fields(item):
    """Only the keys actually present in this item — see db.touch_robot()'s
    docstring for why presence (not just truthiness) matters here."""
    return {k: item[k] for k in TELEMETRY_KEYS if k in item}


@app.route("/api/v1/telemetry", methods=["POST"])
def api_telemetry_post():
    body = _body()
    items = body["telemetry"] if isinstance(body.get("telemetry"), list) else [body]

    for item in items:
        if not isinstance(item, dict) or not item.get("robot_id"):
            return jsonify({"ok": False, "error": "robot_id is required"}), 400

    conn = db.get_db()
    received = now_ts()
    for item in items:
        robot_id = item["robot_id"]
        ts = item.get("ts") or received
        db.ensure_robot(conn, robot_id, received)
        fields = _telemetry_fields(item)

        is_excursion = False
        if fields.get("temp_c") is not None:
            c = _to_float(fields["temp_c"], None)
            if c is not None:
                is_excursion = not (TEMP_MIN_C <= c <= TEMP_MAX_C)
                if is_excursion:
                    # Only log a NEW excursion (the falling edge into the
                    # bad band), not every telemetry post while it stays
                    # bad — R6 needs the reading gap-free, not the audit
                    # log spammed every ~30s for one ongoing event.
                    prev = db.get_last_telemetry(conn, robot_id)
                    was_excursion = bool(prev and prev["excursion"])
                    if not was_excursion:
                        active = db.get_active_task(conn, robot_id)
                        db.append_event(
                            conn,
                            robot_id=robot_id,
                            kind="temp_excursion",
                            severity=SEVERITY["temp_excursion"],
                            detail="%.1f C outside safe band [%.1f, %.1f]"
                            % (c, TEMP_MIN_C, TEMP_MAX_C),
                            task_id=active["id"] if active else None,
                            ts=ts,
                            received_ts=received,
                        )

        db.insert_telemetry(
            conn, robot_id=robot_id, ts=ts, received_ts=received,
            fields=fields, excursion=is_excursion,
        )
        db.touch_robot(conn, robot_id, received, fields)

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Pi-facing: POST /api/v1/auth/verify — THE critical endpoint (root
# ARCHITECTURE.md S0.4). Read the module docstring before touching this function.
# ---------------------------------------------------------------------------
def _refuse(conn, robot_id, task_id, reason, ts):
    event_id = db.append_event(
        conn,
        robot_id=robot_id or "unknown",  # events.robot_id is NOT NULL; a
        # malformed request with no robot_id still gets logged, under a
        # sentinel, rather than being silently dropped.
        kind="auth_refused",
        severity=SEVERITY["auth_refused"],
        detail=reason,
        task_id=task_id,
        ts=ts,
        received_ts=ts,
    )
    return jsonify(
        {
            "authorized": False,
            "reason": reason,
            "dispense_count": 0,
            "open_latch": False,
            "event_id": event_id,
        }
    )


@app.route("/api/v1/auth/verify", methods=["POST"])
def api_auth_verify():
    body = _body()
    robot_id = body.get("robot_id")
    task_id_raw = body.get("task_id")
    staff_uid = body.get("staff_uid")
    patient_uid = body.get("patient_uid")
    ts = now_ts()

    conn = db.get_db()

    if not robot_id or task_id_raw in (None, "") or not staff_uid or not patient_uid:
        return _refuse(conn, robot_id, None, "malformed_request", ts)

    try:
        task_id = int(task_id_raw)
    except (TypeError, ValueError):
        return _refuse(conn, robot_id, None, "malformed_request", ts)

    db.ensure_robot(conn, robot_id, ts)

    task = db.get_task(conn, task_id)
    if task is None:
        return _refuse(conn, robot_id, task_id, "no_active_task", ts)

    if task["state"] != "awaiting_auth":
        return _refuse(conn, robot_id, task_id, "wrong_task_state", ts)

    expires = task["awaiting_auth_expires_ts"]
    if expires and ts > expires:
        # Lexical ISO-8601 comparison is safe here: both sides always come
        # from now_ts()'s fixed UTC "+00:00", fixed-width millisecond
        # format, so string order == chronological order.
        db.set_task_state(conn, task_id, "refused", "auth window expired", None, ts)
        return _refuse(conn, robot_id, task_id, "auth_window_expired", ts)

    staff = db.find_staff_by_uid(conn, staff_uid)
    if staff is None:
        db.set_task_state(conn, task_id, "refused", "unknown staff uid", None, ts)
        return _refuse(conn, robot_id, task_id, "unknown_staff_uid", ts)
    if not (staff["active"] and staff["authorized"]):
        db.set_task_state(conn, task_id, "refused", "staff not authorized", None, ts)
        return _refuse(conn, robot_id, task_id, "staff_not_authorized", ts)

    tag = db.find_patient_tag_by_uid(conn, patient_uid)
    if tag is None or not tag["active"]:
        # An inactive tag is treated the same as "never heard of it" —
        # neither one is a usable, resolvable wristband right now.
        db.set_task_state(conn, task_id, "refused", "unknown patient uid", None, ts)
        return _refuse(conn, robot_id, task_id, "unknown_patient_uid", ts)
    if tag["patient_id"] != task["patient_id"]:
        db.set_task_state(conn, task_id, "refused", "patient tag mismatch", None, ts)
        return _refuse(conn, robot_id, task_id, "patient_tag_mismatch", ts)

    # Every check passed. Move the task to "dispensing" in THIS same
    # request, synchronously with the audit write below: if the Pi process
    # crashes the instant after reading this response, the task must
    # already be un-reopenable (see the "awaiting_auth" reopen guard in
    # api_task_state above) — the decision is final the moment this
    # function returns, not whenever some later request happens to land.
    db.set_task_state(conn, task_id, "dispensing", "auth ok", None, ts)
    event_id = db.append_event(
        conn,
        robot_id=robot_id,
        kind="auth_ok",
        severity=SEVERITY["auth_ok"],
        detail="staff=%s patient=%s" % (staff_uid, patient_uid),
        task_id=task_id,
        ts=ts,
        received_ts=ts,
    )
    return jsonify(
        {
            "authorized": True,
            "reason": "match",
            "dispense_count": task["units"],
            "open_latch": bool(task["cold_item"]),
            "event_id": event_id,
        }
    )


# ---------------------------------------------------------------------------
# Pi-facing + browser-facing: GET/POST /api/v1/config
# ---------------------------------------------------------------------------
@app.route("/api/v1/config", methods=["GET"])
def api_config_get():
    robot_id = request.args.get("robot_id")
    if not robot_id:
        return jsonify({"error": "robot_id is required"}), 400
    conn = db.get_db()
    ts = now_ts()
    db.ensure_robot(conn, robot_id, ts)
    row = db.get_config(conn, robot_id)
    return jsonify(_config_json(row))


@app.route("/api/v1/config", methods=["POST"])
def api_config_post():
    body = _body()
    robot_id = body.get("robot_id")
    if not robot_id:
        return jsonify({"ok": False, "error": "robot_id is required"}), 400

    conn = db.get_db()
    ts = now_ts()
    db.ensure_robot(conn, robot_id, ts)

    updates = {}
    bad_fields = []
    for key, (col, caster) in CONFIG_FIELD_MAP.items():
        if key in body:
            try:
                updates[col] = caster(body[key])
            except (TypeError, ValueError):
                bad_fields.append(key)
    nav_body = body.get("nav")
    if isinstance(nav_body, dict):
        for key, (col, caster) in NAV_FIELD_MAP.items():
            if key in nav_body:
                try:
                    updates[col] = caster(nav_body[key])
                except (TypeError, ValueError):
                    bad_fields.append("nav.%s" % key)

    if bad_fields:
        return (
            jsonify({"ok": False, "error": "bad value(s) for: %s" % ", ".join(bad_fields)}),
            400,
        )

    new_rev = db.update_config(conn, robot_id, updates, ts)
    return jsonify({"ok": True, "config_rev": new_rev})


# ---------------------------------------------------------------------------
# Pi-facing + browser-facing: GET/POST /api/v1/teleop
# ---------------------------------------------------------------------------
@app.route("/api/v1/teleop", methods=["GET"])
def api_teleop_get():
    robot_id = request.args.get("robot_id")
    if not robot_id:
        return jsonify({"cmd": None})

    conn = db.get_db()
    db.ensure_robot(conn, robot_id, now_ts())
    row = db.get_teleop(conn, robot_id)
    if row is None or row["cmd"] is None:
        return jsonify({"cmd": None})
    # No event write here — see the module docstring: medic/task_bridge.py
    # logs "teleop_nudge" itself, once, the moment it actually relays this
    # command to the MCU (it only acts on a NEW seq, so that single log
    # line and this hand-out are guaranteed to correspond 1:1).
    return jsonify({"cmd": row["cmd"], "spd": row["spd"], "seq": row["seq"]})


@app.route("/api/v1/teleop", methods=["POST"])
def api_teleop_post():
    body = _body()
    robot_id = body.get("robot_id")
    cmd = body.get("cmd")
    if not robot_id or cmd not in VALID_TELEOP_CMDS:
        return jsonify({"ok": False, "error": "robot_id and a valid cmd are required"}), 400

    default_spd = 0.0 if cmd == "stop" else 0.3
    spd = _to_float(body.get("spd"), default_spd)
    spd = max(0.0, min(1.0, spd))

    conn = db.get_db()
    ts = now_ts()
    db.ensure_robot(conn, robot_id, ts)
    seq = db.enqueue_teleop(conn, robot_id, cmd, spd, ts)
    return jsonify({"ok": True, "seq": seq})


# ---------------------------------------------------------------------------
# Browser-facing: fleet / events / temp / patients / tasks
# ---------------------------------------------------------------------------
@app.route("/api/v1/fleet", methods=["GET"])
def api_fleet():
    conn = db.get_db()
    rows = db.get_fleet(conn)
    now = datetime.now(timezone.utc)

    robots = []
    for r in rows:
        online = False
        if r["last_seen_ts"]:
            try:
                seen = datetime.fromisoformat(r["last_seen_ts"])
                online = (now - seen).total_seconds() <= ONLINE_TIMEOUT_S
            except ValueError:
                online = False
        robots.append(
            {
                "robot_id": r["robot_id"],
                "state": r["last_state"],
                "ovr": r["last_ovr"],
                "last_seen": r["last_seen_ts"],
                "online": online,
                "temp_c": r["last_temp_c"],
                "task_id": r["task_id"],
            }
        )
    return jsonify({"robots": robots})


@app.route("/api/v1/events", methods=["GET"])
def api_events_get():
    conn = db.get_db()
    limit = _to_int(request.args.get("limit"), 100) or 100
    limit = max(1, min(limit, 500))
    kind = request.args.get("kind") or None
    severity = request.args.get("severity") or None
    robot_id = request.args.get("robot_id") or None
    since_id = _to_int(request.args.get("since_id"), None)

    rows, max_id = db.list_events(
        conn, limit=limit, kind=kind, severity=severity, robot_id=robot_id, since_id=since_id
    )
    return jsonify({"events": [dict(r) for r in rows], "max_id": max_id})


@app.route("/api/v1/temp", methods=["GET"])
def api_temp():
    conn = db.get_db()
    robot_id = request.args.get("robot_id") or db.first_robot_id(conn)
    minutes = _to_float(request.args.get("minutes"), 60.0) or 60.0

    if not robot_id:
        return jsonify({"series": [], "excursions": [], "min_c": None, "max_c": None})

    series, excursions = db.get_temp_series(conn, robot_id, minutes)
    values = [p["c"] for p in series if p["c"] is not None]
    return jsonify(
        {
            "series": series,
            "excursions": excursions,
            "min_c": min(values) if values else None,
            "max_c": max(values) if values else None,
        }
    )


@app.route("/api/v1/patients", methods=["GET"])
def api_patients():
    conn = db.get_db()
    rows = db.list_patients(conn)
    return jsonify({"patients": [dict(r) for r in rows]})


@app.route("/api/v1/tasks", methods=["GET"])
def api_tasks_get():
    conn = db.get_db()
    limit = _to_int(request.args.get("limit"), 50) or 50
    limit = max(1, min(limit, 200))
    rows = db.list_tasks(conn, limit)
    return jsonify({"tasks": [_serialize_task(conn, r) for r in rows]})


@app.route("/api/v1/tasks", methods=["POST"])
def api_tasks_post():
    body = _body()
    patient_id = body.get("patient_id")
    robot_id = body.get("robot_id")
    units = _to_int(body.get("units"), None)
    cold_item = _to_bool(body.get("cold_item"), False)
    destination_marker = _to_int(body.get("destination_marker"), None)

    if not patient_id or not robot_id:
        return jsonify({"error": "patient_id and robot_id are required"}), 400
    if units is None or units < 1:
        return jsonify({"error": "units must be a positive integer"}), 400
    if destination_marker is None:
        return jsonify({"error": "destination_marker is required"}), 400

    conn = db.get_db()
    if not db.patient_exists(conn, patient_id):
        return jsonify({"error": "unknown patient_id %r" % (patient_id,)}), 400

    ts = now_ts()
    db.ensure_robot(conn, robot_id, ts)

    # Plan from where the robot actually IS, not from a fixed start. That is
    # what makes any-to-any dispatch work once the map has been taught: a robot
    # sitting at the Ward can be sent straight to Room 4B without going home
    # first. origin_marker is optional — omit it and we assume the Pharmacy,
    # which is the old behaviour.
    origin_marker = _to_int(body.get("origin_marker"), None)
    route = _route_for(destination_marker, origin_marker, conn)
    payload_desc = _build_payload_desc(units, cold_item)

    task_id = db.create_task(
        conn,
        robot_id=robot_id,
        patient_id=patient_id,
        units=units,
        cold_item=cold_item,
        destination_marker=destination_marker,
        route=route,
        payload_desc=payload_desc,
        ts=ts,
    )
    db.append_event(
        conn,
        robot_id=robot_id,
        kind="dispatch",
        severity=SEVERITY["dispatch"],
        detail="task %d: %s for %s -> marker %d"
        % (task_id, payload_desc, patient_id, destination_marker),
        task_id=task_id,
        ts=ts,
        received_ts=ts,
    )
    return jsonify({"task_id": task_id})


# ---------------------------------------------------------------------------
# LIVE CONSOLE — one page that shows everything at once.
#
# Why this exists: the audit log (events) and the system log (journald) are two
# different streams, and until now you had to watch the dashboard in a browser
# AND `journalctl -f` over SSH to see the whole picture. Events tell you what
# the ROBOT did; the journal tells you what the SOFTWARE did (camera timeouts,
# tracebacks, serial drops, restarts). Debugging needs both side by side.
# ---------------------------------------------------------------------------

# Hardcoded on purpose. The unit names must NEVER come from the query string —
# they are arguments to a subprocess, and accepting them from the client would
# let anyone on the LAN read arbitrary units out of the journal.
LIVE_UNITS = ("medic-dashboard", "medic-bridge", "medic-nav", "medic-ears")

# journald cursors are opaque tokens like "s=abc;i=1f3;b=...". Validate the
# charset before handing one back to journalctl.
_CURSOR_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789;=_-."
)

_PRIORITY_NAME = {
    "0": "emerg", "1": "alert", "2": "crit", "3": "err",
    "4": "warning", "5": "notice", "6": "info", "7": "debug",
}


@app.route("/live")
def page_live():
    return render_template("live.html")


@app.route("/api/v1/logs")
def api_logs():
    """Tail the journal for our four units. Cursor-based so the page appends
    instead of refetching, exactly like the audit tail."""
    import subprocess

    cursor = (request.args.get("cursor") or "").strip()
    if cursor and (len(cursor) > 512 or not set(cursor) <= _CURSOR_OK):
        cursor = ""  # ignore anything that doesn't look like a journald cursor

    try:
        limit = max(1, min(int(request.args.get("limit", 120)), 500))
    except (TypeError, ValueError):
        limit = 120

    cmd = ["journalctl", "--no-pager", "-o", "json"]
    for unit in LIVE_UNITS:
        cmd += ["-u", unit]
    if cursor:
        cmd += ["--after-cursor", cursor, "-n", str(limit)]
    else:
        cmd += ["-n", str(limit)]

    try:
        # shell=False (a list, not a string), so nothing here is shell-parsed.
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=8
        ).stdout
    except Exception as exc:
        log.warning("journalctl failed: %s", exc)
        return jsonify({"lines": [], "cursor": cursor, "error": str(exc)})

    lines, last_cursor = [], cursor
    for raw in out.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        unit = rec.get("_SYSTEMD_UNIT") or rec.get("SYSLOG_IDENTIFIER") or "?"
        unit = str(unit).replace(".service", "").replace("medic-", "")
        msg = rec.get("MESSAGE", "")
        if isinstance(msg, list):  # journald returns bytes-as-int-array sometimes
            try:
                msg = bytes(msg).decode("utf-8", "replace")
            except Exception:
                msg = str(msg)
        # Drop our OWN successful HTTP access logs. The live page polls four
        # endpoints every 1-5 s, and werkzeug logs each one, so without this
        # filter the log pane fills with a record of itself asking for the log
        # pane — drowning the real messages. Non-2xx lines are KEPT, because a
        # 500 on an API call is exactly what you'd want to see here.
        # Method-agnostic on purpose: it isn't only the page's own GETs. The
        # bridge POSTs telemetry every cycle and events on every MCU message,
        # so GET-only filtering still left the pane full of "POST
        # /api/v1/telemetry 200". Those are redundant anyway — the CONTENT of
        # every one of them is already rendered in the audit pane next door.
        # Anything that is not a clean 200/304 is KEPT, so a 500 on an API call
        # still shows up, which is exactly when you'd be looking here.
        if unit == "dashboard" and "werkzeug" in msg:
            if " 200 -" in msg or " 304 -" in msg:
                if rec.get("__CURSOR"):
                    last_cursor = rec["__CURSOR"]  # still advance past it
                continue

        try:
            ts = int(rec.get("__REALTIME_TIMESTAMP", "0")) / 1000000.0
        except (TypeError, ValueError):
            ts = 0.0
        prio = str(rec.get("PRIORITY", "6"))
        lines.append({
            "ts": ts,
            "unit": unit,
            "level": _PRIORITY_NAME.get(prio, "info"),
            "msg": msg,
        })
        if rec.get("__CURSOR"):
            last_cursor = rec["__CURSOR"]

    return jsonify({"lines": lines, "cursor": last_cursor})


# ---------------------------------------------------------------------------
# LEARNED MARKER MAP — teach mode + any-to-any routing.
# ---------------------------------------------------------------------------



@app.route("/api/v1/map/sighting", methods=["POST"])
def api_map_sighting():
    """medic/nav.py --teach posts every frame that saw 2+ markers."""
    body = _body()
    ids = body.get("markers") or []
    if not isinstance(ids, list) or len(ids) < 2:
        # A single marker teaches nothing about connectivity — it is the PAIRS
        # that make an edge. Accept it quietly so nav need not filter.
        return jsonify({"ok": True, "learned": 0})
    try:
        ids = [int(m) for m in ids]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "markers must be integers"}), 400

    conn = db.get_db()
    learned = db.record_sighting(conn, ids, now_ts())
    return jsonify({"ok": True, "learned": len(learned), "markers": learned})


@app.route("/api/v1/map")
def api_map_get():
    conn = db.get_db()
    markers, edges = db.list_map(conn, EDGE_MIN_SIGHTINGS)
    _all_markers, all_edges = db.list_map(conn, 1)
    return jsonify({
        "markers": markers,
        "edges": edges,
        "unnamed": db.unnamed_markers(conn),
        "min_edge_sightings": EDGE_MIN_SIGHTINGS,
        # Links seen but not yet trusted — shows teaching progress rather than
        # leaving the operator wondering why a link they just drove is missing.
        "provisional_edges": max(0, len(all_edges) - len(edges)),
    })


@app.route("/api/v1/map/marker", methods=["POST"])
def api_map_name():
    body = _body()
    try:
        marker_id = int(body.get("marker_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "marker_id is required"}), 400
    name = (body.get("name") or "").strip()
    kind = body.get("kind") or "waypoint"
    if kind not in ("station", "waypoint"):
        return jsonify({"ok": False, "error": "kind must be station|waypoint"}), 400
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400

    conn = db.get_db()
    ts = now_ts()
    db.name_marker(conn, marker_id, name, kind, ts)
    # Naming a marker changes where the robot can be sent, so it belongs in the
    # audit trail like any other operator action.
    db.append_event(
        conn,
        robot_id=body.get("robot_id") or "dashboard",
        kind="dispatch",
        severity=INFO,
        detail="map: marker %d named '%s' (%s)" % (marker_id, name, kind),
        task_id=None,
        ts=ts,
        received_ts=ts,
    )
    return jsonify({"ok": True, "marker_id": marker_id, "name": name, "kind": kind})


@app.route("/api/v1/map/route")
def api_map_route():
    # "from" is optional and defaults to the SAME origin _route_for() assumes,
    # so the dispatch page's route preview shows the route the robot would
    # really drive rather than a second, subtly different guess.
    raw_from = request.args.get("from")
    try:
        a = PHARMACY_MARKER if raw_from in (None, "") else int(raw_from)
        b = int(request.args.get("to"))
    except (TypeError, ValueError):
        return jsonify({"error": "a numeric 'to' marker id is required"}), 400
    conn = db.get_db()
    path = db.find_route(conn, a, b, EDGE_MIN_SIGHTINGS)
    return jsonify({"from": a, "to": b, "route": path, "hops": max(0, len(path) - 1),
                    "known": bool(path)})


@app.route("/map")
def page_map():
    return render_template("map.html")


@app.route("/api/v1/camera.jpg")
def api_camera_jpg():
    """Serve the newest frame nav.py published.

    nav OWNS the camera (only one process can hold the Pi Camera), so this
    endpoint never opens a camera itself -- it just serves the file nav writes
    to tmpfs. If nav isn't running, there is no feed, and that is the honest
    answer rather than a stale image pretending to be live.

    Snapshot polling, deliberately NOT an MJPEG stream: a stream holds a
    worker thread open for as long as the tab is open, and on a Pi 4 also
    serving three robot processes that is a real cost for a diagnostic view.
    """
    path = os.environ.get("MEDIC_NAV_FRAME_PATH", "/dev/shm/medic-nav.jpg")
    try:
        age = time.time() - os.path.getmtime(path)
        # Older than a few seconds means nav stopped publishing (crashed, or
        # the camera stalled). Report it instead of serving a frozen frame that
        # looks like a working camera pointed at something very still.
        if age > 5.0:
            return jsonify({"error": "stale", "age_s": round(age, 1)}), 503
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return jsonify({"error": "no feed"}), 503

    resp = app.response_class(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/v1/system")
def api_system():
    """Service states + whether the hardware is actually present. This is the
    'why is nothing happening' strip at the top of the live page."""
    import glob as _glob
    import subprocess

    services = {}
    for unit in LIVE_UNITS:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", unit], capture_output=True,
                text=True, timeout=5,
            )
            state = (r.stdout or "").strip() or "unknown"
        except Exception:
            state = "unknown"
        try:
            r2 = subprocess.run(
                ["systemctl", "is-enabled", unit], capture_output=True,
                text=True, timeout=5,
            )
            enabled = (r2.stdout or "").strip() or "unknown"
        except Exception:
            enabled = "unknown"
        services[unit.replace("medic-", "")] = {"active": state, "enabled": enabled}

    serial_ports = (_glob.glob("/dev/serial/by-id/*") or _glob.glob("/dev/ttyUSB*")
                    or _glob.glob("/dev/ttyACM*"))

    # A CSI camera shows up as a libcamera device, not /dev/video0 alone, so
    # check both. This is presence only — it does NOT prove frames flow (a
    # half-seated ribbon detects fine on I2C and still times out on capture).
    camera = bool(_glob.glob("/dev/video0")) or bool(
        _glob.glob("/base/soc/i2c0mux/*")
    )

    mic = False
    try:
        r = subprocess.run(["arecord", "-l"], capture_output=True, text=True,
                           timeout=5)
        mic = "card " in (r.stdout or "")
    except Exception:
        pass

    return jsonify({
        "services": services,
        "hardware": {
            "mcu_serial": serial_ports[0] if serial_ports else None,
            "camera": camera,
            "mic": mic,
        },
    })


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    host = os.environ.get("MEDIC_DASHBOARD_HOST", "0.0.0.0")
    port = _to_int(os.environ.get("MEDIC_DASHBOARD_PORT"), 5000)
    debug = os.environ.get("MEDIC_DASHBOARD_DEBUG", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    log.info(
        "Hospital Central dashboard starting on %s:%d (debug=%s, db=%s)",
        host, port, debug, db.DB_PATH,
    )
    app.run(host=host, port=port, debug=debug)
