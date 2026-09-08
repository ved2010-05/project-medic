#!/usr/bin/env bash
# install.sh — one-command setup for Project MEDIC on a fresh Raspberry Pi
# 4B running Raspberry Pi OS 64-bit.
#
# VALIDATED ON: Trixie (Debian 13) — Python 3.13, picamera2 0.3.36,
# libcamera 0.7.1, OpenCV 4.10.0 + numpy 2.2.4 (both from apt).
# Bookworm is still supported, but needs two reverts — the OS check below
# detects the release and tells you exactly which ones.
#
# Safe to re-run: nothing here deletes data or overwrites your config
# without asking first.
#
# What it does, in order:
#   1. Sanity checks (not root, looks like a Pi/Debian).
#   2. apt packages — system libraries plus python3-picamera2 and
#      python3-libcamera, which are APT-ONLY and CANNOT be pip installed.
#   3. A Python venv with --system-site-packages (see the comment at that
#      step for why this exact flag matters more than anything else in
#      this file).
#   4. pip install -r requirements.txt, then a cv2.aruco sanity check
#      with an automatic pip fallback if apt's OpenCV build is missing it.
#   5. medic.env, copied from medic.env.example if it doesn't exist yet.
#   6. Seed the mock hospital database (asks before overwriting one that
#      already has data in it).
#   7. Install + enable the four systemd services, in dependency order.
#   8. Print the dashboard URL (this Pi's real IP, not a hardcoded one)
#      and a summary of anything that needs a human to look at it.
#
# BENCH TEST:
#   ./install.sh
#   then: ./scripts/healthcheck.sh
set -euo pipefail

# ---------------------------------------------------------------------
# 0. Where are we?
# ---------------------------------------------------------------------
# Resolve to an ABSOLUTE path no matter how this was invoked
# (./install.sh, bash install.sh, /home/x/pi-deploy/install.sh, a symlink,
# ...). Every systemd unit's WorkingDirectory/ExecStart and every relative
# path below depends on this being right.
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$INSTALL_DIR"

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Warnings are collected here and reprinted as a short list at the very
# end, so a real problem doesn't get lost in 60 lines of apt/pip output.
WARNINGS=()
note_warning() { WARNINGS+=("$1"); warn "$1"; }

# ---------------------------------------------------------------------
# 1. Sanity checks
# ---------------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
    die "Do not run install.sh as root, and don't run it with sudo.
  Run it as the ordinary user that should OWN the robot's files and
  services — usually 'pi', but Raspberry Pi Imager now lets you pick any
  username, so this script never assumes a name and just uses whoever
  invokes it. The script calls sudo itself for the few steps that
  genuinely need root (apt, systemctl). If the WHOLE script ran as root,
  the venv and medic.env would end up root-owned, and the systemd
  services would then run the robot's software as root too — strictly
  more privilege than any of these scripts need, and a pain to undo."
fi

RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"

if ! command -v apt-get >/dev/null 2>&1; then
    die "apt-get not found. This script is written for Raspberry Pi OS
  (Debian-based: Bookworm or Trixie). Wrong device/image?"
fi

# Which Debian release we are on changes real decisions (numpy/OpenCV ABI,
# whether libatlas-base-dev exists), so state it up front rather than making
# the next person diff two install logs to find out.
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    OS_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-unknown}")"
    echo "  detected OS : ${OS_CODENAME}"
    case "$OS_CODENAME" in
        trixie)  : ;;  # what this bundle is currently validated against
        bookworm)
            note_warning "Bookworm detected. This bundle is currently tuned for
  Trixie. Two things need reverting: re-add libatlas-base-dev to APT_PACKAGES,
  and re-add the 'numpy>=1.24,<2' pin to requirements.txt (Bookworm's OpenCV
  is built against the numpy 1.x ABI; Trixie's is 2.x)." ;;
        *)
            note_warning "Untested OS release '${OS_CODENAME}'. Check the numpy
  and OpenCV ABI assumptions in requirements.txt before trusting the camera." ;;
    esac
fi

log "Project MEDIC — Pi install starting"
echo "  install dir : $INSTALL_DIR"
echo "  run as      : $RUN_USER:$RUN_GROUP"

sudo -v || die "sudo access is required (for apt and systemctl steps)."

# ---------------------------------------------------------------------
# 2. apt packages
# ---------------------------------------------------------------------
log "Installing apt packages (system libraries + picamera2/libcamera)"
# python3-picamera2 / python3-libcamera are APT-ONLY — there is no pip
# wheel for either. They MUST come from here, not from requirements.txt.
# See the venv step below for why that has a knock-on effect on how the
# venv itself has to be created.
APT_PACKAGES=(
    python3-venv
    python3-pip
    python3-picamera2
    python3-libcamera
    libcap-dev
    portaudio19-dev
    # numpy MUST come from apt, never pip. On Trixie apt ships numpy 2.2.4 and
    # python3-opencv 4.10.0 built against the numpy 2.x ABI; a pip-installed
    # numpy 1.x would shadow it inside the venv and break `import cv2`.
    # requirements.txt deliberately does not list numpy — see its long note.
    python3-numpy
    # libatlas-base-dev was here. It does NOT exist on Raspberry Pi OS Trixie
    # (Debian 13) — 'apt-cache policy libatlas-base-dev' returns
    # "Candidate: (none)" — and because this script runs under
    # `set -euo pipefail`, apt-get failing on it aborted the WHOLE install
    # before the venv/pip/systemd steps ever ran. Nothing is lost by dropping
    # it: it only ever existed to give numpy a BLAS to link against, and
    # libblas3 is already present. Re-add it only if you go back to Bookworm.
    python3-opencv
    # Not strictly on the original hardware list, but scripts/healthcheck.sh
    # needs these to actually answer "is the mic/camera there": arecord (mic)
    # and v4l2-ctl (USB webcam fallback / general V4L2 diagnostics).
    alsa-utils
    v4l-utils
)
sudo apt-get update -qq
sudo apt-get install -y "${APT_PACKAGES[@]}"

# ---------------------------------------------------------------------
# 3. Python virtual environment
# ---------------------------------------------------------------------
log "Creating Python virtual environment (.venv)"
# --system-site-packages is the single least obvious line in this whole
# bundle, so: picamera2 and libcamera were just installed SYSTEM-WIDE by
# apt above — pip never touches them, because it can't. A plain
# `python3 -m venv .venv` is intentionally *isolated* from system
# packages, so `import picamera2` inside it would fail with no obvious
# reason: libcamera-hello works fine outside the venv, the camera is
# physically fine, but the venv just can't see the apt package. That
# looks exactly like "the camera silently died" and is a classic
# afternoon-eater. --system-site-packages makes the venv able to SEE
# apt-installed packages (picamera2, libcamera, python3-opencv) in
# addition to whatever pip installs into the venv itself — and pip
# installs still land in the venv, not system-wide, which is what
# satisfies the PEP 668 "externally-managed-environment" pip refusal in
# the first place (that applies to Bookworm and Trixie alike).
if [ ! -d .venv ]; then
    python3 -m venv --system-site-packages .venv
    echo "  created .venv"
else
    echo "  .venv already exists — reusing it"
fi
VENV_PY="$INSTALL_DIR/.venv/bin/python"
VENV_PIP="$INSTALL_DIR/.venv/bin/pip"

# ---------------------------------------------------------------------
# 4. pip install + OpenCV/aruco sanity check
# ---------------------------------------------------------------------
log "Installing Python packages into .venv"
"$VENV_PIP" install --upgrade pip
"$VENV_PIP" install -r requirements.txt

log "Checking cv2.aruco is actually available"
# The single most common way a student team loses an afternoon on THIS
# part of the stack: some apt builds of python3-opencv ship WITHOUT the
# contrib "aruco" module, so `import cv2` succeeds (everything LOOKS
# fine) and the failure only shows up once nav.py actually tries to
# detect a marker, usually on the robot, usually during a rehearsal.
# Catch it here instead, loudly, with a working fallback.
ARUCO_CHECK='import cv2, sys; sys.exit(0 if hasattr(cv2, "aruco") else 1)'
if "$VENV_PY" -c "$ARUCO_CHECK" 2>/dev/null; then
    echo "  OK — apt's python3-opencv provides cv2.aruco"
else
    note_warning "apt's python3-opencv is missing cv2.aruco — falling back to
  'pip install opencv-contrib-python' inside the venv. This pulls in a
  second, separate copy of OpenCV, but it is the one that reliably ships
  aruco. On Raspberry Pi OS this installs a PREBUILT ARM wheel from
  piwheels.org, not a from-source build, so it should still take on the
  order of a minute, not hours."
    "$VENV_PIP" install opencv-contrib-python
    if "$VENV_PY" -c "$ARUCO_CHECK" 2>/dev/null; then
        echo "  OK — opencv-contrib-python provides cv2.aruco"
    else
        note_warning "cv2.aruco is STILL missing after installing
  opencv-contrib-python. nav.py (ArUco navigation — R1/R13/R15) will not
  work until this is fixed by hand. Try, inside $INSTALL_DIR:
    .venv/bin/pip uninstall -y opencv-python opencv-contrib-python opencv-python-headless
    .venv/bin/pip install --force-reinstall opencv-contrib-python
  and re-run this script's check with:
    .venv/bin/python -c 'import cv2; print(cv2.aruco.DICT_4X4_50)'"
    fi
fi

if "$VENV_PY" -c "import picamera2" 2>/dev/null; then
    echo "  OK — picamera2 is importable inside the venv"
else
    note_warning "picamera2 is not importable inside .venv. Re-run this
  script (it's safe to re-run) and check the apt step above installed
  python3-picamera2 without error. nav.py can fall back to a USB webcam
  via OpenCV if this never gets fixed, but the Pi Camera path (the
  preferred one on this build) needs it. See README.md's camera section."
fi

# ---------------------------------------------------------------------
# 5. medic.env
# ---------------------------------------------------------------------
log "Setting up medic.env"
if [ -f medic.env ]; then
    echo "  medic.env already exists — leaving it alone (edit it by hand;"
    echo "  install.sh never overwrites it once it's there)"
else
    cp medic.env.example medic.env
    echo "  created medic.env from medic.env.example — the defaults are"
    echo "  correct for one robot talking to the dashboard on this same"
    echo "  Pi. Edit MEDIC_ROBOT_ID if you ever clone this bundle onto a"
    echo "  second robot."
fi

# ---------------------------------------------------------------------
# 6. Seed the mock hospital database
# ---------------------------------------------------------------------
log "Seeding the mock hospital database"
# dashboard/seed.py creates the SQLite schema and inserts the fake
# patients/staff/tags roster. It is idempotent, so re-running install.sh
# never duplicates rows. If the file is somehow missing this step is
# skipped rather than fatal — apt/venv/pip/systemd don't depend on it.
SEED_SCRIPT="dashboard/seed.py"
DB_CANDIDATES=(dashboard/medic.db dashboard/instance/medic.db instance/medic.db)
EXISTING_DB=""
for f in "${DB_CANDIDATES[@]}"; do
    if [ -f "$f" ]; then
        EXISTING_DB="$f"
        break
    fi
done

if [ ! -f "$SEED_SCRIPT" ]; then
    note_warning "$SEED_SCRIPT not found — skipping the DB seed step. This is
  expected if the dashboard code hasn't been added to the bundle yet.
  Once it is, run it by hand:  .venv/bin/python $SEED_SCRIPT
  (or just re-run ./install.sh — it's safe to re-run)."
elif [ -n "$EXISTING_DB" ]; then
    if [ -t 0 ]; then
        read -r -p "  found an existing database at $EXISTING_DB — reseed it and
  overwrite its data? [y/N] " REPLY
    else
        REPLY="n"
        echo "  found an existing database at $EXISTING_DB — not overwriting it"
        echo "  (this is a non-interactive run, e.g. piped in over ssh; re-run"
        echo "  install.sh in an interactive terminal and answer 'y' if you"
        echo "  really want a fresh database)"
    fi
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        # --yes: we just asked; don't make the user confirm the same thing twice.
        # --reset is what actually discards the old audit log.
        "$VENV_PY" "$SEED_SCRIPT" --reset --yes
        echo "  reseeded $EXISTING_DB"
    else
        # Seeding without --reset is safe and idempotent: it tops up any missing
        # roster rows and leaves every logged event untouched.
        "$VENV_PY" "$SEED_SCRIPT"
        echo "  kept the existing database (roster topped up, audit log intact)"
    fi
else
    "$VENV_PY" "$SEED_SCRIPT" --summary
    echo "  seeded a fresh database"
fi

# ---------------------------------------------------------------------
# 7. systemd services
# ---------------------------------------------------------------------
log "Installing systemd services"
# Each systemd/medic-*.service file is a TEMPLATE containing @INSTALL_DIR@
# / @RUN_USER@ / @RUN_GROUP@ placeholders (systemd unit files can't
# reference shell variables or $HOME themselves) — fill them in here and
# write the result to /etc/systemd/system/, which is the only copy
# systemd actually reads.
for svc in medic-dashboard medic-bridge medic-nav medic-ears; do
    sed \
        -e "s#@INSTALL_DIR@#$INSTALL_DIR#g" \
        -e "s#@RUN_USER@#$RUN_USER#g" \
        -e "s#@RUN_GROUP@#$RUN_GROUP#g" \
        "systemd/$svc.service" | sudo tee "/etc/systemd/system/$svc.service" >/dev/null
    echo "  installed $svc.service (User=$RUN_USER)"
done

sudo systemctl daemon-reload

# The unit files already encode this ordering via After=/Wants=, but we
# also enable+start them in the same order here for a clean, predictable
# first boot and because that is simply the order the task spec calls
# for: dashboard (the API everything else needs) first, then the bridge
# (talks to both the dashboard and the MCU), then nav and ears (which
# both need the bridge/dashboard up to have anything useful to do, but
# must — and do — tolerate them being briefly down too).
sudo systemctl enable --now medic-dashboard.service
sudo systemctl enable --now medic-bridge.service
sudo systemctl enable --now medic-nav.service
sudo systemctl enable --now medic-ears.service
echo "  all four services enabled — they will also start automatically on boot"

# ---------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------
log "Install complete"
PI_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -z "$PI_IP" ]; then
    PI_IP="<this-pi-ip>"
    note_warning "could not auto-detect this Pi's IP address ('hostname -I'
  returned nothing). Check it by hand with 'ip addr' — you'll need it to
  reach the dashboard from another machine on the hotspot."
fi
echo "  Dashboard (from another machine on the same hotspot): http://$PI_IP:5000"
echo "  Dashboard (from this Pi):                              http://127.0.0.1:5000"
echo
echo "  Check everything is actually healthy with:"
echo "    ./scripts/healthcheck.sh"
echo
echo "  Watch a service's live logs with, e.g.:"
echo "    journalctl -u medic-nav.service -f"

if [ "${#WARNINGS[@]}" -gt 0 ]; then
    echo
    echo "  --- ${#WARNINGS[@]} warning(s) from this run, repeated here so they"
    echo "      don't get lost above ---"
    for w in "${WARNINGS[@]}"; do
        echo "  * $w"
    done
fi
