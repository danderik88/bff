"""Multi-plate projects: pick one plate, slice only it, start it with its own number (the user 2026-11-15)."""
import base64
import io
import json
import sys
import tempfile
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


class UsedColours(unittest.TestCase):
    def three_mf(self, settings, colours, mesh=b"<mesh/>"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Metadata/model_settings.config", settings)
            z.writestr("Metadata/project_settings.config", json.dumps({"filament_colour": colours}))
            z.writestr("3D/3dmodel.model", mesh)
        return buf.getvalue()

    def test_only_the_colours_the_objects_use(self):
        # the user 2026-11-15: a 2-colour Printables model whose project lists 8 filaments
        cfg = ('<config><object id="1"><metadata key="extruder" value="2"/></object>'
               '<object id="2"><metadata key="extruder" value="5"/></object></config>')
        facts = slicer.model_facts("cat.3mf", self.three_mf(cfg, ["#FFFFFF", "#E05080", "#5040B0", "#F0C040",
                                                                  "#40A0A0", "#80FFFF", "#FFFF40", "#000000"]))
        self.assertEqual(len(facts["colors"]), 8)  # the full list stays: one filament per entry for Orca
        self.assertEqual(facts["used"], [2, 5])

    def test_object_without_extruder_uses_the_first(self):
        cfg = '<config><object id="1"/><object id="2"><part><metadata key="extruder" value="3"/></part></object></config>'
        facts = slicer.model_facts("a.3mf", self.three_mf(cfg, ["#FF0000", "#00FF00", "#0000FF", "#000000"]))
        self.assertEqual(facts["used"], [1, 3])

    def test_painted_model_adds_the_painted_filaments(self):
        # the user 2026-11-15: LazyCat cover, white body (extruder 1) painted black with filament 8 ("5C")
        cfg = '<config><object id="1"><metadata key="extruder" value="1"/></object></config>'
        mesh = b'<triangle v1="0" paint_color="5C"/><triangle v1="1"/><triangle paint_color="8"/>'
        facts = slicer.model_facts("p.3mf", self.three_mf(cfg, ["#FFFFFF"] + ["#123456"] * 6 + ["#000000"], mesh))
        self.assertEqual(facts["used"], [1, 2, 8])

    def test_paint_code_decoding(self):
        self.assertEqual(slicer.paint_states(b"4"), {1})
        self.assertEqual(slicer.paint_states(b"0C"), {3})
        self.assertEqual(slicer.paint_states(b"5C"), {8})
        self.assertEqual(slicer.paint_states(b"84" + b"0" + b"1"), {0, 2, 1})  # split nibble then leaves

    def test_app_shows_only_used_spools(self):
        app = (ROOT / "src/bff_mcp/app.html").read_text()
        self.assertIn("if (!one && usedIdx() && !usedIdx().includes(i)) return \"\";", app)
        self.assertIn("fillUnused();", app)


class CopiesOfAProject(unittest.TestCase):
    def test_copies_become_instances_in_the_3mf(self):
        # the user 2026-11-15: Orca refuses the same 3MF twice (-4), so copies go inside the project
        import tempfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("3D/3dmodel.model", '<model><build p:UUID="b"><item objectid="2" p:UUID="x" transform="1"/></build></model>')
            z.writestr("Metadata/model_settings.config", '<config><plate><model_instance><metadata key="object_id" value="2"/>'
                       '<metadata key="instance_id" value="0"/><metadata key="identify_id" value="147"/></model_instance></plate></config>')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.3mf"
            path.write_bytes(buf.getvalue())
            slicerd._add_instances(path, 3)
            with zipfile.ZipFile(path) as z:
                model = z.read("3D/3dmodel.model").decode()
                settings = z.read("Metadata/model_settings.config").decode()
        self.assertEqual(model.count('objectid="2"'), 3)
        self.assertEqual(len(set(__import__("re").findall(r'item[^>]*p:UUID="([^"]*)"', model))), 3)
        for n in "012":
            self.assertIn(f'key="instance_id" value="{n}"', settings)
        self.assertEqual(settings.count("identify_id"), 1)


class OriginalOrStandard(unittest.TestCase):
    def test_app_offers_original_for_3mf(self):
        app = (ROOT / "src/bff_mcp/app.html").read_text()
        self.assertIn('id="pnorig"', app)
        self.assertIn('return isThreeMf ? { fresh: true } : { supports: "auto" };', app)

    def test_fresh_drops_the_designer_settings(self):
        import tempfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("3D/3dmodel.model", "<model/>")
            z.writestr("Metadata/project_settings.config", "{}")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.3mf"
            path.write_bytes(buf.getvalue())
            slicerd._drop_project_settings(path)
            with zipfile.ZipFile(path) as z:
                self.assertNotIn("Metadata/project_settings.config", z.namelist())
        src = (ROOT / "tools/slicer/slicerd.py").read_text()
        self.assertIn('fresh = ext == ".3mf" and choices.get("fresh") is True', src)


class OutOfRange(unittest.TestCase):
    def test_out_of_range_values_are_clamped(self):
        # the user 2026-11-15: one bad value used to drop every setting of the designer
        log = ('raft_first_layer_expansion: -1 not in range [0, 3.40282e+38]\n'
               'value "enabled" for ensure_vertical_shell_thickness is not valid, using "ensure_all" instead')
        self.assertEqual(slicerd.orca_fixes(log), [("enabled", "ensure_vertical_shell_thickness", "ensure_all"),
                                                   ("-1", "raft_first_layer_expansion", "0")])


class FilePreview(unittest.TestCase):
    """The sliced plate image the app shows before printing (2026-11-19)."""

    def sliced(self, images):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Metadata/plate_2.gcode", "G28")
            for name, png in images.items():
                z.writestr(name, png)
        return buf.getvalue()

    def test_the_sliced_plate_image_comes_back_base64(self):
        data = self.sliced({"Metadata/plate_1.png": b"one", "Metadata/plate_2.png": b"two"})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(server.library, "FOLDER", d):
            server.library.save(7, data)
            got = json.loads(server.tool_file_preview({"file_id": 7})[0]["text"])
        self.assertEqual(base64.b64decode(got["png"]), b"two")  # plate 2 is the one sliced

    def test_no_image_is_none(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(server.library, "FOLDER", d):
            server.library.save(7, self.sliced({}))
            self.assertIsNone(json.loads(server.tool_file_preview({"file_id": 7})[0]["text"])["png"])

    def test_unknown_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(server.library, "FOLDER", d):
            with self.assertRaises(server.ToolError):
                server.tool_file_preview({"file_id": 99})
