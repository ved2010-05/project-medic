"""common.py — shared plumbing for the three MEDIC Pi processes.

nav.py, ears.py and task_bridge.py all import this. It holds exactly three things:
config resolution, the serial pipe to the MCU, and the HTTP client for the
dashboard. Nothing else belongs here.

TWO RULES THIS MODULE MUST NEVER BREAK
  1. It contains NO safety logic. Obstacle stop, E-stop and lost-comms safe-hold
     live on the ESP32 and nowhere else (ARCHITECTURE.md §0.1). SerialLink is a
     dumb pipe.
  2. Central.verify_auth() FAILS CLOSED. Every error path returns a refusal
     (§0.4). There is no code path through it that returns authorized=True
     without the dashboard explicitly saying so.

NO MCU ATTACHED IS A SUPPORTED STATE. The ESP32 is not wired up yet, so
SerialLink degrades quietly: it logs once, keeps retrying in the background, and
every send() becomes a no-op. Nothing crashes and nothing blocks.

BENCH TEST:
    python -m medic.common --selftest
  Prints the resolved config, whether a serial port was found, and whether the
  dashboard answered. Run this first on a fresh Pi — it catches a bad port path
  or a dashboard that isn't up before you go hunting through nav.py.
"""

import argparse
import glob
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover
    serial = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

LOG = logging.getLogger("medic")

# ---------------------------------------------------------------------------
# Event kinds — must match docs/http-api-v1.md exactly.
# ---------------------------------------------------------------------------
INFO, WARN, RED = "info", "warn", "red"

SEVERITY = {
    "robot_online": INFO,
    "robot_offline": WARN,
    "dispatch": INFO,
    "depart": INFO,
    "marker_seen": INFO,
    "marker_lost": RED,
    "station_mismatch": RED,
    "arrive": INFO,
    "scan_staff": INFO,
    "scan_patient": INFO,
    "auth_ok": INFO,
    "auth_refused": RED,
    "auth_timeout": RED,
    "dispense_ok": INFO,
    "dispense_fail": RED,
    "latch_open": INFO,
    "latch_close": INFO,
    "temp_reading": INFO,
    "temp_excursion": WARN,
    "sound_alert": WARN,
    "obstacle_hold": WARN,
    "estop": RED,
    "safehold_comms": RED,
    "teleop_nudge": WARN,
    "task_complete": INFO,
}


def now_ts():
    """ISO-8601 UTC. The dashboard also stamps its own receive time, so a wrong
    Pi clock can never punch a hole in the audit trail."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Config — precedence is: command-line flag > environment variable > default.
# ---------------------------------------------------------------------------
DEFAULTS = {
    "robot_id": "medic-01",
    # The dashboard runs ON the Pi in this deployment.
    "central_url": "http://127.0.0.1:5000",
    "serial_port": "auto",  # "auto" scans; or pin a /dev/serial/by-id/... path
    "serial_baud": 115200,
    "http_timeout": 2.0,
    "poll_interval": 0.5,  # dashboard poll, 500 ms (design doc §4)
    "heartbeat_interval": 0.75,  # MCU safe-holds at 2 s; stay well inside
    "log_level": "INFO",
}

ENV_PREFIX = "MEDIC_"


class Config(object):
    def __init__(self, values):
        self.__dict__.update(values)

    def __repr__(self):
        return "Config(%s)" % json.dumps(self.__dict__, indent=2, default=str)


def add_common_args(ap):
    """Attach the shared flags to any script's ArgumentParser."""
    ap.add_argument("--robot-id")
    ap.add_argument("--central-url")
    ap.add_argument("--serial-port")
    ap.add_argument("--log-level")
    return ap


def load_config(args=None):
    values = dict(DEFAULTS)

    # environment overrides defaults
    for key in DEFAULTS:
        env = os.environ.get(ENV_PREFIX + key.upper())
        if env is not None and env != "":
            default = DEFAULTS[key]
            try:
                if isinstance(default, bool):
                    values[key] = env.lower() in ("1", "true", "yes", "on")
                elif isinstance(default, int):
                    values[key] = int(env)
                elif isinstance(default, float):
                    values[key] = float(env)
                else:
                    values[key] = env
            except ValueError:
                LOG.warning("bad value for %s%s: %r (ignored)", ENV_PREFIX,
                            key.upper(), env)

    # flags override environment
    if args is not None:
        for key in DEFAULTS:
            got = getattr(args, key, None)
            if got is not None:
                values[key] = got

    return Config(values)


def setup_logging(level="INFO", name=None):
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    return logging.getLogger(name or "medic")


# ---------------------------------------------------------------------------
# Serial link to the ESP32 — a dumb pipe, no safety logic (§0.1).
# ---------------------------------------------------------------------------
SERIAL_GLOBS = [
    "/dev/serial/by-id/*",  # stable across reboots — always prefer this
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
]


def find_serial_port():
    """Best-effort autodetect. Returns a path or None."""
    for pattern in SERIAL_GLOBS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


class SerialLink(object):
    """Newline-delimited JSON to the MCU. See docs/serial-protocol-v1.md.

    Never raises at the call site. If the port is missing or dies, send() is a
    no-op and poll() returns []; a background reconnect keeps trying. That is
    what makes "ESP32 not wired up yet" a supported state rather than a crash.
    """

    RECONNECT_S = 3.0

    def __init__(self, port="auto", baud=115200, enabled=True):
        self.requested = port
        self.baud = baud
        self.enabled = enabled
        self.ser = None
        self.path = None
        self._buf = b""
        self._last_try = 0.0
        self._warned = False
        self._lock = threading.Lock()
        if enabled:
            self._connect()

    # -- connection ---------------------------------------------------------
    def _connect(self):
        now = time.time()
        if now - self._last_try < self.RECONNECT_S:
            return False
        self._last_try = now

        if serial is None:
            if not self._warned:
                LOG.error("pyserial not installed — running with no MCU link")
                self._warned = True
            return False

        path = self.requested
        if path in (None, "", "auto"):
            path = find_serial_port()
        if not path or not os.path.exists(path):
            if not self._warned:
                LOG.warning("no MCU serial port found — continuing without it "
                            "(this is expected until the ESP32 is wired up)")
                self._warned = True
            return False

        try:
            # DO NOT let pyserial assert DTR/RTS. On an ESP32 DevKit the
            # auto-reset circuit wires RTS -> EN (reset) and DTR -> IO0 (boot
            # select). pyserial asserts BOTH by default on open, which on this
            # board holds the ESP32 in reset: the USB bridge stays enumerated,
            # the port opens cleanly, every write "succeeds" — and the MCU is
            # dead silent the whole time. Diagnosed on the bench: opening with
            # these deasserted made the chip boot and answer a ping instantly.
            #
            # Must be configured BEFORE open(), which is why this builds the
            # object and opens it manually rather than using the
            # Serial(port, baud) one-liner constructor.
            ser = serial.Serial()
            ser.port = path
            ser.baudrate = self.baud
            ser.timeout = 0
            ser.dtr = False
            ser.rts = False
            ser.open()
            # Belt and braces: some drivers ignore the pre-open attributes, so
            # reassert once the handle exists.
            try:
                ser.dtr = False
                ser.rts = False
            except Exception:
                pass  # not every driver exposes these; not fatal

            self.ser = ser
            self.path = path
            self._buf = b""
            self._warned = False
            # The ESP32 may still reset when the port opens on some boards. Do
            # NOT sleep here — callers are in a loop; the first second of
            # telemetry is simply discarded.
            LOG.info("MCU serial open on %s @ %d (DTR/RTS held low)", path, self.baud)
            return True
        except Exception as exc:
            if not self._warned:
                LOG.warning("could not open %s: %s", path, exc)
                self._warned = True
            self.ser = None
            return False

    @property
    def connected(self):
        return self.ser is not None

    def _drop(self, exc):
        LOG.warning("MCU serial lost (%s) — will retry", exc)
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self._buf = b""

    # -- io -----------------------------------------------------------------
    def send(self, obj):
        """Write one JSON object as a line. Returns True if it actually went out."""
        if not self.enabled:
            return False
        with self._lock:
            if self.ser is None and not self._connect():
                return False
            line = json.dumps(obj, separators=(",", ":")) + "\n"
            try:
                self.ser.write(line.encode("ascii", "ignore"))
                return True
            except Exception as exc:
                self._drop(exc)
                return False

    def poll(self):
        """Return a list of parsed MCU messages. Never blocks, never raises."""
        if not self.enabled:
            return []
        with self._lock:
            if self.ser is None and not self._connect():
                return []
            out = []
            try:
                chunk = self.ser.read(4096)
            except Exception as exc:
                self._drop(exc)
                return out
            if chunk:
                self._buf += chunk
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw.decode("ascii", "ignore")))
                except ValueError:
                    pass  # malformed line: ignore, exactly as the MCU does
            # A pathological partial line must not grow without bound.
            if len(self._buf) > 4096:
                self._buf = b""
            return out

    def close(self):
        with self._lock:
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None


# ---------------------------------------------------------------------------
# Dashboard HTTP client. See docs/http-api-v1.md.
# ---------------------------------------------------------------------------
class Refusal(dict):
    """An authorized=False result. Exists so a refusal is impossible to mistake
    for a success by accident — it is falsey-by-field, never by omission."""

    def __init__(self, reason):
        super(Refusal, self).__init__(
            authorized=False, reason=reason, dispense_count=0,
            open_latch=False, event_id=None,
        )


class Central(object):
    """Talks to the dashboard. Every method swallows transport errors and
    returns a safe default — a dashboard outage must never crash a Pi script."""

    def __init__(self, base_url, robot_id, timeout=2.0):
        self.base = base_url.rstrip("/")
        self.robot_id = robot_id
        self.timeout = timeout
        self._session = requests.Session() if requests else None
        self._down_since = None

    # -- low level ----------------------------------------------------------
    def _request(self, method, path, **kw):
        if self._session is None:
            return None
        url = self.base + path
        kw.setdefault("timeout", self.timeout)
        try:
            resp = self._session.request(method, url, **kw)
        except Exception as exc:
            if self._down_since is None:
                self._down_since = time.time()
                LOG.warning("dashboard unreachable (%s): %s", url, exc)
            return None
        if self._down_since is not None:
            LOG.info("dashboard reachable again after %.1fs",
                     time.time() - self._down_since)
            self._down_since = None
        if resp.status_code // 100 != 2:
            LOG.warning("%s %s -> HTTP %s", method, path, resp.status_code)
            return None
        try:
            return resp.json()
        except ValueError:
            LOG.warning("%s %s -> non-JSON body", method, path)
            return None

    @property
    def offline(self):
        return self._down_since is not None

    # -- endpoints ----------------------------------------------------------
    def get_active_task(self):
        data = self._request("GET", "/api/v1/tasks/active",
                             params={"robot_id": self.robot_id})
        if not data:
            return None
        return data.get("task", data if "task_id" in data else None)

    def set_task_state(self, task_id, state, detail=""):
        return self._request("POST", "/api/v1/tasks/%s/state" % task_id,
                             json={"robot_id": self.robot_id, "state": state,
                                   "detail": detail}) is not None

    def post_event(self, kind, detail="", task_id=None, severity=None):
        payload = {
            "robot_id": self.robot_id,
            "kind": kind,
            "severity": severity or SEVERITY.get(kind, INFO),
            "detail": detail,
            "ts": now_ts(),
        }
        if task_id is not None:
            payload["task_id"] = task_id
        return self._request("POST", "/api/v1/events", json=payload) is not None

    def post_events(self, events):
        if not events:
            return True
        return self._request("POST", "/api/v1/events",
                             json={"events": events}) is not None

    def post_telemetry(self, **fields):
        fields.setdefault("robot_id", self.robot_id)
        fields.setdefault("ts", now_ts())
        return self._request("POST", "/api/v1/telemetry", json=fields) is not None

    def post_map_sighting(self, marker_ids):
        """Teach mode: report markers seen together in ONE frame.

        Co-visibility is the only thing that makes an edge, so this is only
        worth sending when 2+ markers are in the same frame. Fire-and-forget:
        a dropped sighting just means one less observation, and the dashboard
        needs several before it trusts a link anyway.
        """
        if not marker_ids or len(marker_ids) < 2:
            return False
        return self._request(
            "POST", "/api/v1/map/sighting",
            json={"robot_id": self.robot_id, "markers": sorted(int(m) for m in marker_ids)},
        ) is not None

    def get_config(self):
        return self._request("GET", "/api/v1/config",
                             params={"robot_id": self.robot_id}) or {}

    def get_teleop(self):
        return self._request("GET", "/api/v1/teleop",
                             params={"robot_id": self.robot_id}) or {"cmd": None}

    def get_fleet_self(self):
        """This robot's own row from GET /api/v1/fleet (docs/http-api-v1.md:
        {robot_id, state, ovr, last_seen, online, temp_c, task_id}), or {}
        if unreachable or not reporting yet.

        This is a browser-facing endpoint in the doc's table, but it's just
        a GET on the same Flask app -- nothing stops a Pi-side process from
        polling it too, and it is how ears.py reads the MCU's current state
        (relayed here by task_bridge.py's post_telemetry) without opening a
        second, conflicting serial connection to the MCU itself. See
        ears.py's motor-noise gate.
        """
        data = self._request("GET", "/api/v1/fleet")
        if not data or not isinstance(data.get("robots"), list):
            return {}
        for robot in data["robots"]:
            if robot.get("robot_id") == self.robot_id:
                return robot
        return {}

    def verify_auth(self, task_id, staff_uid, patient_uid):
        """THE fail-closed gate (§0.4).

        Returns a dict with at least 'authorized'. EVERY failure mode — network
        down, timeout, HTTP error, non-JSON body, missing field, wrong type —
        returns a Refusal. There is deliberately no branch here that can produce
        authorized=True on its own.
        """
        if not task_id or not staff_uid or not patient_uid:
            return Refusal("malformed_request")

        data = self._request(
            "POST", "/api/v1/auth/verify",
            json={"robot_id": self.robot_id, "task_id": task_id,
                  "staff_uid": staff_uid, "patient_uid": patient_uid},
        )
        if data is None:
            # Unreachable / error / bad body. Refuse — never assume.
            return Refusal("central_unreachable")
        if data.get("authorized") is not True:
            # Note `is not True`: a truthy string or 1 must not slip through.
            return Refusal(str(data.get("reason", "refused")))

        try:
            count = int(data.get("dispense_count", 0))
        except (TypeError, ValueError):
            return Refusal("malformed_response")
        if count < 0:
            return Refusal("malformed_response")

        return {
            "authorized": True,
            "reason": str(data.get("reason", "match")),
            "dispense_count": count,
            "open_latch": bool(data.get("open_latch", False)),
            "event_id": data.get("event_id"),
        }


# ---------------------------------------------------------------------------
def _selftest():
    ap = argparse.ArgumentParser(description="MEDIC common self-test")
    add_common_args(ap)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args)
    setup_logging(cfg.log_level, "selftest")

    print("--- config ---")
    print(cfg)

    print("\n--- serial ---")
    found = find_serial_port()
    print("autodetected port : %s" % (found or "NONE (expected until the ESP32 "
                                              "is wired up)"))
    link = SerialLink(cfg.serial_port, cfg.serial_baud)
    print("connected         : %s" % link.connected)
    if link.connected:
        link.send({"t": "ping"})
        time.sleep(0.3)
        print("reply             : %s" % link.poll())
    link.close()

    print("\n--- dashboard ---")
    central = Central(cfg.central_url, cfg.robot_id, cfg.http_timeout)
    cfgdata = central.get_config()
    print("url               : %s" % cfg.central_url)
    print("reachable         : %s" % (not central.offline))
    print("config            : %s" % (cfgdata or "(none)"))

    print("\n--- fail-closed check ---")
    bad = Central("http://127.0.0.1:1", cfg.robot_id, 0.3)
    result = bad.verify_auth(1, "AAAA", "BBBB")
    print("unreachable verify -> %s" % result)
    assert result["authorized"] is False, "FAIL-CLOSED VIOLATED"
    print("OK: an unreachable dashboard refuses.")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
