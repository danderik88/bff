"""Klipper printers through Moonraker's HTTP API, behind the same surface as PrinterLink.

server.py only needs: start/stop, snapshot(), command(), gcode(), start_print(), and the attributes
alias/host/serial/online/raw/drops/error. A snapshot has the same keys and the same `state` words
(IDLE, RUNNING, PAUSE, FINISH, FAILED, PREPARE) as the Bambu one, so status, notifications and the
queue work unchanged. Bambu-only keys (AMS, HMS, xcam...) are simply absent or empty.

Stdlib only. Moonraker: https://moonraker.readthedocs.io/en/latest/external_api/
"""
import json
import re
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
SENSOR_KINDS = ("filament_switch_sensor", "filament_motion_sensor")  # Klipper's documented runout sensors
SPEED_PERCENT = {1: 50, 2: 100, 3: 124, 4: 166}  # the Bambu levels, as M220 percentages
OBJECTS = ("print_stats&display_status&virtual_sdcard&extruder&heater_bed&gcode_move&fan&idle_timeout&toolhead"
           "&ace&ace_instance_0&ace_instance_1&ace_instance_2&ace_instance_3")  # unknown objects are just left out
# Moonraker's own hostname-free defaults; a printer entry can override port and key.
DEFAULT_PORT = 7125


def pct(fraction):
    return None if fraction is None else round(float(fraction) * 100)


def pause_state(homed_axes, idle_state, timeout_s, paused_for_s):
    """(resume_blocked, seconds_left) for a paused print.

    Past Klipper's idle_timeout the motors and heaters are off, so RESUME reheats and then fails
    "Must home axis first": the app must refuse it and show how long the pause has left.
    """
    blocked = any(axis not in (homed_axes or "") for axis in "xyz") or idle_state == "Idle"
    if blocked or timeout_s is None or paused_for_s is None:
        return blocked, None
    return False, max(0, int(timeout_s - paused_for_s))


def to_snapshot(raw, klippy="ready", light=None, strips=None, direct=None):
    """Moonraker object status (merged `printer/objects/query` result) -> BFF snapshot."""
    stats = raw.get("print_stats") or {}
    ext, bed = raw.get("extruder") or {}, raw.get("heater_bed") or {}
    sd, display = raw.get("virtual_sdcard") or {}, raw.get("display_status") or {}
    move, fan = raw.get("gcode_move") or {}, raw.get("fan") or {}
    head = raw.get("toolhead") or {}
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
    # Display only: the app and Home Assistant show the name as Klipper's UI does, without the extension.
    # Everything that starts or reprints a file keeps printer/webcams' full file name.
    name = re.sub(r"\.gcode$", "", stats.get("filename") or "", flags=re.IGNORECASE)
    return {
        "state": state,
        "subtask_name": name or None,
        "progress": float(progress) if progress is not None else None,
        "remaining_time": remaining,
        "layer_num": info.get("current_layer"),
        "total_layers": info.get("total_layer"),
        "temperatures": temps,
        "door_open": False,
        "home_flag": None,
        "speed_level": speed_level(speed),
        "chamber_light": led_on(raw, light, strips, direct),
        "hms_errors": [{"module": 0, "code": "klipper", "attr": 0, "severity": None, "description": stats["message"]}]
        if stats.get("message") and state == "FAILED" else [],
        "print_error": 1 if state == "FAILED" else 0,
        "axes": None if not head else {
            "homed": head.get("homed_axes") or "",  # "" until G28, then e.g. "xyz"
            "min": list(head.get("axis_minimum") or [])[:3],
            "max": list(head.get("axis_maximum") or [])[:3],
            "pos": list(head.get("position") or [])[:3],
        },
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


def speed_level(speed):
    """The nearest Bambu speed level to Klipper's gcode_move.speed_factor; None if unknown."""
    return None if speed is None else min(SPEED_PERCENT, key=lambda level: abs(SPEED_PERCENT[level] - speed * 100))


def led_on(raw, light, strips=None, direct=None):
    """Whether the chamber light is lit: a `led <name>` object's colour, else the WLED strip's status; None if unknown.

    `direct` is WLED's own /json/state, preferred when read: Moonraker's strips miss changes made in the WLED app.
    """
    colors = (raw.get(f"led {light['led']}") or {}).get("color_data") if light and light.get("led") else None
    if colors:
        return any(c > 0 for c in colors[0])
    if light and light.get("wled"):
        off_preset = light.get("off_preset")
        if direct is not None:
            return direct["on"] and direct["ps"] != off_preset
        strip = (strips or {}).get(light["wled"]) or {}
        if strip.get("status") is None:
            return None
        return strip["status"] == "on" and (off_preset is None or strip.get("preset") != off_preset)
    return None


LED_KINDS = ("led", "neopixel", "dotstar", "pca9533", "pca9632")  # Klipper's documented LED chains
LIGHT_WORDS = ("light", "led", "lamp")


def standard_lights(objects):
    """Klipper's own lights, as object names: LED chains and output_pins whose name says light/led/lamp.

    The name filter keeps display backlights and status pixels (e.g. `neopixel btt_mini12864`) out.
    ponytail: name heuristic; a printers.json override if a light has an odd name.
    """
    found = []
    for obj in objects:
        kind, _, name = obj.partition(" ")
        if name and (kind in LED_KINDS or kind == "output_pin") and any(w in name.lower() for w in LIGHT_WORDS):
            found.append(obj)
    return found


def light_states(raw, lights):
    """[{"id": object, "name", "on": bool|None}] for the standard lights."""
    out = []
    for obj in lights or []:
        status = raw.get(obj) or {}
        if "value" in status:
            on = (status["value"] or 0) > 0
        elif status.get("color_data"):
            on = any(c > 0 for px in status["color_data"] for c in px)
        else:
            on = None
        out.append({"id": obj, "name": obj.split(" ", 1)[1], "on": on})
    return out


def light_gcode(obj, on):
    kind, name = obj.split(" ", 1)
    v = 1 if on else 0
    if kind == "output_pin":
        return f"SET_PIN PIN={name} VALUE={v}"
    return f"SET_LED LED={name} RED={v} GREEN={v} BLUE={v} WHITE={v}"


def macro_arg(text, key):
    """Value of `KEY=value` in a macro's gcode, or None."""
    found = re.search(rf"\b{key}=(\S+)", text or "", re.IGNORECASE)
    return found.group(1) if found else None


def wled_light(on, off, settings, strips):
    """WLED strip behind the light macros: the one in `STRIP=<name>` (else the first), plus its off preset."""
    if not strips:
        return {}
    config = settings or {}
    on_text = (config.get(f"gcode_macro {on.lower()}") or {}).get("gcode", "") if on else ""
    off_text = (config.get(f"gcode_macro {off.lower()}") or {}).get("gcode", "") if off else ""
    named = macro_arg(on_text, "STRIP")
    light = {"wled": named if named in strips else next(iter(strips))}
    preset = macro_arg(off_text, "PRESET")
    if preset and preset.lstrip("+-").isdigit():
        light["off_preset"] = int(preset)
    return light


def find_light(objects, settings=None, strips=None):
    """The chamber light from Moonraker: a `led X` and its macros, else light macros or a bare WLED strip.

    Shape: {"led": name|None, "on": macro|None, "off": macro|None} plus "wled"/"off_preset" for WLED.
    Real E3K: LIGHT_ON = "WLED_ON STRIP=light PRESET=1", LIGHT_OFF = "WLED_ON STRIP=light PRESET=2".
    """
    names = [o.split(" ", 1)[1] for o in objects if o.startswith("gcode_macro ")]
    lower = {n.lower(): n for n in names}
    pair = next(((on, lower[on.lower()[:-3] + "_off"]) for on in sorted(names)
                 if on.lower().endswith("_on") and on.lower()[:-3] + "_off" in lower
                 and ("led" in on.lower() or "light" in on.lower())), None)
    leds = [o.split(" ", 1)[1] for o in objects if o.startswith("led ")]
    if pair and leds:
        return {"led": leds[0], "on": pair[0], "off": pair[1]}
    on, off = pair or (None, None)
    light = {"led": None, "on": on, "off": off}
    light.update(wled_light(on, off, settings, strips))
    return light if on or light.get("wled") else None


def merge(known, update):
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(known.get(key), dict):
            merge(known[key], value)
        else:
            known[key] = value


class MoonrakerLink:
    """Same duck type as PrinterLink; `serial` is the alias (there is none) and `access_code` the API key."""
    kind = "moonraker"

    def __init__(self, alias, host, api_key="", port=DEFAULT_PORT, clock=time.monotonic, light=None):
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
        self.light = light  # {"led","on","off"[,"wled","off_preset"]}: given by the printer entry, else found on the first poll
        self._light_searched = light is not None
        self.wled = {}  # /machine/wled/strips while the light is a WLED strip
        self.wled_address = None  # the strip's own address, read once from Moonraker's config
        self.wled_direct = None  # {"on","ps"} from WLED's /json/state; None = use Moonraker's strips
        self._wled_checked = False
        self.lights = []  # standard Klipper lights (object names); WLED is only an extra next to them
        self.fans = []  # fan_generic names, for aux/chamber fans
        self.sensors = None  # filament_switch_sensor / filament_motion_sensor objects, found on the first poll
        self.cam_flip = (False, False, 0)  # Moonraker's flip_horizontal, flip_vertical, rotation of the webcam used
        self._connected = None
        self._stop = False
        self._paused_since = None  # clock() when the print entered "paused"; None otherwise
        self.idle_timeout_s = None  # printer.cfg [idle_timeout] timeout, read once (600 is Klipper's default)

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
                self.cam_flip = (bool(cam.get("flip_horizontal")), bool(cam.get("flip_vertical")), int(cam.get("rotation") or 0))
                url = cam["snapshot_url"]
                return url if "://" in url else f"http://{self.host}{url if url.startswith('/') else '/' + url}"
        raise OSError("no enabled webcam in Moonraker")

    def _config(self):
        """Moonraker's configfile sections (gcode_macro bodies live here); empty when unavailable."""
        try:
            return (self._request("GET", "/printer/objects/query", configfile="settings").get("result", {}).get("status", {}).get("configfile", {}).get("settings")) or {}
        except (urllib.error.URLError, OSError, ValueError):
            return {}

    def _wled_strips(self):
        """Moonraker's WLED strips ({} when the WLED component is not installed: the endpoint 404s)."""
        try:
            return self._request("GET", "/machine/wled/strips").get("result", {}).get("strips") or {}
        except (urllib.error.URLError, OSError, ValueError):
            return {}

    def _wled_address(self):
        """The WLED strip's own address from Moonraker's config (http strips only); None otherwise."""
        if not (self.light and self.light.get("wled")):
            return None
        try:
            config = self._request("GET", "/server/config").get("result", {}).get("config") or {}
        except (urllib.error.URLError, OSError, ValueError):
            return None
        wled = config.get(f"wled {self.light['wled']}") or {}
        address = (wled.get("address") or "").strip()
        return address if wled.get("type") == "http" and address else None

    def _wled_state(self):
        """WLED's own /json/state (changes made in the WLED app are invisible to Moonraker); None = fallback."""
        if not self.wled_address:
            return None
        try:
            with urllib.request.urlopen(f"http://{self.wled_address}/json/state", timeout=2) as resp:
                state = json.loads(resp.read() or b"{}")
            return {"on": bool(state.get("on")), "ps": int(state.get("ps") or 0)}
        except (urllib.error.URLError, OSError, ValueError):
            return None

    # --- polling --------------------------------------------------------------------------------
    def poll(self):
        """One round trip; the result feeds snapshot(). Returns whether Moonraker answered."""
        try:
            info = self._request("GET", "/printer/info").get("result", {})
            if self.idle_timeout_s is None:  # once: printer.cfg does not change for the life of the connection
                timeout = (self._config().get("idle_timeout") or {}).get("timeout")
                self.idle_timeout_s = float(timeout) if timeout is not None else 600
            if not self._light_searched or self.sensors is None:
                objects = self._request("GET", "/printer/objects/list").get("result", {}).get("objects", [])
                if not self._light_searched:
                    self.light = find_light(objects, self._config(), self._wled_strips())
                    self._light_searched = True
                self.sensors = [o for o in objects if o.split(" ")[0] in SENSOR_KINDS and " " in o]
                self.fans = [o.split(" ", 1)[1] for o in objects if o.startswith("fan_generic ")]
                self.lights = standard_lights(objects)
            if not self._wled_checked:  # once: the light does not change for the life of the connection
                self._wled_checked = True
                self.wled_address = self._wled_address()
            strips = self._wled_strips() if self.light and self.light.get("wled") else None
            led = f"&led%20{urllib.parse.quote(self.light['led'])}" if self.light and self.light.get("led") else ""
            led += "".join(f"&{urllib.parse.quote(s)}" for s in self.sensors + self.lights)
            status = self._request("GET", "/printer/objects/query?" + OBJECTS + led).get("result", {}).get("status", {})
        except (urllib.error.URLError, OSError, ValueError):
            self.online = False
            return False
        direct = self._wled_state()
        with self.lock:
            merge(self.raw, status)
            paused = (self.raw.get("print_stats") or {}).get("state") == "paused"
            if paused and self._paused_since is None:
                self._paused_since = self.clock()
            elif not paused:
                self._paused_since = None
            if strips is not None:
                self.wled = strips
            self.wled_direct = direct
            self.klippy = info.get("state", "ready")
            self.firmware_version = info.get("software_version")
            self.last_report = self.clock()
        self.online = True
        return True

    def snapshot(self):
        with self.lock:
            snapshot = to_snapshot(json.loads(json.dumps(self.raw)), self.klippy, self.light, self.wled,
                                   self.wled_direct)
            # Runout sensors pass through untouched: status_dict turns them into Options switches
            snapshot.update({k: dict(v) for k, v in self.raw.items() if k.split(" ")[0] in SENSOR_KINDS and isinstance(v, dict)})
            snapshot["firmware_version"] = self.firmware_version
            if snapshot["state"] == "PAUSE":  # only while paused: the app shows the pause expiring
                head, idle = self.raw.get("toolhead") or {}, self.raw.get("idle_timeout") or {}
                paused_for = None if self._paused_since is None else self.clock() - self._paused_since
                blocked, left = pause_state(head.get("homed_axes"), idle.get("state"), self.idle_timeout_s, paused_for)
                snapshot["resume_blocked"], snapshot["pause_left_s"] = blocked, left
            # Standard lights are the main light: the one toggle drives them all; WLED becomes an extra.
            lights = light_states(self.raw, self.lights)
            if lights:
                known = [l["on"] for l in lights if l["on"] is not None]
                wled = snapshot["chamber_light"] if self.light and self.light.get("wled") else None
                snapshot["chamber_light"] = any(known) if known else None
                if self.light and self.light.get("wled"):
                    lights.append({"id": "wled", "name": "WLED", "on": wled, "extra": True})
            snapshot["lights"] = lights
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

    def command(self, action, heater=None, target=None, on=None, level=None, fan=None, percent=None, light=None,
                **_unused):
        own_macros = self.light and self.light.get("on") and not self.light.get("wled")  # user's LED macros win
        if action == "light" and self.lights and light != "wled" and not (light is None and own_macros):
            targets = self.lights if light is None else [light]
            if not set(targets) <= set(self.lights):
                return False
            return self.gcode("\n".join(light_gcode(t, on) for t in targets))
        if action == "pause":
            return self._send("POST", "/printer/print/pause")
        if action == "resume":
            return self._send("POST", "/printer/print/resume")
        if action == "stop":
            return self._send("POST", "/printer/print/cancel")
        if action == "temperature":
            gcode = {"bed": "M140", "nozzle": "M104"}[heater]
            return self.gcode(f"{gcode} S{int(target)}")
        if action == "light" and self.light:
            macro = self.light.get("on" if on else "off")
            if macro:
                return self.gcode(macro)
            if self.light.get("wled"):
                return self._send("POST", "/machine/wled/on" if on else "/machine/wled/off",
                                  strip=self.light["wled"])
            return False
        if action == "speed" and level in SPEED_PERCENT:
            return self.gcode(f"M220 S{SPEED_PERCENT[level]}")
        if action == "fan" and fan == "part":
            return self.gcode(f"M106 S{round((percent or 0) * 255 / 100)}")
        if action == "fan":  # aux/chamber only where the printer has a fan_generic with that word
            name = next((n for n in self.fans if fan and fan in n.lower()), None)
            return self.gcode(f"SET_FAN_SPEED FAN={name} SPEED={(percent or 0) / 100:g}") if name else False
        return False  # the rest: printer-specific, not guessed

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
