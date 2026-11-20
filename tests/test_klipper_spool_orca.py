"""Klipper spools from the user's Orca presets: server picks, slicer parent."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "slicer"))
from pygmy import server  # noqa: E402
import slicerd  # noqa: E402


ORCA = {
    "machine": "E3K 0.4 nozzle",
    "machines": {"E3K 0.4 nozzle": {"name": "E3K 0.4 nozzle", "inherits": "MyKlipper 0.4 nozzle",
                                    "nozzle_diameter": ["0.4"]}},
    "processes": {},
    "filaments": {"PolyTerra PLA @E3K": {"name": "PolyTerra PLA @E3K", "inherits": "Generic PLA @System",
                                        "filament_type": ["PLA"]}},
    "loaded": "PolyTerra PLA @E3K",
}


class OrcaPresetPicks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"STATE_DIRECTORY": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def write_orca(self, alias="sv08", doc=ORCA):
        path = Path(self.tmp.name) / "orca" / f"{alias}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def test_user_preset_spool_uses_the_preset(self):
        self.write_orca()
        user = server.user_presets_for("sv08", [{"filament_id": "user:PolyTerra PLA @E3K", "filament_type": "PLA"}])
        chosen = user["filaments"][0]
        self.assertEqual((chosen["name"], chosen["inherits"], chosen["_type"]),
                         ("PolyTerra PLA @E3K", "Generic PLA @System", "PLA"))

    def test_system_spool_is_kept_by_id(self):
        self.write_orca()
        user = server.user_presets_for("sv08", [{"filament_id": "GFOT006", "filament_type": "PLA", "filament_name": "Overture Air PLA"}])
        self.assertEqual(user["filaments"][0], {"_type": "PLA", "_id": "GFOT006", "_name": "Overture Air PLA"})

    def test_no_filament_id_falls_back_to_loaded(self):
        self.write_orca()
        user = server.user_presets_for("sv08", [{"filament_id": None, "filament_type": "PLA"}])
        chosen = user["filaments"][0]
        self.assertEqual((chosen["name"], chosen["_type"]), ("PolyTerra PLA @E3K", "PLA"))

    def test_spool_entries_list_user_presets(self):
        self.write_orca()
        self.assertEqual(server.user_spool_entries("sv08"), [
            {"id": "user:PolyTerra PLA @E3K", "name": "PolyTerra PLA @E3K", "vendor": "My Orca profiles",
             "type": "PLA", "min": 190, "max": 230, "models": "all"}])
        self.assertEqual(server.user_spool_entries("nope"), [])


class SlicerUserParent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for rel, doc in {
            "machine/kl.json": {"name": "MyKlipper 0.4 nozzle", "gcode_flavor": "klipper"},
            "process/std.json": {"name": "0.20mm Standard @KL", "wall_loops": "2"},
            "filament/generic.json": {"name": "Generic PLA @System", "filament_id": "GFA001",
                                      "filament_type": ["PLA"], "filament_flow_ratio": ["1"]},
            "filament/poly.json": {"name": "PolyTerra PLA", "inherits": "Generic PLA @System",
                                   "filament_id": "GFA001", "filament_type": ["PLA"],
                                   "filament_flow_ratio": ["0.98"]},
        }.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc))
        self.entry = {"file": "machine/kl.json", "process": {"0.20mm Standard @KL": "process/std.json"},
                      "filament": {"Generic PLA @System": "filament/generic.json", "PolyTerra PLA": "filament/poly.json"}}

    def test_spool_id_picks_the_brand_preset_as_parent(self):
        user = {"machine": {"name": "My E3K", "inherits": "MyKlipper 0.4 nozzle"},
                "processes": {"0.20mm Standard @MyE3K": {"name": "0.20mm Standard @MyE3K",
                                                         "inherits": "0.20mm Standard @KL"}},
                "filaments": [{"_type": "PLA", "_id": "GFA001"}]}
        with mock.patch.object(slicerd, "PROFILES", self.root):
            out = slicerd.resolve_user(self.entry, user, "normal")
        flat = out["filaments"][0]
        self.assertEqual(flat["name"], "PolyTerra PLA")  # not the Generic one
        self.assertEqual(flat["filament_flow_ratio"], ["0.98"])
        self.assertNotIn("_id", flat)
        self.assertNotIn("_type", flat)
        self.assertEqual(flat["compatible_printers"], [])


if __name__ == "__main__":
    unittest.main()


class NameFallback(unittest.TestCase):
    """Pygmy's catalog has Bambu ids, the slicer's Orca has its own: the name still finds the preset."""

    def test_name_matches_when_the_id_differs(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "slicer"))
        import slicerd
        entry = {"filament": {"Generic PLA @System": "g.json", "Overture PLA @System": "o.json"}}
        with mock.patch.object(slicerd, "_load_filament", lambda f: {"filament_id": "OF" + f, "filament_type": "PLA"}):
            self.assertEqual(slicerd.resolve_filament(entry, None, "GFL04", "PLA", "Overture PLA"), "Overture PLA @System")
            self.assertEqual(slicerd.resolve_filament(entry, None, "GFL04", "PLA", None), "Generic PLA @System")
