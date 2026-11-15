#!/usr/bin/env python3
"""Flatten OrcaSlicer's Bambu profile chains into standalone presets.

Usage: python3 tools/slicer/flatten_profiles.py <profiles_root> <out_dir>

`<profiles_root>` is a local checkout of OrcaSlicer's `resources/profiles` at the pinned tag
(see ORCA_VERSION in slicerd.py). Nothing is downloaded: the slicer container clones the tag once
and runs this script, so slicing never depends on the network.

For every `Bambu Lab <model> 0.4 nozzle` machine, and every process / filament preset whose
flattened `compatible_printers` contains one of those machines (skipping base presets with
`instantiation: "false"`), writes `<preset name>.json` with the whole `inherits` chain applied,
`inherits` dropped and `from: "User"`. Characters outside `[A-Za-z0-9 ._@()+-]` in the file name
become `_`. Also writes `index.json` mapping each machine to its compatible process/filament
presets.

Stdlib only.
"""
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

KINDS = ("machine_model_list", "machine_list", "process_list", "filament_list")
MACHINE = re.compile(r"^Bambu Lab (.+) 0\.4 nozzle$")
UNSAFE = re.compile(r"[^A-Za-z0-9 ._@()+-]")


def file_name(preset_name: str) -> str:
    """Preset names can contain `/` (e.g. `Bambu Support For PA/PET @BBL A1`): keep them in the
    JSON, but not in the file name."""
    return f"{UNSAFE.sub('_', preset_name)}.json"


@lru_cache(maxsize=None)
def _index(root):
    """name -> (kind, path) from the BBL.json index. Handles both layouts shipped over the years:
    BBL.json at the profiles root, or inside BBL/ with sub_paths relative to either folder."""
    root = Path(root)
    index_file = root / "BBL.json"
    if not index_file.is_file():
        index_file = root / "BBL" / "BBL.json"
    vendor = index_file.parent
    doc = json.loads(index_file.read_text())
    out = {}
    for kind in KINDS:
        for entry in doc.get(kind) or []:
            name, sub = entry.get("name"), entry.get("sub_path")
            if not (name and sub):
                continue
            # ponytail: try the three plausible roots instead of parsing Orca's path convention.
            path = next((p for p in (vendor / sub, root / sub, root / "BBL" / sub) if p.is_file()), vendor / sub)
            out.setdefault(name, (kind, path))
    return out


def flatten(root: Path, name: str, seen=()) -> dict:
    """One profile with its whole `inherits` chain applied: parent first, then the child's own keys."""
    index = _index(Path(root))
    if name not in index:
        raise KeyError(f"preset {name!r} not in BBL.json under {root}")
    data = json.loads(index[name][1].read_text())
    parent = data.pop("inherits", None)
    if isinstance(parent, str) and parent:
        if parent not in index:
            raise KeyError(f"preset {name!r} inherits unknown preset {parent!r}")
        if parent in seen:
            raise KeyError(f"inheritance loop: {parent!r} reaches itself")
        profile = flatten(root, parent, seen + (name,))
    else:
        profile = {}
    # OrcaSlicer 2.5 keeps the start/end/filament-change G-code in "template" presets listed under
    # "include": they sit between the parent and the preset's own keys.
    for inc in data.pop("include", None) or []:
        if inc not in index:
            raise KeyError(f"preset {name!r} includes unknown preset {inc!r}")
        if inc in seen:
            raise KeyError(f"include loop: {inc!r} reaches itself")
        part = flatten(root, inc, seen + (name,))
        profile.update({k: v for k, v in part.items() if k not in ("name", "type", "instantiation", "from")})
    profile.update(data)
    profile["from"] = "User"
    return profile


def presets(root):
    """Kept machines plus the process / filament presets compatible with at least one of them.

    Returns `(machines, {preset name: (kind, flattened)})`. Compatibility comes from the flattened
    `compatible_printers`, not from the `@BBL <model>` name suffix: an A1 mini preset ends with
    `@BBL A1M` and a P1S one often ships as `@BBL X1C`. Base presets (`instantiation: "false"`)
    are not instantiable and are skipped.
    """
    index = _index(Path(root))
    machines = [name for name, (kind, _path) in index.items()
                if kind == "machine_list" and MACHINE.match(name)]
    machine_set = set(machines)
    kept = {}
    for name, (kind, _path) in index.items():
        if kind not in ("process_list", "filament_list"):
            continue
        flat = flatten(root, name)
        if flat.get("instantiation") in ("false", False):
            continue
        if machine_set & set(flat.get("compatible_printers") or ()):
            kept[name] = (kind, flat)
    return machines, kept


def main(argv):
    if len(argv) != 3:
        sys.exit("uso: flatten_profiles.py <profiles_root> <out_dir>")
    root, out = Path(argv[1]), Path(argv[2])
    if not _index(root):
        sys.exit(f"indice BBL.json non trovato o vuoto in {root}")
    out.mkdir(parents=True, exist_ok=True)
    machines, kept = presets(root)
    catalog = {name: {"file": file_name(name), "process": {}, "filament": {}} for name in machines}
    for name in machines:
        (out / file_name(name)).write_text(json.dumps(flatten(root, name), ensure_ascii=False, indent=1) + "\n")
    for name, (kind, flat) in kept.items():
        (out / file_name(name)).write_text(json.dumps(flat, ensure_ascii=False, indent=1) + "\n")
        compatible = set(flat.get("compatible_printers") or ())
        section = "process" if kind == "process_list" else "filament"
        for machine in machines:
            if machine in compatible:
                catalog[machine][section][name] = file_name(name)
    (out / "index.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(machines) + len(kept)} preset appiattiti da {root} -> {out}")


if __name__ == "__main__":
    main(sys.argv)
