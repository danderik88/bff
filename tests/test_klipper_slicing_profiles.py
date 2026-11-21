import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "slicer"))
import flatten_profiles as fp  # noqa: E402
from pygmy import slicer  # noqa: E402


def bundle(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, doc in members.items():
            z.writestr(name, json.dumps(doc))
    return buf.getvalue()


def write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


class KlipperSlicingProfilesTest(unittest.TestCase):
    def test_machine_preset_names(self):
        self.assertEqual(slicer.machine_preset("Sovol SV08"), "Sovol SV08 0.4 nozzle")
        self.assertEqual(slicer.machine_preset("A1 mini", 0.4), "Bambu Lab A1 mini 0.4 nozzle")

    def test_sovol_machine_inherits_from_the_generic_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "Sovol.json", {"machine_list": [{"name": "Sovol SV08 0.4 nozzle", "sub_path": "machine/m.json"},
                                                         {"name": "Sovol SV08 MAX 0.4 nozzle", "sub_path": "machine/x.json"}],
                                        "filament_list": [{"name": "PLA @SV08", "sub_path": "filament/f.json"}]})
            write(root / "Sovol" / "machine" / "m.json", {"name": "Sovol SV08 0.4 nozzle", "inherits": "fdm_machine_common", "x": 1})
            write(root / "Sovol" / "machine" / "x.json", {"name": "Sovol SV08 MAX 0.4 nozzle", "inherits": "fdm_machine_common"})
            write(root / "Sovol" / "filament" / "f.json", {"name": "PLA @SV08", "inherits": "Generic PLA @System", "instantiation": "true",
                                                          "compatible_printers": ["Sovol SV08 0.4 nozzle"]})
            write(root / "OrcaFilamentLibrary.json", {"machine_list": [{"name": "fdm_machine_common", "sub_path": "machine/c.json"}],
                                                      "filament_list": [{"name": "Generic PLA @System", "sub_path": "filament/g.json"}]})
            write(root / "OrcaFilamentLibrary" / "machine" / "c.json", {"name": "fdm_machine_common", "common": 1})
            write(root / "OrcaFilamentLibrary" / "filament" / "g.json", {"name": "Generic PLA @System", "instantiation": "false", "pla": 1})
            machines, kept = fp.presets(root, "Sovol")
            self.assertEqual(machines, ["Sovol SV08 0.4 nozzle"])  # the Max is a different printer, not kept
            self.assertEqual(kept["PLA @SV08"][1]["pla"], 1)
            self.assertEqual(fp.flatten(root, machines[0], vendor="Sovol")["common"], 1)


class UserBundleTest(unittest.TestCase):
    def test_filaments_and_processes_without_a_machine(self):
        got = slicer.user_bundle(bundle({"filament/a.json": {"name": "My PLA", "filament_type": ["PLA"]},
                                         "process/p.json": {"name": "0.20mm Mine", "layer_height": 0.2}}))
        self.assertEqual((list(got["machines"]), list(got["processes"]), list(got["filaments"])),
                         ([], ["0.20mm Mine"], ["My PLA"]))

    def test_a_bundle_with_nothing_in_it_is_refused(self):
        with self.assertRaises(slicer.SliceError) as caught:
            slicer.user_bundle(bundle({"readme.txt": {"hello": True}}))
        self.assertIn("No Orca profiles in this file", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
