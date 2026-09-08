"""
POST /api/v1/auth/verify -- the two-scan release check.

This is the endpoint the whole project is a frame for: a staff badge and a
patient wristband must both resolve, both be valid, and the wristband must
belong to the patient the task names, before any payload is released.

Every test here asserts the same shape of property: on ANY doubt, refuse, close
the task, and write an audit event. A release check that fails open is worse
than no check, and a refusal nobody can see afterwards is not much better.

(The RFID reader was removed from the physical build, so on hardware today the
dispense is triggered by arrival rather than by a scan. The endpoint and its
fail-closed structure remain, and remain tested.)
"""

from __future__ import annotations

BASE = {"robot_id": "medic-01", "staff_uid": "STAFF-OK", "patient_uid": "TAG-101"}


def verify(client, task_id=None, **over):
    body = dict(BASE)
    if task_id is not None:
        body["task_id"] = task_id
    body.update(over)
    return client.post("/api/v1/auth/verify", json=body)


def events(conn, kind=None):
    sql = "SELECT * FROM events" + (" WHERE kind = ?" if kind else "")
    return conn.execute(sql, (kind,) if kind else ()).fetchall()


# -- the happy path --------------------------------------------------------

def test_matching_badge_and_wristband_authorize(client, world, conn, app_mod):
    task_id = world(state="awaiting_auth", patient_id="P-101", units=3)
    r = verify(client, task_id)
    assert r.status_code == 200
    body = r.get_json()
    assert body["authorized"] is True
    assert body["reason"] == "match"
    assert body["dispense_count"] == 3, "must release exactly the units dispatched"
    assert app_mod.db.get_task(conn, task_id)["state"] == "dispensing"
    assert len(events(conn, "auth_ok")) == 1


def test_cold_item_opens_the_latch(client, world):
    task_id = world(state="awaiting_auth", cold_item=True)
    assert verify(client, task_id).get_json()["open_latch"] is True


# -- refusals, one per way it can go wrong ---------------------------------

def test_missing_fields_are_refused(client, world):
    task_id = world(state="awaiting_auth")
    for drop in ("robot_id", "staff_uid", "patient_uid"):
        body = dict(BASE, task_id=task_id)
        body.pop(drop)
        r = client.post("/api/v1/auth/verify", json=body)
        assert r.get_json()["reason"] == "malformed_request", "missing " + drop


def test_non_numeric_task_id_is_refused(client, world):
    world(state="awaiting_auth")
    assert verify(client, "not-a-number").get_json()["reason"] == "malformed_request"


def test_unknown_task_is_refused(client, world):
    world(state="awaiting_auth")
    assert verify(client, 9999).get_json()["reason"] == "no_active_task"


def test_task_not_awaiting_auth_is_refused(client, world):
    """Scanning at the wrong moment must not release anything."""
    for state in ("dispatched", "en_route", "arrived", "dispensing", "complete"):
        task_id = world(state=state)
        assert verify(client, task_id).get_json()["reason"] == "wrong_task_state", state


def test_expired_window_refuses_and_closes_the_task(client, world, conn, app_mod):
    """A stale scan is not a valid scan."""
    task_id = world(state="awaiting_auth",
                    expires="2000-01-01T00:00:00.000+00:00")
    r = verify(client, task_id)
    assert r.get_json()["reason"] == "auth_window_expired"
    assert app_mod.db.get_task(conn, task_id)["state"] == "refused"


def test_unknown_staff_uid_refuses_and_closes(client, world, conn, app_mod):
    task_id = world(state="awaiting_auth")
    r = verify(client, task_id, staff_uid="STAFF-NEVER-ISSUED")
    assert r.get_json()["reason"] == "unknown_staff_uid"
    assert app_mod.db.get_task(conn, task_id)["state"] == "refused"


def test_a_real_but_uncleared_badge_is_refused(client, world, conn, app_mod):
    """Distinct from an unknown badge: this one resolves, and still cannot release."""
    task_id = world(state="awaiting_auth")
    r = verify(client, task_id, staff_uid="STAFF-NOTAUTH")
    assert r.get_json()["reason"] == "staff_not_authorized"
    assert app_mod.db.get_task(conn, task_id)["state"] == "refused"


def test_a_deactivated_badge_is_refused(client, world):
    task_id = world(state="awaiting_auth")
    r = verify(client, task_id, staff_uid="STAFF-INACTIVE")
    assert r.get_json()["reason"] == "staff_not_authorized"


def test_unknown_wristband_is_refused(client, world):
    task_id = world(state="awaiting_auth")
    r = verify(client, task_id, patient_uid="TAG-NOBODY")
    assert r.get_json()["reason"] == "unknown_patient_uid"


def test_a_retired_wristband_is_refused(client, world):
    """Right patient, deactivated tag. Treated as unresolvable, not as fine."""
    task_id = world(state="awaiting_auth", patient_id="P-101")
    r = verify(client, task_id, patient_uid="TAG-101-RETIRED")
    assert r.get_json()["reason"] == "unknown_patient_uid"


def test_the_wrong_patient_is_refused(client, world, conn, app_mod):
    """THE headline safety property.

    Valid staff badge, valid wristband, valid task -- but the wristband
    belongs to a different patient than the one dispatched. This is the
    mis-delivery the two-scan check exists to prevent, and the only signal
    that it is wrong is the mismatch itself.
    """
    task_id = world(state="awaiting_auth", patient_id="P-101")
    r = verify(client, task_id, patient_uid="TAG-102")
    body = r.get_json()
    assert body["authorized"] is False
    assert body["reason"] == "patient_tag_mismatch"
    assert body["dispense_count"] == 0, "a refusal must release nothing"
    assert app_mod.db.get_task(conn, task_id)["state"] == "refused"


# -- the guarantees --------------------------------------------------------

def test_a_refusal_never_releases_anything(client, world):
    """Whatever the reason, the payload count is zero and the latch stays shut."""
    cases = [
        {"task_id": 9999},
        {"staff_uid": "STAFF-NEVER-ISSUED"},
        {"staff_uid": "STAFF-NOTAUTH"},
        {"patient_uid": "TAG-NOBODY"},
        {"patient_uid": "TAG-102"},
    ]
    for over in cases:
        task_id = world(state="awaiting_auth", patient_id="P-101", cold_item=True)
        over.setdefault("task_id", task_id)
        body = verify(client, **over).get_json()
        assert body["authorized"] is False, over
        assert body["dispense_count"] == 0, over
        assert body["open_latch"] is False, over


def test_every_decision_is_audited(client, world, conn):
    """No silent refusals: each attempt leaves exactly one event behind."""
    task_id = world(state="awaiting_auth", patient_id="P-101")
    verify(client, task_id, patient_uid="TAG-102")
    refused = events(conn, "auth_refused")
    assert len(refused) == 1
    assert refused[0]["detail"] == "patient_tag_mismatch"
    assert refused[0]["task_id"] == task_id


def test_a_malformed_request_is_still_audited(client, world, conn):
    """Even with no robot_id to attribute it to, the attempt is recorded."""
    world(state="awaiting_auth")
    client.post("/api/v1/auth/verify", json={"staff_uid": "x"})
    refused = events(conn, "auth_refused")
    assert len(refused) == 1
    assert refused[0]["robot_id"] == "unknown"


def test_one_task_one_decision(client, world, conn, app_mod):
    """A second scan against an already-approved task must not release again.

    This is the property the state machine's reopen guard exists to support:
    approval moves the task to `dispensing` synchronously, so any replay of
    the same request lands on a task that is no longer awaiting auth.
    """
    task_id = world(state="awaiting_auth", patient_id="P-101", units=3)
    assert verify(client, task_id).get_json()["authorized"] is True

    second = verify(client, task_id).get_json()
    assert second["authorized"] is False
    assert second["reason"] == "wrong_task_state"
    assert second["dispense_count"] == 0
    assert app_mod.db.get_task(conn, task_id)["state"] == "dispensing"
    assert len(events(conn, "auth_ok")) == 1, "a replay must not re-authorise"
