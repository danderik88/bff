#!/usr/bin/env python3
"""slicerd: tiny HTTP wrapper around the pinned OrcaSlicer CLI.

POST /slice (Authorization: Bearer <SLICER_SECRET>), JSON
  {model_b64, filename, machine, quality, filament, choices, reorient, copies}
  -> 200 {"plates": [{"gcode_3mf", "seconds", "grams", "png"}]} (bytes as base64)
  -> 400 bad input, 401 bad secret, 422 slicer failure.
GET /health -> {"orca": ORCA_VERSION, "profiles": n}
GET /profiles -> {"machines": [...], "filaments": [...]}

Profiles come from flatten_profiles.py (`index.json` + one JSON per preset). Stdlib only.
"""
import base64
import hmac
import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ORCA_VERSION = "2.5.0"
ORCA_BIN = os.environ.get("ORCA_BIN", "/opt/orca/AppRun")
PROFILES = Path(os.environ.get("SLICER_PROFILES", "/opt/slicer/profiles"))
SECRET = os.environ.get("SLICER_SECRET", "")
PORT = int(os.environ.get("SLICER_PORT", "8765"))
MAX_UPLOAD = 100 * 1024 * 1024
TIMEOUT = 300
MODEL_EXT = (".stl", ".step", ".stp", ".3mf")
PATTERNS = ("grid", "gyroid", "cubic", "honeycomb", "line")
SUPPORTS = {
    "off": {"enable_support": "0"},
    "auto": {"enable_support": "1", "support_type": "tree(auto)", "support_on_build_plate_only": "1"},
    "everywhere": {"enable_support": "1", "support_type": "tree(auto)", "support_on_build_plate_only": "0"},
}
LOCK = threading.Lock()  # ponytail: one slice at a time, as the plan wants (CPU/RAM bound CT)


class SliceError(Exception):
    """OrcaSlicer failed or timed out -> 422."""


class TooBig(SliceError):
    """The model does not fit the plate. size/bed in mm; fits_on lists machine presets that fit."""

    def __init__(self, size, bed, fits_on=None):
        super().__init__("model does not fit the plate")
        self.size = size
        self.bed = bed
        self.fits_on = fits_on or []


def _float(value):
    """A profile field that may be a number, a string or a one-item list, as float."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else 0
    return float(value)


def bed_size(machine_json):
    """(x, y, z) of the printable volume in mm: max of the printable_area points, printable_height."""
    points = [str(p).split("x") for p in machine_json.get("printable_area") or []]
    return (max((float(p[0]) for p in points), default=0.0),
            max((float(p[1]) for p in points), default=0.0),
            _float(machine_json.get("printable_height") or 0))


def model_size(path):
    """(x, y, z) mm from `ORCA_BIN --info`, or None if OrcaSlicer cannot tell."""
    try:
        done = subprocess.run([ORCA_BIN, "--info", str(path)], capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode != 0:
        return None
    text = (done.stdout + done.stderr).decode(errors="replace")
    found = dict(re.findall(r"^\s*(size_[xyz])\s*=\s*([-\d.]+)", text, re.M))
    if len(found) < 3:
        return None
    return float(found["size_x"]), float(found["size_y"]), float(found["size_z"])


def fits(size, bed):
    """z fits and the footprint fits flat, as is or rotated 90 degrees."""
    x, y, z = size
    bx, by, bz = bed
    return z <= bz and ((x <= bx and y <= by) or (y <= bx and x <= by))


def _json_file(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _int_in(choices, key, lo, hi):
    value = choices[key]
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{key} must be an integer {lo}-{hi}")
    return value


def build_process(base: dict, choices: dict) -> dict:
    """Process preset with the user's choices applied; keys absent from `choices` stay as in `base`."""
    out = dict(base)
    if "supports" in choices:
        if choices["supports"] not in SUPPORTS:
            raise ValueError(f"supports must be one of {', '.join(SUPPORTS)}")
        out.update(SUPPORTS[choices["supports"]])
    if "infill" in choices:
        out["sparse_infill_density"] = f"{_int_in(choices, 'infill', 0, 100)}%"
    if "pattern" in choices:
        if choices["pattern"] not in PATTERNS:
            raise ValueError(f"pattern must be one of {', '.join(PATTERNS)}")
        out["sparse_infill_pattern"] = choices["pattern"]
    if "walls" in choices:
        out["wall_loops"] = str(_int_in(choices, "walls", 1, 6))
    if "plate" in choices:
        if not isinstance(choices["plate"], str) or not choices["plate"].strip():
            raise ValueError("plate must be a plate type name")
        out["curr_bed_type"] = choices["plate"]
    return out


_LOADED = {}


def _load_filament(preset_file):
    """Flattened preset JSON, cached per profiles root (a new root invalidates the cache)."""
    key = (str(PROFILES), preset_file)
    if key not in _LOADED:
        _LOADED[key] = json.loads((PROFILES / preset_file).read_text())
    return _LOADED[key]


def resolve_filament(entry, filament=None, filament_id=None, filament_type=None):
    """Name of the filament preset for this spool.

    An explicit `filament` preset name must be compatible with the machine. Otherwise pick the
    preset whose flattened JSON has the spool's `filament_id` (preferring a brand preset over a
    Generic one), else the Generic preset of the spool's `filament_type`.
    """
    if filament:
        if filament not in entry["filament"]:
            raise ValueError("filament not compatible with this machine")
        return filament
    if filament_id is not None:
        match = [name for name in entry["filament"]
                 if _load_filament(entry["filament"][name]).get("filament_id") == filament_id]
        if len(match) > 1:
            match = [name for name in match if "Generic" not in name] or match
        if match:
            return match[0]
    if filament_type is not None:
        wanted = str(filament_type).upper()
        for name in sorted(entry["filament"]):
            if name.startswith("Generic "):
                ft = _load_filament(entry["filament"][name]).get("filament_type")
                types = ft if isinstance(ft, list) else ([ft] if ft else [])
                if any(str(t).upper() == wanted for t in types):
                    return name
    raise ValueError("no filament profile for this spool")


def load_index() -> dict:
    return json.loads((PROFILES / "index.json").read_text())


def pick_quality(processes, quality: str) -> str:
    """Global Constraints rule; names come from the index, never built from the model name."""
    rules = {"fast": ("0.24mm", ("Draft", "Standard")),
             "normal": ("0.20mm", ("Standard",)),
             "fine": ("0.12mm", ("Fine", "High Quality"))}
    if quality not in rules:
        raise ValueError("quality must be fast, normal or fine")
    prefix, prefer = rules[quality]
    for word in prefer:
        for name in sorted(processes):
            if name.startswith(f"{prefix} {word}"):
                return name
    raise ValueError(f"no {quality} process for this machine")


def _plates(outdir: Path):
    found = []
    for gcode in outdir.glob("plate_*.gcode"):
        m = re.fullmatch(r"plate_(\d+)\.gcode", gcode.name)
        if m:
            found.append(int(m.group(1)))
    return sorted(found)


INVALID_VALUE = re.compile(r'value "([^"]*)" for (\w+) is not valid, using "([^"]*)" instead')


def _apply_fixes(path, fixes):
    """Put Orca's suggested values into the project settings of the 3MF. True when something changed."""
    try:
        with zipfile.ZipFile(path) as z:
            items = [(i, z.read(i.filename)) for i in z.infolist()]
    except zipfile.BadZipFile:
        return False
    changed = False
    out = []
    for info, data in items:
        if info.filename == "Metadata/project_settings.config":
            try:
                conf = json.loads(data)
            except ValueError:
                conf = None
            if isinstance(conf, dict):
                for bad, key, good in fixes:
                    v = conf.get(key)
                    if v == bad:
                        conf[key], changed = good, True
                    elif isinstance(v, list) and bad in v:
                        conf[key], changed = [good if x == bad else x for x in v], True
                if changed:
                    data = json.dumps(conf, indent=4).encode()
        elif info.filename == "Metadata/model_settings.config":  # per-object and per-part overrides
            text = data.decode("utf-8", errors="surrogateescape")
            new = text
            for bad, key, good in fixes:
                new = new.replace(f'key="{key}" value="{bad}"', f'key="{key}" value="{good}"')
            if new != text:
                data, changed = new.encode("utf-8", errors="surrogateescape"), True
        out.append((info, data))
    if changed:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for info, data in out:
                z.writestr(info, data)
    return changed


def _drop_project_settings(path):
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist() if i.filename != "Metadata/project_settings.config"]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            z.writestr(info, data)


def _add_instances(path, copies):
    """Copies of a 3MF project as extra instances inside it (the user 2026-11-15, 2 LazyCat covers):
    Orca refuses the same 3MF twice (-4) and --clone-objects on a project (-2)."""
    try:
        with zipfile.ZipFile(path) as z:
            items = [(i, z.read(i.filename)) for i in z.infolist()]
    except zipfile.BadZipFile:
        return  # Orca reports a broken file itself
    out = []
    for info, data in items:
        if info.filename == "3D/3dmodel.model":
            text = data.decode("utf-8", errors="surrogateescape")
            build = re.search(r"<build\b[^>]*>(.*?)</build>", text, re.S)
            if build:
                found = re.findall(r"<item\b[^>]*/>", build.group(1))
                extra = "".join("\n  " + re.sub(r'p:UUID="[^"]*"', f'p:UUID="{uuid.uuid4()}"', it)
                                for _ in range(copies - 1) for it in found)
                text = text[:build.end(1)] + extra + text[build.end(1):]
            data = text.encode("utf-8", errors="surrogateescape")
        elif info.filename == "Metadata/model_settings.config":
            def plate(m):
                insts = re.findall(r"<model_instance>.*?</model_instance>", m.group(0), re.S)
                more = ""
                for k in range(1, copies):
                    for s in insts:
                        s = re.sub(r'(key="instance_id" value=")(\d+)', lambda x: x[1] + str(int(x[2]) + k), s)
                        more += "\n    " + re.sub(r'<metadata key="identify_id"[^>]*/>', "", s)
                return m.group(0).replace("</plate>", more + "\n  </plate>")
            text = data.decode("utf-8", errors="surrogateescape")
            data = re.sub(r"<plate>.*?</plate>", plate, text, flags=re.S).encode("utf-8", errors="surrogateescape")
        out.append((info, data))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in out:
            z.writestr(info, data)


BBS_CAP = "02.00.00.00"  # newest Bambu Studio 3MF version our Orca opens (Orca exit -24 above it)


def _cap_bbs_version(path):
    """MakerWorld 3MFs saved by a newer Bambu Studio (02.07, 2026-11-15) make Orca 2.5 stop with
    "Unsupported 3MF version" (-24). Rewrite only the version labels to BBS_CAP; geometry and settings stay."""
    try:
        with zipfile.ZipFile(path) as z:
            items = [(i, z.read(i.filename)) for i in z.infolist()]
    except zipfile.BadZipFile:
        return
    changed = False
    out = []
    for info, data in items:
        if info.filename in ("3D/3dmodel.model", "Metadata/project_settings.config"):
            text = data.decode("utf-8", errors="surrogateescape")
            new = re.sub(r"(BambuStudio-)(\d\d\.\d\d\.\d\d\.\d\d)",
                         lambda m: m[1] + min(m[2], BBS_CAP), text)
            new = re.sub(r'("version":\s*")(\d\d\.\d\d\.\d\d\.\d\d)(")',
                         lambda m: m[1] + min(m[2], BBS_CAP) + m[3], new)
            if new != text:
                data, changed = new.encode("utf-8", errors="surrogateescape"), True
        out.append((info, data))
    if changed:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for info, data in out:
                z.writestr(info, data)


def slice_job(model: bytes, filename: str, machine: str, quality: str, filament: str = None,
              choices: dict = None, reorient: bool = False, copies: int = 1,
              filament_id: str = None, filament_type: str = None,
              other_machines: list = None, filaments: list = None, plate_no: int = 0) -> dict:
    if not model or len(model) > MAX_UPLOAD:
        raise ValueError("model empty or over 100 MB")
    name = Path(filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in MODEL_EXT:
        raise ValueError("model must be .stl, .step, .stp or .3mf")
    if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= 20:
        raise ValueError("copies must be an integer 1-20")
    if not isinstance(choices, dict):
        raise ValueError("choices must be an object")
    if isinstance(plate_no, bool) or not isinstance(plate_no, int) or not 0 <= plate_no <= 200:
        raise ValueError("plate_no must be an integer 0-200 (0 = every plate)")
    if other_machines is not None and (not isinstance(other_machines, list)
                                       or not all(isinstance(m, str) for m in other_machines)):
        raise ValueError("other_machines must be a list of machine preset names")
    if filaments is not None and (not isinstance(filaments, list) or not 1 <= len(filaments) <= 16
                                  or not all(isinstance(f, dict) for f in filaments)):
        raise ValueError("filaments must be a list of 1-16 entries")
    entry = load_index().get(machine)
    if not entry:
        raise ValueError("unknown machine")
    if filaments is None:  # legacy single fields are a 1-entry list
        filament = resolve_filament(entry, filament, filament_id, filament_type)
        presets = [filament]
    else:
        presets = [resolve_filament(entry, None, f.get("filament_id"), f.get("filament_type"))
                   for f in filaments]
    process_name = pick_quality(entry["process"], quality)
    base = json.loads((PROFILES / entry["process"][process_name]).read_text())
    process = build_process(base, choices)

    fresh = ext == ".3mf" and choices.get("fresh") is True
    with LOCK, tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        model_path = tmp / f"model{ext}"  # ponytail: user file name never reaches the shell or disk
        model_path.write_bytes(model)
        process_path = tmp / "process.json"
        process_path.write_text(json.dumps(process))
        outdir = tmp / "out"
        outdir.mkdir()
        if ext == ".3mf":
            _cap_bbs_version(model_path)
            if fresh:  # Standard on a 3MF: BFF's profile, not the designer's settings (the user 2026-11-15)
                _drop_project_settings(model_path)
            if copies > 1:
                _add_instances(model_path, copies)
        cmd = [ORCA_BIN, "--load-settings", f"{PROFILES / entry['file']};{process_path}",
               "--load-filaments", ";".join(str(PROFILES / entry["filament"][f]) for f in presets)]
        if ext != ".3mf" or reorient:
            cmd += ["--orient", "1", "--arrange", "1", "--allow-rotations"]
        elif copies > 1 or fresh:
            cmd += ["--arrange", "1", "--allow-rotations"]
        cmd += ["--slice", str(plate_no), "--export-statistics", "--outputdir", str(outdir),
                "--export-3mf", "out.gcode.3mf"] + [str(model_path)] * (1 if ext == ".3mf" else copies)
        seen_fixes, stripped = [], fresh
        for attempt in range(3):
            try:
                done = subprocess.run(cmd, cwd=tmp, capture_output=True, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                raise SliceError(f"slice timed out after {TIMEOUT} s")
            except OSError as exc:  # binary missing or not executable: a clean 422, not a dropped connection
                raise SliceError(f"slicer not runnable: {exc.strerror}")
            # A newer Bambu Studio writes values Orca does not know (-18); Orca names the fix itself:
            # 'value "enabled" for ensure_vertical_shell_thickness is not valid, using "ensure_all" instead'
            fixes = INVALID_VALUE.findall((done.stdout + done.stderr).decode(errors="replace"))
            seen_fixes += fixes
            if done.returncode == 0 or ext != ".3mf":
                break
            if not stripped and fixes and _apply_fixes(model_path, seen_fixes):
                continue
            code = str((_json_file(outdir / "result.json") or {}).get("return_code"))
            if stripped or code not in ("-18", "-24"):
                break
            # Last resort (caliper from Bambu Studio 02.07, 2026-11-15): settings our Orca cannot read.
            # Keep the objects and their colour assignment, drop the author's settings, place them anew.
            _drop_project_settings(model_path)
            stripped = True
            if "--arrange" not in cmd:
                cmd[cmd.index("--slice"):cmd.index("--slice")] = ["--arrange", "1", "--allow-rotations"]
        if done.returncode != 0:
            result = _json_file(outdir / "result.json")
            if isinstance(result, dict) and (
                    str(result.get("return_code")) == "-50"
                    or str(result.get("error_string") or "").startswith(
                        "No object is fully inside the print volume")):
                size = model_size(model_path)
                index = load_index()
                bed = bed_size(json.loads((PROFILES / entry["file"]).read_text()))
                fits_on = [other for other in other_machines or []
                           if other in index and size and fits(
                               size, bed_size(json.loads((PROFILES / index[other]["file"]).read_text())))]
                raise TooBig(size, bed, fits_on)
            # Orca puts its reason in result.json, not on stderr (exit 232 had an empty stderr, 2026-11-15)
            why = (result or {}).get("error_string") if isinstance(result, dict) else ""
            raise SliceError(f"orca exit {done.returncode} (code {(result or {}).get('return_code') if isinstance(result, dict) else '?'}): "
                             f"{why or done.stderr.decode(errors='replace')[-300:] or done.stdout.decode(errors='replace')[-300:]}"[:600])
        numbers = _plates(outdir)
        if not numbers:
            raise SliceError("orca produced no plate")
        archive = outdir / "out.gcode.3mf"
        gcode_3mf = archive.read_bytes() if archive.is_file() else b""
        plates = []
        for n in numbers:
            try:
                summary = json.loads((outdir / f"plate_{n}.statistics.json").read_text()).get("summary") or {}
            except (OSError, ValueError):
                summary = {}
            png = b""
            if gcode_3mf:
                try:
                    with zipfile.ZipFile(archive) as z:
                        png = z.read(f"Metadata/plate_{n}.png")
                except (KeyError, zipfile.BadZipFile):
                    pass
            # ponytail: whole 3MF per plate, BFF picks the plate index. OrcaSlicer 2.5 headless writes no
            # plate png (no OpenGL); summary keys "time" (s, prepare included) and "weight" (g) checked on CT 112.
            plates.append({"gcode_3mf": gcode_3mf, "seconds": int(summary.get("time") or 0),
                           "grams": round(float(summary.get("weight") or 0), 1), "png": png})
        return {"plates": plates}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"orca": ORCA_VERSION, "profiles": len(load_index())})
        elif self.path == "/profiles":
            index = load_index()
            filaments = sorted({f for entry in index.values() for f in entry["filament"]})
            self._send(200, {"machines": sorted(index), "filaments": filaments})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/slice":
            return self._send(404, {"error": "not found"})
        auth = self.headers.get("Authorization", "")
        if not SECRET or not hmac.compare_digest(auth.encode(), f"Bearer {SECRET}".encode()):
            return self._send(401, {"error": "bad secret"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_UPLOAD * 4 // 3 + 65536:
            return self._send(400, {"error": "missing body or over 100 MB"})
        try:
            req = json.loads(self.rfile.read(length))
            model = base64.b64decode(req["model_b64"], validate=True)
            result = slice_job(model, req["filename"], req["machine"], req["quality"], req.get("filament"),
                               req.get("choices") or {}, bool(req.get("reorient")), req.get("copies", 1),
                               filament_id=req.get("filament_id"), filament_type=req.get("filament_type"),
                               other_machines=req.get("other_machines"), filaments=req.get("filaments"),
                               plate_no=req.get("plate_no", 0))
        except TooBig as exc:
            return self._send(422, {"error": "too_big", "size": list(exc.size) if exc.size else None,
                                    "bed": list(exc.bed), "fits_on": exc.fits_on})
        except SliceError as exc:
            return self._send(422, {"error": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            return self._send(400, {"error": str(exc)})
        for plate in result["plates"]:
            plate["gcode_3mf"] = base64.b64encode(plate["gcode_3mf"]).decode()
            plate["png"] = base64.b64encode(plate["png"]).decode()
        self._send(200, result)


def main():
    if not SECRET:
        raise SystemExit("SLICER_SECRET mancante")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
