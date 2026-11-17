"""slicerd: process builder, slice job with a fake OrcaSlicer, HTTP auth."""
import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tools" / "slicer"))
import slicerd  # noqa: E402

MACHINE = "Bambu Lab A1 0.4 nozzle"
OTHER = "Bambu Lab P1S 0.4 nozzle"
FILAMENT = "Bambu PLA Basic @BBL A1"
FILAMENT2 = "Bambu PLA Matte @BBL A1"

FAKE_ORCA_TOO_BIG = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
Path(os.environ["FAKE_ORCA_ARGV"]).write_text(json.dumps(sys.argv[1:]))
if "--info" in sys.argv:
    print("size_x = 200.000000")
    print("size_y = 150.000000")
    print("size_z = 90.000000")
    sys.exit(0)
out = Path(sys.argv[sys.argv.index("--outputdir") + 1])
(out / "result.json").write_text(json.dumps({{"return_code": -50, "error_string": "No object is fully inside the print volume"}}))
sys.exit(206)
"""

FAKE_ORCA = f"""#!{sys.executable}
import json, os, sys, time, zipfile
from pathlib import Path
Path(os.environ["FAKE_ORCA_ARGV"]).write_text(json.dumps(sys.argv[1:]))
Path(os.environ["FAKE_ORCA_FILAMENTS"]).write_text(json.dumps(
    [json.loads(Path(p).read_text()) for p in sys.argv[sys.argv.index("--load-filaments") + 1].split(";")]))
time.sleep(float(os.environ.get("FAKE_ORCA_SLEEP", "0")))
out = Path(sys.argv[sys.argv.index("--outputdir") + 1])
(out / "plate_1.gcode").write_text("G28\\n")
(out / "plate_1.statistics.json").write_text(json.dumps({{"summary": {{"time": 525.4, "weight": 3.21}}}}))
with zipfile.ZipFile(out / "out.gcode.3mf", "w") as z:
    z.writestr("Metadata/plate_1.png", b"PNG1")
    z.writestr("Metadata/plate_1.gcode", "G28\\n")
"""


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        profiles = self.tmp / "profiles"
        profiles.mkdir()
        processes = {n: f"{n}.json" for n in ("0.24mm Draft @BBL A1", "0.20mm Standard @BBL A1",
                                              "0.12mm Fine @BBL A1")}
        for name, file in processes.items():
            (profiles / file).write_text(json.dumps({"name": name, "wall_loops": "2"}))
        (profiles / f"{MACHINE}.json").write_text(json.dumps({
            "name": MACHINE, "printable_area": ["0x0", "180x0", "180x180", "0x180"],
            "printable_height": "180"}))
        (profiles / f"{OTHER}.json").write_text(json.dumps({
            "name": OTHER, "printable_area": ["0x0", "256x0", "256x256", "0x256"],
            "printable_height": 256}))
        (profiles / f"{FILAMENT}.json").write_text(
            json.dumps({"name": FILAMENT, "filament_id": "GFB60", "filament_type": ["PLA"]}))
        (profiles / f"{FILAMENT2}.json").write_text(
            json.dumps({"name": FILAMENT2, "filament_id": "GFB61", "filament_type": ["PLA"]}))
        entry = {"process": processes, "filament": {FILAMENT: f"{FILAMENT}.json",
                                                    FILAMENT2: f"{FILAMENT2}.json"}}
        (profiles / "index.json").write_text(json.dumps({
            MACHINE: {"file": f"{MACHINE}.json", **entry},
            OTHER: {"file": f"{OTHER}.json", **entry}}))
        orca = self.tmp / "orca"
        orca.write_text(FAKE_ORCA)
        orca.chmod(0o755)
        self.argv_file = self.tmp / "argv.json"
        self.filaments_file = self.tmp / "filaments.json"
        for name, value in (("PROFILES", profiles), ("ORCA_BIN", str(orca)), ("SECRET", "s3cret")):
            patcher = mock.patch.object(slicerd, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"FAKE_ORCA_ARGV": str(self.argv_file),
                                           "FAKE_ORCA_FILAMENTS": str(self.filaments_file)})
        env.start()
        self.addCleanup(env.stop)

    def slice(self, filename, reorient=False, copies=1, quality="normal"):
        return slicerd.slice_job(b"solid x", filename, MACHINE, quality, FILAMENT, {}, reorient, copies)

    def argv(self):
        return json.loads(self.argv_file.read_text())

    def load_filaments(self):
        argv = self.argv()
        return [Path(p).name for p in argv[argv.index("--load-filaments") + 1].split(";")]

    def request(self):
        return {"model_b64": base64.b64encode(b"solid x").decode(), "filename": "cube.stl",
                "machine": MACHINE, "quality": "normal", "filament": FILAMENT, "choices": {"walls": 3}}


class BuildProcess(unittest.TestCase):
    def test_build_process_supports_auto(self):
        out = slicerd.build_process({"enable_support": "0"}, {"supports": "auto"})
        self.assertEqual({"enable_support": "1", "support_type": "tree(auto)",
                          "support_on_build_plate_only": "1"}, out)

    def test_build_process_only_changed_keys(self):
        base = {"wall_loops": "2", "sparse_infill_density": "15%", "enable_support": "0"}
        out = slicerd.build_process(base, {"infill": 40})
        self.assertEqual({"wall_loops": "2", "sparse_infill_density": "40%", "enable_support": "0"}, out)
        self.assertEqual("15%", base["sparse_infill_density"])  # base not mutated

    def test_build_process_rejects_walls_7(self):
        with self.assertRaises(ValueError):
            slicerd.build_process({}, {"walls": 7})


class Geometry(unittest.TestCase):
    def test_bed_size(self):
        self.assertEqual((180.0, 180.0, 180.0), slicerd.bed_size(
            {"printable_area": ["0x0", "180x0", "180x180", "0x180"], "printable_height": "180"}))
        self.assertEqual((256.0, 256.0, 256.0), slicerd.bed_size(
            {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printable_height": [256]}))

    def test_fits_rotated(self):
        bed = (150.0, 100.0, 180.0)
        self.assertTrue(slicerd.fits((150, 100, 180), bed))
        self.assertTrue(slicerd.fits((100, 150, 90), bed))  # only after a 90 degree rotation
        self.assertFalse(slicerd.fits((160, 90, 90), bed))
        self.assertFalse(slicerd.fits((90, 90, 181), bed))


class SliceJob(Base):
    def test_slice_job_uses_orient_for_stl_only(self):
        result = self.slice("cube.stl")
        self.assertIn("--orient", self.argv())
        self.assertEqual(1, len(result["plates"]))
        plate = result["plates"][0]
        self.assertEqual((525, 3.2, b"PNG1"), (plate["seconds"], plate["grams"], plate["png"]))
        self.assertTrue(plate["gcode_3mf"].startswith(b"PK"))
        machine_json, process_json = self.argv()[self.argv().index("--load-settings") + 1].split(";")
        self.assertEqual(f"{MACHINE}.json", Path(machine_json).name)
        self.assertEqual("process.json", Path(process_json).name)

        self.slice("cube.3mf")
        self.assertNotIn("--orient", self.argv())
        self.slice("cube.3mf", reorient=True)
        self.assertIn("--orient", self.argv())

        self.slice("cube.3mf", copies=3)
        argv = self.argv()
        self.assertIn("--arrange", argv)
        self.assertEqual(1, sum(a.endswith("model.3mf") for a in argv))  # copies live inside the 3MF (Orca -4 on repeats)

    def test_slice_job_filament_id(self):
        result = slicerd.slice_job(b"solid x", "cube.stl", MACHINE, "normal", None, {}, False, 1,
                                   filament_id="GFB60")
        self.assertEqual(1, len(result["plates"]))
        with self.assertRaises(ValueError):
            slicerd.slice_job(b"solid x", "cube.stl", MACHINE, "normal", None, {}, False, 1, filament_id="NOPE")
        with self.assertRaises(ValueError):
            slicerd.slice_job(b"solid x", "cube.stl", MACHINE, "normal", None, {}, False, 1)

    def test_two_filaments_command_line(self):
        slicerd.slice_job(b"solid x", "cube.3mf", MACHINE, "normal", None, {}, False, 1,
                          filaments=[{"filament_id": "GFB60", "filament_type": "PLA"},
                                     {"filament_id": "GFB61", "filament_type": "PLA"}])
        self.assertEqual([f"{FILAMENT}.json", f"{FILAMENT2}.json"], self.load_filaments())

    def bundle(self, *members):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in members:
                z.writestr(name, data)
        return buf.getvalue()

    def test_zip_bundle_slices_every_member_copies_times(self):
        data = self.bundle(("a.stl", b"solid a"), ("b.step", b"STEP b"))
        slicerd.slice_job(data, "page.bundle.zip", MACHINE, "normal", FILAMENT, {}, False, 2)
        argv = self.argv()
        self.assertIn("--orient", argv)  # treated like STLs
        models = [Path(p).name for p in argv[argv.index("--export-3mf") + 2:]]
        self.assertEqual(["model_0.stl", "model_1.step"], models[:2])
        self.assertEqual(models, ["model_0.stl", "model_1.step"] * 2)

    def test_zip_bundle_with_a_non_mesh_is_refused(self):
        data = self.bundle(("a.stl", b"solid a"), ("evil.exe", b"MZ"))
        with self.assertRaisesRegex(ValueError, "every file in the zip"):
            slicerd.slice_job(data, "page.bundle.zip", MACHINE, "normal", FILAMENT, {}, False, 1)

    def test_quality_and_filament_checked(self):
        self.assertEqual("0.24mm Draft @BBL A1", slicerd.pick_quality(["0.24mm Draft @BBL A1"], "fast"))
        self.assertEqual("0.24mm Standard @BBL A1",
                         slicerd.pick_quality(["0.24mm Standard @BBL A1", "0.20mm Standard @BBL A1"], "fast"))
        self.assertEqual("0.12mm High Quality @BBL P2S",
                         slicerd.pick_quality(["0.12mm High Quality @BBL P2S"], "fine"))
        with self.assertRaises(ValueError):
            slicerd.slice_job(b"x", "a.stl", MACHINE, "normal", "Other PLA", {}, False, 1)
        with self.assertRaises(ValueError):
            self.slice("a.exe")

    def test_allow_rotations_passed(self):
        self.slice("cube.stl")
        argv = self.argv()
        self.assertIn("--allow-rotations", argv)
        self.assertEqual("--allow-rotations", argv[argv.index("--arrange") + 2])
        self.slice("cube.3mf", copies=2)
        self.assertIn("--allow-rotations", self.argv())

    def test_timeout_is_422(self):
        with mock.patch.object(slicerd, "TIMEOUT", 1), mock.patch.dict(os.environ, {"FAKE_ORCA_SLEEP": "3"}):
            server = Server(self)
            code, body = server.post({"Authorization": "Bearer s3cret"}, self.request())
        self.assertEqual(422, code)
        self.assertIn("timed out", body["error"])

class KlipperTexturedBed(Base):
    KLI = "MyKlipper 0.4 nozzle"
    PLA = "Generic PLA @MyKlipper"
    PETG = "Generic PETG @MyKlipper"

    def setUp(self):
        super().setUp()
        profiles = self.tmp / "profiles"
        (profiles / "klipper.json").write_text(json.dumps({
            "name": self.KLI, "gcode_flavor": "klipper",
            "printable_area": ["0x0", "220x0", "220x220", "0x220"], "printable_height": "250"}))
        (profiles / "generic-pla.json").write_text(json.dumps({
            "name": self.PLA, "filament_type": ["PLA"], "filament_id": "GFA00",
            "textured_plate_temp": "55", "textured_plate_temp_initial_layer": "55"}))
        (profiles / "generic-petg.json").write_text(json.dumps({
            "name": self.PETG, "filament_type": ["PETG"], "filament_id": "GFA01",
            "textured_plate_temp": ["70"], "textured_plate_temp_initial_layer": ["70"]}))
        index = json.loads((profiles / "index.json").read_text())
        index[self.KLI] = dict(index[MACHINE], file="klipper.json")
        for machine in (MACHINE, self.KLI):
            index[machine]["filament"].update({self.PLA: "generic-pla.json", self.PETG: "generic-petg.json"})
        (profiles / "index.json").write_text(json.dumps(index))

    def filament(self, machine, name, plate):
        slicerd.slice_job(b"solid x", "cube.stl", machine, "normal", name, {"plate": plate})
        return json.loads(self.filaments_file.read_text())[0]

    def test_textured_raises_generic_pla_to_60(self):
        fil = self.filament(self.KLI, self.PLA, "Textured PEI Plate")
        self.assertEqual(("60", "60"), (fil["textured_plate_temp"], fil["textured_plate_temp_initial_layer"]))

    def test_textured_petg_70_stays(self):
        fil = self.filament(self.KLI, self.PETG, "Textured PEI Plate")
        self.assertEqual((["70"], ["70"]), (fil["textured_plate_temp"], fil["textured_plate_temp_initial_layer"]))

    def test_bambu_machine_unchanged(self):
        fil = self.filament(MACHINE, self.PLA, "Textured PEI Plate")
        self.assertEqual(("55", "55"), (fil["textured_plate_temp"], fil["textured_plate_temp_initial_layer"]))

    def test_cool_plate_unchanged(self):
        fil = self.filament(self.KLI, self.PLA, "Cool Plate")
        self.assertEqual(("55", "55"), (fil["textured_plate_temp"], fil["textured_plate_temp_initial_layer"]))


class TooBigPlate(Base):
    def setUp(self):
        super().setUp()
        orca = self.tmp / "orca_too_big"
        orca.write_text(FAKE_ORCA_TOO_BIG)
        orca.chmod(0o755)
        patcher = mock.patch.object(slicerd, "ORCA_BIN", str(orca))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_too_big_raises_with_size_and_bed(self):
        with self.assertRaises(slicerd.TooBig) as ctx:
            slicerd.slice_job(b"solid x", "cube.stl", MACHINE, "normal", FILAMENT, {}, False, 1,
                              other_machines=[OTHER])
        self.assertEqual((200.0, 150.0, 90.0), ctx.exception.size)
        self.assertEqual((180.0, 180.0, 180.0), ctx.exception.bed)
        self.assertEqual([OTHER], ctx.exception.fits_on)

    def test_too_big_422_body(self):
        server = Server(self)
        code, body = server.post({"Authorization": "Bearer s3cret"},
                                 dict(self.request(), other_machines=[OTHER]))
        self.assertEqual(422, code)
        self.assertEqual("too_big", body["error"])
        self.assertEqual([200.0, 150.0, 90.0], body["size"])
        self.assertEqual([180.0, 180.0, 180.0], body["bed"])
        self.assertEqual([OTHER], body["fits_on"])


class Server:
    def __init__(self, test):
        self.httpd = slicerd.ThreadingHTTPServer(("127.0.0.1", 0), slicerd.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        test.addCleanup(self.httpd.server_close)
        test.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def post(self, headers, body):
        req = urllib.request.Request(self.url + "/slice", json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())


class Http(Base):
    def test_bad_secret_401(self):
        server = Server(self)
        self.assertEqual(401, server.post({"Authorization": "Bearer nope"}, self.request())[0])
        self.assertEqual(401, server.post({}, self.request())[0])

    def test_http_slice_ok(self):
        server = Server(self)
        code, body = server.post({"Authorization": "Bearer s3cret"}, self.request())
        self.assertEqual(200, code, body)
        self.assertEqual(b"PNG1", base64.b64decode(body["plates"][0]["png"]))
        bad = dict(self.request(), quality="ultra")
        self.assertEqual(400, server.post({"Authorization": "Bearer s3cret"}, bad)[0])
        with urllib.request.urlopen(server.url + "/health") as resp:
            self.assertEqual({"orca": "2.5.0", "profiles": 2}, json.loads(resp.read()))

    def test_single_legacy_fields_still_work(self):
        server = Server(self)
        body = {k: v for k, v in self.request().items() if k != "filament"}
        body["filament_id"] = "GFB60"
        code, out = server.post({"Authorization": "Bearer s3cret"}, body)
        self.assertEqual(200, code, out)
        self.assertEqual([f"{FILAMENT}.json"], self.load_filaments())

    def test_sixteen_filaments_ok_seventeen_rejected(self):
        server = Server(self)
        ok = dict(self.request(), filaments=[{"filament_id": "GFB60"}] * 16)
        self.assertEqual(200, server.post({"Authorization": "Bearer s3cret"}, ok)[0])
        body = dict(self.request(), filaments=[{"filament_id": "GFB60"}] * 17)
        self.assertEqual(400, server.post({"Authorization": "Bearer s3cret"}, body)[0])


class ResolveFilament(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.FILAMENTS = {
            "Bambu PLA Basic @BBL A1": "bambu-pla.json",
            "Bambu PLA Tough @BBL A1": "bambu-pla-tough.json",
            "Generic PLA @BBL A1": "generic-pla.json",
            "Bambu PETG Basic @BBL A1": "bambu-petg.json",
            "Generic PETG @BBL A1": "generic-petg.json",
        }
        files = {
            "bambu-pla.json": {"name": "Bambu PLA Basic @BBL A1", "filament_id": "GFB60",
                               "filament_type": ["PLA"]},
            "bambu-pla-tough.json": {"name": "Bambu PLA Tough @BBL A1", "filament_id": "GFB60",
                                     "filament_type": ["PLA"]},
            "generic-pla.json": {"name": "Generic PLA @BBL A1", "filament_id": "GFB60",
                                "filament_type": ["PLA"]},
            "bambu-petg.json": {"name": "Bambu PETG Basic @BBL A1", "filament_id": "GFG60",
                                "filament_type": ["PETG"]},
            "generic-petg.json": {"name": "Generic PETG @BBL A1", "filament_id": "GFX99",
                                  "filament_type": ["PETG"]},
        }
        for name, content in files.items():
            (self.tmp / name).write_text(json.dumps(content))
        patcher = mock.patch.object(slicerd, "PROFILES", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def entry(self):
        return {"file": "m.json", "process": {}, "filament": self.FILAMENTS}

    def test_explicit_filament(self):
        self.assertEqual("Bambu PLA Basic @BBL A1",
                         slicerd.resolve_filament(self.entry(), "Bambu PLA Basic @BBL A1"))
        with self.assertRaises(ValueError):
            slicerd.resolve_filament(self.entry(), "Bambu PLA Other @BBL A1")

    def test_filament_id_prefers_brand(self):
        self.assertEqual("Bambu PLA Basic @BBL A1",
                         slicerd.resolve_filament(self.entry(), None, "GFB60", None))

    def test_filament_id_on_generic_only(self):
        self.assertEqual("Generic PETG @BBL A1",
                         slicerd.resolve_filament(self.entry(), None, "GFX99", None))

    def test_filament_type_falls_back_to_generic(self):
        self.assertEqual("Generic PLA @BBL A1",
                         slicerd.resolve_filament(self.entry(), None, "NOPE", "PLA"))
        self.assertEqual("Generic PETG @BBL A1",
                         slicerd.resolve_filament(self.entry(), None, None, "petg"))

    def test_nothing_matches(self):
        with self.assertRaisesRegex(ValueError, "no filament profile"):
            slicerd.resolve_filament(self.entry(), None, None, None)
        with self.assertRaisesRegex(ValueError, "no filament profile"):
            slicerd.resolve_filament(self.entry(), None, "NOPE", "ABS")


if __name__ == "__main__":
    unittest.main()


class CapBambuStudioVersion(unittest.TestCase):
    def test_newer_bambu_studio_label_is_capped_rest_untouched(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.3mf"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("3D/3dmodel.model", '<metadata name="Application">BambuStudio-02.07.01.57</metadata><v>1</v>')
                z.writestr("Metadata/project_settings.config", '{"version": "02.07.01.57", "layer_height": "0.2"}')
                z.writestr("Metadata/plate_1.png", b"\x89PNG")
            slicerd._cap_bbs_version(path)
            with zipfile.ZipFile(path) as z:
                self.assertIn("BambuStudio-02.00.00.00</metadata><v>1</v>", z.read("3D/3dmodel.model").decode())
                self.assertEqual(z.read("Metadata/project_settings.config").decode(),
                                 '{"version": "02.00.00.00", "layer_height": "0.2"}')
                self.assertEqual(z.read("Metadata/plate_1.png"), b"\x89PNG")

    def test_older_label_is_left_alone(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.3mf"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("3D/3dmodel.model", '<metadata name="Application">BambuStudio-01.09.00.00</metadata>')
            before = path.read_bytes()
            slicerd._cap_bbs_version(path)
            self.assertEqual(path.read_bytes(), before)


class OrcaSuggestedValues(unittest.TestCase):
    def test_orca_warning_value_is_written_into_the_project(self):
        import tempfile
        import zipfile
        line = 'warning: x.3mf: value "enabled" for ensure_vertical_shell_thickness is not valid, using "ensure_all" instead'
        fixes = slicerd.INVALID_VALUE.findall(line)
        self.assertEqual(fixes, [("enabled", "ensure_vertical_shell_thickness", "ensure_all")])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.3mf"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("Metadata/project_settings.config",
                           '{"ensure_vertical_shell_thickness": "enabled", "wall_loops": "2"}')
            self.assertTrue(slicerd._apply_fixes(path, fixes))
            with zipfile.ZipFile(path) as z:
                conf = __import__("json").loads(z.read("Metadata/project_settings.config"))
            self.assertEqual(conf, {"ensure_vertical_shell_thickness": "ensure_all", "wall_loops": "2"})
            self.assertFalse(slicerd._apply_fixes(path, fixes))  # nothing left to fix: no endless retry

    def test_object_overrides_get_the_same_fix(self):
        import tempfile
        import zipfile
        fixes = [("enabled", "ensure_vertical_shell_thickness", "ensure_all")]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.3mf"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("Metadata/model_settings.config",
                           '<object><metadata key="ensure_vertical_shell_thickness" value="enabled"/></object>')
            self.assertTrue(slicerd._apply_fixes(path, fixes))
            with zipfile.ZipFile(path) as z:
                self.assertIn('value="ensure_all"', z.read("Metadata/model_settings.config").decode())



class MachineOverrides(unittest.TestCase):
    def test_printer_cfg_on_top_of_the_preset(self):
        import slicerd as sd
        base = {"printable_height": "250", "gcode_flavor": "klipper", "name": "Generic Klipper Printer 0.4 nozzle"}
        got = sd.with_overrides(base, {"printable_height": "230", "nozzle_diameter": ["0.6"]})
        self.assertEqual((got["printable_height"], got["nozzle_diameter"], got["name"]), ("230", ["0.6"], base["name"]))
        self.assertIs(sd.with_overrides(base, {}), base)
        for bad in ({"post_process": ["rm -rf /"]}, {"printable_height": 230}, ["x"]):
            with self.assertRaises(ValueError):
                sd.with_overrides(base, bad)

    def test_firmware_retraction_drops_wipe(self):
        import slicerd as sd
        got = sd.with_overrides({"wipe": ["1", "1", "1"]}, {"use_firmware_retraction": "1"})
        self.assertEqual(got["wipe"], ["0", "0", "0"])

    def test_wipe_untouched_without_firmware_retraction(self):
        import slicerd as sd
        got = sd.with_overrides({"wipe": ["1", "1", "1"]}, {"printable_height": "230"})
        self.assertEqual(got["wipe"], ["1", "1", "1"])


class UserPresets(unittest.TestCase):
    def test_resolve_on_system_presets(self):
        import slicerd as sd
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sd, "PROFILES", Path(d)):
            for name, body in {"m.json": {"name": "MyKlipper 0.6 nozzle", "printable_height": "250", "nozzle_diameter": ["0.6"]},
                               "p.json": {"name": "0.20mm Standard @MyKlipper", "layer_height": "0.2", "wall_loops": "2"},
                               "f.json": {"name": "Generic PLA @System", "filament_type": ["PLA"], "nozzle_temperature": ["220"]}}.items():
                (Path(d) / name).write_text(json.dumps(body))
            entry = {"file": "m.json", "process": {"0.20mm Standard @MyKlipper": "p.json"}, "filament": {"Generic PLA @System": "f.json"}}
            user = {"machine": {"name": "E3K 0.6 nozzle", "inherits": "MyKlipper 0.6 nozzle", "printable_height": "230", "post_process": ["x"]},
                    "processes": {"0.20mm Standard @E3K 0.6": {"inherits": "0.20mm Standard @MyKlipper", "wall_loops": "3"},
                                  "0.20mm X @BBL": {"inherits": "0.20mm Standard @BBL X1C"}},
                    "filaments": [{"name": "PolyTerra PLA @E3K", "inherits": "PolyTerra PLA @System", "_type": "PLA", "nozzle_temperature": ["210"]}]}
            got = sd.resolve_user(entry, user, "normal")
            with self.assertRaises(ValueError):
                sd.resolve_user(entry, {"machine": {}, "processes": {}, "filaments": []}, "normal")
        self.assertEqual((got["machine"]["name"], got["machine"]["printable_height"]), ("E3K 0.6 nozzle", "230"))
        self.assertNotIn("post_process", got["machine"])
        self.assertEqual((got["process"]["wall_loops"], got["process"]["layer_height"], got["process"]["compatible_printers"]),
                         ("3", "0.2", ["E3K 0.6 nozzle"]))
        fil = got["filaments"][0]  # parent not shipped: the Generic of its type, the user's values on top
        self.assertEqual((fil["name"], fil["nozzle_temperature"], fil["filament_type"], fil["compatible_printers"]),
                         ("PolyTerra PLA @E3K", ["210"], ["PLA"], []))
        self.assertNotIn("_type", fil)
