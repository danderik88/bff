"""Client for the slicerd service (Task 2 HTTP contract). Stdlib only.

The server sets `URL` / `SECRET` from BFF_SLICER_URL / BFF_SLICER_SECRET via `setup`.
The secret never appears in exceptions or logs.
"""
import base64
import io
import json
import re
import time
import zipfile
import xml.etree.ElementTree as ET
import urllib.error
import urllib.request

URL = ""
SECRET = ""
_AVAIL = (0.0, False)  # (monotonic, available)
AVAIL_TTL = 60.0
HEALTH_TIMEOUT = 5.0
TIMEOUT = 360.0


def setup(url, secret):
    global URL, SECRET, _AVAIL
    URL = url or ""
    SECRET = secret or ""
    _AVAIL = (0.0, False)


def configured():
    return bool(URL and SECRET)


def available():
    """configured and GET URL+/health returns 200 within 5 s, cached 60 s. Never raises."""
    global _AVAIL
    if not configured():
        return False
    now = time.monotonic()
    if now - _AVAIL[0] < AVAIL_TTL:
        return _AVAIL[1]
    ok = False
    try:
        with urllib.request.urlopen(URL + "/health", timeout=HEALTH_TIMEOUT) as resp:
            ok = resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        ok = False
    _AVAIL = (now, ok)
    return ok


def model_facts(name, data):
    """{"plates", "colors"} of a model. STL/STEP: one plate, no colours. A broken 3MF reads as one plate."""
    if not name.lower().endswith(".3mf"):
        return {"plates": 1, "colors": []}
    plates, colors = 1, []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = set(z.namelist())
            if "Metadata/model_settings.config" in files:
                root = ET.fromstring(z.read("Metadata/model_settings.config"))
                plates = max(1, len(root.findall(".//plate")))
            if "Metadata/project_settings.config" in files:
                found = json.loads(z.read("Metadata/project_settings.config")).get("filament_colour")
                if isinstance(found, list):
                    colors = [c for c in found if isinstance(c, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", c)]
    except (zipfile.BadZipFile, ET.ParseError, ValueError, KeyError, AttributeError):
        pass  # ponytail: the slicer reports a really broken file; facts only steer the app
    return {"plates": plates, "colors": [c[:7].upper() for c in colors]}


def machine_preset(model, nozzle_diameter=None):
    """"Bambu Lab <model> <d> nozzle", d formatted like 0.4 (default 0.4)."""
    d = nozzle_diameter if nozzle_diameter is not None else 0.4
    return f"Bambu Lab {model} {float(d):g} nozzle"


class SliceError(Exception):
    """English message; the secret never appears in it."""
    fits_on = ()  # machine presets the model fits on, filled by a "too_big" reply


def _too_big_message(payload):
    bx, by, bz = (round(float(v)) for v in payload["bed"])
    size = payload.get("size")
    if size:
        x, y, z = (round(float(v)) for v in size)
        return f"Too big for this printer: {x} × {y} × {z} mm, plate is {bx} × {by} × {bz} mm"
    return f"Too big for this printer: plate is {bx} × {by} × {bz} mm"


def slice(model, filename, machine, filaments, choices, reorient, copies,
          other_machines=None):
    body = {
        "model_b64": base64.b64encode(model).decode(),
        "filename": filename,
        "machine": machine,
        "quality": choices.get("quality", "normal"),
        "filaments": filaments,
        "choices": {k: v for k, v in choices.items() if k != "quality"},
        "reorient": reorient,
        "copies": copies,
        "other_machines": other_machines or [],
    }
    if len(filaments) == 1:  # legacy single fields, still understood by an older slicerd
        body["filament_id"] = filaments[0].get("filament_id")
        body["filament_type"] = filaments[0].get("filament_type")
    req = urllib.request.Request(
        URL + "/slice", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    # Never forward the secret to whatever host a redirect points at.
    req.add_unredirected_header("Authorization", "Bearer " + SECRET)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        if err.code == 422:
            try:
                fail = json.loads(err.read().decode())
            except (OSError, ValueError):
                fail = {}
            if isinstance(fail, dict) and fail.get("error") == "too_big":
                exc = SliceError(_too_big_message(fail))
                exc.fits_on = fail.get("fits_on") or []
                raise exc from None
        if err.code in (400, 422):
            raise SliceError("Could not slice this model") from None
        raise SliceError("Slicer not reachable") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise SliceError("Slicer not reachable") from None
    plates = []
    for plate in payload.get("plates") or []:
        plates.append({"gcode_3mf": base64.b64decode(plate["gcode_3mf"]),
                       "seconds": plate.get("seconds"), "grams": plate.get("grams")})
    return plates
