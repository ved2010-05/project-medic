"""seed.py — fake hospital data for the "Hospital Central" mock DB.

CANDY ONLY (ARCHITECTURE.md §0.2). There is not a single real medication name in
this file and there must never be one. Every payload is a candy/bead stand-in,
and the `payload_desc` strings below are what end up on the dashboard, in the
audit log, and on the demo projector — so they are written to be read out loud
by a judge without anyone thinking we are handling real drugs.

The roster below is built around the §2 demo contract, not made up at random:

  * P-102 Rosa Almeida — 2 units + 1 cold item. This is THE demo patient from
    ARCHITECTURE.md §2 step 2 ("Patient P-102, 2x pills + 1 cold item"), so a
    freshly seeded database can run the scripted demo with zero setup.
  * P-103 Priya Raghunathan — in the room NEXT DOOR to Rosa. Her wristband is
    the deliberate WRONG-PATIENT refusal (§2 step 4, "demoed on purpose"). A
    plausible neighbouring-room mix-up is a far better story than a random tag.
  * Two badges that FAIL for different reasons, because "refused" is not one
    thing and the audit log should prove we can tell them apart:
      - Kev Brannigan  : a real, known badge that is NOT cleared to release
                         -> reason "staff_not_authorized"
      - Elena Marchetti: a deactivated/lost badge      -> also refused
    Anything not in these tables at all -> "unknown_staff_uid". Keep one spare
    unregistered fob in the box to demo that third case.

Idempotent: safe to run repeatedly. Use --reset to wipe and start clean.

BENCH TEST:
    python -m dashboard.seed --reset --summary
  Then check the demo path resolves end to end:
    curl "http://127.0.0.1:5000/api/v1/patients"
    curl -X POST http://127.0.0.1:5000/api/v1/tasks \
         -H 'Content-Type: application/json' \
         -d '{"patient_id":"P-102","units":2,"cold_item":true,
              "destination_marker":20,"robot_id":"medic-01"}'
  Then the two-scan check — the RIGHT tag must pass and the NEIGHBOUR must not:
    curl -X POST http://127.0.0.1:5000/api/v1/auth/verify -H 'Content-Type: application/json' \
         -d '{"robot_id":"medic-01","task_id":1,"staff_uid":"04A1B2C3","patient_uid":"04D4E5F6"}'
      -> authorized: true
    curl -X POST http://127.0.0.1:5000/api/v1/auth/verify -H 'Content-Type: application/json' \
         -d '{"robot_id":"medic-01","task_id":1,"staff_uid":"04A1B2C3","patient_uid":"04F60718"}'
      -> authorized: false, reason "patient_tag_mismatch", and a RED event logged
"""

import argparse
import os
import sys
from datetime import datetime, timezone

# Works both as `python -m dashboard.seed` (from pi-deploy/) and as
# `python dashboard/seed.py`. The first form has a package context and the
# relative import succeeds; the second does not, and dashboard/ is already on
# sys.path because it is the script's own directory.
try:
    from . import db
except ImportError:  # pragma: no cover - depends only on how it was invoked
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db


def now_ts():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# The roster. UIDs are 4-byte MIFARE-style hex, uppercase, exactly as the RC522
# reports them and exactly as medic/task_bridge.py forwards them.
# 04A1B2C3 (staff) and 04D4E5F6 (Rosa's wristband) are the same UIDs used in the
# worked examples in docs/serial-protocol-v1.md and docs/http-api-v1.md — keep
# them in sync so the docs stay copy-pasteable.
# ---------------------------------------------------------------------------
PATIENTS = [
    ("P-101", "Marcus Whitfield", "3A"),
    ("P-102", "Rosa Almeida", "4B"),
    ("P-103", "Priya Raghunathan", "4C"),
    ("P-104", "Tobias Lindqvist", "7A"),
    ("P-105", "Amara Okonkwo", "7B"),
]

# (patient_id, description, units, cold_item)
PRESCRIPTIONS = [
    ("P-101", "3x round candy 'tablet' stand-in", 3, 0),
    ("P-102", "2x round candy 'tablet' stand-in + 1 chilled bead pouch", 2, 1),
    ("P-103", "1x round candy 'tablet' stand-in", 1, 0),
    ("P-104", "4x round candy 'tablet' stand-in", 4, 0),
    ("P-105", "2x round candy 'tablet' stand-in + 1 chilled bead pouch", 2, 1),
]

# (uid, name, role, authorized, active)
STAFF = [
    ("04A1B2C3", "Dr. Hannah Voss", "physician", 1, 1),
    ("04B2C3D4", "Nurse Iwu Chen", "nurse", 1, 1),
    ("04C3D4E5", "Nurse Sam Okafor", "nurse", 1, 1),
    # Known badge, deliberately NOT cleared to release -> "staff_not_authorized".
    ("049F8E7D", "Kev Brannigan", "orderly", 0, 1),
    # Deactivated / reported-lost badge -> refused for a different reason.
    ("0455AA11", "Dr. Elena Marchetti", "physician", 1, 0),
]

# (uid, patient_id, active)
PATIENT_TAGS = [
    ("04D4E5F6", "P-102", 1),  # Rosa — the CORRECT tag for the demo task
    ("04E5F607", "P-101", 1),
    ("04F60718", "P-103", 1),  # Priya — the WRONG-PATIENT refusal demo
    ("04071829", "P-104", 1),
    ("0418293A", "P-105", 1),
]

DEFAULT_ROBOT = "medic-01"

TABLES = (
    "teleop_queue", "config", "telemetry", "events", "tasks",
    "patient_tags", "staff", "prescriptions", "patients", "robots",
)


def reset(conn):
    """Wipe every table. Ordered child-before-parent so foreign keys hold."""
    for table in TABLES:
        conn.execute("DELETE FROM %s" % table)
    # Reset AUTOINCREMENT counters so a fresh seed starts at task_id 1 again,
    # which keeps the copy-pasteable curl examples above honest.
    conn.execute(
        "DELETE FROM sqlite_sequence WHERE name IN (%s)"
        % ",".join("?" for _ in TABLES),
        TABLES,
    )
    conn.commit()


def seed(conn, robot_ids):
    ts = now_ts()

    conn.executemany(
        "INSERT OR IGNORE INTO patients (patient_id, name, room) VALUES (?,?,?)",
        PATIENTS,
    )

    # Prescriptions have an autoincrement id and no natural key, so INSERT OR
    # IGNORE would happily create duplicates on a re-run. Only insert the ones
    # that are not already present for that patient.
    for patient_id, description, units, cold in PRESCRIPTIONS:
        row = conn.execute(
            "SELECT 1 FROM prescriptions WHERE patient_id=? AND description=?",
            (patient_id, description),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO prescriptions "
                "(patient_id, description, units, cold_item, created_ts) "
                "VALUES (?,?,?,?,?)",
                (patient_id, description, units, cold, ts),
            )

    conn.executemany(
        "INSERT OR IGNORE INTO staff (uid, name, role, authorized, active) "
        "VALUES (?,?,?,?,?)",
        STAFF,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO patient_tags (uid, patient_id, active) VALUES (?,?,?)",
        PATIENT_TAGS,
    )
    conn.commit()

    # ensure_robot() also creates the config row, so the config endpoint and the
    # fleet view work immediately on a fresh database.
    for robot_id in robot_ids:
        db.ensure_robot(conn, robot_id, ts)

    conn.commit()


def summarize(conn):
    print("\nSeeded database: %s" % db.DB_PATH)
    for table in ("patients", "prescriptions", "staff", "patient_tags", "robots"):
        n = conn.execute("SELECT COUNT(*) AS n FROM %s" % table).fetchone()["n"]
        print("  %-14s %d" % (table, n))

    # Plain ASCII on purpose: this prints on a Windows console during export as
    # well as on the Pi, and a stray non-ASCII char turns into mojibake there.
    print("\nDemo cheat-sheet (ARCHITECTURE.md section 2, the demo contract):")
    print("  dispatch to      P-102 Rosa Almeida, room 4B - 2 units + 1 cold item")
    print("  staff badge      04A1B2C3  Dr. Hannah Voss      -> authorized")
    print("  CORRECT tag      04D4E5F6  Rosa Almeida  (P-102) -> auth_ok")
    print("  WRONG tag        04F60718  Priya Raghunathan (P-103)")
    print("                   -> auth_refused / patient_tag_mismatch  (RED event)")
    print("  uncleared badge  049F8E7D  Kev Brannigan -> staff_not_authorized")
    print("  any other fob              -> unknown_staff_uid / unknown_patient_uid")
    print("\n  Nothing here is a real medication. Candy stand-ins only.\n")


def main():
    ap = argparse.ArgumentParser(description="Seed the MEDIC mock hospital DB")
    ap.add_argument("--reset", action="store_true",
                    help="wipe every table first (destroys the audit log too)")
    ap.add_argument("--robot-id", default=DEFAULT_ROBOT,
                    help="robot to register (default: %s)" % DEFAULT_ROBOT)
    ap.add_argument("--fleet-demo", action="store_true",
                    help="also register medic-02 so you can see the fleet view "
                         "render more than one robot (it never hardcodes 1)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the --reset confirmation (install.sh already asked)")
    ap.add_argument("--summary", action="store_true", help="print a demo cheat-sheet")
    ap.add_argument("--db", help="override the database path")
    args = ap.parse_args()

    if args.db:
        db.DB_PATH = args.db

    conn = db.connect()
    try:
        db.init_db(conn)

        if args.reset:
            # The audit log is the product, so deleting it is never silent.
            existing = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
            if existing and not args.yes and sys.stdin.isatty():
                answer = input(
                    "--reset will DELETE %d logged audit events at %s. Type "
                    "'yes' to continue: " % (existing, db.DB_PATH)
                )
                if answer.strip().lower() != "yes":
                    print("aborted, nothing changed")
                    return 1
            reset(conn)
            print("reset: all tables cleared")

        robots = [args.robot_id]
        if args.fleet_demo and "medic-02" not in robots:
            robots.append("medic-02")

        seed(conn, robots)
        print("seeded %d patients, %d staff badges, %d wristband tags, robot(s): %s"
              % (len(PATIENTS), len(STAFF), len(PATIENT_TAGS), ", ".join(robots)))

        if args.summary:
            summarize(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
