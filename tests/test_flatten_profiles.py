"""Profile flattener: Bambu inheritance chains resolved from local files, one JSON preset per file."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tools" / "slicer"))
import flatten_profiles  # noqa: E402

FIXTURE = ROOT / "tests" / "data" / "profiles"


class Chain(unittest.TestCase):
    def test_chain_resolved(self):
        machine = flatten_profiles.flatten(FIXTURE, "Bambu Lab A1 0.4 nozzle")
        self.assertEqual(["12000"], machine["machine_max_acceleration_x"])  # leaf beats base
        self.assertIn("bed_shape", machine)  # base-only key survives
        self.assertNotIn("inherits", machine)
        self.assertEqual("User", machine["from"])

        process = flatten_profiles.flatten(FIXTURE, "0.20mm Standard @BBL A1")
        self.assertEqual(["15%"], process["sparse_infill_density"])  # leaf
        self.assertEqual(["2"], process["wall_loops"])  # mid level
        self.assertEqual(["0.2"], process["layer_height"])  # base

        filament = flatten_profiles.flatten(FIXTURE, "Bambu PLA Basic @BBL A1")
        self.assertEqual(["230"], filament["nozzle_temperature"])  # leaf
        self.assertEqual("GFA00", filament["filament_id"])  # mid level
        self.assertEqual(["PLA"], filament["filament_type"])  # base

    def test_unknown_parent_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "BBL" / "process").mkdir(parents=True)
            (root / "BBL" / "process" / "orphan.json").write_text(
                json.dumps({"name": "orphan", "inherits": "missing parent"}))
            (root / "BBL.json").write_text(json.dumps(
                {"process_list": [{"name": "orphan", "sub_path": "BBL/process/orphan.json"}]}))
            with self.assertRaises(KeyError) as caught:
                flatten_profiles.flatten(root, "orphan")
            self.assertIn("missing parent", str(caught.exception))


class Cli(unittest.TestCase):
    def run_cli(self, out):
        done = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "slicer" / "flatten_profiles.py"), str(FIXTURE), out],
            capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)

    def test_writes_one_json_per_bambu_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(tmp)
            names = sorted(p.name for p in Path(tmp).glob("*.json") if p.name != "index.json")
            self.assertEqual(["0.20mm Standard @BBL A1.json",
                              "Bambu Lab A1 0.4 nozzle.json",
                              "Bambu PLA Basic @BBL A1.json",
                              "Bambu Support For PA_PET @BBL A1M.json"], names)
            profile = json.loads((Path(tmp) / "Bambu PLA Basic @BBL A1.json").read_text())
            self.assertEqual("User", profile["from"])

    def test_slash_in_name_sanitized(self):
        self.assertEqual("Bambu Support For PA_PET @BBL A1M.json",
                         flatten_profiles.file_name("Bambu Support For PA/PET @BBL A1M"))
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(tmp)
            self.assertTrue((Path(tmp) / "Bambu Support For PA_PET @BBL A1M.json").is_file())

    def test_selected_by_compatible_printers(self):
        machines, kept = flatten_profiles.presets(FIXTURE)
        self.assertEqual(["Bambu Lab A1 0.4 nozzle"], machines)
        self.assertIn("Bambu Support For PA/PET @BBL A1M", kept)  # other suffix, compatible
        self.assertNotIn("0.20mm Standard @BBL X1C", kept)  # not compatible with the kept machine
        self.assertNotIn("Support base", kept)  # instantiation: false

    def test_index_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(tmp)
            index = json.loads((Path(tmp) / "index.json").read_text())
        self.assertEqual({
            "Bambu Lab A1 0.4 nozzle": {
                "file": "Bambu Lab A1 0.4 nozzle.json",
                "process": {
                    "0.20mm Standard @BBL A1": "0.20mm Standard @BBL A1.json",
                    "Bambu Support For PA/PET @BBL A1M": "Bambu Support For PA_PET @BBL A1M.json",
                },
                "filament": {
                    "Bambu PLA Basic @BBL A1": "Bambu PLA Basic @BBL A1.json",
                },
            },
        }, index)


if __name__ == "__main__":
    unittest.main()
