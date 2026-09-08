# Open decisions — settle at Day-0 kickoff, then freeze

1. Pi 4 vs 5 vs spare (default: known-good SD + supply)
2. ESP32 vs Arduino Mega (default: ESP32)
3. Encoder motors available? -> PID + odometry (strongly preferred)
4. Camera: Pi Camera vs USB webcam (default: whichever OpenCV sees first)
5. Marker plan: size (default 120-150 mm), height (~camera height); density per leg is the tunable dial (default 2 stations + 1 corner - angled toward approach + a redundant second; add more only if measured drift needs it)
6. Camera settings: 640x480, locked exposure/WB
7. Docking standoff distance (default ~20 cm)
8. Solenoid vs servo latch (default: solenoid if 5V units exist)
9. HTTP polling vs WebSocket (default: polling 500 ms)
10. Candy candidate x3, tested Day 1
11. Robot name (non-blocking; will take longest)
12. Integration/Demo lead (scope veto) + Battery officer
