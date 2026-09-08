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

## Demo failure choreography (rehearse these — grace under failure beats features)
Demonstrate on purpose: wrong staff badge (refused), wrong patient tag (refused), E-stop mid-run, comms loss (safe-hold), marker occlusion (search → safe-hold, R15), and Pi unplug (falls back to teleop, R14). Each should produce a clear dashboard event and a calm, correct robot response.
