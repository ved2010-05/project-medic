#!/usr/bin/env bash
# healthcheck.sh — the "why is it not working" tool for Project MEDIC on a
# Raspberry Pi. It changes nothing; it just checks the five usual suspects
# and tells you plainly which one is the problem: camera, mic, MCU serial
# (expected ABSENT right now — see below), dashboard, and the four
# systemd services.
#
# Run this FIRST whenever something seems broken, before digging into any
# single script's logs — it will usually point straight at the cause.
#
# BENCH TEST: this script IS the bench test.
#   ./scripts/healthcheck.sh
set -uo pipefail   # deliberately NOT -e: one failing check must not stop the rest

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$INSTALL_DIR"

PASS=0; WARNCOUNT=0; FAILCOUNT=0
ok()    { printf '[ OK ] %s\n' "$*"; PASS=$((PASS+1)); }
info()  { printf '[INFO] %s\n' "$*"; }
warnc() { printf '[WARN] %s\n' "$*"; WARNCOUNT=$((WARNCOUNT+1)); }
failc() { printf '[FAIL] %s\n' "$*"; FAILCOUNT=$((FAILCOUNT+1)); }

echo "Project MEDIC healthcheck — $(date -Is 2>/dev/null || date)"
echo "install dir: $INSTALL_DIR"
echo

# ---------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------
echo "-- Camera --"
CAM_TOOL=""
for t in rpicam-hello libcamera-hello; do
    if command -v "$t" >/dev/null 2>&1; then
        CAM_TOOL="$t"
        break
    fi
done
if [ -n "$CAM_TOOL" ]; then
    # --list-cameras returns immediately and opens no preview window, so
    # this is safe to run over SSH with no display attached.
    CAM_OUT="$(timeout 5 "$CAM_TOOL" --list-cameras 2>&1)"
    if echo "$CAM_OUT" | grep -qi "no cameras available"; then
        failc "$CAM_TOOL reports NO camera detected."
        info "       Check: the CSI ribbon is seated at both ends (contacts"
        info "       face the board's HDMI ports on most Pi 4 layouts — check"
        info "       your board), the camera is enabled, and"
        info "       'camera_auto_detect=1' is in /boot/firmware/config.txt"
        info "       (Bookworm and Trixie alike). See README.md's camera setup section."
    else
        ok "$CAM_TOOL detects a camera:"
        echo "$CAM_OUT" | sed 's/^/         /'
    fi
else
    warnc "neither rpicam-hello nor libcamera-hello is installed — cannot"
    warnc "  confirm the camera at the libcamera level. Re-run ./install.sh"
    warnc "  (it installs python3-picamera2, which pulls these in), or:"
    warnc "  sudo apt-get install -y rpicam-apps"
fi
if ls /dev/video* >/dev/null 2>&1; then
    info "V4L2 device nodes present: $(ls /dev/video* 2>/dev/null | tr '\n' ' ')"
else
    info "no /dev/video* nodes. Normal for a CSI Pi Camera on Bookworm/Trixie — it"
    info "  goes through libcamera, not V4L2 (and NOT cv2.VideoCapture — see"
    info "  README.md). Only expected to matter for a USB webcam fallback."
fi
echo

# ---------------------------------------------------------------------
# Microphone
# ---------------------------------------------------------------------
echo "-- Microphone --"
if command -v arecord >/dev/null 2>&1; then
    MIC_OUT="$(arecord -l 2>&1)"
    if echo "$MIC_OUT" | grep -q "^card"; then
        ok "USB mic found by ALSA:"
        echo "$MIC_OUT" | sed 's/^/         /'
    else
        failc "arecord -l lists no capture devices."
        info "       Is the USB mic plugged in? Try a different USB port —"
        info "       avoid an unpowered hub, some draw more current than a"
        info "       hub can supply on its own."
    fi
else
    warnc "arecord not installed. sudo apt-get install -y alsa-utils"
    warnc "  (or re-run ./install.sh, which installs it for you)."
fi
echo

# ---------------------------------------------------------------------
# MCU serial port
# ---------------------------------------------------------------------
echo "-- MCU serial port --"
SERIAL_FOUND=""
for pattern in /dev/serial/by-id/* /dev/ttyUSB* /dev/ttyACM*; do
    if [ -e "$pattern" ]; then
        SERIAL_FOUND="$pattern"
        break
    fi
done
if [ -n "$SERIAL_FOUND" ]; then
    ok "serial port present: $SERIAL_FOUND"
else
    info "no MCU serial port found."
    info "  THIS IS EXPECTED right now — the ESP32 is not wired up yet."
    info "  task_bridge.py and nav.py are both designed to run fine without"
    info "  it: they log this once and keep retrying in the background"
    info "  (medic/common.py's SerialLink). This only becomes something to"
    info "  fix once the ESP32 IS plugged in and it's STILL not found —"
    info "  check the USB cable and 'dmesg | tail' for enumeration errors."
fi
echo

# ---------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------
echo "-- Dashboard --"
DASH_URL="http://127.0.0.1:5000"
if [ -f medic.env ]; then
    ENV_URL="$(grep -E '^MEDIC_CENTRAL_URL=' medic.env 2>/dev/null | tail -1 | cut -d= -f2-)"
    [ -n "$ENV_URL" ] && DASH_URL="$ENV_URL"
fi
if command -v curl >/dev/null 2>&1; then
    HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$DASH_URL/" 2>/dev/null || true)"
    if [ "$HTTP_CODE" = "200" ]; then
        ok "dashboard responding at $DASH_URL (HTTP 200)"
    else
        failc "dashboard NOT responding at $DASH_URL (got '${HTTP_CODE:-no response}')."
        info "       Is medic-dashboard.service running? See Services below."
        info "       From another machine, also check the firewall isn't"
        info "       blocking port 5000, and that you're on the SAME"
        info "       hotspot/router — never mix in venue Wi-Fi."
    fi
else
    warnc "curl not installed — cannot check the dashboard HTTP endpoint."
    warnc "  sudo apt-get install -y curl"
fi
echo

# ---------------------------------------------------------------------
# systemd services
# ---------------------------------------------------------------------
echo "-- Services --"
if command -v systemctl >/dev/null 2>&1; then
    for svc in medic-dashboard medic-bridge medic-nav medic-ears; do
        if ! systemctl cat "$svc.service" >/dev/null 2>&1; then
            warnc "$svc.service is not installed yet (run ./install.sh)"
            continue
        fi
        ACTIVE="$(systemctl is-active "$svc.service" 2>/dev/null || true)"
        ENABLED="$(systemctl is-enabled "$svc.service" 2>/dev/null || true)"
        if [ "$ACTIVE" = "active" ]; then
            ok "$svc.service: active (enabled=$ENABLED)"
        elif [ "$ENABLED" = "disabled" ] && [ "$svc" = "medic-nav" -o "$svc" = "medic-ears" ]; then
            # DELIBERATELY not a failure. nav needs a camera and ears needs a
            # mic + sounddevice; until that hardware is attached, leaving these
            # disabled is the CORRECT state -- the alternative is a unit that
            # crash-loops every RestartSec and buries the real problems in the
            # journal. Same reasoning as the "no MCU serial port" note above.
            # A healthcheck that shouts FAIL at an intended state just teaches
            # people to ignore it, which is how a real failure gets missed.
            info "$svc.service: stopped and disabled -- EXPECTED until its"
            info "       hardware is attached. Enable it once that is done:"
            info "         sudo systemctl enable --now $svc.service"
            if [ "$svc" = "medic-ears" ]; then
                info "       ears also needs: .venv/bin/pip install sounddevice"
                info "       (that package ALONE, never -r requirements.txt on"
                info "        a network where pip is unreliable)"
            fi
        else
            # Enabled but not running IS a real fault -- something tried to
            # start and failed.
            failc "$svc.service: $ACTIVE (enabled=$ENABLED)"
            info "       journalctl -u $svc.service -e"
        fi
    done
else
    warnc "systemctl not available on this machine — skipping service checks"
    warnc "  (expected if you're running this somewhere other than the Pi)"
fi
echo

# ---------------------------------------------------------------------
echo "== Summary: $PASS ok, $WARNCOUNT warning(s), $FAILCOUNT failure(s) =="
if [ "$FAILCOUNT" -gt 0 ]; then
    exit 1
fi
exit 0
