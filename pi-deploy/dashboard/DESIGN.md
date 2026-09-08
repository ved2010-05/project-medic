# ARCHITECTURE.md — dashboard/ ("Hospital Central")

Local context for the laptop-side app. Read `ARCHITECTURE.md` first.

## Stack
- Flask (default) or FastAPI + SQLite + plain HTML/JS. **No React/frameworks** — this is a 2-week build. Keep it boring and legible.
- Runs on the lab laptop. The Pi polls it every 500 ms over our own Wi-Fi.

## Screens
1. Fleet/status (renders N robots by `robot_id`; N=1 now, but never hardcode 1).
2. Dispatch form — pick a patient → creates a task (patient, items, count, destination).
3. Live audit log — every scan/dispense/alert/command with timestamps. **This is the product; make it complete and readable.**
4. Temp chart — cold-box DS18B20 stream + excursion warnings.
5. Teleop panel — fwd/left/right/stop + speed. This is the R14 parachute AND the live supervised-nudge channel (a bounded, disclosed heading nudge before SAFEHOLD); it must always work, and every nudge is logged to the audit trail.
6. Sound-threshold slider — pushes the threshold to `ears.py`.

## Mock DB (SQLite) — seed with fun fake data
Tables (minimum): `patients`, `prescriptions`, `staff` (badge UID), `patient_tags` (wristband UID), `tasks` (state machine of a delivery), `events` (append-only audit log). Expose a small REST API — treat it as the stand-in for a real EHR integration and keep the shape clean.

## Rules
- **Log everything, in and out.** Append-only `events`; never mutate a logged event.
- Auth verification (staff badge + patient tag vs task) may live here; it must **fail closed** and write a red event on mismatch/timeout before anything unlocks.
- No secrets, no real patient data — fake names only.
- The dashboard is authoritative for tasks; the robot only ever *executes dispatched* tasks (human-supervised, low autonomy — a core selling point).
