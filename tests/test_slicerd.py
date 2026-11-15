"""slicerd: process builder, slice job with a fake OrcaSlicer, HTTP auth."""
import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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
        for name, value in (("PROFILES", profiles), ("ORCA_BIN", str(orca)), ("SECRET", "s3cret")):
            patcher = mock.patch.object(slicerd, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"FAKE_ORCA_ARGV": str(self.argv_file)})
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
        self.assertEqual(3, sum(a.endswith("model.3mf") for a in argv))

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
