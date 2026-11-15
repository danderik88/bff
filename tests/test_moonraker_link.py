import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "bff_mcp"))
import moonraker_link as ml  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from bff_mcp import server  # noqa: E402

STATUS = {
    "print_stats": {"state": "printing", "filename": "cube.gcode", "print_duration": 600.0,
                    "info": {"current_layer": 12, "total_layer": 50}},
    "virtual_sdcard": {"progress": 0.25},
    "display_status": {"progress": 0.0},
    "extruder": {"temperature": 198.5, "target": 210.0},
    "heater_bed": {"temperature": 60.0, "target": 60.0},
    "gcode_move": {"speed_factor": 1.2},
    "fan": {"speed": 0.5},
    "ace": {"current_index": 2},
    "ace_instance_0": {"humidity": 43, "temp": 25, "dryer_status": {"state_code": 0, "remain_time": 0, "target_temp": 0},
                       "slots": [{"index": 0, "status": "ready", "material": "PLA", "color": [235, 235, 235], "temp": 210},
                                 {"index": 1, "status": "empty", "material": "Unknown", "color": [0, 0, 0]},
                                 {"index": 2, "status": "ready", "material": "PETG", "color": [240, 140, 170], "temp": 240},
                                 {"index": 3, "status": "ready", "material": "PLA", "color": [25, 25, 25], "temp": 210}]},
}


class FakeMoonraker(BaseHTTPRequestHandler):
    calls = []
    bodies = []
    klippy = "ready"

    def log_message(self, *args):
        pass

    def _reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/printer/info"):
            return self._reply({"result": {"state": FakeMoonraker.klippy, "software_version": "v0.12.0"}})
        if self.path.startswith("/printer/objects/query"):
            return self._reply({"result": {"status": STATUS}})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        FakeMoonraker.calls.append((self.path, self.headers.get("X-Api-Key")))
        FakeMoonraker.bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        self._reply({"result": "ok"})


class MoonrakerLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeMoonraker)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeMoonraker.calls.clear()
        FakeMoonraker.bodies.clear()
        FakeMoonraker.klippy = "ready"
        self.link = ml.MoonrakerLink("sv08", "127.0.0.1", api_key="k", port=self.port)

    def test_snapshot_maps_to_bambu_shape(self):
        self.assertTrue(self.link.poll())
        s = self.link.snapshot()
        self.assertTrue(s["connected"])
        self.assertEqual(s["state"], "RUNNING")
        self.assertEqual(s["subtask_name"], "cube.gcode")
        self.assertEqual(s["progress"], 25.0)
        self.assertEqual((s["layer_num"], s["total_layers"]), (12, 50))
        self.assertEqual(s["temperatures"]["nozzle"], 198.5)
        self.assertTrue(s["temperatures"]["nozzle_heating"])
        self.assertEqual(s["speed_percent"], 120)
        self.assertEqual(s["fans"]["part"], 50)
        self.assertEqual(s["remaining_time"], 30)  # 600 s done at 25% -> 1800 s left
        self.assertEqual(len(s["ams"]), 1)  # the ACE Pro

    def test_ace_pro_shows_up_as_an_ams_unit(self):
        s = ml.to_snapshot(STATUS)
        self.assertEqual(s["tray_now"], 2)
        status = server.onboard.ams_units(s["ams"], s["tray_now"], None)[0]
        self.assertEqual(status["model"], "ACE Pro")
        slots = status["slots"]
        self.assertEqual([x["empty"] for x in slots], [False, True, False, False])
        self.assertEqual((slots[0]["material"], slots[0]["color"]), ("PLA", "EBEBEBFF"))
        self.assertTrue(slots[2]["active"])
        self.assertEqual(status["humidity"], 43)

    def test_no_ace_means_no_units(self):
        self.assertEqual(ml.to_snapshot({"print_stats": {"state": "standby"}})["ams"], [])

    def test_m73_progress_wins_over_file_position(self):
        raw = json.loads(json.dumps(STATUS))
        raw["display_status"]["progress"] = 0.5
        self.assertEqual(ml.to_snapshot(raw)["progress"], 50.0)

    def test_klippy_shutdown_is_failed(self):
        self.assertEqual(ml.to_snapshot(STATUS, "shutdown")["state"], "FAILED")

    def test_state_words(self):
        for klipper, bambu in ml.STATES.items():
            raw = {"print_stats": {"state": klipper}}
            self.assertEqual(ml.to_snapshot(raw)["state"], bambu)

    def test_unreachable_is_disconnected_and_counts_a_drop(self):
        self.link.poll()
        self.assertTrue(self.link.snapshot()["connected"])
        clock = [self.link.last_report + 60]
        self.link.clock = lambda: clock[0]
        self.assertFalse(self.link.snapshot()["connected"])
        self.assertEqual(self.link.drops, 1)
        dead = ml.MoonrakerLink("x", "127.0.0.1", port=1)
        self.assertFalse(dead.poll())
        self.assertFalse(dead.snapshot()["connected"])

    def test_commands_hit_moonraker_with_key(self):
        self.assertTrue(self.link.command("pause"))
        self.assertTrue(self.link.command("resume"))
        self.assertTrue(self.link.command("stop"))
        self.assertTrue(self.link.command("temperature", heater="nozzle", target=215))
        self.assertTrue(self.link.start_print("cube.gcode"))
        paths = [p for p, _ in FakeMoonraker.calls]
        self.assertEqual(paths[:3], ["/printer/print/pause", "/printer/print/resume", "/printer/print/cancel"])
        self.assertIn("/printer/gcode/script?script=M104+S215", paths)
        self.assertIn("/printer/print/start?filename=cube.gcode", paths)
        self.assertTrue(all(key == "k" for _, key in FakeMoonraker.calls))

    def test_bambu_payloads_and_unknown_actions_fail_closed(self):
        self.assertFalse(self.link.publish({"print": {"command": "stop"}}))
        self.assertFalse(self.link.command("light", on=True))

    def test_command_to_dead_host_is_false_not_raised(self):
        self.assertFalse(ml.MoonrakerLink("x", "127.0.0.1", port=1).command("pause"))


    def test_server_adds_a_klipper_printer_and_reports_status(self):
        entry = {"host": "127.0.0.1", "port": self.port, "name": "SV08", "model": "KLIPPER-SV08", "api_key": ""}
        server.add_link("sv08test", entry)
        try:
            self.assertIsInstance(server.LINKS["sv08test"], ml.MoonrakerLink)
            server.LINKS["sv08test"].poll()
            status = server.status_dict({"printer": "sv08test"})
            self.assertEqual(status["state"], "RUNNING")
            self.assertEqual(status["progress"], 25.0)
            self.assertTrue(status["connected"])
        finally:
            server.drop_link("sv08test")

    def test_upload_sends_multipart_gcode(self):
        self.link.upload("a.gcode", b"G28\nG1 X1\n")
        self.assertEqual(FakeMoonraker.calls[0][0], "/server/files/upload")
        body = FakeMoonraker.bodies[0]
        self.assertIn(b'name="root"\r\n\r\ngcodes', body)
        self.assertIn(b'filename="a.gcode"', body)
        self.assertIn(b"G28\nG1 X1\n", body)

    def test_start_klipper_unpacks_a_3mf_plate_then_starts(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Metadata/plate_1.gcode", "G28\n; real gcode\n")
        link = ml.MoonrakerLink("sv08", "127.0.0.1", port=self.port)
        server.start_klipper(link, "cube.gcode.3mf", buf.getvalue())
        self.assertIn(b"; real gcode", FakeMoonraker.bodies[0])
        self.assertIn(b'filename="cube.gcode"', FakeMoonraker.bodies[0])
        self.assertEqual(FakeMoonraker.calls[-1][0], "/printer/print/start?filename=cube.gcode")

    def test_start_klipper_rejects_a_3mf_without_gcode(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("3D/3dmodel.model", "<m/>")
        link = ml.MoonrakerLink("sv08", "127.0.0.1", port=self.port)
        with self.assertRaises(server.ftps.UploadError):
            server.start_klipper(link, "x.3mf", buf.getvalue())
        self.assertEqual(FakeMoonraker.calls, [])  # nothing sent


if __name__ == "__main__":
    unittest.main()
