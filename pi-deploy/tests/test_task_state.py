"""
POST /api/v1/tasks/<id>/state -- the task state machine.

The property under test is "one task, one decision" (docs/http-api-v1.md): a
task that has reached a terminal state is permanently closed, and a task whose
authorisation has already been approved cannot have that decision reopened.
The auth endpoint depends on both being true, so they are tested here directly
rather than inferred from behaviour further downstream.
"""

from __future__ import annotations


def post_state(client, task_id, **body):
    body.setdefault("robot_id", "medic-01")
    return client.post(f"/api/v1/tasks/{task_id}/state", json=body)


# -- request validation ----------------------------------------------------

def test_robot_id_is_required(client, world):
    task_id = world(state="dispatched")
    r = client.post(f"/api/v1/tasks/{task_id}/state", json={"state": "en_route"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_unknown_state_is_rejected(client, world):
    task_id = world(state="dispatched")
    r = post_state(client, task_id, state="teleporting")
    assert r.status_code == 400
    assert "unknown state" in r.get_json()["error"]


def test_unknown_task_is_404(client, world):
    world(state="dispatched")
    r = post_state(client, 9999, state="en_route")
    assert r.status_code == 404


def test_state_change_requires_the_owning_robot(client, world):
    """A second robot must not be able to drive someone else's task."""
    task_id = world(state="dispatched")
    r = post_state(client, task_id, robot_id="medic-02", state="en_route")
    assert r.status_code == 400
    assert "does not match" in r.get_json()["error"]


# -- normal progression ----------------------------------------------------

def test_task_advances_through_the_happy_path(client, world, conn, app_mod):
    task_id = world(state="dispatched")
    for state in ("en_route", "arrived", "awaiting_auth", "dispensing", "complete"):
        r = post_state(client, task_id, state=state)
        assert r.status_code == 200, f"{state}: {r.get_json()}"
        assert r.get_json()["state"] == state
    assert app_mod.db.get_task(conn, task_id)["state"] == "complete"


def test_entering_awaiting_auth_opens_a_bounded_window(client, world, conn, app_mod):
    """The auth window must expire on its own, not wait for someone to close it."""
    task_id = world(state="arrived")
    assert app_mod.db.get_task(conn, task_id)["awaiting_auth_expires_ts"] is None
    post_state(client, task_id, state="awaiting_auth")
    row = app_mod.db.get_task(conn, task_id)
    assert row["awaiting_auth_expires_ts"] is not None
    assert row["awaiting_auth_expires_ts"] > row["updated_ts"]


def test_leaving_awaiting_auth_clears_the_window(client, world, conn, app_mod):
    task_id = world(state="awaiting_auth")
    post_state(client, task_id, state="aborted")
    assert app_mod.db.get_task(conn, task_id)["awaiting_auth_expires_ts"] is None


# -- the guarantees --------------------------------------------------------

def test_a_terminal_task_never_reopens(client, world, conn, app_mod):
    """complete / refused / aborted are final, from every direction."""
    for terminal in ("complete", "refused", "aborted"):
        task_id = world(state=terminal)
        for attempt in ("en_route", "awaiting_auth", "dispensing", "complete"):
            r = post_state(client, task_id, state=attempt)
            assert r.status_code == 409, (
                f"{terminal} -> {attempt} returned {r.status_code}, "
                f"expected 409: a closed task must stay closed"
            )
            assert "terminal" in r.get_json()["error"]
        assert app_mod.db.get_task(conn, task_id)["state"] == terminal


def test_an_approved_task_cannot_reopen_its_auth_window(client, world, conn, app_mod):
    """The reopening loophole.

    task_bridge.py remembers decided task ids only in an in-process set, which
    is lost if that process restarts. Without this guard the MCU could
    re-enter its wait-for-auth state for a task that already had an approved
    verdict, and collect a second pair of scans against it.
    """
    task_id = world(state="dispensing")
    r = post_state(client, task_id, state="awaiting_auth")
    assert r.status_code == 409
    assert "already has an approved auth decision" in r.get_json()["error"]
    assert app_mod.db.get_task(conn, task_id)["state"] == "dispensing"


def test_dispensing_can_still_finish_or_abort(client, world):
    """The reopen guard must block ONE transition, not wedge the task."""
    for onward in ("complete", "aborted", "refused"):
        task_id = world(state="dispensing")
        r = post_state(client, task_id, state=onward)
        assert r.status_code == 200, (
            f"dispensing -> {onward} was blocked; only the reopen path "
            f"back to awaiting_auth should be"
        )
