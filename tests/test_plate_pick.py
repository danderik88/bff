"""Multi-plate projects: pick one plate, slice only it, start it with its own number (the user 2026-11-15)."""
import base64
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "bff_mcp"))
sys.path.insert(0, str(ROOT / "tools" / "slicer"))
import slicer  # noqa: E402
import slicerd  # noqa: E402
from bff_mcp import server  # noqa: E402
from printer_link import project_file  # noqa: E402

MODEL_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="2"><metadata key="name" value="Body"/><metadata key="extruder" value="1"/>
    <part id="1"><metadata key="extruder" value="2"/></part></object>
  <object id="5"><metadata key="name" value="Knob"/><metadata key="extruder" value="3"/></object>
  <plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="Main body"/>
    <metadata key="thumbnail_file" value="Metadata/plate_1.png"/>
    <model_instance><metadata key="object_id" value="2"/></model_instance></plate>
  <plate><metadata key="plater_id" value="2"/><metadata key="plater_name" value=""/>
    <model_instance><metadata key="object_id" value="5"/></model_instance>
    <model_instance><metadata key="object_id" value="5"/></model_instance></plate>
</config>"""


def project(thumbs=("Metadata/plate_1.png",)):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/model_settings.config", MODEL_SETTINGS)
        z.writestr("Metadata/project_settings.config",
                   json.dumps({"filament_colour": ["#FF0000", "#FFFFFF", "#00AE42"]}))
        for t in thumbs:
            z.writestr(t, b"\x89PNG fake")
    return buf.getvalue()


class PlateFacts(unittest.TestCase):
    def test_each_plate_gets_name_objects_colours_and_thumbnail(self):
        facts = slicer.model_facts("caliper.3mf", project())
        self.assertEqual(facts["plates"], 2)
        p1, p2 = facts["plate_info"]
        self.assertEqual((p1["n"], p1["name"], p1["objects"]), (1, "Main body", ["Body"]))
        self.assertEqual(p1["colors"], ["#FF0000", "#FFFFFF"])  # object extruder 1 + its part on extruder 2
        self.assertEqual(p1["thumb"], "Metadata/plate_1.png")
        self.assertEqual((p2["n"], p2["name"], p2["objects"], p2["colors"]), (2, "", ["Knob ×2"], ["#00AE42"]))
        self.assertIsNone(p2["thumb"])  # not in the file: the picker shows no image

    def test_single_plate_has_no_plate_info(self):
        self.assertNotIn("plate_info", slicer.model_facts("cube.stl", b"solid"))


class SliceOnePlate(unittest.TestCase):
    def test_body_carries_plate_no_outside_the_choices(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            seen.update(json.loads(req.data))
            return Resp(json.dumps({"plates": []}).encode())

        with mock.patch.object(slicer.urllib.request, "urlopen", fake), mock.patch.object(slicer, "URL", "http://s"), \
                mock.patch.object(slicer, "SECRET", "x"):
            slicer.slice(b"x", "a.3mf", "m", [{"filament_id": "f", "filament_type": "PLA"}],
                         {"plate_no": 2, "plate": "Textured PEI Plate"}, False, 1)
        self.assertEqual(seen["plate_no"], 2)
        self.assertNotIn("plate_no", seen["choices"])
        self.assertEqual(seen["choices"]["plate"], "Textured PEI Plate")  # the bed type is a different thing

    def test_slicerd_rejects_a_bad_plate_number(self):
        for bad in (-1, 201, "2", True):
            with self.assertRaisesRegex(ValueError, "plate_no"):
                slicerd.slice_job(b"x", "a.3mf", "m", "normal", choices={}, plate_no=bad)


class StartThatPlate(unittest.TestCase):
    def sliced(self, n):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"Metadata/plate_{n}.gcode", "G28")
        return buf.getvalue()

    def test_sliced_plate_number_is_read_from_the_file(self):
        self.assertEqual(server.sliced_plate(self.sliced(3)), 3)
        self.assertEqual(server.sliced_plate(b"not a zip"), 1)

    def test_project_file_names_that_plate(self):
        self.assertEqual(project_file("a.gcode.3mf", (0,), 3)["print"]["param"], "Metadata/plate_3.gcode")


class PlatePickerInTheApp(unittest.TestCase):
    def test_slice_opens_the_picker_and_sends_the_plate(self):
        app = (ROOT / "src/bff_mcp/app.html").read_text()
        self.assertIn('id="platesheet"', app)
        self.assertIn("return openPlatePicker();", app)
        self.assertIn("plate_no: pnPlate()", app)
        self.assertIn('rpc("plate_preview"', app)
        self.assertIn('"plate_preview": (tool_plate_preview', (ROOT / "src/bff_mcp/server.py").read_text())


if __name__ == "__main__":
    unittest.main()
