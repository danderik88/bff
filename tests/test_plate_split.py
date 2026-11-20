import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "slicer"))
import slicerd  # noqa: E402

INFO = ('<config><plate><metadata key="index" value="1"/><filament id="1" type="PLA"/></plate>'
        '<plate><metadata key="index" value="2"/><filament id="1" type="PETG"/></plate></config>')


def project(**files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name.replace("__", "/"), data)
    return buf.getvalue()


class PlateOnly(unittest.TestCase):
    def test_keeps_one_plate(self):
        src = project(**{"Metadata__plate_1.gcode": "g1", "Metadata__plate_2.gcode": "g2", "Metadata__plate_2.png": "p2",
                         "Metadata__plate_2_small.png": "s2", "Metadata__slice_info.config": INFO, "3D__3dmodel.model": "m"})
        with zipfile.ZipFile(io.BytesIO(slicerd.plate_only(src, 2))) as z:
            names = set(z.namelist())
            info = z.read("Metadata/slice_info.config").decode()
        self.assertEqual(names, {"Metadata/plate_2.gcode", "Metadata/plate_2.png", "Metadata/plate_2_small.png",
                                 "Metadata/slice_info.config", "3D/3dmodel.model"})
        self.assertIn("PETG", info)
        self.assertNotIn("PLA\"", info)


class ForeignProject(unittest.TestCase):
    def check(self, theirs, ours):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "m.json").write_text(json.dumps({"printer_model": ours}))
            model = project(**{"Metadata__project_settings.config": json.dumps({"printer_model": theirs})})
            with mock.patch.object(slicerd, "PROFILES", Path(d)):
                return slicerd._foreign_project(model, {"file": "m.json"})

    def test_bambu_project_on_klipper_is_foreign(self):
        self.assertTrue(self.check("Bambu Lab A1", "MyKlipper"))

    def test_same_printer_keeps_the_project(self):
        self.assertFalse(self.check("Bambu Lab A1", "Bambu Lab A1"))

    def test_plain_model_is_not_foreign(self):
        self.assertFalse(slicerd._foreign_project(b"solid x", {"file": "m.json"}))
