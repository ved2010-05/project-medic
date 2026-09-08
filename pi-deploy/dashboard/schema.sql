-- schema.sql — "Hospital Central" mock hospital DB (Project MEDIC).
--
-- Loaded by dashboard/db.py via executescript() on every process start
-- (app.py, seed.py). Every statement is IF NOT EXISTS, so running this
-- against an already-initialized database is a safe no-op — there is no
-- separate "first run" flag to get out of sync.
--
-- WAL mode is turned on from Python (db.py connect(), via `PRAGMA
-- journal_mode = WAL`), not here — journal_mode is a per-connection PRAGMA
-- that only "sticks" to the database file once some connection actually
-- issues it, so putting it in this script wouldn't reliably take effect
-- before the schema below is created. Same story for `PRAGMA foreign_keys
-- = ON`, which does NOT persist at all and must be set on every single
-- connection — see db.py connect().
--
-- Foreign keys: used where dashboard/app.py always creates the parent row
-- first (patients before prescriptions/patient_tags/tasks; robots before
-- tasks/config/teleop_queue — see db.py's ensure_robot()). Deliberately
-- OMITTED on events.robot_id, events.task_id and telemetry.robot_id: those
-- two tables are the append-only audit trail and the high-frequency
-- telemetry firehose, and a referential-integrity hiccup (e.g. a stray
-- robot_id nobody has registered yet, or a task_id that raced ahead of an
-- insert) must NEVER be the reason an audit write silently fails. Losing a
-- logged event is worse than a slightly loose schema — dashboard/DESIGN.md
-- calls the audit log "the product."

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Patients + prescriptions ("mock EHR")
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS patients (
    patient_id  TEXT PRIMARY KEY,   -- e.g. "P-102"
    name        TEXT NOT NULL,
    room        TEXT
);

-- One row per standing order. Candy-only stand-ins (ARCHITECTURE.md S0.2 —
-- never a real drug name, anywhere, ever). GET /api/v1/patients shows the
-- most recent row per patient as that patient's "prescription" string.
CREATE TABLE IF NOT EXISTS prescriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id  TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    description TEXT NOT NULL,   -- e.g. "2x round candy 'tablet' stand-in + 1 chilled bead pouch"
    units       INTEGER NOT NULL,
    cold_item   INTEGER NOT NULL DEFAULT 0,  -- 0/1 boolean
    created_ts  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prescriptions_patient ON prescriptions(patient_id);

-- ---------------------------------------------------------------------
-- Auth roster — RFID badge/wristband UIDs (ARCHITECTURE.md S0.4 two-scan)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staff (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL UNIQUE,   -- MIFARE badge UID, hex string
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    -- authorized=0 models a badge that resolves (it's a real row) but is not
    -- cleared to release meds -- reason "staff_not_authorized", distinct
    -- from a UID nobody has ever heard of ("unknown_staff_uid").
    authorized  INTEGER NOT NULL DEFAULT 1,
    -- active=0 models a lost/deactivated badge. Treated the same as
    -- "not authorized" by the auth check (both fail closed either way).
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS patient_tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL UNIQUE,   -- MIFARE wristband UID
    patient_id  TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_patient_tags_patient ON patient_tags(patient_id);

-- ---------------------------------------------------------------------
-- Robots (fleet). One row per robot_id that has ever been seen by any
-- endpoint (db.ensure_robot()). last_state/last_ovr/last_temp_c are a
-- cheap denormalized "latest telemetry snapshot" so GET /api/v1/fleet is a
-- single indexed lookup instead of a correlated MAX(ts) subquery per robot
-- on every browser poll (dashboard/DESIGN.md: "keep it light").
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS robots (
    robot_id       TEXT PRIMARY KEY,
    created_ts     TEXT NOT NULL,
    last_seen_ts   TEXT,   -- NULL until the first telemetry POST arrives
    last_state     TEXT,   -- last MCU state name reported, e.g. "EN_ROUTE"
    last_ovr       TEXT,   -- last override reported, or NULL if none active
    last_temp_c    REAL
);

-- ---------------------------------------------------------------------
-- Tasks — the delivery state machine. States (docs/http-api-v1.md):
--   dispatched -> en_route -> arrived -> awaiting_auth -> dispensing -> complete
--                                                       \-> refused
--                                                \-> aborted (e.g. dispense miscount)
-- Terminal states (complete/refused/aborted) are enforced read-only by
-- db.py/app.py, not by SQL here -- see app.py's task-state transition
-- guard for why ("one task, one decision", docs/http-api-v1.md).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id                  TEXT NOT NULL REFERENCES robots(robot_id),
    patient_id                TEXT NOT NULL REFERENCES patients(patient_id),
    units                     INTEGER NOT NULL,
    cold_item                 INTEGER NOT NULL DEFAULT 0,
    destination_marker        INTEGER NOT NULL,
    route                     TEXT NOT NULL,     -- JSON array of marker IDs, e.g. "[15, 20]"
    payload_desc              TEXT NOT NULL,      -- candy-only, see prescriptions.description
    state                     TEXT NOT NULL DEFAULT 'dispatched',
    detail                    TEXT,
    -- Set when state -> "awaiting_auth" (docs/design-doc-v0.3.md S4.3: 30 s
    -- window). auth/verify refuses with "auth_window_expired" once this has
    -- passed. Belt-and-suspenders alongside task_bridge.py's own client-side
    -- 30 s timer -- this one still protects the DB even if that process dies.
    awaiting_auth_expires_ts  TEXT,
    created_ts                TEXT NOT NULL,
    updated_ts                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_robot_state ON tasks(robot_id, state);

-- ---------------------------------------------------------------------
-- Events — the APPEND-ONLY audit log. db.py exposes append_event() and
-- list_events() and NOTHING that updates or deletes a row here. See the
-- big comment on append_event() in db.py: "never mutate a logged event"
-- (dashboard/DESIGN.md) is enforced by simply not writing that code, not
-- by a database trigger -- keep it that way.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,      -- canonical vocabulary, docs/http-api-v1.md
    severity     TEXT NOT NULL,      -- info | warn | red
    detail       TEXT,
    task_id      INTEGER,            -- soft reference to tasks(id); see note above
    ts           TEXT NOT NULL,      -- client-claimed time (or our fallback if absent)
    received_ts  TEXT NOT NULL       -- always OUR clock -- a wrong Pi clock can never
                                      -- punch a hole in the audit trail (http-api-v1.md)
);
-- events(id) needs no explicit index: INTEGER PRIMARY KEY on a rowid table
-- IS the table's own index (SQLite aliases it to the rowid) -- it's free.
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_robot_id ON events(robot_id);

-- ---------------------------------------------------------------------
-- Telemetry — one row per POST /api/v1/telemetry item. Most fields are
-- optional per the wire format (a single MCU message like "obstacle" only
-- ever fills in a couple of columns), so nearly everything here is
-- nullable on purpose; see db.py's touch_robot() for how a partial row
-- still updates the right "last known" fields on robots without
-- clobbering the others with NULL.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id    TEXT NOT NULL,
    ts          TEXT NOT NULL,
    state       TEXT,
    ovr         TEXT,
    obstacle_cm REAL,
    blocked     INTEGER,
    estop       INTEGER,
    temp_c      REAL,
    -- 1 if temp_c was outside the safe band (app.py's TEMP_*_C constants)
    -- at the moment this row was logged. Drives GET /api/v1/temp's
    -- "excursions" list and R6's gap/excursion check.
    excursion   INTEGER NOT NULL DEFAULT 0,
    received_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_robot_ts ON telemetry(robot_id, ts);

-- ---------------------------------------------------------------------
-- Per-robot tunables, GET/POST /api/v1/config. The "nav" sub-object in the
-- JSON API response is these four nav_* columns nested by app.py -- kept
-- flat here because SQLite has no native nested-object column type.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config (
    robot_id           TEXT PRIMARY KEY REFERENCES robots(robot_id),
    sound_threshold    REAL NOT NULL DEFAULT 0.18,
    sound_min_ms       INTEGER NOT NULL DEFAULT 120,
    sound_refractory_s REAL NOT NULL DEFAULT 5.0,
    -- Full speed by request. This row OVERRIDES nav.py's MAX_SPD constant
    -- (nav re-polls this table every few seconds), so changing the constant
    -- alone has no effect on a robot whose config row already exists.
    nav_max_spd        REAL NOT NULL DEFAULT 1.0,
    nav_target_w_px    INTEGER NOT NULL DEFAULT 150,
    nav_coast_s        REAL NOT NULL DEFAULT 1.5,
    nav_search_s       REAL NOT NULL DEFAULT 8.0,
    teleop_enabled     INTEGER NOT NULL DEFAULT 1,
    config_rev         INTEGER NOT NULL DEFAULT 1,
    updated_ts         TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Teleop mailbox — one pending command per robot. GET /api/v1/teleop just
-- reads this row; POST /api/v1/teleop overwrites it and bumps seq. There is
-- deliberately no queue/history here (docs/http-api-v1.md: the client acts
-- only on a NEW seq, so "latest command wins" is the whole contract) --
-- the audit trail (events, kind="teleop_nudge") is what keeps the history,
-- written by task_bridge.py when it actually hands the command to the MCU.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS teleop_queue (
    robot_id    TEXT PRIMARY KEY REFERENCES robots(robot_id),
    cmd         TEXT,      -- fwd | back | left | right | stop | NULL (nothing queued)
    spd         REAL,
    seq         INTEGER NOT NULL DEFAULT 0,
    updated_ts  TEXT
);

-- ---------------------------------------------------------------------
-- LEARNED MARKER MAP (teach mode)
--
-- A TOPOLOGICAL map, deliberately not a metric one: nodes are markers,
-- edges mean "these two were visible in the same camera frame". There are
-- no coordinates, no occupancy grid and no pose estimation anywhere in
-- here -- that would be SLAM, which ARCHITECTURE.md S0.3 rules out. This is
-- the same "home on the next marker you can see" idea nav.py already uses,
-- just written down so the robot can plan a chain of them instead of
-- following one hardcoded list.
--
-- Populated by driving the robot around once under teleop with
-- `medic.nav --teach`. A human names each marker as it appears, which is
-- also what keeps "the robot never decides" true: it observes and asks,
-- it does not go exploring on its own.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS map_markers (
    marker_id     INTEGER PRIMARY KEY,   -- the ArUco ID itself
    name          TEXT,                  -- NULL until a human names it
    kind          TEXT NOT NULL DEFAULT 'waypoint',  -- 'station' | 'waypoint'
    sightings     INTEGER NOT NULL DEFAULT 0,
    first_seen_ts TEXT NOT NULL,
    last_seen_ts  TEXT NOT NULL
);

-- Undirected in reality, stored BOTH ways so path-finding is a plain
-- symmetric lookup with no special cases.
--
-- CAVEAT worth knowing: co-visibility means "I could see both", which is a
-- good proxy for "I can drive between them" but not a guarantee -- you can
-- see a marker across a railing you cannot drive through. That is why
-- edges carry seen_count and app.py only trusts an edge after it has been
-- observed several times from a moving robot.
CREATE TABLE IF NOT EXISTS map_edges (
    from_id      INTEGER NOT NULL,
    to_id        INTEGER NOT NULL,
    seen_count   INTEGER NOT NULL DEFAULT 0,
    last_seen_ts TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id)
);
CREATE INDEX IF NOT EXISTS idx_map_edges_from ON map_edges(from_id);
