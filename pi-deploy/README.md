# Project MEDIC — Pi deployment bundle

This directory (`pi-deploy/`) is everything that runs **on the robot's
Raspberry Pi**: the "Hospital Central" dashboard, and the three Pi
processes (`nav.py`, `ears.py`, `task_bridge.py`) described in
`pi-deploy/DESIGN.md` and `docs/design-doc-v0.3.md`. It is meant to be copied to
a fresh Pi and installed with **one command**.

The ESP32 reflex-brain firmware is a separate deploy target — see
`firmware/`. **The ESP32 is not connected yet on this build.** Every
process in this bundle is designed to start, run, and log correctly with
no MCU attached — that is a normal, supported state here, not an error,
until the day the ESP32 is actually wired up.

---

## 0. Before you touch this bundle at all: verify the hardware

Nine times out of ten, "the Pi code doesn't work" is actually "the camera
isn't enabled" or "the mic isn't plugged into the port you think it is."
Rule these out on a bare Pi **before** blaming `nav.py` or `ears.py` —
it will save you the afternoon this bundle is trying to save you.

### Camera

1. Physically seat the CSI ribbon at both ends (Pi Camera connector AND
   the Pi's camera port), contacts facing the correct way for your board
   — check the silkscreen/markings near the connector, orientation
   varies by Pi Camera version and by which CSI port you use on a Pi 5.
2. On Raspberry Pi OS (Bookworm and Trixie), camera support is normally auto-detected
   (`camera_auto_detect=1` in `/boot/firmware/config.txt` — check it's
   there and uncommented). If you're on an older/imaged-from-Bullseye
   setup, enable it with `sudo raspi-config` → *Interface Options* →
   *Camera*, then reboot.
3. **Verify with the OS tool first, not with Python:**
   ```
   rpicam-hello --list-cameras     # current name on Bookworm/Trixie
   # or, on slightly older images:
   libcamera-hello --list-cameras
   ```
   This should print your camera's sensor name. If it says "no cameras
   available," that is a wiring/firmware problem — fix it here before
   you ever run `nav.py`.
4. Optional: `rpicam-hello -t 5000` (or `libcamera-hello -t 5000`) opens
   a 5-second live preview if you have a display attached — the most
   direct "is the camera actually working" check there is.

**Why this matters more than usual on Bookworm/Trixie:** a CSI Pi Camera does
**not** work through `cv2.VideoCapture(0)` on either — that path is for
V4L2/USB cameras only. `nav.py`'s primary camera path is Picamera2
(`import picamera2`), with a `cv2.VideoCapture` fallback kept for
Bullseye or a USB webcam. If `libcamera-hello` works but `nav.py` still
can't see a camera, the next suspect is the venv, not the hardware — see
the troubleshooting table below.

### Microphone

Plug in the USB mic, then:
```
arecord -l
```
You should see a `card N:` line for it. If you see nothing, try a
different USB port — an unpowered hub is a common culprit. `ears.py`
uses `sounddevice` (PortAudio), which will also list devices:
```
.venv/bin/python -c "import sounddevice; print(sounddevice.query_devices())"
```

---

## 1. Getting the bundle onto the Pi

From your laptop, with the Pi powered on and reachable on **our own
hotspot/router** (never venue Wi-Fi — `ARCHITECTURE.md` §0.7):

```
scp -r pi-deploy pi@<pi-ip-address>:~/
ssh pi@<pi-ip-address>
cd ~/pi-deploy
```

(Replace `pi` with whatever username you picked when you imaged the SD
card — recent Raspberry Pi Imager versions ask you to choose one instead
of defaulting to `pi`.)

---

## 2. Install — one command

```
./install.sh
```

It is safe to re-run any time (idempotent) — re-running just confirms
everything is still in place and re-applies the systemd unit files. It
will:

1. Refuse to run as root (`sudo ./install.sh` is wrong — see the error
   message it prints if you try; run it as your normal user, it calls
   `sudo` itself only for the apt/systemctl steps).
2. Install the apt packages this bundle needs (including
   `python3-picamera2` and `python3-libcamera`, which are **apt-only** —
   there is no pip wheel for either).
3. Create `.venv` with `python3 -m venv --system-site-packages .venv`.
   **That flag is the single least obvious thing in this whole bundle:**
   without it, the venv cannot see the apt-installed `picamera2`, and the
   camera looks "silently dead" with no useful error message, even
   though `libcamera-hello` works fine outside the venv. See the comment
   in `install.sh` at that step for the full explanation.
4. `pip install -r requirements.txt`, then check that `cv2.aruco` is
   actually importable (some `apt` builds of `python3-opencv` strip the
   contrib modules that `aruco` lives in) — falling back to
   `pip install opencv-contrib-python` automatically if needed.
5. Copy `medic.env.example` → `medic.env` if it doesn't exist yet (never
   overwrites an existing one).
6. Seed the mock hospital SQLite database (asks first if one already
   exists with data in it).
7. Install and enable the four systemd services, in dependency order:
   dashboard → bridge → nav / ears.
8. Print the dashboard URL, using this Pi's real IP address (resolved at
   install time, never hardcoded) — and a short list of anything that
   needs a human to look at it.

---

## 3. Bundle layout this install expects

`install.sh` / the systemd units assume this shape. Everything below is
written and deployed — `medic/common.py` holds the shared config/serial/HTTP
plumbing, so import it rather than duplicating that logic:

```
pi-deploy/
  install.sh, requirements.txt, medic.env(.example), README.md   <- this bundle
  medic/
    common.py            <- shared config/serial/HTTP; `-m medic.common --selftest`
    camera.py             <- Picamera2/V4L2 camera abstraction
    nav.py                <- runnable as: python -m medic.nav
    ears.py                <- runnable as: python -m medic.ears
    task_bridge.py          <- runnable as: python -m medic.task_bridge
  dashboard/
    app.py                <- Flask app; runnable as: python dashboard/app.py
                              (must bind 0.0.0.0:5000, e.g. via
                              app.run(host="0.0.0.0", port=5000) under
                              `if __name__ == "__main__":`)
    seed.py                <- standalone script that creates the SQLite
                              schema + the fake patient/staff/tag roster.
                              Idempotent, so re-running never duplicates
                              rows. install.sh calls it as
                              `.venv/bin/python dashboard/seed.py`; by hand
                              use `python -m dashboard.seed --summary`
                              (--summary prints the demo cheat-sheet:
                              which badge and which wristband to scan for
                              the pass case and for the wrong-patient
                              refusal). Use --reset to wipe and start over.
  systemd/  scripts/       <- this bundle
```

`-m medic.xxx` relies on `WorkingDirectory=` (in the systemd units) / `cd`
(in `scripts/run_all.sh`) being `pi-deploy/` itself, so Python resolves
`medic` as a package in the current directory — the exact same convention
`medic/common.py`'s own bench test already uses
(`python -m medic.common --selftest`). `dashboard/app.py` is run as a
plain script path instead (`python dashboard/app.py`, not
`-m dashboard.app`) — that is how `medic/task_bridge.py`'s own bench-test
doc already invokes it by hand, so this bundle's systemd unit and
`scripts/run_all.sh` match that convention rather than inventing a second
one.

If `dashboard/seed.py` is somehow missing when you run `install.sh`, the
seed step is skipped with a warning rather than failing the whole
install — everything else still sets up.

---

## 4. Running things by hand for bench testing

Systemd is for "leave it running unattended"; for active debugging, run
one process at a time in its own terminal so you see its output live and
can Ctrl-C it without touching the others:

```
cd ~/pi-deploy
source .venv/bin/activate      # so `python` below is the venv's python
set -a; source medic.env; set +a   # export MEDIC_* config into this shell

python dashboard/app.py
python -m medic.task_bridge --log-level DEBUG
python -m medic.nav --dry-run --debug     # no MCU/dashboard needed for this one
python -m medic.ears --list-devices       # find the right mic first...
python -m medic.ears --calibrate          # ...then pick a real threshold
python -m medic.camera --camera-backend auto   # is it even a camera problem?
```

Each script's own module docstring has a full step-by-step bench-test
walkthrough (sign checks, marker-lost behaviour, refractory-period checks,
etc.) — read it with `python -m pydoc medic.nav` or just open the file;
`--help` lists the flags. `medic/camera.py`'s bench test is worth running
*before* `nav.py`'s: "camera works but ArUco sees nothing" is easy to
mistake for a nav bug when it's actually a wiring/backend problem.

Or use the tmux launcher to get all four in one window at once, split
into panes:
```
./scripts/run_all.sh
```
(`Ctrl-b` then an arrow key to switch panes, `Ctrl-b d` to detach without
stopping anything, `tmux kill-session -t medic` to stop everything.)

For the day-to-day "just leave it running" case, use systemd instead —
that's what `install.sh` sets up. Restart a single service after editing
its code:
```
sudo systemctl restart medic-nav.service
```

---

## 5. Reading logs

Every process's stdout/stderr goes to the systemd journal:
```
journalctl -u medic-dashboard.service -f     # follow live
journalctl -u medic-nav.service -e           # jump to the end
journalctl -u medic-nav.service --since "10 min ago"
```
Add `-u medic-bridge.service` / `-u medic-ears.service` the same way.
Running by hand (section 4) just prints straight to your terminal
instead — no journal involved.

---

## 6. Healthcheck — "why is it not working"

```
./scripts/healthcheck.sh
```
Checks, in order: camera detected (via `rpicam-hello`/`libcamera-hello`),
mic detected (via `arecord -l`), MCU serial port present (**expected
ABSENT right now** — it says so plainly, that is not an error until the
ESP32 is actually wired up), dashboard responding on `MEDIC_CENTRAL_URL`,
and each systemd service's state. Run it any time something seems wrong
— it usually points straight at the cause.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `libcamera-hello`/`rpicam-hello` says "no cameras available" | Ribbon not seated, wrong orientation, or camera not enabled | Reseat the CSI ribbon at both ends; check `/boot/firmware/config.txt` has `camera_auto_detect=1`; reboot. Fix this before touching any Python. |
| `libcamera-hello` sees the camera but `nav.py` doesn't (or crashes on `import picamera2`) | `picamera2` not importable inside the venv | Confirm `.venv` was created with `--system-site-packages` (`grep include-system-site-packages .venv/pyvenv.cfg` should say `true`); if not, delete `.venv` and re-run `./install.sh`. |
| `nav.py` opens a camera but never sees ArUco markers, or `cv2.aruco` raises `AttributeError`/`ModuleNotFoundError` | apt's `python3-opencv` build doesn't include the contrib `aruco` module | `install.sh` already checks for and works around this automatically (falls back to `pip install opencv-contrib-python`). If it's still broken, see the fallback commands `install.sh` prints, or run: `.venv/bin/python -c "import cv2; print(cv2.aruco.DICT_4X4_50)"` to check by hand. |
| `import cv2` raises something like `numpy.core.multiarray failed to import` | numpy ABI mismatch: a **pip-installed numpy inside the venv is shadowing the apt one** that OpenCV was built against. On **Trixie** apt ships numpy 2.2.4 and OpenCV 4.10.0 built for the numpy **2.x** ABI, so a pip numpy 1.x breaks it. (On Bookworm it was the reverse — do not copy Bookworm advice here.) | **Remove** the pip copy, don't add one: `.venv/bin/pip uninstall -y numpy`, then confirm the system one is back with `.venv/bin/python -c "import numpy,os;print(numpy.__version__, os.path.dirname(numpy.__file__))"` — you want `2.2.4` and a path under `/usr/lib/python3/dist-packages`. numpy is intentionally **absent** from `requirements.txt` so pip leaves it alone. |
| `ears.py` finds no mic / `sounddevice.query_devices()` is empty | Mic not detected by ALSA, or PortAudio not installed | `arecord -l` first (rules the hardware in/out); if PortAudio itself is missing, re-run `./install.sh` (installs `portaudio19-dev`). Try a different USB port. |
| Can't reach the dashboard from a laptop, but `curl http://127.0.0.1:5000/` works fine on the Pi itself | Not on the same hotspot/router, or a firewall is blocking port 5000, or the dashboard is only listening on `127.0.0.1` | Confirm both devices are on the SAME hotspot (never venue Wi-Fi, `ARCHITECTURE.md` §0.7); confirm `dashboard/app.py` binds `0.0.0.0`, not `127.0.0.1`, in its `app.run(...)` call. |
| `pip install ...` fails with "externally-managed-environment" | Trying to `pip install` outside a venv (PEP 668, the default on Bookworm and Trixie) | Don't. Always use `.venv/bin/pip` (or `source .venv/bin/activate` first) — never bare system `pip`/`pip3`. `install.sh` already does this correctly. |
| A `medic-*.service` won't start / restarts in a loop | Usually a Python traceback on startup | `journalctl -u <service> -e` to see it; run the same module by hand (section 4) to iterate faster without waiting on systemd restarts. |
| Everything runs but nothing ever talks to the MCU | ESP32 not wired up yet | **Expected on this build right now.** `scripts/healthcheck.sh` reports the missing serial port as informational, not a failure, for exactly this reason. Every process is designed to run and log fine without it. |

---

## 8. What this bundle deliberately does NOT do

Per `ARCHITECTURE.md` §0 and this task's scope: it does not add any MCU
simulator or mock serial device (the ESP32 not being present is meant to
surface as-is, not be papered over); it does not run anything as root; it
does not touch venue Wi-Fi; and safety logic (obstacle-stop, E-stop,
lost-comms safe-hold) is never something a Pi service decides — it only
ever logs what the MCU reports.
