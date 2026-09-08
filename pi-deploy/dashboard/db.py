"""db.py — SQLite plumbing for the "Hospital Central" dashboard.

Pure persistence layer: connection handling, schema init, and one query
function per shape app.py needs. NO Flask request/response logic lives
here (that's app.py's job) and NO medic.common import either — this module
works standalone (seed.py uses it with no Flask app running at all).

THE ONE RULE THIS FILE MUST NEVER BREAK
    events is an APPEND-ONLY audit log (dashboard/DESIGN.md: "never mutate
    a logged event"). This module defines append_event() and list_events()
    and NOTHING else that touches the events table — no update_event(), no
    delete_event(), not even for internal cleanup. If you ever find
    yourself wanting to "fix" a bad row, log a NEW event that explains the
    correction instead. Do not add an UPDATE/DELETE against events here,
    ever, for any reason.

THREADING: sqlite3 connections are NOT shareable across threads. Flask's
dev server (and most WSGI servers) may run each request in its own
thread, so get_db()/close_db() use flask.g to hand out exactly one
connection per request, opened lazily and closed in teardown_appcontext.
Standalone scripts (seed.py) instead call connect() directly and manage
the connection themselves — see seed.py.

COMMIT POLICY: every write helper below commits its own change before
returning. That costs a little throughput compared to batching commits at
the end of a request, but WAL mode (enabled in connect()) makes small
frequent commits cheap, and it means "did my event actually land" is never
a question of whether some other code path remembered to call commit() —
for an audit trail, that trade is worth it.

BENCH TEST:
    cd pi-deploy
    python -m dashboard.db
  Creates (or reuses) dashboard/medic.db, prints the resolved path and the
  list of tables now in it. Run this before app.py the first time on a
  fresh Pi to confirm the schema actually applies with no errors.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

try:
    import flask
except ImportError:  # pragma: no cover — only needed for the Flask g glue
    flask = None

# ---------------------------------------------------------------------------
# Location. Overridable via env var so a test run never has to touch the
# real deploy database. Defaults to a file right next to this module —
# matches pi-deploy/install.sh's first DB-candidate path (dashboard/medic.db).
# ---------------------------------------------------------------------------
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medic.db")
DB_PATH = os.environ.get("MEDIC_DASHBOARD_DB_PATH", DEFAULT_DB_PATH)

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# A task in one of these states is permanently closed — see
# set_task_state()'s transition guard in this file and its caller in
# app.py. Mirrors the state list frozen in docs/http-api-v1.md.
TERMINAL_STATES = ("complete", "refused", "aborted")

VALID_TASK_STATES = (
    "dispatched", "en_route", "arrived", "awaiting_auth",
    "dispensing", "complete", "refused", "aborted",
)


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------
def connect(path=None):
    """Open a new connection with the pragmas this project needs.

    A fresh connection every call — callers own the lifetime (get_db()
    caches one per Flask request; seed.py opens and closes its own).
    """
    conn = sqlite3.connect(path or DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # journal_mode=WAL lets the Pi's browser polling, the three Pi
    # processes, and app.py's own writes all proceed without blocking each
    # other on a single file (dashboard/DESIGN.md: this runs on a Pi 4B
    # alongside OpenCV — keep it light). WAL is persisted in the database
    # file itself once set, but there's no harm re-asserting it here.
    conn.execute("PRAGMA journal_mode = WAL")
    # foreign_keys is NOT persisted — SQLite requires this on every single
    # connection, unlike journal_mode.
    conn.execute("PRAGMA foreign_keys = ON")
    # A writer holding the WAL briefly must not make a concurrent reader
    # raise "database is locked" — wait up to 5s instead of failing
    # immediately (five Pi/browser clients hitting one file at 1-2 Hz each
    # can otherwise race into this under WAL without a busy handler).
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn=None):
    """Create every table/index if missing. Safe to call on every process
    start — schema.sql is 100% CREATE ... IF NOT EXISTS."""
    owns_conn = conn is None
    conn = conn or connect()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    if owns_conn:
        conn.close()


# -- Flask per-request connection --------------------------------------------
def get_db():
    """Return this request's connection, opening one on first use.

    Requires an active Flask application/request context (init_app()
    below wires the teardown that closes it). Each request gets its own
    sqlite3.Connection — never share one across threads.
    """
    if flask is None:
        raise RuntimeError("flask is not installed — get_db() needs an app context")
    if "db" not in flask.g:
        flask.g.db = connect()
    return flask.g.db


def close_db(_exc=None):
    if flask is None:
        return
    db = flask.g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app):
    """Wire this module into a Flask app: schema init at startup, and a
    fresh/closed connection per request from then on."""
    init_db()
    app.teardown_appcontext(close_db)


# ---------------------------------------------------------------------------
# Robots
# ---------------------------------------------------------------------------
def ensure_robot(conn, robot_id, ts):
    """Register robot_id if this is the first time we've ever heard of it,
    and make sure it has a config row. Every endpoint that receives a
    robot_id calls this first so the fleet view and config endpoint never
    have to special-case "robot exists yet?"."""
    conn.execute(
        "INSERT OR IGNORE INTO robots (robot_id, created_ts) VALUES (?, ?)",
        (robot_id, ts),
    )
    conn.execute(
        "INSERT OR IGNORE INTO config (robot_id, updated_ts) VALUES (?, ?)",
        (robot_id, ts),
    )
    conn.commit()


def touch_robot(conn, robot_id, ts, fields):
    """Update the robots table's "latest telemetry snapshot" columns.

    `fields` may contain any of "state", "ovr", "temp_c" — ONLY keys that
    are actually PRESENT get written; an absent key leaves that column
    completely alone. This distinction matters: medic/task_bridge.py posts
    partial telemetry per MCU message (e.g. an obstacle update carries no
    "state" key at all), so blindly overwriting every column on every call
    would wipe out the last known state/temp every time an unrelated field
    came in. A key that IS present with value None (e.g. {"ovr": null}
    when an override just cleared) DOES overwrite the column to NULL —
    that's a real, meaningful update, not a missing field.
    """
    colmap = {"state": "last_state", "ovr": "last_ovr", "temp_c": "last_temp_c"}
    set_clauses = ["last_seen_ts = ?"]
    values = [ts]
    for key, column in colmap.items():
        if key in fields:
            set_clauses.append("%s = ?" % column)
            values.append(fields[key])
    values.append(robot_id)
    conn.execute(
        "UPDATE robots SET %s WHERE robot_id = ?" % ", ".join(set_clauses), values
    )
    conn.commit()


def first_robot_id(conn):
    row = conn.execute(
        "SELECT robot_id FROM robots ORDER BY created_ts ASC, robot_id ASC LIMIT 1"
    ).fetchone()
    return row["robot_id"] if row else None


def get_fleet(conn):
    """One row per known robot, with its current non-terminal task_id (if
    any) folded in. Small dataset (a handful of robots at most) so a
    per-row scalar subquery is simpler and plenty fast — see
    idx_tasks_robot_state, which is exactly the index this subquery uses.
    """
    placeholders = ",".join("?" for _ in TERMINAL_STATES)
    sql = (
        "SELECT r.robot_id, r.last_state, r.last_ovr, r.last_seen_ts, r.last_temp_c, "
        "(SELECT t.id FROM tasks t WHERE t.robot_id = r.robot_id "
        " AND t.state NOT IN (%s) ORDER BY t.created_ts DESC, t.id DESC LIMIT 1) AS task_id "
        "FROM robots r ORDER BY r.robot_id ASC" % placeholders
    )
    return conn.execute(sql, TERMINAL_STATES).fetchall()


# ---------------------------------------------------------------------------
# Patients / prescriptions
# ---------------------------------------------------------------------------
def list_patients(conn):
    """Each patient plus their most recent prescription's description as
    a single "prescription" display string (GET /api/v1/patients)."""
    sql = (
        "SELECT p.patient_id, p.name, p.room, "
        "(SELECT pr.description FROM prescriptions pr WHERE pr.patient_id = p.patient_id "
        " ORDER BY pr.created_ts DESC, pr.id DESC LIMIT 1) AS prescription "
        "FROM patients p ORDER BY p.patient_id ASC"
    )
    return conn.execute(sql).fetchall()


def patient_exists(conn, patient_id):
    row = conn.execute(
        "SELECT 1 FROM patients WHERE patient_id = ?", (patient_id,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Staff / patient tags — the two-scan roster
# ---------------------------------------------------------------------------
def find_staff_by_uid(conn, uid):
    return conn.execute("SELECT * FROM staff WHERE uid = ?", (uid,)).fetchone()


def find_patient_tag_by_uid(conn, uid):
    return conn.execute(
        "SELECT * FROM patient_tags WHERE uid = ?", (uid,)
    ).fetchone()


def get_expected_patient_uid(conn, patient_id):
    """The (informational-only, docs/http-api-v1.md) wristband UID for a
    task's patient — display/logging convenience for the Pi side. Picks
    that patient's oldest active tag; the real decision always happens
    server-side in /api/v1/auth/verify, never from this value."""
    row = conn.execute(
        "SELECT uid FROM patient_tags WHERE patient_id = ? AND active = 1 "
        "ORDER BY id ASC LIMIT 1",
        (patient_id,),
    ).fetchone()
    return row["uid"] if row else None


def get_expected_staff_uid(conn):
    """A representative authorized-and-active staff UID, for the same
    display-only purpose as get_expected_patient_uid(). ANY authorized,
    active badge is actually valid at auth time — this is just "here's
    one that would work," not an assignment."""
    row = conn.execute(
        "SELECT uid FROM staff WHERE authorized = 1 AND active = 1 "
        "ORDER BY id ASC LIMIT 1"
    ).fetchone()
    return row["uid"] if row else None


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
_TASK_SELECT = (
    "SELECT t.*, p.name AS patient_name FROM tasks t "
    "JOIN patients p ON p.patient_id = t.patient_id "
)


def get_task(conn, task_id):
    return conn.execute(_TASK_SELECT + "WHERE t.id = ?", (task_id,)).fetchone()


def get_active_task(conn, robot_id):
    """Most recent non-terminal task for this robot, or None. "Active"
    here matches GET /api/v1/tasks/active's contract exactly."""
    placeholders = ",".join("?" for _ in TERMINAL_STATES)
    sql = (
        _TASK_SELECT + "WHERE t.robot_id = ? AND t.state NOT IN (%s) "
        "ORDER BY t.created_ts DESC, t.id DESC LIMIT 1" % placeholders
    )
    return conn.execute(sql, (robot_id,) + TERMINAL_STATES).fetchone()


def list_tasks(conn, limit=50):
    sql = _TASK_SELECT + "ORDER BY t.created_ts DESC, t.id DESC LIMIT ?"
    return conn.execute(sql, (limit,)).fetchall()


def create_task(
    conn, *, robot_id, patient_id, units, cold_item, destination_marker,
    route, payload_desc, ts,
):
    cur = conn.execute(
        "INSERT INTO tasks (robot_id, patient_id, units, cold_item, "
        "destination_marker, route, payload_desc, state, created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatched', ?, ?)",
        (
            robot_id, patient_id, units, 1 if cold_item else 0,
            destination_marker, json.dumps(route), payload_desc, ts, ts,
        ),
    )
    conn.commit()
    return cur.lastrowid


def set_task_state(conn, task_id, state, detail, awaiting_auth_expires_ts, ts):
    """Unconditional write — the transition-legality guard ("one task, one
    decision"; a terminal task never reopens) lives in app.py, where the
    HTTP-level error response also gets decided. This function just
    applies whatever the caller already validated."""
    conn.execute(
        "UPDATE tasks SET state = ?, detail = ?, awaiting_auth_expires_ts = ?, "
        "updated_ts = ? WHERE id = ?",
        (state, detail, awaiting_auth_expires_ts, ts, task_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Events — append-only. See the module docstring before adding anything here.
# ---------------------------------------------------------------------------
def append_event(conn, *, robot_id, kind, severity, detail, task_id, ts, received_ts):
    cur = conn.execute(
        "INSERT INTO events (robot_id, kind, severity, detail, task_id, ts, received_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (robot_id, kind, severity, detail, task_id, ts, received_ts),
    )
    conn.commit()
    return cur.lastrowid


def list_events(conn, *, limit, kind=None, severity=None, robot_id=None, since_id=None):
    """Returns (rows, max_id).

    max_id is the cursor the caller should poll with next (docs/http-api-v1.md:
    "since_id lets the audit page tail efficiently instead of refetching
    everything"). It must NEVER be advanced past the last row we actually
    returned in `rows` -- app.js's initAudit() sends whatever we hand back
    here straight back as its next since_id (see its `sinceId = data.max_id`),
    so if `rows` was truncated by `limit` (more matching rows exist than we
    sent) and we still reported the table's true global MAX(id), the client
    would jump its cursor past everything we didn't send and silently skip
    it forever. That is exactly the bug this function used to have.

    Rule: once a page is truncated (len(rows) == limit), max_id is the id of
    the last row we actually served, so the next poll picks up exactly where
    this one left off. Only when we know `rows` holds EVERY currently
    matching row (len(rows) < limit -- we're caught up) is it safe to jump
    the cursor all the way to the table's true global max: no matching row
    can exist between the last one we returned and that id, and it saves the
    caller from re-scanning events already ruled out by the filter.

    Rows come back oldest-first within the page, matching audit.html's
    "insert each new row at the top" logic in app.js.
    """
    max_row = conn.execute("SELECT MAX(id) AS m FROM events").fetchone()
    table_max_id = max_row["m"] if max_row and max_row["m"] is not None else 0

    where = []
    params = []
    if since_id is not None:
        where.append("id > ?")
        params.append(since_id)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if severity:
        where.append("severity = ?")
        params.append(severity)
    if robot_id:
        where.append("robot_id = ?")
        params.append(robot_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    if since_id is not None:
        # Tailing forward: oldest-of-the-new first, capped at `limit`.
        sql = "SELECT * FROM events %s ORDER BY id ASC LIMIT ?" % where_sql
        rows = conn.execute(sql, params + [limit]).fetchall()
    else:
        # Fresh page load: the most recent `limit` rows, re-sorted to the
        # same oldest-first order so callers never have to branch on which
        # mode they're in.
        sql = "SELECT * FROM events %s ORDER BY id DESC LIMIT ?" % where_sql
        rows = list(reversed(conn.execute(sql, params + [limit]).fetchall()))

    if len(rows) >= limit:
        # Truncated -- more matching rows may still be waiting. Advance the
        # cursor only to what we actually served; the next poll (same
        # filters, since_id = this) will pick up the rest, possibly over
        # several polls if there's a big backlog. Slower than jumping ahead,
        # but it never loses an event.
        max_id = rows[-1]["id"]
    else:
        # Caught up: `rows` already contains every row matching the filter
        # at query time, so nothing matching can exist between the last row
        # we returned and the table's true max -- safe to jump the cursor
        # there.
        max_id = table_max_id

    return rows, max_id


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------
def insert_telemetry(conn, *, robot_id, ts, received_ts, fields, excursion):
    """`fields` is whatever subset of state/ovr/obstacle_cm/blocked/estop/
    temp_c this particular POST included — see app.py's presence-preserving
    parsing. Missing keys land as NULL in this row (that's fine; this table
    is a raw log of individual telemetry posts, not a rolling snapshot —
    touch_robot() is the rolling snapshot)."""
    cur = conn.execute(
        "INSERT INTO telemetry (robot_id, ts, state, ovr, obstacle_cm, blocked, "
        "estop, temp_c, excursion, received_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            robot_id, ts,
            fields.get("state"), fields.get("ovr"), fields.get("obstacle_cm"),
            fields.get("blocked"), fields.get("estop"), fields.get("temp_c"),
            1 if excursion else 0, received_ts,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_last_telemetry(conn, robot_id):
    return conn.execute(
        "SELECT * FROM telemetry WHERE robot_id = ? ORDER BY id DESC LIMIT 1",
        (robot_id,),
    ).fetchone()


def get_temp_series(conn, robot_id, minutes):
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat(timespec="milliseconds")
    rows = conn.execute(
        "SELECT ts, temp_c, excursion FROM telemetry "
        "WHERE robot_id = ? AND temp_c IS NOT NULL AND ts >= ? "
        "ORDER BY ts ASC",
        (robot_id, cutoff),
    ).fetchall()
    series = [{"ts": r["ts"], "c": r["temp_c"]} for r in rows]
    excursions = [
        {"ts": r["ts"], "c": r["temp_c"], "detail": "%.1f C outside safe band" % r["temp_c"]}
        for r in rows
        if r["excursion"]
    ]
    return series, excursions


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def get_config(conn, robot_id):
    return conn.execute(
        "SELECT * FROM config WHERE robot_id = ?", (robot_id,)
    ).fetchone()


def update_config(conn, robot_id, updates, ts):
    """`updates` is a dict of already-validated column -> value pairs (see
    app.py's whitelist/cast step — this function trusts its caller)."""
    if updates:
        set_clauses = ["%s = ?" % col for col in updates]
        values = list(updates.values())
        conn.execute(
            "UPDATE config SET %s, config_rev = config_rev + 1, updated_ts = ? "
            "WHERE robot_id = ?" % ", ".join(set_clauses),
            values + [ts, robot_id],
        )
    else:
        # Nothing to change but still asked to — bump the revision so a
        # caller polling config_rev sees SOMETHING happened, rather than
        # silently doing nothing on a body with no recognized fields.
        conn.execute(
            "UPDATE config SET config_rev = config_rev + 1, updated_ts = ? "
            "WHERE robot_id = ?",
            (ts, robot_id),
        )
    conn.commit()
    return conn.execute(
        "SELECT config_rev FROM config WHERE robot_id = ?", (robot_id,)
    ).fetchone()["config_rev"]


# ---------------------------------------------------------------------------
# Teleop mailbox
# ---------------------------------------------------------------------------
def get_teleop(conn, robot_id):
    return conn.execute(
        "SELECT * FROM teleop_queue WHERE robot_id = ?", (robot_id,)
    ).fetchone()


def enqueue_teleop(conn, robot_id, cmd, spd, ts):
    row = get_teleop(conn, robot_id)
    next_seq = (row["seq"] + 1) if row else 1
    conn.execute(
        "INSERT INTO teleop_queue (robot_id, cmd, spd, seq, updated_ts) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(robot_id) DO UPDATE SET "
        "cmd = excluded.cmd, spd = excluded.spd, seq = excluded.seq, "
        "updated_ts = excluded.updated_ts",
        (robot_id, cmd, spd, next_seq, ts),
    )
    conn.commit()
    return next_seq


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    conn = connect()
    init_db(conn)
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    ]
    print("db path : %s" % DB_PATH)
    print("tables  : %s" % ", ".join(tables))
    conn.close()


# ---------------------------------------------------------------------------
# Learned marker map (teach mode). See the big comment in schema.sql.
# ---------------------------------------------------------------------------
def record_sighting(conn, marker_ids, ts):
    """One camera frame saw these marker IDs at the same time.

    Upserts a node per marker and an edge for every pair. Edges are stored
    both ways so path-finding needs no direction special-casing.
    """
    ids = sorted({int(m) for m in marker_ids})
    for mid in ids:
        conn.execute(
            "INSERT INTO map_markers (marker_id, sightings, first_seen_ts, last_seen_ts) "
            "VALUES (?, 1, ?, ?) "
            "ON CONFLICT(marker_id) DO UPDATE SET "
            "  sightings = sightings + 1, last_seen_ts = excluded.last_seen_ts",
            (mid, ts, ts),
        )
    # Every unordered pair in this frame becomes a bidirectional edge.
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            for x, y in ((a, b), (b, a)):
                conn.execute(
                    "INSERT INTO map_edges (from_id, to_id, seen_count, last_seen_ts) "
                    "VALUES (?, ?, 1, ?) "
                    "ON CONFLICT(from_id, to_id) DO UPDATE SET "
                    "  seen_count = seen_count + 1, last_seen_ts = excluded.last_seen_ts",
                    (x, y, ts),
                )
    conn.commit()
    return ids


def name_marker(conn, marker_id, name, kind, ts):
    """Name a marker (the human half of teach mode). Creates the node if the
    operator names one the robot has not seen yet."""
    conn.execute(
        "INSERT INTO map_markers (marker_id, name, kind, first_seen_ts, last_seen_ts) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(marker_id) DO UPDATE SET name = excluded.name, kind = excluded.kind",
        (int(marker_id), name, kind, ts, ts),
    )
    conn.commit()


def list_map(conn, min_edge_sightings=1):
    markers = [dict(r) for r in conn.execute(
        "SELECT marker_id, name, kind, sightings, first_seen_ts, last_seen_ts "
        "FROM map_markers ORDER BY marker_id"
    )]
    edges = [dict(r) for r in conn.execute(
        "SELECT from_id, to_id, seen_count, last_seen_ts FROM map_edges "
        "WHERE seen_count >= ? AND from_id < to_id ORDER BY from_id, to_id",
        (min_edge_sightings,),
    )]
    return markers, edges


def unnamed_markers(conn):
    return [dict(r) for r in conn.execute(
        "SELECT marker_id, sightings, first_seen_ts FROM map_markers "
        "WHERE name IS NULL OR name = '' ORDER BY first_seen_ts"
    )]


def find_route(conn, start_id, goal_id, min_edge_sightings=1):
    """Shortest marker chain from start to goal, breadth-first.

    BFS, not Dijkstra: every edge is "one marker hop" and we have no distances
    (no encoders, no metric map), so all edges cost the same by construction.
    Fewest hops is also what we actually want — every extra hop is another
    chance to lose a marker.

    Returns [] when there is no known path, which the caller MUST treat as
    "I don't know how to get there" rather than falling back to driving
    blindly at it.
    """
    start_id, goal_id = int(start_id), int(goal_id)
    if start_id == goal_id:
        return [goal_id]

    adj = {}
    for r in conn.execute(
        "SELECT from_id, to_id FROM map_edges WHERE seen_count >= ?",
        (min_edge_sightings,),
    ):
        adj.setdefault(r["from_id"], []).append(r["to_id"])

    from collections import deque
    q, seen = deque([[start_id]]), {start_id}
    while q:
        path = q.popleft()
        for nxt in sorted(adj.get(path[-1], [])):
            if nxt in seen:
                continue
            if nxt == goal_id:
                return path + [nxt]
            seen.add(nxt)
            q.append(path + [nxt])
    return []
