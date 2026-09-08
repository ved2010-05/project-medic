"""
Fixtures for the dashboard API tests.

Two things make this fiddly enough to explain:

1. `dashboard/db.py` reads MEDIC_DASHBOARD_DB_PATH at IMPORT time, so the
   environment has to be set before the module is first imported. Each test
   therefore drops the modules from sys.modules and re-imports them against a
   fresh temporary database. That is slower than sharing one, and it buys
   complete isolation: no test can see another's rows, and no ordering
   dependency can hide here.

2. `pi-deploy/` is the import root -- app.py does `from dashboard import db`
   and `from medic.common import now_ts` -- so it goes on sys.path, not the
   dashboard directory.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent      # pi-deploy/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app_mod(tmp_path, monkeypatch):
    """The dashboard app, bound to a throwaway database."""
    monkeypatch.setenv("MEDIC_DASHBOARD_DB_PATH", str(tmp_path / "test.db"))
    # Drop the PACKAGE too, not just the submodules. `dashboard` keeps a `db`
    # attribute pointing at the already-imported module, so clearing only
    # sys.modules["dashboard.db"] leaves `from dashboard import db` handing
    # back the stale one -- still bound to the previous test's database.
    for name in [n for n in list(sys.modules)
                 if n == "dashboard" or n.startswith("dashboard.")]:
        sys.modules.pop(name, None)
    from dashboard import app as app_module
    app_module.app.config.update(TESTING=True)
    return app_module


@pytest.fixture()
def client(app_mod):
    with app_mod.app.test_client() as c:
        yield c


@pytest.fixture()
def conn(app_mod):
    """A direct connection, for arranging state and asserting on it."""
    c = app_mod.db.connect()
    yield c
    c.close()


@pytest.fixture()
def world(conn, app_mod):
    """A populated ward: one robot, one patient with a wristband, and staff.

    Deliberately includes the awkward rows as well as the happy ones -- a
    badge that resolves but is not cleared to release, a deactivated badge,
    and a wristband belonging to somebody else -- because those are the cases
    the auth endpoint exists to refuse.
    """
    from medic.common import now_ts
    ts = now_ts()
    db = app_mod.db

    db.ensure_robot(conn, "medic-01", ts)

    conn.executemany(
        "INSERT INTO patients (patient_id, name, room) VALUES (?, ?, ?)",
        [("P-101", "Patient One", "4A"), ("P-102", "Patient Two", "4B")],
    )
    conn.executemany(
        "INSERT INTO staff (uid, name, role, authorized, active) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("STAFF-OK",        "Nurse Cleared",     "nurse",   1, 1),
            ("STAFF-NOTAUTH",   "Porter Uncleared",  "porter",  0, 1),
            ("STAFF-INACTIVE",  "Nurse Departed",    "nurse",   1, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO patient_tags (uid, patient_id, active) VALUES (?, ?, ?)",
        [
            ("TAG-101",          "P-101", 1),
            ("TAG-102",          "P-102", 1),   # a real tag, wrong patient
            ("TAG-101-RETIRED",  "P-101", 0),   # right patient, dead tag
        ],
    )
    conn.commit()

    def make_task(state="awaiting_auth", patient_id="P-101", units=3,
                  cold_item=False, expires=None):
        task_id = db.create_task(
            conn, robot_id="medic-01", patient_id=patient_id, units=units,
            cold_item=cold_item, destination_marker=20, route=[10, 15, 20],
            payload_desc="candy", ts=ts,
        )
        if state != "dispatched":
            exp = expires
            if state == "awaiting_auth" and expires is None:
                exp = app_mod._auth_window_expiry(now_ts())
            db.set_task_state(conn, task_id, state, "", exp, now_ts())
        return task_id

    return make_task
