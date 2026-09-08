# Requirements scorecard (test at Day-10 gate and Day-11 rehearsals)

| ID | Requirement | Pass criterion |
|---|---|---|
| R1 | Dispatch -> autonomous marker-homing delivery Pharmacy->Ward, untouched | >= 8/10 consecutive |
| R2 | Obstacle stop + resume | 10/10, stop at 15-35 cm |
| R3 | Refuse on wrong staff badge | 10/10, logged |
| R4 | Refuse on wrong patient tag | 10/10, logged |
| R5 | Correct two-scan -> exact commanded count dispensed | >= 9/10 (jams = fail) |
| R6 | Cold-box temp logged | <= 60 s intervals, gap-free over 10 min |
| R7 | Sound alert latency | <= 3 s dashboard-visible, >= 8/10 |
| R8 | False alerts in quiet standby | <= 1 per 10 min |
| R9 | E-stop | Motors halt < 0.5 s, payload stays locked |
| R10 | Lost comms en route | Safe-hold <= 10 s, locks held, flagged on reconnect |
| R11 | Endurance | >= 45 min continuous loop, no brownout, no Pi reset |
| R12 | Audit completeness | Every scan/dispense/alert/command timestamped for a full run |
| R13 | Arrive at correct station (marker ID == task) | 10/10; wrong station -> no auth + exception event |
| R14 | Pi-failure fallback | Pi unplugged -> teleop demo runs end-to-end |
| R15 | Marker lost / occluded | Coast -> MARKER_SEARCH -> reacquire or SAFEHOLD+alert; never wanders. 10/10 |
