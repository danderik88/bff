#!/usr/bin/env python3
"""Regenerate profiles/filaments.json from an OrcaSlicer system profiles folder.

Usage: python3 tools/filament_catalog.py ["<Orca system dir>"] [output.json]
Default source: OrcaSlicer Nightly's bundled profiles. Its BBL filaments keep Bambu's own ids (GFB01 for
Bambu ASA), which the printer and the AMS recognise; the stable OrcaSlicer and Orca-Cad replace them with
Orca ids (OF...), so do not generate from those.
Default output: profiles/filaments.json

BFF runs on a server without OrcaSlicer, so the catalog is generated here and committed.
Stdlib only.
"""
import json
import os
import re
import sys
from pathlib import Path

ROOTS = ("BBL", "OrcaFilamentLibrary")  # the other vendors (Anycubic, Creality, Custom) are for other printer brands
NOZZLE = re.compile(r"\s+[\d.]+\s+nozzle$")


def first(value):
    """Orca stores most fields as a one-item list; a few are plain scalars."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def load(root):
    """All profiles under <root>/filament, keyed by their "name" (files may sit in vendor subfolders)."""
    profiles = {}
    for path in sorted((Path(root) / "filament").rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            profiles.setdefault(data["name"], data)
    return profiles


def resolve(name, index, seen=()):
    """A profile with its parent chain applied: parent first, then the child's own keys."""
    data = index.get(name, {})
    parent = data.get("inherits")
    if not isinstance(parent, str) or not parent or parent in seen:
        return dict(data)
    out = resolve(parent, index, seen + (name,))
    out.update(data)
    return out


def models_of(data):
    printers = data.get("compatible_printers")
    if isinstance(printers, str):
        printers = [printers]
    models = {NOZZLE.sub("", p).strip() for p in printers or [] if isinstance(p, str) and p.strip()}
    return sorted(models) if models else "all"


def int_or_none(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def entry_of(data):
    fid = first(data.get("filament_id"))
    vendor = first(data.get("filament_vendor"))
    if not (isinstance(fid, str) and fid) or not isinstance(vendor, str) or not vendor:
        return None
    name = data["name"].split(" @")[0]
    return {"id": fid, "name": name, "vendor": vendor, "type": first(data.get("filament_type")) or "",
            "min": int_or_none(first(data.get("nozzle_temperature_range_low"))),
            "max": int_or_none(first(data.get("nozzle_temperature_range_high"))),
            "models": models_of(data)}


def merge(entries):
    """One entry per filament_id: union of models, first name/vendor/type/temps."""
    merged, order = {}, []
    for e in entries:
        old = merged.get(e["id"])
        if old is None:
            merged[e["id"]] = e
            order.append(e["id"])
            continue
        if old["models"] == "all" or e["models"] == "all":
            old["models"] = "all"
        else:
            old["models"] = sorted(set(old["models"]) | set(e["models"]))
    return [merged[i] for i in order]


def normalize(entries):
    """Generic names under the Generic vendor; one entry per vendor+name, Bambu's own (GF) id preferred."""
    for e in entries:
        if e["name"].startswith("Generic "):
            e["vendor"] = "Generic"
    kept = {}
    for e in entries:
        old = kept.get((e["vendor"], e["name"]))
        if old is None:
            kept[(e["vendor"], e["name"])] = e
            continue
        if e["id"].startswith("GF") and not old["id"].startswith("GF"):
            kept[(e["vendor"], e["name"])] = e
            old, e = e, old
        if old["models"] == "all" or e["models"] == "all":
            old["models"] = "all"
        else:
            old["models"] = sorted(set(old["models"]) | set(e["models"]))
    return list(kept.values())


def vendor_key(vendor):
    if vendor.lower().startswith("bambu"):
        return (0, "")
    if str(vendor).startswith("Bambu"):
        return (0, "")
    return (2, "") if vendor == "Generic" else (1, vendor)


def build(system):
    bbl = load(Path(system) / "BBL")
    lib = load(Path(system) / "OrcaFilamentLibrary")
    index = {**lib, **bbl}  # a parent is looked up in its own folder first, then in OrcaFilamentLibrary
    entries = []
    for root in (bbl, lib):
        for name in root:
            data = resolve(name, index)
            if data.get("instantiation") != "true":
                continue
            e = entry_of(data)
            if e:
                entries.append(e)
    entries = normalize(merge(entries))
    entries.sort(key=lambda e: (vendor_key(e["vendor"]), e["type"], e["name"]))
    try:
        version = json.loads((Path(system) / "BBL" / "BBL.json").read_text()).get("version")
    except (OSError, ValueError):
        version = None
    return {"generated": version, "filaments": entries}


def main(argv):
    system = argv[1] if len(argv) > 1 else "/Applications/OrcaSlicer Nightly.app/Contents/Resources/profiles"
    out = Path(argv[2] if len(argv) > 2 else "profiles/filaments.json")
    if not (Path(system) / "BBL" / "filament").is_dir():
        sys.exit(f"Profili OrcaSlicer non trovati in {system}")
    doc = build(system)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    counts = {}
    for e in doc["filaments"]:
        counts[e["vendor"]] = counts.get(e["vendor"], 0) + 1
    print(f"{len(doc['filaments'])} filamenti da OrcaSlicer {doc['generated']} -> {out}")
    for vendor, count in sorted(counts.items(), key=lambda kv: (vendor_key(kv[0]), kv[0])):
        print(f"  {vendor}: {count}")


if __name__ == "__main__":
    main(sys.argv)
