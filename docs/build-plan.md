# 13-day build plan (navigation proven early; integration starts Day 6)

Daily 10-min standup, non-negotiable. Roles: Mech / EE / FW (firmware) / SW (Pi+dashboard) / Integration-Demo lead (scope veto).

| Day | Milestone |
|---|---|
| 0 | Read docs; settle open decisions; lab inventory vs BOM; order gaps; repo+group; flash Pi; appoint battery officer + integration lead |
| 1 | Route+markers laid out; CAMERA DETECTS ARUCO + prints centre-offset/width on bench (calibration-free, no pose); mock DB schema+seed; serial protocol frozen; magazine v0 (cardboard) validates candy |
| 2 | Chassis rolling under MCU PID; odometry to Pi; Flask serves fleet page; Pi<->MCU serial echo passes |
| 3 | BASIC SINGLE-MARKER HOMING works (drive to marker, stop at standoff); magazine v1 printed |
| 4 | Multi-marker route + odometry coast + MARKER_SEARCH; RFID two-scan bench; dispatch form creates tasks |
| 5 | Magazine 50-cycle jam test >= 9/10; cold box latched + DS18B20 streaming |
| 6 | INTEGRATION begins: dispatch->navigate->arrive->auth->dispense->return on real course |
| 7 | Full loop end-to-end at least once; failure list on whiteboard |
| 8 | Audio primary (threshold) -> dashboard alert + slider; start classifier sample collection |
| 9 | Nav robustness (exposure/blur/marker-lost R15); obstacle tuning; lost-comms + Pi-fail->teleop (R14); teleop panel run live as supervised nudge; classifier go/no-go |
| 10 | GATE (go/no-go): run R1-R15 incl. a BLIND unassisted nav block (nudge disabled, non-driver logging) -> >=7/10 = supervised autonomy is the headline, else teleop-primary. Any red -> descope ladder; stretch stays locked. All green -> pick ONE stretch |
| 11 | Dress rehearsals x5 w/ metrics; perfboard/loom wiring; Pi image backup; battery rotation plan |
| 12 | FEATURE FREEZE. Practice demo until boring; poster/slides w/ measured numbers; pack spares |
| 13 | Buffer / travel / sleep |
