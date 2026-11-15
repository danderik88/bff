"""Adding printers from the app: discovery, probe, the app-added store, models, profile, AMS.

Bambu printers announce themselves with SSDP NOTIFY on UDP 2021 (checked on 2026-11-12:
A1 N2S, P1P C11, A1 mini N1, P2S N7). The TLS certificate on 8883 carries CN=<serial>,
so IP + access code are enough when discovery fails.
"""
import re
import socket
import threading
import time
from pathlib import Path

# camera "jpeg" = the TLS JPEG feed on port 6000; "rtsp" = the printer's own RTSP stream on port 322.
MODELS = {
    "N2S": {"name": "A1", "supported": True, "enclosed": None, "camera": "jpeg"},
    "N1": {"name": "A1 mini", "supported": True, "enclosed": None, "camera": "jpeg"},
    "C11": {"name": "P1P", "supported": True, "enclosed": "C12", "camera": "jpeg"},  # P1S enclosure kit
    "C12": {"name": "P1S", "supported": True, "enclosed": None, "camera": "jpeg"},
    "N7": {"name": "P2S", "supported": True, "enclosed": None, "camera": "rtsp", "chamber_sensor": True,
           "ams_drying": True},
    "BL-P001": {"name": "X1 Carbon", "supported": False, "enclosed": None, "camera": "rtsp", "chamber_sensor": True},
    "BL-P002": {"name": "X1", "supported": False, "enclosed": None, "camera": "rtsp", "chamber_sensor": True},
    "C13": {"name": "X1E", "supported": False, "enclosed": None, "camera": "rtsp", "chamber_sensor": True},
    "O1D": {"name": "H2D", "supported": False, "enclosed": None, "camera": "rtsp", "chamber_sensor": True,
            "ams_drying": True},
    # Klipper printers (Moonraker, see moonraker_link.py). Not in TESTED_MODELS: Expert mode stays locked.
    "KLIPPER-SV08": {"name": "Sovol SV08", "supported": True, "enclosed": None, "camera": "jpeg", "kind": "moonraker"},
    "KLIPPER-IR3V2": {"name": "IdeaFormer IR3 V2", "supported": True, "enclosed": None, "camera": "jpeg", "kind": "moonraker"},
}


# A1, A1 mini, P1P, P1S, P2S; all AMS units and the BMCU tested too (the user 2026-11-14);
# a P1P with the enclosure kit is a P1S.
TESTED_MODELS = {"N2S", "N1", "C11", "C12", "N7"}


def camera_kind(model_code):
    """How BFF gets this model's camera; unknown models keep the JPEG feed."""
    return MODELS.get(model_code, {}).get("camera", "jpeg")


def chamber_sensor(model_code):
    """Whether this model reports a real chamber temperature (A1/P1 echo a constant 5)."""
    return MODELS.get(model_code, {}).get("chamber_sensor", False)


def ams_drying(model_code):
    """Whether this model can start AMS drying from the printer itself (AMS 2 Pro / AMS HT)."""
    return MODELS.get(model_code, {}).get("ams_drying", False)


def _profile(name, slug, nozzle, bed):
    spec = f"https://bambulab.com/en/{slug}/tech-specs"
    return {"model": name, "slicer_model": f"Bambu Lab {name}", "command_timeout_s": 15, "start_timeout_s": 150,
            "supports": {"pause": True, "resume": True, "stop": True, "start": True},
            "limits": {"nozzle_max_c": {"value": nozzle, "source": spec}, "bed_max_c": {"value": bed, "source": spec},
                       "nozzle_confirm_above_c": {"value": nozzle, "source": spec},
                       "bed_confirm_above_c": {"value": bed, "source": spec}}}


# The profile BFF needs for an app-added printer (the A1 keeps its hand-written profiles/a1.json).
MODEL_PROFILES = {
    "N2S": _profile("A1", "a1", 300, 100),
    "N1": _profile("A1 mini", "a1-mini", 300, 80),
    "C11": _profile("P1P", "p1p", 300, 100),
    "C12": _profile("P1S", "p1s", 300, 100),
    "N7": _profile("P2S", "p2s", 300, 110),
}


def _klipper_profile(name, nozzle_max, bed_max, nozzle_confirm, bed_confirm, src="conservative placeholder: check printer.cfg max_temp"):
    # Anything above *_confirm_above_c asks for a confirmation. Prefer limits read from the printer's own printer.cfg.
    return {"model": name, "slicer_model": name, "command_timeout_s": 15, "start_timeout_s": 150,
            "supports": {"pause": True, "resume": True, "stop": True, "start": True},
            "limits": {"nozzle_max_c": {"value": nozzle_max, "source": src}, "bed_max_c": {"value": bed_max, "source": src},
                       "nozzle_confirm_above_c": {"value": nozzle_confirm, "source": src},
                       "bed_confirm_above_c": {"value": bed_confirm, "source": src}}}


MODEL_PROFILES["KLIPPER-SV08"] = _klipper_profile("Sovol SV08", 300, 100, 260, 90,
                                                  "printer.cfg of an SV08 (2026-11-15): extruder max_temp 305, heater_bed max_temp 105")
MODEL_PROFILES["KLIPPER-IR3V2"] = _klipper_profile("IdeaFormer IR3 V2", 300, 110, 260, 90)


def is_klipper(model_code):
    return MODELS.get(model_code, {}).get("kind") == "moonraker"


def model_profile(code, enclosure=False, profile=None):
    """The model's profile, enclosure-aware (a P1P with the kit is a P1S), or None if not supported."""
    m = MODELS.get(code)
    if m and m["enclosed"] and (enclosure or "chamber" in (profile or {}).get("fans", [])):
        code = m["enclosed"]
    return MODEL_PROFILES.get(code)


def model_name(code, enclosure=False, profile=None):
    m = MODELS.get(code)
    if not m:
        return f"Unknown ({code})"
    if (enclosure or "chamber" in (profile or {}).get("fans", [])) and m["enclosed"]:
        return MODELS[m["enclosed"]]["name"]
    return m["name"]


def learn(profile, snap, raw):
    """What a printer has, as seen while it prints: fans, AMS units, xcam, options, lights."""
    p = {"fans": ["part"], "xcam": [], "options": [], "lights": [], **(profile or {})}
    fans = snap.get("fans") or {}
    for k in ("aux", "chamber"):
        if fans.get(k) and fans.get(k) != fans.get("part") and k not in p["fans"]:  # the A1 echoes part into aux/chamber
            p["fans"] = p["fans"] + [k]
    p.pop("ams_units", None)  # an AMS moves between printers (the user 2026-11-12): read live, never learned
    p["xcam"] = sorted(set(p["xcam"]) | set(snap.get("xcam") or {}))
    p["options"] = sorted(set(p["options"]) | set(snap.get("options") or {}))
    p["lights"] = sorted(set(p["lights"]) | {l["node"] for l in raw.get("lights_report") or []})
    return p


def supported(code):
    return MODELS.get(code, {}).get("supported", False)


def parse_notify(text):
    """{ip, serial, model, name} from a Bambu SSDP NOTIFY, or None for anything else."""
    head = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            head[k.strip().lower()] = v.strip()
    if "devmodel.bambu.com" not in head or not head.get("usn") or not head.get("location"):
        return None
    ip = re.sub(r"^\w+://", "", head["location"]).split("/")[0].split(":")[0]
    return {"ip": ip, "serial": head["usn"], "model": head["devmodel.bambu.com"],
            "name": head.get("devname.bambu.com", "")}


def listen(seconds=4.0, sock=None):
    """Printers heard within `seconds`, one per serial. They announce every few seconds."""
    own = sock is None
    if own:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 2021))
        sock.settimeout(1.0)
    found, end = {}, time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            p = parse_notify(data.decode("utf-8", "replace"))
            if p:
                found[p["serial"]] = p
                with HEARD_LOCK:
                    HEARD[p["serial"]] = (p, time.monotonic())
    finally:
        if own:
            sock.close()
    return list(found.values())


HEARD = {}  # serial -> (printer, monotonic time last heard)
HEARD_LOCK = threading.Lock()  # keep_listening writes while HTTP threads read.
HEARD_TTL_S = 60.0  # A1 and P1 announce every ~10 s, the P2S every ~5 s (measured 2026-11-12).


def keep_listening(sock=None, clock=time.monotonic, seconds=30.0):
    """Background thread: keeps HEARD fresh so the app sees printers at once."""
    while True:
        try:
            for p in listen(seconds, sock):
                with HEARD_LOCK:
                    HEARD[p["serial"]] = (p, clock())
        except OSError as err:
            import sys
            print(f"discovery: {err}", file=sys.stderr)
            time.sleep(30)
        if sock is not None:
            return  # tests


def heard(clock=time.monotonic):
    """Printers heard in the last HEARD_TTL_S; a short listen when nothing is known yet."""
    now = clock()
    with HEARD_LOCK:
        fresh = [p for p, t in list(HEARD.values()) if now - t <= HEARD_TTL_S]
    return fresh or listen(11.0)


def clean_code(code):
    """The LAN access code (8 letters or digits) without surrounding spaces."""
    code = (code or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8}", code):
        raise ValueError("access code: 8 characters")
    return code


def slug(name):
    s = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not s:
        raise ValueError("name: letters or digits needed")
    return s


class ProbeError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def peer_cert(host, timeout):
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # self-signed: identity is CN + pin
    with socket.create_connection((host, 8883), timeout) as raw, ctx.wrap_socket(raw) as tls:
        return tls.getpeercert(binary_form=True)


def cert_names(der):
    """(subject CN, issuer CN) of a DER certificate, stdlib only."""
    import os
    import ssl
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(ssl.DER_cert_to_PEM_cert(der))
        info = ssl._ssl._test_decode_cert(path)
    finally:
        os.remove(path)
    cn = lambda rdns: next((v for rdn in rdns for k, v in rdn if k == "commonName"), "")  # noqa: E731
    return cn(info.get("subject", ())), cn(info.get("issuer", ()))


def mqtt_login(host, serial, access_code, der, timeout):
    """(CONNACK reason code, got a report). Sends the code only to the certificate just probed."""
    import hashlib
    import ssl
    import threading
    import paho.mqtt.client as mqtt
    pin, done, result = hashlib.sha256(der).digest(), threading.Event(), {"rc": None, "report": False}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bff-probe")
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)

    def on_socket_open(c, _u, sock):
        same = hashlib.sha256(sock.getpeercert(binary_form=True)).digest() == pin
        c.username_pw_set("bblp", access_code if same else "")

    def on_connect(c, _u, _f, reason, _p):
        result["rc"] = reason.value
        if reason.is_failure:
            done.set()
            return
        c.subscribe(f"device/{serial}/report")
        c.publish(f"device/{serial}/request", '{"pushing": {"command": "pushall", "sequence_id": "0"}}')

    def on_message(*_):
        result["report"] = True
        done.set()

    client.on_socket_open, client.on_connect, client.on_message = on_socket_open, on_connect, on_message
    try:
        client.connect(host, 8883, keepalive=15)
        client.loop_start()
        done.wait(timeout)
    finally:
        client.disconnect()
        client.loop_stop()
    return result["rc"], result["report"]


def probe(host, access_code, timeout=10, cert=peer_cert, connect=mqtt_login):
    """{serial, der} once the printer took the code and sent a report; ProbeError otherwise."""
    try:
        der = cert(host, timeout)
    except OSError:
        raise ProbeError("unreachable") from None
    serial, issuer = cert_names(der)
    # A1/P1 certificates come from "BBL CA", newer models (P2S) from "BBL Device CA N7-V2" and the like.
    if not (issuer or "").startswith("BBL") or not serial:
        raise ProbeError("not a Bambu printer")
    try:
        rc, report = connect(host, serial, access_code, der, timeout)
    except OSError:
        raise ProbeError("unreachable") from None
    if rc != 0:
        raise ProbeError("wrong access code")
    if not report:
        raise ProbeError("no report")
    return {"serial": serial, "der": der}


def load_store(path):
    import json
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def save_store(path, printers):
    """Atomic, owner-only: the file holds access codes."""
    import json
    import os
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(printers, f, indent=1)
    os.replace(tmp, path)


def ams_trays(units):
    """Loaded AMS trays as reported right now (an AMS moves between printers): never the RFID ids."""
    out = []
    for unit in units or []:
        try:
            u = int(unit.get("id"))
        except (TypeError, ValueError):
            continue
        for tray in unit.get("tray") or []:
            if not tray.get("tray_type"):
                continue  # empty slot
            slot = int(tray.get("id", 0))
            remain = tray.get("remain")
            have_tag = not _no_tag(tray.get("tag_uid"))
            out.append({"slot": f"{'ABCD'[u % 4]}{slot + 1}", "index": u * 4 + slot, "material": tray["tray_type"],
                        "name": tray.get("tray_sub_brands") or None, "color": tray.get("tray_color") or None,
                        "profile_id": tray.get("tray_info_idx") or None,
                        "remain": remain if isinstance(remain, int) and remain >= 0 and have_tag else None,
                        "nozzle_temp_min": tray.get("nozzle_temp_min"),
                        "nozzle_temp_max": tray.get("nozzle_temp_max"),
                        "drying_temp": tray.get("tray_temp"),
                        "drying_hours": tray.get("tray_time"),
                        "state": tray.get("state")})
    return out


def _no_tag(uid):
    """True when a tray has no RFID tag (tag_uid all zeros)."""
    if uid is None:
        return False
    return str(uid) == "" or set(str(uid)) == {"0"}


def _rgb(color):
    return (color or "").lstrip("#").upper()[:6] or None


def pick_feed(material, color, trays, external):
    """(feed, note) for a one-filament file: AMS tray index, or 254 for the external spool.

    Exact material and colour first (AMS, then external); else the only tray of that material, with a note.
    ValueError names what is loaded.
    """
    material, want = (material or "").upper(), _rgb(color)
    same = lambda t: (t.get("material") or "").upper() == material  # noqa: E731
    for t in trays:
        if same(t) and (want is None or _rgb(t.get("color")) == want):
            return t["index"], None
    ext = external or {}
    if same(ext) and (want is None or _rgb(ext.get("color")) == want):
        return 254, None
    only = [t for t in trays if same(t)]
    if len(only) == 1:
        return only[0]["index"], f"colore diverso dal file: uso {only[0]['slot']} {material}"
    if same(ext) and not only:
        return 254, "colore diverso dal file: uso la bobina esterna"
    loaded = [f"{t['slot']} {t['material']}" for t in trays] + ([f"esterna {ext['material']}"] if ext.get("material") else [])
    raise ValueError(f"serve {material}{' ' + want if want else ''}: caricati " + (", ".join(loaded) or "nessuno"))


SLOT_FIELDS = ("material", "name", "color", "remain", "profile_id", "nozzle_temp_min",
               "nozzle_temp_max", "drying_temp", "drying_hours", "state")


def ams_units(units, tray_now=None, exist_bits=None):
    """Every AMS unit with its four slots, empty ones too, for drawing it: never the RFID ids."""
    loaded = {t["index"]: t for t in ams_trays(units)}
    out = []
    for unit in units or []:
        try:
            u = int(unit.get("id"))
        except (TypeError, ValueError):
            continue
        try:
            humidity = int(unit.get("humidity"))
        except (TypeError, ValueError):
            humidity = None
        try:
            humidity_raw = int(unit.get("humidity_raw"))
        except (TypeError, ValueError):
            humidity_raw = None
        try:
            dry_time = int(unit.get("dry_time"))
        except (TypeError, ValueError):
            dry_time = None
        try:
            temp = float(unit.get("temp")) if unit.get("temp") is not None else None
        except (TypeError, ValueError):
            temp = None
        slots = []
        try:
            exist = int(exist_bits, 16) if isinstance(exist_bits, str) else None
        except ValueError:
            exist = None
        for slot in range(4):
            index = u * 4 + slot
            t = loaded.get(index)
            if t is None and exist is not None and exist >> index & 1:
                # The inlet sensor feels filament but the AMS has no data for it (no tag read, not set by hand).
                slots.append({"slot": f"{'ABCD'[u % 4]}{slot + 1}", "empty": False, "unknown": True,
                              "material": "Unknown", "active": tray_now == index})
                continue
            slots.append({"slot": f"{'ABCD'[u % 4]}{slot + 1}", "empty": t is None, "active": tray_now == index,
                          **({k: t[k] for k in SLOT_FIELDS} if t else {})})
        out.append({"unit": "ABCD"[u % 4], "humidity": humidity, "temp": temp, "humidity_raw": humidity_raw,
                    "dry_time": dry_time, "info": unit.get("info"), "serial": unit.get("serial"),
                    "model": unit.get("model") or "AMS", "can_dry": bool(unit.get("can_dry")),
                    "drying": bool(unit.get("drying")), "dry_target": unit.get("dry_target"),
                    "dry_hours": unit.get("dry_hours"), "dry_block": unit.get("dry_block") or [],
                    "slots": slots})
    return out
