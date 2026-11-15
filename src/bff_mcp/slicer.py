"""Client for the slicerd service (Task 2 HTTP contract). Stdlib only.

The server sets `URL` / `SECRET` from BFF_SLICER_URL / BFF_SLICER_SECRET via `setup`.
The secret never appears in exceptions or logs.
"""
import base64
import io
import json
import sys
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
    plates, colors, info, used = 1, [], [], None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = set(z.namelist())
            if "Metadata/project_settings.config" in files:
                found = json.loads(z.read("Metadata/project_settings.config")).get("filament_colour")
                if isinstance(found, list):
                    colors = [c for c in found if isinstance(c, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", c)]
            if "Metadata/model_settings.config" in files:
                root = ET.fromstring(z.read("Metadata/model_settings.config"))
                plates = max(1, len(root.findall(".//plate")))
                if plates > 1:
                    info = plate_info(root, files, [c[:7].upper() for c in colors])
                used = used_filaments(root, z, files)
    except (zipfile.BadZipFile, ET.ParseError, ValueError, KeyError, AttributeError):
        pass  # ponytail: the slicer reports a really broken file; facts only steer the app
    out = {"plates": plates, "colors": [c[:7].upper() for c in colors]}
    if plates > 1 and info:
        out["plate_info"] = info
    if used and len(out["colors"]) > 1 and len(used) < len(out["colors"]):
        out["used"] = [n for n in used if n <= len(out["colors"])] or None  # 1-based; the rest only fill the list
        if not out["used"]:
            del out["used"]
    return out


def used_filaments(root, z, files):
    """1-based filament numbers the objects use (the user 2026-11-15: a 2-colour Printables file listed 8).
    Painted meshes add the filaments their paint names. None when unsure."""
    used = set()
    for name in files:
        if name.startswith("3D/") and name.endswith(".model"):
            states = painted_states(z, name)
            if states is None:
                return None
            used |= states
    for obj in root.findall("object"):
        ext = _meta(obj, "extruder")
        used.add(int(ext) if ext.isdigit() else 1)
        for part in obj.findall("part"):
            e = _meta(part, "extruder")
            if e.isdigit():
                used.add(int(e))
    return sorted(n for n in used if n > 0) or None


PAINT_RE = re.compile(rb'(?:paint_color|mmu_segmentation)="([0-9A-Fa-f]+)"')


def paint_states(code):
    """Filament states in one triangle's paint code (TriangleSelector serialisation: hex nibbles read
    from the end; low 2 bits = split, else high 2 bits = state, 3 = next nibble + 3)."""
    nibbles = [int(c, 16) for c in reversed(code.decode())]
    out, i = set(), 0
    while i < len(nibbles):
        n = nibbles[i]
        i += 1
        if n & 3:
            continue  # split: the children follow in the stream
        state = n >> 2
        if state == 3:
            if i >= len(nibbles):
                break
            state = nibbles[i] + 3
            i += 1
        out.add(state)
    return out


def painted_states(z, name, cap=256_000_000):
    """Painted filament numbers (> 0) in one mesh file; None past `cap` bytes."""
    states, tail, seen = set(), b"", 0
    with z.open(name) as f:
        while chunk := f.read(4_000_000):
            seen += len(chunk)
            if seen > cap:
                return None
            buf = tail + chunk
            for m in PAINT_RE.finditer(buf):
                states |= paint_states(m.group(1))
            tail = buf[-4096:]  # ponytail: a code split across chunks longer than 4 KB is missed
    states.discard(0)  # 0 = unpainted, the object's own filament
    return states


def _meta(el, key):
    m = el.find(f"metadata[@key='{key}']")
    return m.get("value", "") if m is not None else ""


def plate_info(root, files, colors):
    """Per plate of a multi-plate 3MF: number, author's name, objects (with counts), colours, thumbnail.
    For the plate picker (the user 2026-11-15); never trusted for slicing."""
    objects = {}
    for obj in root.findall("object"):
        extruders = {_meta(obj, "extruder")} | {_meta(part, "extruder") for part in obj.findall("part")}
        objects[obj.get("id")] = (_meta(obj, "name") or "Object", {int(e) for e in extruders if e.isdigit()})
    out = []
    for plate in root.findall("plate"):
        n = _meta(plate, "plater_id")
        if not n.isdigit():
            continue
        names, used = {}, set()
        for inst in plate.findall("model_instance"):
            name, ext = objects.get(_meta(inst, "object_id"), ("Object", set()))
            names[name] = names.get(name, 0) + 1
            used |= ext
        thumb = _meta(plate, "thumbnail_file") or f"Metadata/plate_{n}.png"
        out.append({"n": int(n), "name": _meta(plate, "plater_name")[:60],
                    "objects": [f"{k} ×{v}" if v > 1 else k for k, v in names.items()][:8],
                    "colors": [colors[e - 1] for e in sorted(used) if 0 < e <= len(colors)],
                    "thumb": thumb if thumb in files else None})
    return sorted(out, key=lambda p: p["n"])


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
        "choices": {k: v for k, v in choices.items() if k not in ("quality", "plate_no")},
        "plate_no": int(choices.get("plate_no") or 0),  # 0 = every plate; a multi-plate project picks one
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
            # Orca's own words go to the log: "could not slice" alone hid the cause (2026-11-15)
            print(f"slicer 422: {str((fail or {}).get('error', ''))[-600:]}", file=sys.stderr, flush=True)
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
