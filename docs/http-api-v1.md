# HTTP API v1 — dashboard ↔ Pi processes

**Deployment for this build: the dashboard runs ON the Pi.** `MEDIC_CENTRAL_URL`
therefore defaults to `http://127.0.0.1:5000`. Nothing below assumes that — point it
at a laptop instead and it works unchanged (invariant §0.7: our own hotspot, never
venue Wi-Fi).

Transport: HTTP/JSON, polling (open-decision #9 default; no WebSocket). Every request
carries `robot_id`. Every response is JSON. The fleet view renders N robots — never
hardcode 1.

**Blip tolerance is mandatory.** Every client call has a short timeout, retries
quietly, and *never raises out of the caller*. A dashboard outage must never crash a
Pi script and must never unlock anything.

---

## Fail-closed rules (invariant §0.4, in API terms)

These are not suggestions. They are the contract.

1. `POST /api/v1/auth/verify` is the **only** endpoint that can authorize a release.
2. The robot treats **every** non-2xx, timeout, connection error, malformed body, or
   missing field as `authorized: false`. There is no default-allow path.
3. The dashboard writes the audit event **before** it returns the response, on both
   outcomes. A refusal that wasn't logged is a bug.
4. A refused verification is never retried into an approval. One task, one decision.

---

## Pi-facing endpoints

### `GET /api/v1/tasks/active?robot_id=medic-01`
The task this robot should currently be executing, or `null`.

```json
{
  "task_id": 7,
  "robot_id": "medic-01",
  "patient_id": "P-102",
  "patient_name": "Rosa Almeida",
  "destination_marker": 20,
  "route": [15, 20],
  "units": 2,
  "cold_item": true,
  "payload_desc": "2x round candy 'tablet' stand-in + 1 chilled bead pouch",
  "state": "dispatched",
  "expected_staff_uid": "04A1B2C3",
  "expected_patient_uid": "04D4E5F6",
  "created_ts": "2026-08-02T10:15:03Z"
}
```
`expected_*_uid` are returned for display and logging only. **The robot never compares
them itself** — it posts what it scanned to `/auth/verify` and obeys the answer (D3).

No active task → `{"task": null}` with HTTP 200.

### `POST /api/v1/tasks/<task_id>/state`
```json
{"robot_id": "medic-01", "state": "en_route", "detail": "leg 1 of 2"}
```
Valid states: `dispatched`, `en_route`, `arrived`, `awaiting_auth`, `dispensing`,
`complete`, `refused`, `aborted`. Returns `{"ok": true, "state": "en_route"}`.

### `POST /api/v1/events`
Append one event, or a batch. Append-only — there is no update or delete route.

```json
{"robot_id": "medic-01", "kind": "marker_lost", "severity": "warn",
 "detail": "marker 20 not reacquired", "task_id": 7,
 "ts": "2026-08-02T10:16:41Z"}
```
Batch form: `{"events": [ {...}, {...} ]}`. Returns `{"ok": true, "ids": [42]}`.
`ts` is optional; the server stamps it if absent. The server *also* records its own
receive time, so a wrong Pi clock can never create a gap in the audit trail.

### `POST /api/v1/telemetry`
```json
{"robot_id": "medic-01", "ts": "...", "state": "EN_ROUTE", "ovr": null,
 "obstacle_cm": 82.0, "blocked": false, "estop": false, "temp_c": 6.4}
```
All fields except `robot_id` optional. Batch form: `{"telemetry": [...]}`.
Temperature is stored to its own series for the chart and the R6 gap check.

### `POST /api/v1/auth/verify` — the critical one
```json
{"robot_id": "medic-01", "task_id": 7,
 "staff_uid": "04A1B2C3", "patient_uid": "04D4E5F6"}
```
Response:
```json
{"authorized": true, "reason": "match", "dispense_count": 2,
 "open_latch": true, "event_id": 88}
```
Refusal:
```json
{"authorized": false, "reason": "patient_tag_mismatch",
 "dispense_count": 0, "open_latch": false, "event_id": 89}
```
`reason` is one of: `match`, `unknown_staff_uid`, `unknown_patient_uid`,
`staff_not_authorized`, `patient_tag_mismatch`, `no_active_task`, `wrong_task_state`,
`auth_window_expired`, `malformed_request`.

Authorize **only** when both UIDs resolve AND the patient tag belongs to this task's
patient AND the task is in a releasable state. Anything else refuses. Never partial-match.

### `GET /api/v1/config?robot_id=medic-01`
```json
{"sound_threshold": 0.18, "sound_min_ms": 120, "sound_refractory_s": 5.0,
 "nav": {"max_spd": 0.45, "target_w_px": 150, "coast_s": 1.5, "search_s": 8.0},
 "teleop_enabled": true, "config_rev": 12}
```
`config_rev` increments on every change so clients can cheaply detect updates.

### `GET /api/v1/teleop?robot_id=medic-01`
```json
{"cmd": "fwd", "spd": 0.3, "seq": 41}
```
`{"cmd": null}` when nothing is queued. Clients track `seq` and act only on a new one,
so a slow poll can't replay a stale nudge. **Every command handed out is logged as
`teleop_nudge`** (pi-deploy/dashboard/DESIGN.md requires it — this channel is the R14 parachute
*and* the live supervised nudge).

---

## Browser-facing

Pages: `/` (fleet), `/dispatch`, `/audit`, `/temp`, `/teleop`.
Templates: `base.html`, `fleet.html`, `dispatch.html`, `audit.html`, `temp.html`,
`teleop.html`. The sound-threshold slider lives on `/teleop`.

JSON the page JS polls:

| Method | Path | Returns |
|---|---|---|
| GET | `/api/v1/fleet` | `{"robots":[{robot_id,state,ovr,last_seen,online,temp_c,task_id}]}` |
| GET | `/api/v1/events?limit=&kind=&severity=&robot_id=&since_id=` | `{"events":[...],"max_id":N}` |
| GET | `/api/v1/temp?robot_id=&minutes=60` | `{"series":[{ts,c}],"excursions":[...],"min_c":..,"max_c":..}` |
| GET | `/api/v1/patients` | `{"patients":[{patient_id,name,room,prescription}]}` |
| GET | `/api/v1/tasks?limit=` | `{"tasks":[...]}` |
| POST | `/api/v1/tasks` | dispatch: `{patient_id,units,cold_item,destination_marker,robot_id}` → `{task_id}` |
| POST | `/api/v1/teleop` | `{robot_id,cmd,spd}` → `{ok,seq}` |
| POST | `/api/v1/config` | `{robot_id,sound_threshold,...}` → `{ok,config_rev}` |

`since_id` lets the audit page tail efficiently instead of refetching everything.

---

## Event kinds (canonical vocabulary)

Firmware, Pi and dashboard all use these exact strings. Severity drives the colour.

| kind | severity | emitted by |
|---|---|---|
| `robot_online` / `robot_offline` | info / warn | task_bridge |
| `dispatch` | info | dashboard |
| `depart` | info | task_bridge |
| `marker_seen` | info | nav |
| `marker_lost` | **red** | nav |
| `station_mismatch` | **red** | nav (R13) |
| `arrive` | info | nav |
| `scan_staff` / `scan_patient` | info | task_bridge |
| `auth_ok` | info | dashboard |
| `auth_refused` | **red** | dashboard |
| `auth_timeout` | **red** | task_bridge |
| `dispense_ok` | info | task_bridge |
| `dispense_fail` | **red** | task_bridge |
| `latch_open` / `latch_close` | info | task_bridge |
| `temp_reading` | info | task_bridge |
| `temp_excursion` | warn | dashboard |
| `sound_alert` | warn | ears |
| `obstacle_hold` | warn | task_bridge (relayed from MCU) |
| `estop` | **red** | task_bridge |
| `safehold_comms` | **red** | task_bridge |
| `teleop_nudge` | warn | dashboard |
| `task_complete` | info | task_bridge |

Severity is `info` | `warn` | `red`. Red events must be visually unmissable — the
wrong-patient refusal is demoed on purpose.
