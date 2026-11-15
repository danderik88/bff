"""Klipper printers through Moonraker's HTTP API, behind the same surface as PrinterLink.

server.py only needs: start/stop, snapshot(), command(), gcode(), start_print(), and the attributes
alias/host/serial/online/raw/drops/error. A snapshot has the same keys and the same `state` words
(IDLE, RUNNING, PAUSE, FINISH, FAILED, PREPARE) as the Bambu one, so status, notifications and the
queue work unchanged. Bambu-only keys (AMS, HMS, xcam...) are simply absent or empty.

Stdlib only. Moonraker: https://moonraker.readthedocs.io/en/latest/external_api/
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

POLL_S = 2.0
STALE_S = 10  # No answer for this long means the printer is considered disconnected.
TIMEOUT_S = 5

# print_stats.state -> the gcode_state words the rest of BFF understands.
STATES = {"standby": "IDLE", "printing": "RUNNING", "paused": "PAUSE", "complete": "FINISH",
          "cancelled": "FAILED", "error": "FAILED"}
OBJECTS = ("print_stats&display_status&virtual_sdcard&extruder&heater_bed&gcode_move&fan&idle_timeout&toolhead"
           "&ace&ace_instance_0&ace_instance_1&ace_instance_2&ace_instance_3")  # unknown objects are just left out
# Moonraker's own hostname-free defaults; a printer entry can override port and key.
DEFAULT_PORT = 7125


def pct(fraction):
    return None if fraction is None else round(float(fraction) * 100)


def to_snapshot(raw, klippy="ready"):
    """Moonraker object status (merged `printer/objects/query` result) -> BFF snapshot."""
    stats = raw.get("print_stats") or {}
    ext, bed = raw.get("extruder") or {}, raw.get("heater_bed") or {}
    sd, display = raw.get("virtual_sdcard") or {}, raw.get("display_status") or {}
    move, fan = raw.get("gcode_move") or {}, raw.get("fan") or {}
    info = stats.get("info") or {}
    state = STATES.get(stats.get("state"), "IDLE")
    if klippy in ("shutdown", "error"):
        state = "FAILED"
    elif klippy == "startup":
        state = "PREPARE"
    printing = state in ("RUNNING", "PAUSE")
    # M73 progress (slicer/Orca) when the print sets it, otherwise bytes read from the file.
    fraction = display.get("progress") if display.get("progress") else sd.get("progress")
    progress = pct(fraction) if printing or state == "FINISH" else None
    remaining = None
    if printing and fraction and stats.get("print_duration"):
        remaining = max(0, round(stats["print_duration"] * (1 - fraction) / fraction / 60))
    nozzle, bed_t = ext.get("temperature"), bed.get("temperature")
    temps = {"bed": bed_t, "bed_target": bed.get("target"), "nozzle": nozzle, "nozzle_target": ext.get("target")}
    temps = {k: v for k, v in temps.items() if v is not None}
    temps["nozzle_heating"] = 0 < (temps.get("nozzle_target") or 0) and (nozzle or 0) < temps["nozzle_target"]
    speed = move.get("speed_factor")
    current = (raw.get("ace") or {}).get("current_index")
    tray_now = current if isinstance(current, int) and current >= 0 else None
    return {
        "state": state,
        "subtask_name": stats.get("filename") or None,
        "progress": float(progress) if progress is not None else None,
        "remaining_time": remaining,
        "layer_num": info.get("current_layer"),
        "total_layers": info.get("total_layer"),
        "temperatures": temps,
        "door_open": False,
        "home_flag": None,
        "speed_level": None,
        "chamber_light": None,
        "hms_errors": [{"module": 0, "code": "klipper", "attr": 0, "severity": None, "description": stats["message"]}]
        if stats.get("message") and state == "FAILED" else [],
        "print_error": 1 if state == "FAILED" else 0,
        "stage": None,
        "prepare_percent": None,
        "ams": ace_units(raw), "tray_now": tray_now, "ams_exist_bits": None, "vt_tray": [],
        "fans": {"part": pct(fan.get("speed")), "aux": None, "chamber": None},
        "xcam": {}, "options": {}, "nozzles": [],
        "speed_percent": round(speed * 100) if speed is not None else None,
        "heatbreak_fan": None, "print_source": "klipper", "pressure_advance": None,
        "nozzle_diameter": None, "nozzle_type": None, "wifi_signal": None, "sd_card": None,
        "filament_sensor": None, "ams_status": None, "firmware_update": False,
        "timelapse": False, "camera_recording": False, "plate_detection": None, "chamber_temp": None,
        "diagnostics": {}, "firmware_version": None,
    }


def ace_units(raw):
    """ACE Pro filament changers (Klipper ACE driver) in the Bambu AMS shape, so the app draws them as spool units."""
    units = []
    for i in range(4):
        inst = raw.get(f"ace_instance_{i}")
        if not isinstance(inst, dict) or not inst.get("slots"):  # absent instances come back empty
            continue
        dryer = inst.get("dryer_status") or {}
        trays = []
        for slot in inst.get("slots") or []:
            loaded = slot.get("status") != "empty"
            r, g, b = (list(slot.get("color") or [0, 0, 0]) + [0, 0, 0])[:3]
            trays.append({"id": slot.get("index", len(trays)), "tray_type": slot.get("material") if loaded else "",
                          "tray_color": f"{r:02X}{g:02X}{b:02X}FF", "nozzle_temp_min": slot.get("temp") or None,
                          "nozzle_temp_max": slot.get("temp") or None, "state": 3 if loaded else 0})
        units.append({"id": i, "humidity": inst.get("humidity"), "temp": inst.get("temp"), "model": "ACE Pro",
                      "tray": trays, "dry_time": (dryer.get("remain_time") or 0) if dryer.get("state_code") else 0,
                      "drying": bool(dryer.get("state_code")), "dry_target": dryer.get("target_temp") or None})
    return units


def merge(known, update):
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(known.get(key), dict):
            merge(known[key], value)
        else:
            known[key] = value


class MoonrakerLink:
    """Same duck type as PrinterLink; `serial` is the alias (there is none) and `access_code` the API key."""
    kind = "moonraker"

    def __init__(self, alias, host, api_key="", port=DEFAULT_PORT, clock=time.monotonic):
        self.alias, self.host, self.port = alias, host, port
        self.serial, self.access_code, self.fingerprint_dir = alias, api_key or "", None
        self.clock = clock
        self.lock = threading.Lock()
        self.raw = {}
        self.klippy = "ready"
        self.last_report = None
        self.online = False
        self.refused = self.cert_refused = False
        self.error = None
        self.drops = 0
        self.firmware_version = None
        self._connected = None
        self._stop = False

    # --- HTTP -----------------------------------------------------------------------------------
    def _url(self, path, **query):
        qs = "?" + urllib.parse.urlencode(query) if query else ""
        return f"http://{self.host}:{self.port}{path}{qs}"

    def _request(self, method, path, **query):
        req = urllib.request.Request(self._url(path, **query), method=method)
        if self.access_code:
            req.add_header("X-Api-Key", self.access_code)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read()
        return json.loads(body) if body else {}

    def _send(self, method, path, **query):
        """Once, never retried: a command that was refused or timed out is reported, not repeated."""
        try:
            self._request(method, path, **query)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def snapshot_url(self):
        """URL of the first enabled webcam's still image, from Moonraker's webcam list."""
        for cam in self._request("GET", "/server/webcams/list").get("result", {}).get("webcams", []):
            if cam.get("enabled") and cam.get("snapshot_url"):
                url = cam["snapshot_url"]
                return url if "://" in url else f"http://{self.host}{url if url.startswith('/') else '/' + url}"
        raise OSError("no enabled webcam in Moonraker")

    # --- polling --------------------------------------------------------------------------------
    def poll(self):
        """One round trip; the result feeds snapshot(). Returns whether Moonraker answered."""
        try:
            info = self._request("GET", "/printer/info").get("result", {})
            status = self._request("GET", "/printer/objects/query?" + OBJECTS).get("result", {}).get("status", {})
        except (urllib.error.URLError, OSError, ValueError):
            self.online = False
            return False
        with self.lock:
            merge(self.raw, status)
            self.klippy = info.get("state", "ready")
            self.firmware_version = info.get("software_version")
            self.last_report = self.clock()
        self.online = True
        return True

    def snapshot(self):
        with self.lock:
            snapshot = to_snapshot(json.loads(json.dumps(self.raw)), self.klippy)
            snapshot["firmware_version"] = self.firmware_version
            fresh = self.last_report is not None and self.clock() - self.last_report <= STALE_S
        snapshot["connected"] = fresh
        self._set_connected(fresh, "no data for 10 s")
        return snapshot

    def _set_connected(self, connected, reason):
        if connected == self._connected:
            return
        if self._connected and not connected:
            self.drops += 1
        self._connected = connected
        print(f"{self.alias}: {reason if not connected else 'reconnected'}", file=sys.stderr)

    def start(self):
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop:
            self.poll()
            time.sleep(POLL_S)

    def stop(self):
        self._stop = True

    # --- commands -------------------------------------------------------------------------------
    def publish(self, payload):
        """Bambu MQTT payloads have no meaning here: refused, so a Bambu-only tool fails closed."""
        return False

    def command(self, action, heater=None, target=None, on=None, **_unused):
        if action == "pause":
            return self._send("POST", "/printer/print/pause")
        if action == "resume":
            return self._send("POST", "/printer/print/resume")
        if action == "stop":
            return self._send("POST", "/printer/print/cancel")
        if action == "temperature":
            gcode = {"bed": "M140", "nozzle": "M104"}[heater]
            return self.gcode(f"{gcode} S{int(target)}")
        return False  # light and the rest: printer-specific macros, not guessed

    def gcode(self, text):
        return self._send("POST", "/printer/gcode/script", script=text.strip())

    def upload(self, filename, data):
        """Put a G-code file in Moonraker's gcodes root. Raises OSError/ValueError on failure (nothing started)."""
        boundary = "bff" + format(int(time.time() * 1000), "x")
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"root\"\r\n\r\ngcodes\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode()
        body = head + data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(self._url("/server/files/upload"), data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        if self.access_code:
            req.add_header("X-Api-Key", self.access_code)
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status not in (200, 201):
                raise OSError(f"upload answered {resp.status}")

    def start_print(self, filename, ams_mapping=None, plate=1):
        """Print a file already in Moonraker's gcodes root (see upload())."""
        return self._send("POST", "/printer/print/start", filename=filename)
