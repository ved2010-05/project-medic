#!/usr/bin/env bash
# ultrasonic-check.sh — is the HC-SR04 telling the truth?
#
# Prints live distance readings from the MCU so you can hold a tape measure
# against them. This is the tool for "the robot brakes for nothing" and for
# "it blocks at 50 cm when the threshold is 25 cm" — both of which are sensor
# problems, not threshold problems, and you cannot tell which without numbers.
#
# HOW TO USE IT
#   1. Put a flat object (a book, not your hand — cloth and skin absorb
#      ultrasound and read badly) at a MEASURED distance. 50 cm is a good start.
#   2. Run this.
#   3. Compare "reported" against your tape measure.
#
# READING THE RESULT
#   reported ~= actual        -> sensor is fine; the thresholds are doing their job
#   reported consistently LOW -> under-reading. Almost always the ECHO line:
#                                docs/pinout.md requires a level shifter, and a
#                                divider made of large resistors gives slow edges
#                                that truncate the pulse -> short pulse -> short
#                                distance. Try 1k/2k, and verify the HC-SR04 has
#                                a solid 5 V (it does NOT work reliably on 3.3 V).
#   wild / jumping values     -> ECHO floating, or motor wiring noise coupling in.
#   nothing at all            -> TRIG not connected, or no 5 V to the sensor.
#
# If it reads low by a CONSISTENT factor and the wiring is genuinely correct,
# set ULTRA_SCALE in firmware/src/config.h (see that file) and reflash.
#
# BENCH TEST: this script IS the bench test.
set -uo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$INSTALL_DIR"

SECS="${1:-20}"

echo "Ultrasonic check — sampling for ${SECS}s"
echo

# task_bridge is the SOLE reader of the MCU link (two readers split the byte
# stream and both get corrupted JSON), so it has to stand down while we listen.
BRIDGE_WAS_ACTIVE=0
if systemctl is-active --quiet medic-bridge; then
    BRIDGE_WAS_ACTIVE=1
    echo "  stopping medic-bridge so we can read the port cleanly..."
    sudo systemctl stop medic-bridge
    sleep 2
fi

restore() {
    if [ "$BRIDGE_WAS_ACTIVE" = "1" ]; then
        echo
        echo "  restarting medic-bridge..."
        sudo systemctl start medic-bridge
    fi
}
trap restore EXIT

.venv/bin/python - "$SECS" <<'PY'
import sys, time, json, glob
import serial

secs = float(sys.argv[1])
hits = sorted(glob.glob("/dev/serial/by-id/*")) or sorted(glob.glob("/dev/ttyUSB*")) \
       or sorted(glob.glob("/dev/ttyACM*"))
if not hits:
    print("  NO SERIAL PORT — is the ESP32 plugged in?")
    sys.exit(1)

# DTR/RTS must stay low or the ESP32 sits in reset and says nothing at all
# (RTS->EN, DTR->IO0 on a DevKit). Same reason medic/common.py does this.
s = serial.Serial()
s.port = hits[0]; s.baudrate = 115200; s.timeout = 0
s.dtr = False; s.rts = False
s.open()
time.sleep(2.0)
s.reset_input_buffer()

buf, vals, blocked_n, t0 = b"", [], 0, time.time()
print("  %-8s %-10s %s" % ("t(s)", "reported", "blocked"))
last_print = 0.0
while time.time() - t0 < secs:
    buf += s.read(4096)
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line.decode("ascii", "ignore"))
        except ValueError:
            continue
        if m.get("t") != "obstacle":
            continue
        d = m.get("d")
        b = m.get("blocked")
        if b:
            blocked_n += 1
        if isinstance(d, (int, float)):
            vals.append(d)
        el = time.time() - t0
        if el - last_print >= 0.5:      # don't scroll faster than you can read
            last_print = el
            shown = ("%.1f cm" % d) if isinstance(d, (int, float)) and d > 0 else "no echo"
            print("  %-8.1f %-10s %s" % (el, shown, "YES" if b else "no"))
    time.sleep(0.05)
s.close()

print()
if not vals:
    print("  NO VALID READINGS AT ALL.")
    print("  -> TRIG not wired, no 5 V to the sensor, or ECHO never pulses.")
else:
    vals.sort()
    n = len(vals)
    mean = sum(vals) / n
    median = vals[n // 2]
    print("  samples : %d" % n)
    print("  min/max : %.1f / %.1f cm" % (vals[0], vals[-1]))
    print("  mean    : %.1f cm     median: %.1f cm" % (mean, median))
    print("  spread  : %.1f cm  (a steady target should be within ~2 cm)" % (vals[-1] - vals[0]))
    print("  blocked : %d frames" % blocked_n)
    print()
    print("  Compare median against your tape measure.")
    print("  If median is LOW by a steady factor, that factor is your ULTRA_SCALE")
    print("  (actual / reported). Fix the ECHO level shifter FIRST — scaling a")
    print("  miswired sensor hides the fault instead of curing it.")
PY
