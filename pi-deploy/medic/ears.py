"""ears.py — event-only sound alerting from the USB microphone.

============================================================================
PRIVACY BY DESIGN: LABELS, NEVER AUDIO. This is the entire point of this
file (ARCHITECTURE.md invariant §0.5). ears.py NEVER opens a file for audio,
NEVER writes a .wav, NEVER buffers raw samples beyond one short rolling
analysis window that keeps overwriting itself, and NEVER transmits a single
audio sample anywhere. Every audio block that comes off the microphone is
reduced to a tiny JSON event -- {robot_id, type, label, confidence, ts} --
and the raw samples are discarded within about a second. If a judge asks
"do you record what people say near the robot," the honest answer is no,
by construction, and this file is the proof.
============================================================================

What it does: opens the USB mic, keeps a short rolling window of samples,
runs classify() on that window to decide "loud" or "quiet", and -- only
when "loud" persists for a minimum duration, and the robot is in a
motor-quiet state, and we're not still in the refractory period after the
last alert -- posts exactly one sound_alert event to the dashboard via
medic.common.Central. See docs/http-api-v1.md (event kind `sound_alert`,
config fields `sound_threshold`/`sound_min_ms`/`sound_refractory_s`) and
pi-deploy/DESIGN.md §ears.

The default decision rule is a plain amplitude threshold (see classify()
below) -- the shippable primary per design doc §4.4. A {shout, clap, alarm,
background} classifier is explicitly Day-9-gated and OUT OF SCOPE here;
classify() is left as a documented drop-in hook for that later swap.

BENCH TEST:
    1. Confirm the Pi sees the USB mic (not the HDMI/CSI audio devices that
       often sit at the low indices on Bookworm/Trixie):
         python -m medic.ears --list-devices

    2. Pick a real threshold on the bench -- make normal room noise, then
       shout/clap and watch the peak:
         python -m medic.ears --calibrate

    3. Run it for real (dashboard must be reachable at --central-url,
       default http://127.0.0.1:5000; the MCU is optional -- see the
       motor-noise gate note in SoundAlerter.__init__):
         python -m medic.ears
       Clap once near the mic -> exactly ONE sound_alert event should land
       in the dashboard audit log within ~1 s (label=loud, confidence>0).
       Sit quietly for a few minutes -> zero further events (R8). Talk
       continuously for 10 s -> still only ONE event, not a stream of them
       (that's the refractory period doing its job).
"""

import argparse
import json
import queue
import sys
import time
from collections import deque

try:
    import sounddevice as sd  # PortAudio binding
except ImportError:  # pragma: no cover - depends on the target machine
    sd = None

try:
    import numpy as np
except ImportError:  # pragma: no cover - depends on the target machine
    np = None

from medic.common import (
    add_common_args,
    load_config,
    setup_logging,
    now_ts,
    Central,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How much recent audio classify() is allowed to see. This is the entire
# "rolling analysis window" the privacy invariant talks about: samples older
# than this are dropped from memory every time a new block arrives, so there
# is never more than ~1 s of audio sitting in RAM, and none of it is ever
# written anywhere.
ANALYSIS_WINDOW_S = 1.0

# If the MCU hasn't sent a {"t":"state",...} telemetry line in this long,
# treat it as "no MCU attached" for gating purposes (see _gate_active()).
MCU_STATE_STALE_S = 5.0

# How often to print a quiet "still alive" line so a headless Pi run doesn't
# look hung during a demo rehearsal.
HEALTH_LOG_INTERVAL_S = 30.0

# MCU state-machine names (ARCHITECTURE.md §7.2 / design doc §8) in which the
# drive motors are NOT expected to be turning, i.e. safe to listen without
# picking up motor/gear noise as a false "loud" event. Keep this narrow on
# purpose -- pi-deploy/DESIGN.md says "gate the mic to standby/patrol and
# at-station waits", nothing more.
QUIET_STATES = {"IDLE_AT_PHARMACY", "PATROL", "AT_WARD_WAIT_AUTH"}

QUIET_LABEL = "quiet"
LOUD_LABEL = "loud"

# TODO(day-8): these three starting values are copied straight from the
# frozen API doc's example config (docs/http-api-v1.md) so this script is
# runnable out of the box before the dashboard has ever pushed a config. The
# real numbers depend on the venue's room noise and the actual USB mic's
# sensitivity -- re-pick them on the bench with `--calibrate` once both are
# in hand. Once the dashboard is reachable these are immediately overridden
# by GET /api/v1/config (the sound-threshold slider), live, no restart.
DEFAULT_THRESHOLD = 0.18
DEFAULT_MIN_MS = 120.0
DEFAULT_REFRACTORY_S = 5.0


# ---------------------------------------------------------------------------
# The classifier hook. THIS is the piece a future upgrade replaces.
# ---------------------------------------------------------------------------
def classify(window, threshold):
    """Decide what a short window of audio is. THE drop-in hook.

    Args:
        window: 1-D numpy float32 array, mono, the most recent
            ANALYSIS_WINDOW_S seconds of audio and NOTHING older -- the
            caller trims it every block, so this function never receives
            more audio than the live rolling window. Do not stash a
            reference to it anywhere that outlives the call.
        threshold: current amplitude threshold (0..1-ish RMS scale), live
            from the dashboard config.

    Returns:
        (label: str, confidence: float in [0.0, 1.0])

    Default implementation (the shippable primary, design doc §4.4): a
    plain RMS-energy threshold. `label` is "loud" if the window's RMS is at
    or above `threshold`, else "quiet". `confidence` is how far over the
    threshold it went, normalised into 0..1 (0 = right at the line, 1 = at
    or beyond double the threshold).

    TO UPGRADE (Day 8-9 only, per pi-deploy/DESIGN.md -- explicitly out of scope
    for this file otherwise): replace the body of this function with a
    {shout, clap, alarm, background} classifier (e.g. MFCC features +
    scikit-learn, or an exported Edge Impulse model). Everything else in
    ears.py -- the min-duration/refractory state machine, the motor-noise
    gate, and the event schema posted to the dashboard -- stays exactly the
    same, because it only ever looks at this function's (label, confidence)
    return value. Two rules the replacement MUST keep:
      1. Never return or leak the raw `window` samples anywhere (no saving,
         no logging the array, no network call with it in the body).
      2. `label` must still be a short string and `confidence` still a
         float in [0.0, 1.0] -- callers downstream (this file's
         SoundAlerter._fire, and the dashboard) don't know or care which
         model produced them.
    A label other than "quiet" is treated as "something worth alerting on"
    by the state machine below; a background/silence label should map to
    "quiet" exactly like the threshold rule does, or the refractory logic
    below won't reset correctly between events.
    """
    if window.size == 0:
        return QUIET_LABEL, 0.0
    rms = float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
    if rms < threshold or threshold <= 0:
        return QUIET_LABEL, 0.0
    overshoot = (rms - threshold) / threshold  # 0 at the line, 1 at 2x threshold
    confidence = min(1.0, max(0.0, overshoot))
    return LOUD_LABEL, confidence


# ---------------------------------------------------------------------------
# Small audio-device helpers, shared by the live run and --calibrate.
# ---------------------------------------------------------------------------
def _to_mono(indata):
    """Collapse a possibly-multi-channel PortAudio block to one mono track.

    We average channels rather than just grabbing channel 0 so a
    stereo-only USB mic still gives a representative level.
    """
    if indata.ndim == 1:
        return indata.copy()
    if indata.shape[1] == 1:
        return indata[:, 0].copy()
    return indata.mean(axis=1).astype(np.float32)


def _device_samplerate(device, fallback=16000.0):
    try:
        info = sd.query_devices(device)
        rate = float(info["default_samplerate"])
        if rate > 0:
            return rate
    except Exception:
        pass
    return fallback


def _open_input_stream(device, samplerate, callback, log):
    """Open a mono InputStream, falling back to the device's native channel
    count (downmixed by _to_mono in the callback) if it refuses mono capture.
    Some cheap USB mics only expose stereo capture even though they're
    physically one microphone.
    """
    try:
        return sd.InputStream(
            device=device,
            channels=1,
            samplerate=samplerate,
            dtype="float32",
            callback=callback,
        )
    except Exception as exc:
        log.warning(
            "mono capture failed on this device (%s); retrying with its "
            "native channel count and downmixing in software",
            exc,
        )
    info = sd.query_devices(device)
    channels = max(1, int(info.get("max_input_channels", 1)))
    return sd.InputStream(
        device=device,
        channels=channels,
        samplerate=samplerate,
        dtype="float32",
        callback=callback,
    )


def _print_devices():
    if sd is None:
        print("sounddevice is not installed -- nothing to list.")
        return
    print(sd.query_devices())


def _resolve_device(requested, log):
    """Pick which audio input device to open.

    If --device was given, accept either a numeric PortAudio index or a
    case-insensitive substring of the device name (so `--device usb` works
    without knowing the exact index, which shifts between reboots).

    Otherwise autodetect: on a Raspberry Pi running Bookworm/Trixie the built-in
    HDMI/analog audio devices frequently occupy the low indices and are
    NOT microphones (max_input_channels == 0 for most of them anyway, but
    some HDMI entries claim odd channel counts) -- prefer a device whose
    name contains "usb" over just grabbing index 0.
    """
    if sd is None:
        return None
    try:
        devices = sd.query_devices()
    except Exception as exc:
        log.error("could not query audio devices: %s", exc)
        return None

    if requested:
        try:
            idx = int(requested)
        except ValueError:
            idx = None
        if idx is not None:
            if 0 <= idx < len(devices) and devices[idx]["max_input_channels"] > 0:
                return idx
            log.error("--device %s is not a valid input-capable device index "
                      "(see --list-devices)", requested)
            return None
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0 and requested.lower() in d["name"].lower():
                return i
        log.error("--device %r matched no input-capable device (see "
                  "--list-devices)", requested)
        return None

    usb_candidates = [
        i for i, d in enumerate(devices)
        if d["max_input_channels"] > 0 and "usb" in d["name"].lower()
    ]
    if usb_candidates:
        return usb_candidates[0]

    input_candidates = [i for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    if input_candidates:
        log.warning(
            "no device with 'usb' in its name found -- falling back to the "
            "first input-capable device: %r. Pass --device to pin a "
            "specific mic if this picks the wrong one.",
            devices[input_candidates[0]]["name"],
        )
        return input_candidates[0]

    return None


# ---------------------------------------------------------------------------
# --calibrate: a bench tool, not part of the live pipeline. No network, no
# MCU, no event posting -- it only prints numbers so a human can pick a
# threshold (this is how R8's false-alert budget actually gets tuned).
# ---------------------------------------------------------------------------
def _run_calibrate(device, log):
    samplerate = _device_samplerate(device)
    log.info(
        "calibrating on device=%r samplerate=%.0f -- make normal room noise "
        "for a bit, then shout/clap to see the peak. Ctrl+C when you have a "
        "number you like.",
        device,
        samplerate,
    )

    block_queue = queue.Queue(maxsize=64)

    def _on_block(indata, frames, time_info, status):
        try:
            block_queue.put_nowait(_to_mono(indata))
        except queue.Full:
            pass  # bench tool only -- fine to drop a block under load

    try:
        stream = _open_input_stream(device, samplerate, _on_block, log)
    except Exception as exc:
        log.error("could not open microphone stream: %s", exc)
        return 1

    history = deque()  # (monotonic_ts, rms) for the last ~10 s, for the mean/std
    last_print = 0.0
    with stream:
        try:
            while True:
                try:
                    block = block_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                rms = (
                    float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
                    if block.size
                    else 0.0
                )
                now = time.monotonic()
                history.append((now, rms))
                while history and now - history[0][0] > 10.0:
                    history.popleft()

                if now - last_print >= 0.25:
                    last_print = now
                    values = [v for _, v in history]
                    mean = sum(values) / len(values)
                    variance = sum((v - mean) ** 2 for v in values) / len(values)
                    std = variance ** 0.5
                    peak = max(values)
                    suggested = mean + 4 * std
                    print(
                        "level=%.4f  peak(last %.0fs)=%.4f  "
                        "suggested_threshold~=%.4f (quiet-mean + 4*std)"
                        % (rms, now - history[0][0], peak, suggested)
                    )
        except KeyboardInterrupt:
            print()
            log.info("calibration stopped.")
    return 0


# ---------------------------------------------------------------------------
# The live pipeline.
# ---------------------------------------------------------------------------
class SoundAlerter(object):
    """Owns the mic stream, the detector state machine, and the dashboard
    posting. One instance per process; run() blocks until Ctrl+C.
    """

    def __init__(self, cfg, device, log, config_poll_interval=2.0):
        self.cfg = cfg
        self.device = device
        self.log = log
        self.robot_id = cfg.robot_id

        self.central = Central(cfg.central_url, cfg.robot_id, cfg.http_timeout)

        # Read-only telemetry tap for the motor-noise gate below. ears.py
        # NEVER sends serial commands -- it isn't in the drive or safety
        # path (ARCHITECTURE.md §0.1) -- it only needs to know whether the
        # robot is moving right now.
        #
        # This deliberately does NOT open its own SerialLink to the MCU.
        # task_bridge.py already opens that port and already relays the
        # MCU's {"t":"state",...} line to the dashboard via post_telemetry
        # (see task_bridge.py's _handle_state), which shows up per-robot on
        # GET /api/v1/fleet (docs/http-api-v1.md). A second independent
        # reader on the same tty would race task_bridge for incoming bytes
        # -- pyserial does not give a tty exclusive-lock by default, so both
        # readers would see a corrupted, interleaved JSON-lines stream (this
        # used to be exactly that second reader; see git history / the old
        # INTEGRATION NOTE that lived here). Polling the dashboard instead
        # costs one more HTTP round trip but can never corrupt anyone else's
        # read of the MCU link, and degrades exactly the same way a missing
        # MCU already did: no recent state -> gate goes inert, listening
        # stays always-on (see _gate_active()). This does not touch safety:
        # the gate only ever decides whether ears.py *fires an alert*, never
        # a motor command.
        self._mcu_poll_interval = 1.0  # independent of config_poll_interval
        self._last_mcu_poll = 0.0

        # -- live-tunable detector parameters (dashboard "sound-threshold
        # slider"). Only ever read/written from the main loop thread below
        # (the audio callback thread never touches these), so no lock is
        # needed.
        self.threshold = DEFAULT_THRESHOLD
        self.min_duration_s = DEFAULT_MIN_MS / 1000.0
        self.refractory_s = DEFAULT_REFRACTORY_S
        self._config_rev = None
        self._last_config_poll = 0.0
        self.config_poll_interval = config_poll_interval

        # -- motor-noise gate state --
        self._mcu_state = None
        self._mcu_state_ts = 0.0
        self._gate_inert_logged = False

        # -- rolling analysis window: raw samples, bounded to
        # ANALYSIS_WINDOW_S and nothing more (the privacy invariant). --
        self._window_blocks = deque()
        self._window_samples = 0
        self._max_window_samples = 1  # replaced once we know the samplerate

        # -- fire / refractory state machine --
        self._active_since = None
        self._refractory_until = 0.0

        # -- audio callback -> main thread handoff --
        self._queue = queue.Queue(maxsize=64)
        self._dropped_blocks = 0
        self._last_health_log = 0.0

    # -- audio callback: PortAudio's real-time thread. Keep this tiny: no
    # network I/O, no disk I/O, no logging -- just hand the block off. --
    def _on_audio(self, indata, frames, time_info, status):
        try:
            mono = _to_mono(indata)
            self._queue.put_nowait(mono)
        except Exception:
            self._dropped_blocks += 1

    # -- main-thread work --------------------------------------------------
    def _refresh_config(self, force=False):
        """Pull the dashboard's sound-threshold slider. Cheap and safe to
        call every loop iteration -- it self-rate-limits, and skips the
        actual field updates when config_rev hasn't changed."""
        now = time.monotonic()
        if not force and (now - self._last_config_poll) < self.config_poll_interval:
            return
        self._last_config_poll = now

        data = self.central.get_config()
        if not data:
            return  # dashboard unreachable right now -- keep last-known values

        rev = data.get("config_rev")
        if rev is not None and rev == self._config_rev:
            return  # nothing changed since last time; don't redo the work

        try:
            threshold = float(data.get("sound_threshold", self.threshold))
            min_ms = float(data.get("sound_min_ms", self.min_duration_s * 1000.0))
            refractory = float(data.get("sound_refractory_s", self.refractory_s))
        except (TypeError, ValueError):
            self.log.warning("dashboard sent a malformed config; keeping the "
                             "previous values: %r", data)
            return

        self.threshold = threshold
        self.min_duration_s = min_ms / 1000.0
        self.refractory_s = refractory
        self._config_rev = rev
        self.log.info(
            "config updated (rev=%s): threshold=%.3f min_ms=%.0f refractory_s=%.1f",
            rev, self.threshold, self.min_duration_s * 1000.0, self.refractory_s,
        )

    def _refresh_mcu_state(self):
        # Central.get_fleet_self() never raises and never blocks the caller
        # beyond cfg.http_timeout -- safe to call from the main loop. Rate
        # limited the same way _refresh_config() is: this is a gate input,
        # not a safety decision, so it does not need to be checked every
        # tick.
        now = time.monotonic()
        if now - self._last_mcu_poll < self._mcu_poll_interval:
            return
        self._last_mcu_poll = now

        robot = self.central.get_fleet_self()
        s = robot.get("state")
        if s:
            self._mcu_state = str(s)
            self._mcu_state_ts = now

    def _gate_active(self):
        """True = OK to listen right now. False = robot is (probably)
        moving and motor/gear noise would drown out or fake a real alert.

        No MCU attached is a SUPPORTED state (the ESP32 isn't wired up
        yet): default to always-listening so bench testing works, but log
        once that the gate is doing nothing so nobody mistakes silence for
        "it's working."
        """
        stale = (
            self._mcu_state is None
            or (time.monotonic() - self._mcu_state_ts) > MCU_STATE_STALE_S
        )
        if stale:
            if not self._gate_inert_logged:
                self.log.info(
                    "motor-noise gate is inert (no recent MCU 'state' "
                    "telemetry) -- listening stays ON always. Expected "
                    "until the ESP32 is wired up and sending state; bench "
                    "testing works fine either way."
                )
                self._gate_inert_logged = True
            return True
        self._gate_inert_logged = False
        return self._mcu_state.upper() in QUIET_STATES

    def _process_block(self, block):
        # Grow the rolling window, then trim from the front so it never
        # holds more than ANALYSIS_WINDOW_S seconds -- this is the "never
        # keep samples beyond the live rolling analysis window" rule.
        self._window_blocks.append(block)
        self._window_samples += len(block)
        while (
            self._window_samples > self._max_window_samples
            and len(self._window_blocks) > 1
        ):
            old = self._window_blocks.popleft()
            self._window_samples -= len(old)
        window = np.concatenate(self._window_blocks)

        label, confidence = classify(window, self.threshold)

        now = time.monotonic()
        gate_open = self._gate_active()

        if label != QUIET_LABEL and gate_open:
            if self._active_since is None:
                self._active_since = now
        else:
            # Either it went quiet, or the gate closed (robot is moving) --
            # either way, don't let a loud moment carry across a gap.
            self._active_since = None

        # NOTE: compare to None explicitly, not truthiness -- a monotonic
        # timestamp of exactly 0.0 is a legitimate "started right now" value
        # (e.g. right after process start) and must not be treated as unset.
        active_duration = (
            now - self._active_since if self._active_since is not None else 0.0
        )

        if (
            active_duration >= self.min_duration_s
            and now >= self._refractory_until
            and gate_open
            and label != QUIET_LABEL
        ):
            self._fire(label, confidence)
            self._refractory_until = now + self.refractory_s
            self._active_since = None  # one shout -> exactly one event

    def _fire(self, label, confidence):
        # THE schema (ARCHITECTURE.md §0.5 / pi-deploy/DESIGN.md): robot_id, type,
        # label, confidence, ts -- nothing else, and definitely no audio.
        event = {
            "robot_id": self.robot_id,
            "type": "sound",
            "label": label,
            "confidence": round(confidence, 3),
            "ts": now_ts(),
        }
        self.log.warning("sound_alert label=%s confidence=%.2f", label, confidence)
        # Central.post_event wraps this in the dashboard's generic events
        # envelope (kind="sound_alert" -> severity "warn" per common.SEVERITY,
        # matching docs/http-api-v1.md's canonical event-kind table); the
        # detail carries the exact {type,label,confidence,ts} sub-schema so
        # the audit log entry is self-explanatory without a lookup table.
        detail = json.dumps({k: v for k, v in event.items() if k != "robot_id"})
        self.central.post_event("sound_alert", detail=detail)

    def _maybe_log_health(self):
        now = time.monotonic()
        if now - self._last_health_log < HEALTH_LOG_INTERVAL_S:
            return
        self._last_health_log = now
        self.log.info(
            "alive: gate_state=%s dropped_blocks=%d central_offline=%s",
            self._mcu_state or "unknown", self._dropped_blocks, self.central.offline,
        )

    def run(self):
        samplerate = _device_samplerate(self.device)
        self._max_window_samples = max(1, int(samplerate * ANALYSIS_WINDOW_S))
        self._refresh_config(force=True)

        try:
            stream = _open_input_stream(self.device, samplerate, self._on_audio, self.log)
        except Exception as exc:
            self.log.error(
                "could not open the microphone stream: %s. Check the USB mic "
                "is plugged in and not already held by another program "
                "(e.g. another instance of this script), then retry. "
                "`python -m medic.ears --list-devices` shows what the Pi "
                "currently sees.",
                exc,
            )
            return 1

        self.log.info(
            "ears listening (device=%r samplerate=%.0f) -- events only, no "
            "audio is ever recorded, buffered beyond %.1fs, or transmitted",
            self.device, samplerate, ANALYSIS_WINDOW_S,
        )

        with stream:
            try:
                while True:
                    self._refresh_config()
                    self._refresh_mcu_state()
                    self._maybe_log_health()
                    try:
                        block = self._queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    try:
                        self._process_block(block)
                    except Exception:
                        self.log.exception(
                            "error processing one audio block -- skipping it "
                            "and continuing (never crash the whole listener "
                            "over one bad block)"
                        )
            except KeyboardInterrupt:
                self.log.info("ears stopping (Ctrl+C)")
        return 0


# ---------------------------------------------------------------------------
def build_arg_parser():
    ap = argparse.ArgumentParser(
        description="MEDIC ears.py -- event-only sound alerting from the USB mic."
    )
    add_common_args(ap)
    ap.add_argument(
        "--device", default=None,
        help="Audio input device: a PortAudio index (see --list-devices) or a "
             "case-insensitive substring of its name (e.g. 'usb'). Default: "
             "autodetect a USB mic.",
    )
    ap.add_argument(
        "--list-devices", action="store_true",
        help="Print every audio device the Pi can see, then exit.",
    )
    ap.add_argument(
        "--calibrate", action="store_true",
        help="Bench mode: print the live mic level (and a suggested threshold) "
             "instead of running the real detector. No network, no MCU.",
    )
    ap.add_argument(
        "--config-poll-interval", type=float, default=2.0,
        help="Seconds between checks of the dashboard's sound-threshold slider "
             "(default: 2.0). This is deliberately slow -- the audio loop "
             "itself runs far faster and never waits on this.",
    )
    return ap


def main(argv=None):
    ap = build_arg_parser()
    args = ap.parse_args(argv)
    cfg = load_config(args)
    log = setup_logging(cfg.log_level, "ears")

    if sd is None:
        log.error(
            "sounddevice is not installed. On the Pi: `pip install sounddevice` "
            "inside the project venv (PEP 668 blocks a system-wide install on "
            "Bookworm/Trixie) -- it also needs PortAudio: `sudo apt install "
            "libportaudio2`. Exiting cleanly; the other MEDIC processes are "
            "unaffected."
        )
        return 1
    if np is None:
        log.error(
            "numpy is not installed. `pip install numpy` inside the project "
            "venv. Exiting cleanly; the other MEDIC processes are unaffected."
        )
        return 1

    if args.list_devices:
        _print_devices()
        return 0

    device = _resolve_device(args.device, log)
    if device is None:
        log.error(
            "No usable audio input device found. Plug in the USB microphone "
            "and re-run, or check `python -m medic.ears --list-devices` to "
            "see what the Pi currently sees. Exiting cleanly; the other "
            "MEDIC processes are unaffected."
        )
        return 1

    if args.calibrate:
        return _run_calibrate(device, log)

    alerter = SoundAlerter(cfg, device, log, config_poll_interval=args.config_poll_interval)
    return alerter.run()


if __name__ == "__main__":
    sys.exit(main())
