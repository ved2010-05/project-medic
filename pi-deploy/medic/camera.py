"""camera.py — camera abstraction for MEDIC nav (Raspberry Pi OS, Bookworm/Trixie).

PLATFORM: validated on Raspberry Pi OS Trixie (Debian 13), Python 3.13,
picamera2 0.3.36, libcamera 0.7.1, OpenCV 4.10.0 (apt), numpy 2.2.4 (apt).
The Picamera2 "RGB888"-means-BGR mapping this file depends on was re-confirmed
against picamera2 0.3.36's own request.py FORMAT_TABLE on that release.

One small class, `Camera`, that hides three possible camera stacks behind
open() / read_gray() / close():

  1. Picamera2 (libcamera) — THE PRIMARY PATH for a Pi Camera on the CSI
     ribbon on Raspberry Pi OS Bookworm/Trixie. IMPORTANT: on Bookworm/Trixie a CSI camera
     does NOT show up through cv2.VideoCapture(0) at all — that call will
     just fail or grab the wrong device. Picamera2 is the only way in.
  2. cv2.VideoCapture + V4L2 — the legacy stack on Bullseye, and also how
     ANY USB webcam shows up on any Pi OS version (USB webcams are always a
     V4L2 device, CSI or not).
  3. Neither worked -> CameraError with a message that names the likely
     cause, because "camera works but ArUco sees nothing" is almost always
     a wiring/driver problem, not a code problem.

Exposure/white-balance locking is built in and on by default: auto-exposure
hunting and glare are the #1 killer of ArUco detection (design doc §4.1).

BENCH TEST:
    cd pi-deploy
    python -m medic.camera --camera-backend auto
  Prints which backend won, then a live mean-brightness number once a
  second. Point the camera at the arena lighting and watch the number: with
  --lock-exposure (the default) it should settle and stay flat; run once
  more with --no-lock-exposure to see it drift as auto-exposure hunts. Use
  the settled number to sanity-check you're not near 0 (too dark) or 255
  (blown out) before ever touching cv2.aruco. Ctrl-C to stop.
"""

import argparse
import logging
import sys
import time

import cv2

try:
    from picamera2 import Picamera2
except ImportError:  # pragma: no cover - not installed off-Pi, and that's fine
    Picamera2 = None

log = logging.getLogger("medic.camera")

# ---------------------------------------------------------------------------
# Design doc §4.1 / open-decision #6: 640x480, locked exposure.
# ---------------------------------------------------------------------------
FRAME_W, FRAME_H = 640, 480

BACKENDS = ("auto", "picamera2", "v4l2")

# Fallback fixed exposure/gain used only if reading back the auto-converged
# values fails (see _open_picamera2). Not calibrated for any real venue.
# TODO(day-1): measure the actual arena lighting and set real numbers here,
# or rely on the auto-converge-then-freeze path below (usually good enough).
PICAM_EXPOSURE_US = 8000  # ExposureTime, microseconds
PICAM_GAIN = 2.0  # AnalogueGain

# V4L2's CAP_PROP_EXPOSURE units/semantics are driver-specific. -6 is a
# common "somewhere in the middle" starting point for USB webcams.
# TODO(day-1): tune on the actual webcam/venue if using the V4L2 path.
V4L2_EXPOSURE = -6


class CameraError(RuntimeError):
    """Raised by Camera.open() when no backend could be started. The message
    is written to be read out loud by whoever is standing next to the robot,
    not just logged — it names the likely physical cause."""


def to_gray(frame):
    """Convert any frame this module might produce into single-channel
    grayscale, the only thing cv2.aruco.detectMarkers wants.

    Handles every shape we might see: already-gray (2D), 3-channel color, or
    4-channel color+padding (a common Picamera2 stream format). Handing
    ArUco a wrongly-ordered or 4-channel array is the classic silent
    "camera works, detector finds nothing" bug — this function is the one
    place in the whole pipeline that has to get it right, so every caller
    below routes through it instead of doing its own cvtColor.

    Note on channel order: Picamera2's "RGB888" stream format is a
    documented libcamera naming quirk — the bytes it actually delivers are
    in B, G, R order (i.e. it matches OpenCV's own BGR convention, on
    purpose, so OpenCV code doesn't need a manual swap). We configure that
    format in _open_picamera2() below specifically so this function can use
    the same COLOR_BGR2GRAY conversion for both backends. If a future
    picamera2 version or format change ever makes marker detection go dark
    while the brightness number here still looks sane, this is the first
    place to check — for a black/white ArUco marker a reversed R/B order is
    usually harmless (grayscale luma weights R and B only slightly
    differently), but it would matter the day this pipeline looks at color.
    """
    if frame is None:
        return None
    if frame.ndim == 2:
        return frame
    channels = frame.shape[2]
    if channels == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
    if channels == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    raise ValueError("unexpected camera frame shape %s" % (frame.shape,))


def mean_brightness(gray):
    """Mean pixel value 0-255 of a grayscale frame. Printed live by the
    bench test below so the team can pick/verify an exposure on site: near
    0 is too dark, near 255 is blown out, and both kill ArUco detection."""
    if gray is None:
        return float("nan")
    return float(gray.mean())


class Camera(object):
    """Hides Picamera2 vs cv2.VideoCapture(V4L2) vs "nothing worked" behind
    open() / read_gray() / close(). See the module docstring for the
    backend order and why it's ordered that way.
    """

    def __init__(self, index=0, backend="auto", lock_exposure=True,
                 width=FRAME_W, height=FRAME_H):
        if backend not in BACKENDS:
            raise ValueError("backend must be one of %s, got %r" % (BACKENDS, backend))
        self.index = index
        self.backend_pref = backend
        self.lock_exposure = lock_exposure
        self.width = width
        self.height = height
        self.backend = None  # "picamera2" | "v4l2" once open() succeeds
        self._picam = None
        self._cap = None

    # -- opening --------------------------------------------------------
    def open(self):
        """Try backends in order and keep the first one that actually
        works. Raises CameraError, with an actionable message, only if all
        of them fail."""
        order = ([self.backend_pref] if self.backend_pref != "auto"
                  else ["picamera2", "v4l2"])
        tried = []
        for name in order:
            try:
                if name == "picamera2":
                    self._open_picamera2()
                else:
                    self._open_v4l2()
                self.backend = name
                log.info("camera backend: %s (%dx%d, lock_exposure=%s)",
                         name, self.width, self.height, self.lock_exposure)
                return
            except Exception as exc:
                tried.append("%s: %s" % (name, exc))
                log.warning("camera backend %r failed: %s", name, exc)

        raise CameraError(
            "no camera backend available (tried: %s). Most likely causes, "
            "in order of how often we've seen them: (1) the CSI ribbon is "
            "seated backwards or only half-inserted -- reseat it, contacts "
            "facing the correct way, and make sure the connector latch is "
            "pressed flush; (2) the camera interface isn't enabled -- "
            "`sudo raspi-config` -> Interface Options -> Camera, then "
            "reboot, or check `camera_auto_detect=1` is in "
            "/boot/firmware/config.txt on Bookworm/Trixie; (3) picamera2 isn't "
            "installed -- `sudo apt install -y python3-picamera2` (it must "
            "come from apt, not pip, because it wraps the system libcamera "
            "build -- pip alone will not work here); and if none of that "
            "applies, plug in a USB webcam as a fallback." % "; ".join(tried)
        )

    def _open_picamera2(self):
        if Picamera2 is None:
            raise CameraError("picamera2 not installed")

        picam = Picamera2(camera_num=self.index or 0)
        try:
            # "RGB888" here is the libcamera/Picamera2 name; see the long
            # comment on to_gray() above for why that's actually BGR bytes
            # and why that's convenient, not a bug.
            config = picam.create_preview_configuration(
                main={"size": (self.width, self.height), "format": "RGB888"}
            )
            picam.configure(config)
            picam.start()
            time.sleep(0.5)  # let AE/AWB converge before we optionally freeze it

            if self.lock_exposure:
                try:
                    # Auto-converge once, then freeze exactly there. Simpler
                    # and more robust on an unknown venue than guessing a
                    # fixed exposure up front, and it still ends with
                    # explicit AeEnable/AwbEnable=False + fixed
                    # ExposureTime/AnalogueGain, which is what actually
                    # stops the mid-run "hunting" that kills ArUco (design
                    # doc §4.1). Falls back to the module constants above
                    # if the metadata read fails for any reason.
                    metadata = picam.capture_metadata()
                    exposure = int(metadata.get("ExposureTime", PICAM_EXPOSURE_US))
                    gain = float(metadata.get("AnalogueGain", PICAM_GAIN))
                    picam.set_controls({
                        "AeEnable": False,
                        "AwbEnable": False,
                        "ExposureTime": exposure,
                        "AnalogueGain": gain,
                    })
                    log.info("picamera2 exposure locked: %d us, gain %.2f",
                             exposure, gain)
                except Exception as exc:
                    log.warning("could not lock picamera2 exposure/white "
                               "balance (continuing with auto): %s", exc)
        except Exception:
            # Don't leave a half-started camera device open if we're about
            # to fall back to the V4L2 backend.
            try:
                picam.close()
            except Exception:
                pass
            raise

        self._picam = picam

    def _open_v4l2(self):
        cap = cv2.VideoCapture(self.index, cv2.CAP_V4L2)
        if not cap.isOpened():
            # CAP_V4L2 may not be compiled into this OpenCV build (rare, but
            # cheap to try) -- fall back to OpenCV's default backend guess.
            cap.release()
            cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            raise CameraError("cv2.VideoCapture(%s) did not open" % self.index)

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # freshest frame, not a stale queued one

        if self.lock_exposure:
            # Best-effort: not every driver/webcam supports every property,
            # and V4L2 property units/semantics vary by driver, so each one
            # is set independently and a failure is only logged.
            for prop, value, name in (
                (cv2.CAP_PROP_AUTO_EXPOSURE, 0.25,
                 "manual exposure mode (V4L2 quirk: 0.25=manual, 0.75=auto)"),
                (cv2.CAP_PROP_EXPOSURE, V4L2_EXPOSURE, "fixed exposure"),
                (cv2.CAP_PROP_AUTO_WB, 0, "auto white balance off"),
            ):
                try:
                    cap.set(prop, value)
                except Exception:
                    log.debug("V4L2 property unsupported: %s", name)

        # isOpened() can be True for a device node that never actually
        # produces data -- confirm a real frame comes back before declaring
        # this backend the winner.
        ok, _ = cap.read()
        if not ok:
            cap.release()
            raise CameraError("V4L2 device %s opened but returned no frame"
                              % self.index)

        self._cap = cap

    # -- runtime ----------------------------------------------------------
    def read_gray(self):
        """Grab one frame and return it as grayscale (H, W) uint8, or None
        on a dropped frame. Never raises -- callers treat None exactly like
        any other dropped frame and just try again next loop tick."""
        if self.backend == "picamera2":
            try:
                frame = self._picam.capture_array("main")
            except Exception as exc:
                log.warning("picamera2 capture failed: %s", exc)
                return None
            return to_gray(frame)

        if self.backend == "v4l2":
            ok, frame = self._cap.read()
            if not ok or frame is None:
                return None
            return to_gray(frame)

        return None

    def close(self):
        if self.backend == "picamera2" and self._picam is not None:
            try:
                self._picam.stop()
                self._picam.close()
            except Exception as exc:
                log.warning("error closing picamera2: %s", exc)
            self._picam = None
        if self.backend == "v4l2" and self._cap is not None:
            try:
                self._cap.release()
            except Exception as exc:
                log.warning("error closing V4L2 capture: %s", exc)
            self._cap = None
        self.backend = None


# ---------------------------------------------------------------------------
def add_camera_args(ap):
    """Attach the shared camera flags to any script's ArgumentParser. Both
    this module's own bench test and nav.py use this, so the flag names and
    defaults only exist in one place."""
    ap.add_argument("--camera-index", type=int, default=0,
                    help="V4L2 device index, or the libcamera camera number "
                         "for Picamera2 (default 0 -- the first camera)")
    ap.add_argument("--camera-backend", choices=list(BACKENDS), default="auto",
                    help="camera stack to use (default: auto -- try "
                         "Picamera2 first, then V4L2/USB webcam)")
    lock = ap.add_mutually_exclusive_group()
    lock.add_argument("--lock-exposure", dest="lock_exposure", action="store_true",
                      default=True,
                      help="freeze exposure/white balance once at startup "
                           "(default -- auto-exposure hunting is the #1 "
                           "ArUco killer, design doc §4.1)")
    lock.add_argument("--no-lock-exposure", dest="lock_exposure", action="store_false",
                      help="leave auto-exposure/AWB running (debugging only)")
    return ap


def main():
    ap = argparse.ArgumentParser(
        description="MEDIC camera bench test: opens the camera and prints "
                    "the backend plus a live mean-brightness reading."
    )
    add_camera_args(ap)
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="stop after N seconds (default 0 = run until Ctrl-C)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s")

    cam = Camera(index=args.camera_index, backend=args.camera_backend,
                lock_exposure=args.lock_exposure)
    try:
        cam.open()
    except CameraError as exc:
        log.error("%s", exc)
        return 1

    log.info("camera open on backend=%s lock_exposure=%s -- Ctrl-C to stop",
             cam.backend, args.lock_exposure)
    start = time.time()
    try:
        while args.seconds <= 0 or time.time() - start < args.seconds:
            gray = cam.read_gray()
            if gray is None:
                log.warning("frame grab failed")
                time.sleep(0.2)
                continue
            log.info("frame %dx%d  mean_brightness=%.1f", gray.shape[1],
                     gray.shape[0], mean_brightness(gray))
            time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        cam.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
