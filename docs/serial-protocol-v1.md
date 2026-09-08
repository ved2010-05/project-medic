# Serial protocol v1 (Pi ↔ ESP32) — FREEZE THIS DAY 1

Transport: USB serial, **115200 baud** (set in one place both sides). Framing: **one JSON object per line**, `\n`-terminated, UTF-8. Both sides **ignore unknown fields and unknown message types** (forward-compatible). Every message has a `"t"` (type). Optional `"id"` for request/ack correlation.

This is a draft to be frozen at Day-0/1 kickoff. Keep total types small; trim anything unused.

## Pi → MCU (commands / goals)

| `t` | Fields | Meaning |
|---|---|---|
| `drive` | `hdg` (deg error, +right), `spd` (0–1) | Steering goal during EN_ROUTE. MCU obeys unless a safety override is active. |
| `stop` | — | Non-emergency halt (hold position). |
| `search` | `dir` (`cw`/`ccw`) | Enter/continue MARKER_SEARCH rotate. |
| `dispense` | `count` (int) | Actuate magazine exactly `count` times. Only sent after verified two-scan auth. |
| `latch` | `open` (bool) | Cold-box latch. Only after verified auth. |
| `teleop` | `cmd` (`fwd/back/left/right/stop`), `spd` | Manual override: the R14 Pi-dead parachute AND the live, disclosed supervised nudge during EN_ROUTE (log every nudge). |
| `ux` | `led` and/or `buzz` (pattern id) | Status indication. |
| `ping` | — | Heartbeat; expects `pong`. |

## MCU → Pi (telemetry / events)

| `t` | Fields | Meaning |
|---|---|---|
| `odom` | `dl`, `dr` (tick deltas) or `x,y,th` | Odometry for coast-between-markers. |
| `rfid` | `uid` (hex), `reader` (`scan1`/`scan2`) | A tag was read. **MCU only reports; Pi/dashboard decides the match.** |
| `obstacle` | `d` (cm), `blocked` (bool) | Ultrasonic state. |
| `estop` | `active` (bool) | Physical E-stop state. |
| `temp` | `c` (float) | Cold-box DS18B20 reading. |
| `state` | `s` (state name) | Current state-machine state. |
| `ack` | `of` (type), `ok` (bool) | Result of a `dispense`/`latch`/etc. |
| `pong` | — | Heartbeat reply. |

## Timing / safety contract
- MCU expects at least one valid frame every 2 s. Silence > 2 s → `SAFEHOLD_COMMS` (stop, hold locks) and keep emitting `state`.
- Safety overrides (`OBSTACLE_HOLD`, `ESTOPPED`, `SAFEHOLD_COMMS`) **ignore** incoming `drive`/`search`/`teleop` motion until cleared. `dispense`/`latch` are refused unless the state allows it.
- `dispense`/`latch` are only ever emitted by the Pi *after* a verified two-scan auth (fail-closed). The MCU trusts the command but still refuses if not in an unlock-eligible state.

## Example session (happy path)
```
PI→MCU  {"t":"drive","hdg":-4,"spd":0.3}
MCU→PI  {"t":"odom","dl":120,"dr":118}
MCU→PI  {"t":"obstacle","d":80,"blocked":false}
PI→MCU  {"t":"stop"}                         // arrived at Ward marker, standoff reached
MCU→PI  {"t":"state","s":"AT_WARD_WAIT_AUTH"}
MCU→PI  {"t":"rfid","uid":"04A1B2C3","reader":"scan1"}   // staff badge
MCU→PI  {"t":"rfid","uid":"04D4E5F6","reader":"scan2"}   // patient tag
PI→MCU  {"t":"dispense","count":2}           // only after dashboard verifies match
MCU→PI  {"t":"ack","of":"dispense","ok":true}
PI→MCU  {"t":"latch","open":true}
MCU→PI  {"t":"ack","of":"latch","ok":true}
```

---

## v1 additive optional fields (backward compatible)

**Nothing above changes.** The protocol is frozen; this section only records *optional* fields both
sides already agreed to ignore-if-unknown. No new message types, no changed semantics. A parser that
knows only the tables above still works against a sender that emits these.

| Message | Optional field | Values | Why it exists |
|---|---|---|---|
| `drive` (Pi→MCU) | `leg` | `outbound` / `return` / `patrol` | Selects `EN_ROUTE` vs `RETURNING` vs `PATROL`. Absent ⇒ `EN_ROUTE`. |
| `stop` (Pi→MCU) | `at` | `pharmacy` / `ward` / `waypoint` | Which wait-state to enter on arrival. Absent ⇒ plain hold in the current state. |
| `state` (MCU→Pi) | `ovr` | override name or `null` | Which override is currently winning, for the dashboard. |
| `state` (MCU→Pi) | `since_ms` | int | Milliseconds in the current state (state-timeout debugging). |
| `ack` (MCU→Pi) | `n` | int | Units *actually* actuated — R5 checks exact count, so "ok" alone is not enough. |
| `ack` (MCU→Pi) | `why` | string | Refusal reason when `ok:false`, written straight to the audit log. |

**Why `stop.at` is needed:** without it the MCU cannot tell "stop, you have arrived at the Ward"
(→ `AT_WARD_WAIT_AUTH`) from "stop, just hold" — the example session above relies on that distinction.

### One RFID reader, two ordered scans
The BOM has a **single RC522**, but `rfid.reader` carries `scan1`/`scan2`. These are **ordered scans
within the open auth window**, not two physical readers:

- First accepted UID while in `AT_WARD_WAIT_AUTH` → `reader:"scan1"` (staff badge).
- Next **different** UID → `reader:"scan2"` (patient wristband).
- The same UID re-read inside a ~1.5 s debounce is ignored.
- The window resets on leaving `AT_WARD_WAIT_AUTH` or on the 30 s auth timeout.

The MCU **only reports UIDs**. It never compares them to anything (invariant §0.4 — the match
decision belongs to the dashboard).
