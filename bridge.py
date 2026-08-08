#!/usr/bin/env python3
"""rtlamr -> MQTT bridge, drop-in compatible with rtl_433's MQTT output.

Reads rtlamr's JSON output, converts each message to the shape rtl_433 would
have published for the same meter, and publishes to the same MQTT topics.
Existing dashboards and automations built on rtl_433 topics keep working,
and meters rtl_433 cannot decode (notably some Neptune R900 variants, and
BCD-encoded R900s that rtl_433 misparses) appear alongside with correct
readings.

Configuration is entirely via environment variables; see README.md.
"""

import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt

log = logging.getLogger("rtlamr-mqtt-bridge")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_bool(name, default=False):
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


MQTT_HOST = env("MQTT_HOST", "localhost")
MQTT_PORT = int(env("MQTT_PORT", "1883"))
MQTT_USER = env("MQTT_USER")
MQTT_PASSWORD = env("MQTT_PASSWORD")
MQTT_RETAIN = env_bool("MQTT_RETAIN", False)
MQTT_BASE_TOPIC = env("MQTT_BASE_TOPIC", "rtl_433").rstrip("/")
EVENTS_TOPIC = env("MQTT_EVENTS_TOPIC", MQTT_BASE_TOPIC + "/events")
DEVICES_TOPIC = env("MQTT_DEVICES_TOPIC",
                    MQTT_BASE_TOPIC + "/devices/{model}/{id}")
STATUS_TOPIC = env("MQTT_STATUS_TOPIC", MQTT_BASE_TOPIC + "/bridge/status")

RTLAMR_BIN = env("RTLAMR_BIN", "rtlamr")
RTLAMR_MSGTYPE = env("RTLAMR_MSGTYPE", "r900,r900bcd")
RTLAMR_SERVER = env("RTLAMR_SERVER", "127.0.0.1:1234")
RTLAMR_CENTERFREQ = env("RTLAMR_CENTERFREQ")
RTLAMR_FILTER_ID = env("RTLAMR_FILTER_ID")
RTLAMR_ARGS = env("RTLAMR_ARGS", "")
# Symbol length sets rtlamr's sample rate (rate = 32768 * symbollength), and the
# decoder cost scales with it. rtlamr's own default of 72 means 2.36 MS/s, which
# many hosts cannot demodulate in real time: rtlamr then pegs a core and floods
# stderr with "not keeping up with rtl_tcp" (measured: ~45% of a core, endless
# drop errors). 32 -> 1.05 MS/s decodes the same meters at ~31% with no drops.
# Raise it (48/72) if a distant meter stops being heard; lower it (8) to trade
# more sensitivity for less CPU.
RTLAMR_SYMBOLLENGTH = env("RTLAMR_SYMBOLLENGTH", "32")
# rtlamr logs one drop line per lagging block, which can reach millions of lines
# a day and fill the container log. Collapse repeats into one summary per window.
RTLAMR_DROP_SUMMARY_INTERVAL = float(env("RTLAMR_DROP_SUMMARY_INTERVAL", "300"))

RTL_TCP_SPAWN = env_bool("RTL_TCP_SPAWN", True)
RTL_TCP_BIN = env("RTL_TCP_BIN", "rtl_tcp")
RTL_TCP_ARGS = env("RTL_TCP_ARGS", "")

# R900 consumption encoding handling: auto | binary | bcd  (see README)
R900_MODE = env("R900_MODE", "auto")
R900_BCD_IDS = {s.strip() for s in env("R900_BCD_IDS", "").split(",") if s.strip()}
R900_BINARY_IDS = {s.strip() for s in env("R900_BINARY_IDS", "").split(",") if s.strip()}
R900_PAIR_WINDOW = float(env("R900_PAIR_WINDOW", "2.0"))

RESTART_DELAY = float(env("RESTART_DELAY", "5.0"))

# Robustness: rtl_tcp keeps running (and serving an empty sample stream) when
# the USB dongle re-enumerates, so rtlamr connects but reads nothing and loops
# forever. The supervisor below recovers by recycling rtl_tcp -- forcing it to
# re-open the device -- whenever rtlamr exits or meter data goes stale.
WATCHDOG_TIMEOUT = float(env("WATCHDOG_TIMEOUT", "300"))    # 0 disables
WATCHDOG_INTERVAL = float(env("WATCHDOG_INTERVAL", "30"))
# Don't recycle more than once per cooldown, so a watchdog recycle and the
# rtlamr-exit it triggers don't kill each other's fresh rtl_tcp.
RECYCLE_COOLDOWN = float(env("RECYCLE_COOLDOWN", str(max(RESTART_DELAY * 2, 10))))

# Heartbeat file: holds the epoch time of the last meter message. The Docker
# HEALTHCHECK (python bridge.py --healthcheck) marks the container unhealthy
# when this goes stale, so external monitoring / autoheal can act too.
HEARTBEAT_FILE = env("HEARTBEAT_FILE", "/tmp/rtlamr-bridge.heartbeat")
HEALTHCHECK_MAX_AGE = float(env("HEALTHCHECK_MAX_AGE", "600"))


# --------------------------------------------------------------------------
# rtlamr -> rtl_433 message mapping
# --------------------------------------------------------------------------

def _hex(value, digits=2):
    return "0x%0*X" % (digits, int(value or 0))


# rtl_433 src/devices/scmplus.c meter-type table (low nibble of endpoint type)
SCMPLUS_METER_TYPE = {
    4: "Electric", 5: "Electric", 7: "Electric", 8: "Electric",
    0: "Gas", 1: "Gas", 2: "Gas", 9: "Gas", 12: "Gas",
    3: "Water", 11: "Water", 13: "Water",
}


def map_r900(msg, consumption=None, encoding=None):
    """Neptune R900 -> rtl_433 'Neptune-R900'.

    rtl_433 reference output:
      {"model":"Neptune-R900","id":...,"unkn1":...,"unkn2":...,"nouse":...,
       "backflow":...,"consumption":...,"unkn3":...,"leak":...,"leaknow":...}
    rtlamr has no unkn2/extra equivalents; they are omitted.
    """
    out = {
        "model": "Neptune-R900",
        "id": msg["ID"],
        "unkn1": msg.get("Unkn1"),
        "nouse": msg.get("NoUse"),
        "backflow": msg.get("BackFlow"),
        "consumption": msg["Consumption"] if consumption is None else consumption,
        "unkn3": msg.get("Unkn3"),
        "leak": msg.get("Leak"),
        "leaknow": msg.get("LeakNow"),
    }
    if encoding:
        out["r900_encoding"] = encoding  # bridge extra; not in rtl_433 output
    return out


def map_scm(msg):
    """Itron SCM -> rtl_433 'ERT-SCM'."""
    return {
        "model": "ERT-SCM",
        "id": msg["ID"],
        "physical_tamper": msg.get("TamperPhy"),
        "ert_type": msg.get("Type"),
        "encoder_tamper": msg.get("TamperEnc"),
        "consumption_data": msg.get("Consumption"),
    }


def map_scmplus(msg):
    """Itron SCM+ -> rtl_433 'SCMplus'."""
    endpoint_type = int(msg.get("EndpointType") or 0)
    return {
        "model": "SCMplus",
        "id": msg.get("EndpointID"),
        "ProtocolID": _hex(msg.get("ProtocolID")),
        "EndpointType": _hex(endpoint_type),
        "EndpointID": msg.get("EndpointID"),
        "Consumption": msg.get("Consumption"),
        "Tamper": _hex(msg.get("Tamper"), 4),
        "PacketCRC": _hex(msg.get("PacketCRC"), 4),
        "MeterType": SCMPLUS_METER_TYPE.get(endpoint_type & 0x0F, "unknown"),
    }


def _snake(name):
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and (not name[i - 1].isupper()
                                   or (i + 1 < len(name) and name[i + 1].islower())):
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def map_generic(msg, model):
    """Best-effort mapping for message types without a verified rtl_433
    reference capture (IDM, NetIDM): snake_case all scalar fields."""
    out = {"model": model,
           "id": msg.get("ERTSerialNumber") or msg.get("EndpointID") or msg.get("ID")}
    for key, value in msg.items():
        if isinstance(value, (list, dict)):
            continue  # interval arrays etc. don't fit per-field topics
        out.setdefault(_snake(key), value)
    return out


def map_message(msg_type, msg):
    """Map one rtlamr message to a rtl_433-shaped dict (model/id first).
    R900/R900BCD are NOT handled here -- they go through R900Resolver."""
    if msg_type == "SCM":
        return map_scm(msg)
    if msg_type == "SCM+":
        return map_scmplus(msg)
    if msg_type == "IDM":
        return map_generic(msg, "ERT-IDM")
    if msg_type == "NetIDM":
        return map_generic(msg, "ERT-NetIDM")
    return map_generic(msg, msg_type)


# --------------------------------------------------------------------------
# R900 BCD-vs-binary resolution
# --------------------------------------------------------------------------
#
# Older Neptune R900 MIUs encode the consumption odometer as BCD (one decimal
# digit per nibble); newer ones use a plain binary integer. A BCD reading
# misparsed as binary yields its decimal digits read as hex, e.g. a true
# reading of 16000 shows up as 0x16000 = 90112 (this is exactly rtl_433's
# bug as of mid-2026). When both r900 and r900bcd msgtypes are enabled,
# every transmission decodes under both parsers; this resolver pairs the two
# decodes and decides, once per meter ID, which interpretation is real:
#
#   - BCD parse == 0 while binary parse != 0  ->  binary meter
#     (the BCD parser yields 0 when a nibble exceeds 9)
#   - binary parse == int(str(bcd parse), 16) ->  BCD meter
#     (the misparse identity holds on every packet for true-BCD meters; for
#     a binary meter it requires every hex digit of the reading to be <= 9,
#     so a handful of packets makes the call unambiguous in practice)
#
# Ambiguous IDs can be forced with R900_BCD_IDS / R900_BINARY_IDS.

def is_bcd_misparse(binary_value, bcd_value):
    """True if binary_value is bcd_value's decimal digits read as hex."""
    if bcd_value is None or binary_value is None:
        return False
    try:
        return int(binary_value) == int(str(int(bcd_value)), 16)
    except (ValueError, TypeError):
        return False


class R900Resolver:
    """Pairs R900/R900BCD decodes of the same transmission and emits one
    rtl_433-shaped event with the correct consumption value."""

    def __init__(self, mode, pairing, bcd_ids=(), binary_ids=(), window=2.0):
        self.mode = mode
        self.pairing = pairing  # both msgtypes enabled and mode == auto
        self.window = window
        self.decided = {}       # id -> "binary" | "bcd"
        self.pending = {}       # id -> {"binary"|"bcd": (msg, time, mono)}
        self.lock = threading.Lock()
        for id_ in bcd_ids:
            self.decided[str(id_)] = "bcd"
        for id_ in binary_ids:
            self.decided[str(id_)] = "binary"

    @staticmethod
    def _event(msg, encoding):
        return map_r900(msg, encoding=encoding)

    def feed(self, msg_type, msg, msg_time):
        """Returns a list of (event, time) ready to publish."""
        kind = "bcd" if msg_type == "R900BCD" else "binary"
        meter_id = str(msg.get("ID"))

        if self.mode in ("binary", "bcd"):
            return [(self._event(msg, kind), msg_time)] if kind == self.mode else []

        with self.lock:
            decided = self.decided.get(meter_id)
            if decided:
                if kind == decided:
                    return [(self._event(msg, kind), msg_time)]
                return []

            if not self.pairing:
                # only one R900 msgtype enabled; nothing to pair against
                return [(self._event(msg, kind), msg_time)]

            slot = self.pending.setdefault(meter_id, {})
            slot[kind] = (msg, msg_time, time.monotonic())
            if "binary" not in slot or "bcd" not in slot:
                return []

            binary_msg, binary_time, _ = slot["binary"]
            bcd_msg, bcd_time, _ = slot["bcd"]
            del self.pending[meter_id]

            binary_value = binary_msg.get("Consumption")
            bcd_value = bcd_msg.get("Consumption")
            if not bcd_value and binary_value:
                choice = "binary"
            elif not binary_value and bcd_value:
                choice = "bcd"
            elif is_bcd_misparse(binary_value, bcd_value):
                choice = "bcd"
            else:
                choice = "binary"
            self.decided[meter_id] = choice
            log.info("R900 %s: consumption encoding resolved as %s "
                     "(binary parse %s, bcd parse %s)",
                     meter_id, choice.upper(), binary_value, bcd_value)
            if choice == "bcd":
                return [(self._event(bcd_msg, "bcd"), bcd_time)]
            return [(self._event(binary_msg, "binary"), binary_time)]

    def flush(self):
        """Publish pending messages whose counterpart never arrived."""
        ready = []
        now = time.monotonic()
        with self.lock:
            for meter_id in list(self.pending):
                slot = self.pending[meter_id]
                kind, (msg, msg_time, mono) = next(iter(slot.items()))
                if now - mono >= self.window:
                    del self.pending[meter_id]
                    ready.append((self._event(msg, kind), msg_time))
        return ready


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------

def rtl433_time(iso_time):
    """rtlamr ISO timestamp -> rtl_433's local 'YYYY-MM-DD HH:MM:SS'."""
    try:
        parsed = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError, TypeError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def publish(client, event, event_time):
    data = {"time": event_time}
    data.update({k: v for k, v in event.items() if v is not None})
    client.publish(EVENTS_TOPIC, json.dumps(data, separators=(",", ":")),
                   retain=MQTT_RETAIN)
    device_base = DEVICES_TOPIC.format(model=data["model"], id=data["id"])
    for key, value in data.items():
        if key in ("model", "id"):
            continue
        client.publish("%s/%s" % (device_base, key), str(value),
                       retain=MQTT_RETAIN)


# --------------------------------------------------------------------------
# Subprocess supervision
# --------------------------------------------------------------------------

class Supervisor:
    def __init__(self):
        self.stopping = threading.Event()
        self.procs = []
        self.lock = threading.Lock()
        self.rtl_tcp_proc = None        # current rtl_tcp Popen, or None
        self.rtl_tcp_recycling = False  # this exit was a requested recycle
        self.last_recycle_mono = None   # monotonic time of last recycle
        self.last_data_mono = time.monotonic()  # liveness clock for watchdog

    def stop(self, *_):
        self.stopping.set()
        for proc in self.procs:
            try:
                proc.terminate()
            except OSError:
                pass

    def mark_data(self):
        """Record that meter data is flowing (resets watchdog + heartbeat)."""
        now = time.monotonic()
        with self.lock:
            self.last_data_mono = now
        if HEARTBEAT_FILE:
            try:
                with open(HEARTBEAT_FILE, "w") as f:
                    f.write(str(time.time()))
            except OSError as e:
                log.warning("could not write heartbeat %s: %r", HEARTBEAT_FILE, e)

    def data_age(self):
        with self.lock:
            return time.monotonic() - self.last_data_mono

    def recycle_rtl_tcp(self, reason):
        """Kill the current rtl_tcp so rtl_tcp_loop respawns it, forcing a fresh
        open of the USB device (recovers from dongle re-enumeration). No-ops if
        rtl_tcp was already recycled within RECYCLE_COOLDOWN, so a watchdog
        recycle and the rtlamr exit it causes don't kill each other's restart."""
        with self.lock:
            now = time.monotonic()
            if (self.last_recycle_mono is not None
                    and now - self.last_recycle_mono < RECYCLE_COOLDOWN):
                return
            proc = self.rtl_tcp_proc
            if proc is None:
                return
            self.last_recycle_mono = now
            self.rtl_tcp_recycling = True
        log.warning("recycling rtl_tcp (%s)", reason)
        try:
            proc.terminate()
        except OSError:
            pass

    def watchdog_loop(self):
        """Recycle rtl_tcp if no meter data has arrived for WATCHDOG_TIMEOUT."""
        if WATCHDOG_TIMEOUT <= 0:
            return
        while not self.stopping.is_set():
            self.stopping.wait(WATCHDOG_INTERVAL)
            if self.stopping.is_set():
                return
            age = self.data_age()
            if age >= WATCHDOG_TIMEOUT:
                self.mark_data()  # reset clock so we don't re-fire immediately
                self.recycle_rtl_tcp("watchdog: no data for %.0fs" % age)

    def rtl_tcp_loop(self):
        cmd = [RTL_TCP_BIN] + shlex.split(RTL_TCP_ARGS)
        while not self.stopping.is_set():
            log.info("starting: %s", " ".join(cmd))
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.STDOUT)
            with self.lock:
                self.rtl_tcp_proc = proc
            self.procs.append(proc)
            proc.wait()
            self.procs.remove(proc)
            with self.lock:
                self.rtl_tcp_proc = None
                requested = self.rtl_tcp_recycling
                self.rtl_tcp_recycling = False
            if self.stopping.is_set():
                return
            self.mark_data()  # grace period for the fresh rtl_tcp + rtlamr
            if requested:
                log.info("rtl_tcp recycled; restarting now")
            else:
                log.warning("rtl_tcp exited (%s); restarting in %.0fs",
                            proc.returncode, RESTART_DELAY)
                self.stopping.wait(RESTART_DELAY)

    def rtlamr_cmd(self):
        cmd = [RTLAMR_BIN, "-format=json",
               "-msgtype=" + RTLAMR_MSGTYPE, "-server=" + RTLAMR_SERVER]
        if RTLAMR_CENTERFREQ:
            cmd.append("-centerfreq=" + RTLAMR_CENTERFREQ)
        if RTLAMR_FILTER_ID:
            cmd.append("-filterid=" + RTLAMR_FILTER_ID)
        extra = shlex.split(RTLAMR_ARGS)
        # An explicit -symbollength in RTLAMR_ARGS wins over the env default.
        if RTLAMR_SYMBOLLENGTH and not any(
                a == "-symbollength" or a.startswith("-symbollength=")
                for a in extra):
            cmd.append("-symbollength=" + RTLAMR_SYMBOLLENGTH)
        cmd += extra
        return cmd

    def drain_stderr(self, stream):
        """Forward rtlamr's stderr, collapsing "not keeping up with rtl_tcp"
        floods into one summary per RTLAMR_DROP_SUMMARY_INTERVAL. rtlamr emits
        that line for every block it fails to demodulate in time, which on a
        loaded host is thousands per minute -- enough to fill a disk with
        container logs. The condition still gets reported, just not per-block."""
        dropped = 0
        window_start = time.monotonic()

        def flush():
            nonlocal dropped, window_start
            if dropped:
                elapsed = time.monotonic() - window_start
                log.warning("rtlamr fell behind rtl_tcp: %d dropped block(s) in "
                            "%.0fs -- lower RTLAMR_SYMBOLLENGTH (currently %s) "
                            "or reduce RTLAMR_MSGTYPE",
                            dropped, elapsed, RTLAMR_SYMBOLLENGTH or "rtlamr default")
            dropped = 0
            window_start = time.monotonic()

        try:
            for line in stream:
                if "not keeping up with rtl_tcp" in line:
                    dropped += 1
                    if time.monotonic() - window_start >= RTLAMR_DROP_SUMMARY_INTERVAL:
                        flush()
                    continue
                sys.stderr.write(line)
                sys.stderr.flush()
        except (OSError, ValueError):
            pass  # stream closed as the process went away
        finally:
            flush()

    def rtlamr_loop(self, client, resolver):
        cmd = self.rtlamr_cmd()
        while not self.stopping.is_set():
            log.info("starting: %s", " ".join(cmd))
            self.mark_data()  # grace period so the watchdog waits out startup
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            self.procs.append(proc)
            draining = threading.Thread(target=self.drain_stderr,
                                        args=(proc.stderr,), daemon=True)
            draining.start()
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    log.warning("unparseable rtlamr line: %.200s", line)
                    continue
                self.mark_data()  # a decoded record == rtl_tcp is alive
                msg_type = record.get("Type")
                msg = record.get("Message") or {}
                event_time = rtl433_time(record.get("Time"))
                if msg_type in ("R900", "R900BCD"):
                    events = resolver.feed(msg_type, msg, event_time)
                else:
                    events = [(map_message(msg_type, msg), event_time)]
                for event, ts in events:
                    publish(client, event, ts)
            proc.wait()
            draining.join(timeout=5)  # let the last drop summary land
            self.procs.remove(proc)
            if not self.stopping.is_set():
                log.warning("rtlamr exited (%s); restarting in %.0fs",
                            proc.returncode, RESTART_DELAY)
                # rtlamr exiting almost always means rtl_tcp went dead (i/o
                # timeout from a re-enumerated dongle); recycle it so the
                # restarted rtlamr reconnects to a freshly opened device.
                self.recycle_rtl_tcp("rtlamr exited")
                self.stopping.wait(RESTART_DELAY)


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")

    msgtypes = {t.strip().lower() for t in RTLAMR_MSGTYPE.split(",")}
    pairing = R900_MODE == "auto" and {"r900", "r900bcd"} <= msgtypes
    resolver = R900Resolver(R900_MODE, pairing,
                            bcd_ids=R900_BCD_IDS, binary_ids=R900_BINARY_IDS,
                            window=R900_PAIR_WINDOW)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="rtlamr-mqtt-bridge")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.will_set(STATUS_TOPIC, "offline", retain=True)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    client.publish(STATUS_TOPIC, "online", retain=True)
    log.info("connected to mqtt://%s:%s, base topic '%s', msgtypes %s, "
             "r900 mode %s%s", MQTT_HOST, MQTT_PORT, MQTT_BASE_TOPIC,
             RTLAMR_MSGTYPE, R900_MODE, " (pairing)" if pairing else "")

    supervisor = Supervisor()
    signal.signal(signal.SIGTERM, supervisor.stop)
    signal.signal(signal.SIGINT, supervisor.stop)

    if RTL_TCP_SPAWN:
        threading.Thread(target=supervisor.rtl_tcp_loop, daemon=True).start()
        time.sleep(2)  # let rtl_tcp claim the dongle before rtlamr connects
        # watchdog recycles rtl_tcp on stale data; only meaningful when we own it
        threading.Thread(target=supervisor.watchdog_loop, daemon=True).start()

    def flush_loop():
        while not supervisor.stopping.is_set():
            for event, ts in resolver.flush():
                publish(client, event, ts)
            supervisor.stopping.wait(0.5)

    threading.Thread(target=flush_loop, daemon=True).start()

    try:
        supervisor.rtlamr_loop(client, resolver)
    finally:
        client.publish(STATUS_TOPIC, "offline", retain=True)
        client.loop_stop()


def healthcheck():
    """Exit 0 if a meter message was published within HEALTHCHECK_MAX_AGE
    seconds, else exit 1. Backs the Docker HEALTHCHECK."""
    try:
        with open(HEARTBEAT_FILE) as f:
            last = float(f.read().strip())
    except (OSError, ValueError):
        sys.exit(1)
    sys.exit(0 if time.time() - last < HEALTHCHECK_MAX_AGE else 1)


if __name__ == "__main__":
    if "--healthcheck" in sys.argv:
        healthcheck()
    main()
