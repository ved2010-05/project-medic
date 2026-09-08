# Safety — read before touching batteries or writing safety-adjacent code

Safety here is both physical (people, the lab) and design (the demo's credibility). The invariants in `ARCHITECTURE.md` §0 are non-negotiable.

## LiPo batteries — the one risk that can end the project (and burn the lab)
- **Named battery officer** owns every cell. No one else charges.
- Charge only in a fireproof LiPo bag, on a proper balance charger, **never unattended**.
- Storage-charge (≈ 3.8 V/cell) overnight; never leave fully charged or fully drained.
- Inspect for puffing/damage before each session; a puffed cell is retired, not "one more run."
- Correct polarity, fused main line, no shorting the leads while wiring.

## E-stop
- A physical switch cuts **motor power** directly. It works even if all software is hung. Logic may stay alive so the robot can report `estop:true`.
- Mounted where a bystander can reach it. Test R9 (< 0.5 s halt, payload stays locked) as a real, repeated test.

## Reflex safety (firmware, MCU only)
- Obstacle-stop (ultrasonic threshold), E-stop sense, and comms-loss safe-hold live on the MCU and **override any Pi command**. Never relocate them to the Pi.
- Watchdog: Pi serial silent > 2 s → stop + hold locks.
- The magazine/latch only actuate on an explicit command issued **after** a verified two-scan auth (fail-closed).

## Payload
- **Candy only. Never real medication.** No real drugs in code, docs, labels, or photos — including "just for the demo photo." This is a hard line.

## Movement
- Slow is safe and reliable. A slow robot that never fails beats a fast one that sometimes does. Keep speeds low, especially near people.

## Network trust model

State this plainly, because the code does not enforce it and a reader who
assumes otherwise will draw the wrong conclusion from the two-scan auth check.

**The HTTP API has no authentication of any kind.** Not a password, not a
token, not a session. Every endpoint in `dashboard/app.py` is open to anything
that can reach port 5000, and the dashboard binds `0.0.0.0` by default
(`MEDIC_DASHBOARD_HOST`), so that is every host on the same network.

Concretely, anyone on the LAN can:

| Endpoint | What an unauthenticated caller can do |
|---|---|
| `POST /api/v1/teleop` | Drive the robot. Forward, reverse, turn, at a speed they choose. |
| `POST /api/v1/tasks` | Dispatch a delivery to any patient. |
| `POST /api/v1/tasks/<id>/state` | Move a task through its states, including to `dispensing`. |
| `POST /api/v1/auth/verify` | Submit badge and wristband UIDs. Correct ones release the payload. |
| `POST /api/v1/events` | Write anything into the audit trail. |
| `GET /api/v1/camera.jpg` | Watch the camera. |

### What is actually being trusted

The security boundary is **the physical network and the room**, not the
software. The design assumes an isolated lab Wi-Fi network, a supervised
demonstration, and that everyone who can reach the robot could also simply walk
over and pick it up. Under those assumptions an unauthenticated API is a
reasonable trade: it removed an entire class of "why is it 401-ing" debugging
from a short build, and cost nothing that mattered under those assumptions.

That assumption is doing real work, so it is worth being explicit about where
it fails. On a shared or hostile network this system offers no defence at all.

### What the two-scan check is and is not

`POST /api/v1/auth/verify` is an **interlock, not an access control.** It
answers "is this the right patient for this task?" It does not answer "is the
caller allowed to ask?" A badge UID is a plain identifier read off a MIFARE
card, transmitted in clear, and trivially replayable by anyone who can watch
the traffic or guess a UID.

So the check defends against the failure it was built for — a payload reaching
the wrong patient through mistake or mix-up — and not against an attacker. That
distinction is the entire difference between this and a real system, and
conflating them would be the most misleading claim in the project.

The audit trail has the same shape: it is a **record**, not evidence. It is
append-only by convention in `db.py`, on an unauthenticated endpoint, in a
SQLite file with no signing. It reliably tells you what the robot did. It could
not establish what a person did, to anyone who was not already inclined to
believe it.

### The one thing the network cannot override

Compromising the Pi does **not** give an attacker the robot's brakes. Every
safety reflex — obstacle stop, comms watchdog, state timeouts, E-stop — lives on
the ESP32, and the obstacle switch is compile-time in `config.h` rather than a
serial command precisely so that no message from the Pi, malicious or
malformed, can disable it (see "Reflex safety" above). The worst a network
attacker achieves is driving the robot around at up to the firmware's own speed
cap, which a physical E-stop ends.

That is not a security control. It is a blast radius, and it is bounded because
the safety architecture was chosen for a different reason and happens to hold
here too.

### What would have to change before this went anywhere real

Listed so the gap is legible, not as a roadmap:

- Bind to `127.0.0.1` and put the dashboard behind a reverse proxy with TLS
  and real authentication; `MEDIC_DASHBOARD_HOST` already exists for this.
- Authenticate the robot to the server and the server to the robot, so a task
  cannot be dispatched or a state changed by an arbitrary host.
- Treat badge UIDs as identifiers, never as secrets: challenge-response on the
  card, or a reader that signs its reads.
- Make the audit trail tamper-evident (append-only storage, signed entries)
  before anyone relies on it to reconstruct an incident.
- Rate-limit and authorise `/api/v1/teleop`, which is remote motion control and
  the most dangerous endpoint in the system.

None of this is implemented. It is a student prototype on a lab bench, and the
honest framing is that the interesting engineering here is the two-brain safety
split and calibration-free navigation, not the security posture.

## Demo failure choreography (rehearse these — grace under failure beats features)
Demonstrate on purpose: wrong staff badge (refused), wrong patient tag (refused), E-stop mid-run, comms loss (safe-hold), marker occlusion (search → safe-hold, R15), and Pi unplug (falls back to teleop, R14). Each should produce a clear dashboard event and a calm, correct robot response.
