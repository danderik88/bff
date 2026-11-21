#!/usr/bin/env python3
"""slicerd: tiny HTTP wrapper around the pinned OrcaSlicer CLI.

POST /slice (Authorization: Bearer <SLICER_SECRET>), JSON
  {model_b64, filename, machine, quality, filament, choices, reorient, copies}
  -> 200 {"plates": [{"gcode_3mf", "seconds", "grams", "png"}]} (bytes as base64)
  -> 400 bad input, 401 bad secret, 422 slicer failure.
GET /health -> {"orca": ORCA_VERSION, "profiles": n}
GET /profiles -> {"machines": [...], "filaments": [...]}
GET /orca-user (Authorization: Bearer <SLICER_SECRET>) -> zip of the Orca Cloud presets synced on the slicer

Profiles come from flatten_profiles.py (`index.json` + one JSON per preset). Stdlib only.
"""
import base64
import hmac
import io
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ORCA_VERSION = "2.5.0"
ORCA_BIN = os.environ.get("ORCA_BIN", "/opt/orca/AppRun")
PROFILES = Path(os.environ.get("SLICER_PROFILES", "/opt/slicer/profiles"))
SECRET = os.environ.get("SLICER_SECRET", "")
PORT = int(os.environ.get("SLICER_PORT", "8765"))
BIND = os.environ.get("SLICER_BIND", "0.0.0.0")  # 127.0.0.1 when BFF runs on the same host
MAX_UPLOAD = 100 * 1024 * 1024
TIMEOUT = 300
MODEL_EXT = (".stl", ".step", ".stp", ".3mf")
BUNDLE_EXT = ".zip"  # several meshes from one page, sliced together (2026-11-17)
BUNDLE_EXTS = (".stl", ".step", ".stp", ".obj", ".3mf")
BUNDLE_MAX = 200 * 1024 * 1024
PATTERNS = ("grid", "gyroid", "cubic", "honeycomb", "line")
KLIPPER_TEXTURED_MIN_BED = 60  # the user's E3K: Generic PLA's 55 C does not stick on a textured PEI plate
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


def bundle_members(data, tmp):
    """A bundle's meshes written to tmp as model_0.stl, model_1.step... (member names never touch disk)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = [m for m in z.infolist() if not m.is_dir()]
            if not members:
                raise ValueError("the zip has no files")
            if sum(m.file_size for m in members) > BUNDLE_MAX:
                raise ValueError("the files in the zip are over 200 MB in all")
            paths = []
            for i, member in enumerate(members):
                member_ext = Path(member.filename).suffix.lower()
                if member_ext not in BUNDLE_EXTS:
                    raise ValueError("every file in the zip must be .stl, .step, .stp, .obj or .3mf")
                path = tmp / f"model_{i}{member_ext}"
                path.write_bytes(z.read(member))
                paths.append(path)
            return paths
    except zipfile.BadZipFile:
        raise ValueError("the model is not a readable zip") from None


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


def resolve_filament(entry, filament=None, filament_id=None, filament_type=None, filament_name=None):
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
    if filament_name:  # Pygmy's catalog keeps Bambu ids (GFL04), this Orca has its own (OFc3xdm9): same name
        match = sorted(name for name in entry["filament"] if name.split(" @")[0] == filament_name)
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


def is_klipper(machine_json: dict) -> bool:
    """Klipper and Custom-vendor machines (MyKlipper, the user's presets on top of it) say so here."""
    return str(machine_json.get("gcode_flavor") or "").lower() == "klipper" \
        or "klipper" in str(machine_json.get("name") or "").lower()


def klipper_textured_bed(filament: dict) -> dict:
    """Textured PEI on Klipper: no bed temperature below KLIPPER_TEXTURED_MIN_BED, Orca's shape kept."""
    def bump(value):
        try:
            return f"{max(float(value), KLIPPER_TEXTURED_MIN_BED):g}"
        except (TypeError, ValueError):
            return value
    out = dict(filament)
    for key in ("textured_plate_temp", "textured_plate_temp_initial_layer"):
        value = out.get(key)
        if value is not None:  # a string or a one-item list of strings
            out[key] = [bump(v) for v in value] if isinstance(value, list) else bump(value)
    return out


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


PLATE_FILE = re.compile(r"Metadata/(?:plate|plate_no_light|top|pick)_(\d+)(?:_small)?\.(?:gcode|gcode\.md5|png|json)")


def plate_only(gcode_3mf: bytes, n: int) -> bytes:
    """The sliced project with plate n alone: Pygmy prints a file that holds exactly one plate."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(gcode_3mf)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            m = PLATE_FILE.fullmatch(info.filename)
            if m and int(m.group(1)) != n:
                continue
            data = src.read(info.filename)
            if info.filename == "Metadata/slice_info.config":
                data = re.sub(r"<plate>.*?</plate>\s*",
                              lambda b: b.group(0) if f'key="index" value="{n}"' in b.group(0) else "",
                              data.decode(), flags=re.S).encode()
            dst.writestr(info, data)
    return out.getvalue()


def _plates(outdir: Path):
    found = []
    for gcode in outdir.glob("plate_*.gcode"):
        m = re.fullmatch(r"plate_(\d+)\.gcode", gcode.name)
        if m:
            found.append(int(m.group(1)))
    return sorted(found)


INVALID_VALUE = re.compile(r'value "([^"]*)" for (\w+) is not valid, using "([^"]*)" instead')
# 'raft_first_layer_expansion: -1 not in range [0, 3.40282e+38]' (the user 2026-11-15): clamp to the nearer bound
OUT_OF_RANGE = re.compile(r'(\w+): (-?[\d.]+(?:e[+-]?\d+)?) not in range \[(-?[\d.]+(?:e[+-]?\d+)?), (-?[\d.]+(?:e[+-]?\d+)?)\]')


def orca_fixes(log):
    """(bad, key, good) for every value Orca rejects: its own suggestion, or the out-of-range value clamped."""
    out = [tuple(f) for f in INVALID_VALUE.findall(log)]
    for key, bad, lo, hi in OUT_OF_RANGE.findall(log):
        out.append((bad, key, lo if float(bad) < float(lo) else hi))
    return out


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


# printer.cfg values BFF may put on a machine preset (Other Klipper printer); anything else is refused.
OVERRIDE_KEYS = {"printable_area", "printable_height", "nozzle_diameter", "gcode_flavor", "use_firmware_retraction",
                 "machine_start_gcode", "machine_end_gcode", "machine_max_speed_x", "machine_max_speed_y",
                 "machine_max_speed_z", "machine_max_acceleration_x", "machine_max_acceleration_y",
                 "machine_max_acceleration_extruding"}


def with_overrides(machine_json: dict, overrides) -> dict:
    if not overrides:
        return machine_json
    if not isinstance(overrides, dict) or set(overrides) - OVERRIDE_KEYS or not all(
            isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v)) for v in overrides.values()):
        raise ValueError("machine_overrides: unknown key or value type")
    merged = {**machine_json, **overrides}
    # OrcaSlicer rejects firmware retraction together with wipe (exit -18).
    if merged.get("use_firmware_retraction") == "1":
        merged["wipe"] = ["0"] * len(machine_json.get("wipe") or ["1"])
    return merged


UNSAFE_PRESET_KEYS = ("post_process",)  # never run a script that came with a user preset


def _merged(parent_file, delta, name):
    out = json.loads((PROFILES / parent_file).read_text())
    out.update({k: v for k, v in delta.items() if k not in ("inherits", "_type")})
    for key in UNSAFE_PRESET_KEYS:
        out.pop(key, None)
    out.update({"name": name, "from": "User"})
    return out


def resolve_user(entry, user, quality):
    """The user's machine, process and filaments on top of the system presets they inherit from."""
    if not isinstance(user, dict) or not isinstance(user.get("machine"), dict) \
            or not isinstance(user.get("processes"), dict) or not isinstance(user.get("filaments"), list):
        raise ValueError("user_presets must have machine, processes and filaments")
    machine = _merged(entry["file"], user["machine"], str(user["machine"].get("name") or "User printer"))
    procs = {n: p for n, p in user["processes"].items() if isinstance(p, dict) and p.get("inherits") in entry["process"]}
    if not procs:
        raise ValueError("none of your process profiles is based on this printer's presets")
    name = pick_quality(procs, quality)
    process = _merged(entry["process"][procs[name]["inherits"]], procs[name], name)
    process["compatible_printers"] = [machine["name"]]
    filaments = []
    for f in user["filaments"]:
        if not isinstance(f, dict):
            raise ValueError("user_presets filaments must be objects")
        parent = f.get("inherits") if f.get("inherits") in entry["filament"] else \
            resolve_filament(entry, None, f.get("_id"), f.get("_type") or "PLA", f.get("_name"))  # a system spool by id, else same type
        f = {k: v for k, v in f.items() if k not in ("_id", "_type", "_name")}
        flat = _merged(entry["filament"][parent], f, str(f.get("name") or parent))
        flat["compatible_printers"] = []
        filaments.append(flat)
    return {"machine": machine, "process": process, "filaments": filaments}


def _foreign_project(model: bytes, entry: dict) -> bool:
    """A 3MF project made for another printer model (a Bambu project on a Klipper printer): its own
    settings do not apply, so it is sliced like Standard. Unknown either way: keep the project."""
    try:
        with zipfile.ZipFile(io.BytesIO(model)) as z:
            theirs = json.loads(z.read("Metadata/project_settings.config")).get("printer_model")
        ours = json.loads((PROFILES / entry["file"]).read_text()).get("printer_model")
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return False
    return bool(theirs and ours and theirs != ours)


def slice_job(model: bytes, filename: str, machine: str, quality: str, filament: str = None,
              choices: dict = None, reorient: bool = False, copies: int = 1,
              filament_id: str = None, filament_type: str = None,
              other_machines: list = None, filaments: list = None, plate_no: int = 0,
              machine_overrides: dict = None, user_presets: dict = None) -> dict:
    if not model or len(model) > MAX_UPLOAD:
        raise ValueError("model empty or over 100 MB")
    name = Path(filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in MODEL_EXT + (BUNDLE_EXT,):
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
        presets = [resolve_filament(entry, None, f.get("filament_id"), f.get("filament_type"), f.get("filament_name"))
                   for f in filaments]
    process_name = pick_quality(entry["process"], quality)
    base = json.loads((PROFILES / entry["process"][process_name]).read_text())
    user = resolve_user(entry, user_presets, quality) if user_presets else None
    if user:
        base = user["process"]
    process = build_process(base, choices)

    fresh = ext == ".3mf" and (choices.get("fresh") is True or _foreign_project(model, entry))
    with LOCK, tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        model_path = tmp / f"model{ext}"  # ponytail: user file name never reaches the shell or disk
        if ext == BUNDLE_EXT:
            model_paths = bundle_members(model, tmp)
        else:
            model_path.write_bytes(model)
            model_paths = [model_path]
        process_path = tmp / "process.json"
        process_path.write_text(json.dumps(process))
        machine_path = PROFILES / entry["file"]
        if machine_overrides:  # Other Klipper printer: its printer.cfg on top of the preset
            machine = with_overrides(json.loads(machine_path.read_text()), machine_overrides)
            machine_path = tmp / "machine.json"
            machine_path.write_text(json.dumps(machine))
        filament_paths = [PROFILES / entry["filament"][f] for f in presets]
        if user:  # the user's own presets, resolved
            machine_path = tmp / "machine.json"
            machine_path.write_text(json.dumps(user["machine"]))
            filament_paths = []
            for i, flat in enumerate(user["filaments"]):
                filament_paths.append(tmp / f"filament_{i}.json")
                filament_paths[-1].write_text(json.dumps(flat))
        if choices.get("plate") == "Textured PEI Plate" and is_klipper(json.loads(machine_path.read_text())):
            flats = [klipper_textured_bed(json.loads(Path(p).read_text())) for p in filament_paths]
            filament_paths = []
            for i, flat in enumerate(flats):
                filament_paths.append(tmp / f"filament_{i}.json")
                filament_paths[-1].write_text(json.dumps(flat))
        outdir = tmp / "out"
        outdir.mkdir()
        if ext == ".3mf":
            _cap_bbs_version(model_path)
            if fresh:  # Standard on a 3MF: BFF's profile, not the designer's settings (the user 2026-11-15)
                _drop_project_settings(model_path)
            if copies > 1:
                _add_instances(model_path, copies)
        cmd = [ORCA_BIN, "--load-settings", f"{machine_path};{process_path}",
               "--load-filaments", ";".join(str(p) for p in filament_paths)]
        if ext != ".3mf" or reorient:
            cmd += ["--orient", "1", "--arrange", "1", "--allow-rotations"]
        elif copies > 1 or fresh:
            cmd += ["--arrange", "1", "--allow-rotations"]
        cmd += ["--slice", str(plate_no), "--export-statistics", "--outputdir", str(outdir),
                "--export-3mf", "out.gcode.3mf"] + [str(p) for p in model_paths] * (1 if ext == ".3mf" else copies)
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
            fixes = orca_fixes((done.stdout + done.stderr).decode(errors="replace"))
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
                size = model_size(model_paths[0])
                index = load_index()
                bed = bed_size(json.loads(machine_path.read_text()))
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
            # One file per plate (plate_only). OrcaSlicer 2.5 headless writes no
            # plate png (no OpenGL); summary keys "time" (s, prepare included) and "weight" (g) checked on CT 112.
            plates.append({"gcode_3mf": plate_only(gcode_3mf, n) if len(numbers) > 1 and gcode_3mf else gcode_3mf, "seconds": int(summary.get("time") or 0),
                           "grams": round(float(summary.get("weight") or 0), 1), "png": png})
        return {"plates": plates}


# --- Orca Cloud: the OrcaSlicer GUI keeps the user's cloud presets synced; GET /orca-user serves them. ---
ORCA_DATA = Path(os.environ.get("ORCA_DATA", "/home/slicer/orca-gui"))
NOVNC_PORT = int(os.environ.get("NOVNC_PORT", "6080"))
LOGIN_TTL = 900        # s the sign-in window stays reachable
GUI_RESTART_S = 3600   # Orca pulls cloud presets only at start and login (GUI_App::start_sync_user_preset)
_GUI = {"proc": None, "started": 0.0, "login": None}  # login: (processes, password, deadline)
_GUI_LOCK = threading.Lock()


def orca_logged_in():
    root = ORCA_DATA / "user"
    return root.is_dir() and any((d / "sync_state").is_file() for d in root.iterdir())


def _orca_conf():
    """A container has no keyring: Orca keeps the login in an encrypted file (keyed to /etc/machine-id).
    Orca rewrites its config on exit, so this only runs while it is stopped."""
    path = ORCA_DATA / "OrcaSlicer.conf"
    try:
        text = path.read_text()
        conf = json.loads(text[:text.rfind("}") + 1])  # drop Orca's trailing "# MD5 checksum" line
    except (OSError, ValueError):
        conf = {}
    if not isinstance(conf, dict):
        conf = {}
    if conf.setdefault("app", {}).get("use_encrypted_token_file") != "true":
        conf["app"]["use_encrypted_token_file"] = "true"
        ORCA_DATA.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(conf, indent=4) + "\n")


def _gui_env():
    return {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99"), "LANG": "en_US.UTF-8",
            "BROWSER": "/opt/slicer/orca-browser", "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt"}


def _spawn(cmd):
    return subprocess.Popen(cmd, env=_gui_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _end(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()


def _stop_login():
    login, _GUI["login"] = _GUI["login"], None
    for proc in (login or ((),))[0]:
        _end(proc)


def gui_tick(now=None):
    """The GUI runs while the user is signed in or signing in; restarted hourly to pull new presets."""
    now = time.monotonic() if now is None else now
    with _GUI_LOCK:
        if _GUI["login"] and (now > _GUI["login"][2] or (not _GUI["login"][3] and orca_logged_in())):
            _stop_login()  # timed out, or the sign-in just went through: close the window
        login = _GUI["login"]
        want = bool(login) or orca_logged_in()
        proc = _GUI["proc"]
        alive = proc is not None and proc.poll() is None
        if alive and not login and (not want or now - _GUI["started"] > GUI_RESTART_S):
            _end(proc)
            alive = False
        if want and not alive:
            _orca_conf()
            _GUI["proc"], _GUI["started"] = _spawn([ORCA_BIN, "--datadir", str(ORCA_DATA)]), now


def start_login():
    """Show the OrcaSlicer window in a browser (noVNC) for the sign-in. The password is new every time."""
    with _GUI_LOCK:
        if not _GUI["login"]:
            password = secrets.token_hex(4)  # VNC passwords stop at 8 characters
            procs = (_spawn(["openbox"]),  # without a window manager the window gets no keyboard focus
                     _spawn(["x11vnc", "-display", os.environ.get("DISPLAY", ":99"), "-rfbport", "5900", "-localhost",
                             "-passwd", password, "-forever", "-shared", "-quiet"]),
                     _spawn(["websockify", "--web", "/usr/share/novnc", str(NOVNC_PORT), "localhost:5900"]))
            _GUI["login"] = (procs, password, time.monotonic() + LOGIN_TTL, orca_logged_in())
        password = _GUI["login"][1]
    gui_tick()
    return {"port": NOVNC_PORT, "password": password}


def stop_login():
    with _GUI_LOCK:
        _stop_login()
    gui_tick()
    return {}


def _gui_loop():
    while True:
        try:
            gui_tick()
        except Exception as err:  # never let the slicer die for the GUI
            print(f"orca gui: {err}", flush=True)
        time.sleep(30)


def orca_user_zip():
    """Zip of the machine/, process/ and filament/ presets of the one Orca Cloud user dir, or None."""
    root = Path(os.environ.get("ORCA_USER_ROOT", str(ORCA_DATA / "user")))
    dirs = [d for d in root.iterdir() if (d / "sync_state").is_file()] if root.is_dir() else []
    if len(dirs) != 1:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for part in ("machine", "process", "filament"):
            base = dirs[0] / part
            if not base.is_dir():
                continue
            for f in sorted(base.rglob("*.json")):
                if ".bak" not in f.name:
                    archive.write(f, f.relative_to(dirs[0]).as_posix())
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        return bool(SECRET) and hmac.compare_digest(auth.encode(), f"Bearer {SECRET}".encode())

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
        elif self.path == "/orca-status":
            if not self._authorized():
                return self._send(401, {"error": "bad secret"})
            self._send(200, {"logged_in": orca_logged_in(), "login_open": _GUI["login"] is not None})
        elif self.path == "/orca-user":
            if not self._authorized():
                return self._send(401, {"error": "bad secret"})
            data = orca_user_zip()
            if data is None:
                return self._send(404, {"error": "no Orca Cloud login on the slicer"})
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/orca-login":
            if not self._authorized():
                return self._send(401, {"error": "bad secret"})
            try:
                action = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1024)) or b"{}").get("action")
            except (ValueError, AttributeError):
                action = None
            if action not in ("start", "stop"):
                return self._send(400, {"error": "action must be start or stop"})
            try:
                return self._send(200, start_login() if action == "start" else stop_login())
            except OSError as err:  # x11vnc / websockify missing (LXC install without them)
                return self._send(503, {"error": f"sign-in window not available: {err}"})
        if self.path != "/slice":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
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
                               machine_overrides=req.get("machine_overrides"), user_presets=req.get("user_presets"),
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
    if os.environ.get("ORCA_GUI", "1") == "1":
        threading.Thread(target=_gui_loop, daemon=True).start()
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
