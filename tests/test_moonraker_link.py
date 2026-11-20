import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "bff_mcp"))
import moonraker_link as ml  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from bff_mcp import server  # noqa: E402
from bff_mcp import ha_mqtt  # noqa: E402

STATUS = {
    "print_stats": {"state": "printing", "filename": "cube.gcode", "print_duration": 600.0,
                    "info": {"current_layer": 12, "total_layer": 50}},
    "virtual_sdcard": {"progress": 0.25},
    "display_status": {"progress": 0.0},
    "extruder": {"temperature": 198.5, "target": 210.0},
    "heater_bed": {"temperature": 60.0, "target": 60.0},
    "gcode_move": {"speed_factor": 1.2},
    "fan": {"speed": 0.5},
    "led main_led": {"color_data": [[0.0, 0.0, 0.0, 1.0]]},
    "ace": {"current_index": 2},
    "ace_instance_0": {"humidity": 43, "temp": 25, "dryer_status": {"state_code": 0, "remain_time": 0, "target_temp": 0},
                       "slots": [{"index": 0, "status": "ready", "material": "PLA", "color": [235, 235, 235], "temp": 210},
                                 {"index": 1, "status": "empty", "material": "Unknown", "color": [0, 0, 0]},
                                 {"index": 2, "status": "ready", "material": "PETG", "color": [240, 140, 170], "temp": 240},
                                 {"index": 3, "status": "ready", "material": "PLA", "color": [25, 25, 25], "temp": 210}]},
}


OBJECTS = ["extruder", "led main_led", "gcode_macro mainled_on", "gcode_macro mainled_off",
           "gcode_macro LOAD_FILAMENT"]
# The real E3K: no `led` object, light driven by plain WLED macros and a WLED strip per macro arg.
E3K_OBJECTS = ["extruder", "fan_generic aux_fan", "gcode_macro LIGHT_ON", "gcode_macro LIGHT_OFF"]
E3K_CONFIG = {"gcode_macro light_on": {"gcode": "WLED_ON STRIP=light PRESET=1"},
              "gcode_macro light_off": {"gcode": "WLED_ON STRIP=light PRESET=2"}}
E3K_STRIPS = {"light": {"status": "on", "preset": 1}}


class FakeMoonraker(BaseHTTPRequestHandler):
    calls = []
    bodies = []
    klippy = "ready"
    objects = OBJECTS
    config = {}
    status = None  # None: the module's STATUS; a test can put an idle/error state here
    webhooks = {}  # the `webhooks` object: {"state": ..., "state_message": ...}
    gcode_store = []  # /server/gcode_store result, oldest first
    server_config = {}  # /server/config: {"wled <strip>": {"type": "http", "address": "host:port"}}
    strips = None  # None: the WLED component is not installed (404)
    webcams = [{"enabled": False, "snapshot_url": "/off"},
               {"enabled": True, "snapshot_url": "/webcam/?action=snapshot"}]

    def log_message(self, *args):
        pass

    def _reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/printer/info"):
            return self._reply({"result": {"state": FakeMoonraker.klippy, "software_version": "v0.12.0"}})
        if self.path.startswith("/printer/objects/list"):
            return self._reply({"result": {"objects": FakeMoonraker.objects}})
        if self.path.startswith("/printer/objects/query") and "configfile=settings" in self.path:
            return self._reply({"result": {"status": {"configfile": {"settings": FakeMoonraker.config}}}})
        if self.path.startswith("/machine/wled/strips"):
            if FakeMoonraker.strips is None:
                return self._not_found()
            return self._reply({"result": {"strips": FakeMoonraker.strips}})
        if self.path.startswith("/server/config"):
            return self._reply({"result": {"config": FakeMoonraker.server_config}})
        if self.path.startswith("/server/webcams/list"):
            return self._reply({"result": {"webcams": FakeMoonraker.webcams}})
        if self.path.startswith("/webcam/"):
            body = b"\xff\xd8fakejpeg\xff\xd9"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if self.path.startswith("/server/gcode_store"):
            return self._reply({"result": {"gcode_store": FakeMoonraker.gcode_store}})
        if self.path.startswith("/printer/objects/query"):
            return self._reply({"result": {"status": dict(FakeMoonraker.status or STATUS,
                                                           webhooks=FakeMoonraker.webhooks)}})
        self._not_found()

    def do_POST(self):
        FakeMoonraker.calls.append((self.path, self.headers.get("X-Api-Key")))
        FakeMoonraker.bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        self._reply({"result": "ok"})


class FakeWled(BaseHTTPRequestHandler):
    """Just the one endpoint BFF reads: WLED's own state, which Moonraker's strips do not track."""
    state = {"on": True, "ps": 1}

    def log_message(self, *args):
        pass

    def do_GET(self):
        if not self.path.startswith("/json/state"):
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(FakeWled.state).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MoonrakerLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeMoonraker)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]
        cls.wled_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeWled)
        threading.Thread(target=cls.wled_srv.serve_forever, daemon=True).start()
        cls.wled_port = cls.wled_srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.wled_srv.shutdown()

    def setUp(self):
        FakeMoonraker.calls.clear()
        FakeMoonraker.bodies.clear()
        FakeMoonraker.klippy = "ready"
        FakeMoonraker.objects = OBJECTS
        FakeMoonraker.config = {}
        FakeMoonraker.status = None
        FakeMoonraker.webhooks = {}
        FakeMoonraker.gcode_store = []
        FakeMoonraker.server_config = {}
        FakeMoonraker.strips = None
        FakeMoonraker.webcams = [{"enabled": False, "snapshot_url": "/off"},
                                 {"enabled": True, "snapshot_url": "/webcam/?action=snapshot"}]
        FakeWled.state = {"on": True, "ps": 1}
        self.link = ml.MoonrakerLink("sv08", "127.0.0.1", api_key="k", port=self.port)

    def test_snapshot_maps_to_bambu_shape(self):
        self.assertTrue(self.link.poll())
        s = self.link.snapshot()
        self.assertTrue(s["connected"])
        self.assertEqual(s["state"], "RUNNING")
        self.assertEqual(s["subtask_name"], "cube")  # display only: no .gcode extension
        self.assertEqual(STATUS["print_stats"]["filename"], "cube.gcode")  # what Klipper gets is untouched
        self.assertEqual(s["progress"], 25.0)
        self.assertEqual((s["layer_num"], s["total_layers"]), (12, 50))
        self.assertEqual(s["temperatures"]["nozzle"], 198.5)
        self.assertTrue(s["temperatures"]["nozzle_heating"])
        self.assertEqual(s["speed_percent"], 120)
        self.assertEqual(s["fans"]["part"], 50)
        self.assertEqual(s["remaining_time"], 30)  # 600 s done at 25% -> 1800 s left
        self.assertEqual(len(s["ams"]), 1)  # the ACE Pro

    def test_subtask_name_drops_a_trailing_gcode_in_any_case(self):
        cases = (("Plate_1.GCODE", "Plate_1"), ("a.gcode", "a"), ("model.3mf", "model.3mf"), ("", None))
        for filename, shown in cases:
            with self.subTest(filename):
                self.assertEqual(ml.to_snapshot({"print_stats": {"filename": filename}})["subtask_name"], shown)
        self.assertIsNone(ml.to_snapshot({"print_stats": {}})["subtask_name"])

    def test_webcam_flip_h_plus_flip_v_asks_go2rtc_for_the_180_preset(self):
        # A webcam mounted upside down (flip_horizontal + flip_vertical, rotation 0) is EXIF Orientation 3,
        # which is go2rtc's h264_o3 template (hflip+vflip): the source string must carry it.
        FakeMoonraker.webcams = [{"name": "Laterale", "enabled": True, "snapshot_url": "/webcam/?action=snapshot",
                                  "flip_horizontal": True, "flip_vertical": True, "rotation": 0}]
        server.LINKS["sv08test"] = self.link
        self.addCleanup(server.LINKS.pop, "sv08test", None)
        src = server.go2rtc.source_for("sv08test", {"model": "KLIPPER-SV08"}, "http://bff", "tok",
                                       server.cam_orientation("sv08test"))
        self.assertEqual(self.link.cam_flip, (True, True, 0))
        self.assertTrue(src.endswith("#video=h264_o3"), src)

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

    def test_idle_state_words_allow_motion(self):
        # A Klipper sitting still must not read as None or as a blocking state, or homing is refused.
        for word in ("standby", "complete", "cancelled", "error"):
            with self.subTest(word=word):
                state = ml.to_snapshot({"print_stats": {"state": word}})["state"]
                self.assertIsNotNone(state)
                self.assertNotIn(state, server.MOTION_BLOCKED)
        self.assertEqual(ml.to_snapshot({"print_stats": {"state": "printing"}})["state"], "RUNNING")
        self.assertEqual(ml.to_snapshot({"print_stats": {"state": "paused"}})["state"], "PAUSE")

    def test_axes_come_from_toolhead(self):
        raw = dict(STATUS, toolhead={"homed_axes": "xyz", "axis_minimum": [-5.0, -5.0, -2.0],
                                     "axis_maximum": [300.0, 300.0, 260.0], "position": [10.0, 20.0, 30.0]})
        self.assertEqual(ml.to_snapshot(raw)["axes"], {"homed": "xyz", "min": [-5.0, -5.0, -2.0],
                                                       "max": [300.0, 300.0, 260.0],
                                                       "pos": [10.0, 20.0, 30.0]})
        self.assertIsNone(ml.to_snapshot(STATUS)["axes"])  # no toolhead object: no limits, no position

    def added_klipper(self, **snap):
        """A Klipper printer added from the app (so motion is unlocked) with the snapshot fields given."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"STATE_DIRECTORY": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        server.onboard.save_store(Path(tmp.name) / "printers.json",
                                  {"sv08": {"host": "127.0.0.1", "port": 1, "name": "SV08",
                                            "model": "KLIPPER-SV08"}})
        state = {"connected": True, "state": "IDLE", "home_flag": None,
                 "axes": {"homed": "xyz", "min": [-5, -5, -2], "max": [300, 300, 260], "pos": [10, 20, 30]}}
        state.update(snap)
        self.link.snapshot = lambda: dict(state)
        server.LINKS["sv08"] = self.link
        self.addCleanup(server.LINKS.pop, "sv08", None)
        self.addCleanup(server.AXES.clear)
        return state

    def test_envelope_from_axes(self):
        self.added_klipper()
        env = server.envelope_of("sv08")
        self.assertEqual((env["x"], env["y"], env["z"]), ((-5, 300), (-5, 300), (0, 260)))
        self.assertEqual(env["first"], {"x": 147.5, "y": 147.5, "z": 10})
        self.assertEqual((env["safe"], env["keep_out"]), (None, None))
        self.assertEqual(server.envelope_name_of("sv08"), "KLIPPER")

    def test_envelope_without_axes_is_an_error(self):
        self.added_klipper(axes=None)
        with self.assertRaisesRegex(server.ToolError, "limiti assi non letti"):
            server.envelope_of("sv08")

    def test_homed_and_pos_come_from_the_printer(self):
        self.added_klipper(axes={"homed": "xyz", "min": [-5, -5, -2], "max": [300, 300, 260],
                                 "pos": [10.004, 20.0, 30.5]})
        state = server.axes_of("sv08")
        self.assertTrue(state["homed"])
        self.assertEqual(state["pos"], {"x": 10.0, "y": 20.0, "z": 30.5})

    def test_partial_homing_resets_pos(self):
        for homed in ("", "x", "xy"):
            with self.subTest(homed=homed):
                self.added_klipper(axes={"homed": homed, "min": [-5, -5, -2], "max": [300, 300, 260],
                                         "pos": [10, 20, 30]})
                state = server.axes_of("sv08")
                self.assertFalse(state["homed"])
                self.assertEqual(state["pos"], {"x": None, "y": None, "z": None})

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

    def test_camera_snapshot_from_the_first_enabled_webcam(self):
        self.assertEqual(self.link.snapshot_url(), f"http://127.0.0.1/webcam/?action=snapshot")
        feed = server.camera.SnapshotFeed(lambda: f"http://127.0.0.1:{self.port}/webcam/?action=snapshot")
        self.assertTrue(feed.latest().startswith(b"\xff\xd8"))
        with self.assertRaises(server.camera.CameraError):
            server.camera.SnapshotFeed(lambda: f"http://127.0.0.1:{self.port}/printer/info").latest()

    def test_light_found_from_macros_and_controlled(self):
        self.link.poll()
        self.assertEqual(self.link.light, {"led": "main_led", "on": "mainled_on", "off": "mainled_off"})
        self.assertTrue(self.link.snapshot()["chamber_light"])  # white channel at 1.0
        self.assertTrue(self.link.command("light", on=True))
        self.assertTrue(self.link.command("light", on=False))
        paths = [p for p, _ in FakeMoonraker.calls]
        self.assertEqual(paths, ["/printer/gcode/script?script=mainled_on", "/printer/gcode/script?script=mainled_off"])

    def test_light_unknown_without_macros(self):
        self.assertEqual(ml.find_light(["led x", "gcode_macro PAUSE"]), None)
        self.assertIsNone(ml.to_snapshot({})["chamber_light"])
        self.assertFalse(ml.MoonrakerLink("x", "127.0.0.1", port=1).command("light", on=True))

    def test_light_from_plain_wled_macros(self):
        self.assertEqual(ml.find_light(E3K_OBJECTS, E3K_CONFIG, E3K_STRIPS),
                         {"led": None, "on": "LIGHT_ON", "off": "LIGHT_OFF", "wled": "light", "off_preset": 2})

    def test_light_from_a_bare_wled_strip(self):
        self.assertEqual(ml.find_light([], None, E3K_STRIPS), {"led": None, "on": None, "off": None, "wled": "light"})

    def test_wled_light_state_and_commands(self):
        FakeMoonraker.objects, FakeMoonraker.config = E3K_OBJECTS, E3K_CONFIG
        FakeMoonraker.strips = E3K_STRIPS
        self.link.poll()
        self.assertTrue(self.link.snapshot()["chamber_light"])  # on, and not at the off preset
        FakeMoonraker.strips = {"light": {"status": "on", "preset": 2}}
        self.link.poll()
        self.assertFalse(self.link.snapshot()["chamber_light"])  # on, but at the off preset
        self.assertTrue(self.link.command("light", on=True))
        self.assertTrue(self.link.command("light", on=False))
        paths = [p for p, _ in FakeMoonraker.calls]
        self.assertEqual(paths, ["/printer/gcode/script?script=LIGHT_ON", "/printer/gcode/script?script=LIGHT_OFF"])

    def test_wled_commands_without_macros(self):
        FakeMoonraker.objects = []
        FakeMoonraker.strips = E3K_STRIPS
        self.link.poll()
        self.assertEqual(self.link.light, {"led": None, "on": None, "off": None, "wled": "light"})
        self.assertTrue(self.link.command("light", on=True))
        self.assertTrue(self.link.command("light", on=False))
        paths = [p for p, _ in FakeMoonraker.calls]
        self.assertEqual(paths, ["/machine/wled/on?strip=light", "/machine/wled/off?strip=light"])

    def test_wled_light_is_unknown_until_the_strip_answers(self):
        light = {"led": None, "on": "LIGHT_ON", "off": "LIGHT_OFF", "wled": "light"}
        self.assertIsNone(ml.to_snapshot(STATUS, light=light, strips={})["chamber_light"])

    def wled_link(self, address):
        """The real E3K light, with WLED's own address in Moonraker's config (served once, on the first poll)."""
        FakeMoonraker.objects, FakeMoonraker.config = E3K_OBJECTS, E3K_CONFIG
        FakeMoonraker.strips = E3K_STRIPS  # Moonraker: on, preset 1 (1 = lit, 2 = off preset)
        FakeMoonraker.server_config = {"wled light": {"type": "http", "address": address}}
        self.link.poll()
        return self.link

    def test_wled_direct_off_wins_over_moonraker_on(self):
        # Turned off from the WLED app: Moonraker still reports the strip on at preset 1.
        link = self.wled_link(f"127.0.0.1:{self.wled_port}")
        FakeWled.state = {"on": False, "ps": 2}
        link.poll()
        self.assertEqual(link.wled_address, f"127.0.0.1:{self.wled_port}")
        self.assertEqual(link.wled_direct, {"on": False, "ps": 2})
        self.assertFalse(link.snapshot()["chamber_light"])

    def test_wled_direct_on_wins_over_moonraker_off_preset(self):
        link = self.wled_link(f"127.0.0.1:{self.wled_port}")
        FakeMoonraker.strips = {"light": {"status": "on", "preset": 2}}  # Moonraker: the off preset
        FakeWled.state = {"on": True, "ps": 1}
        link.poll()
        self.assertTrue(link.snapshot()["chamber_light"])

    def test_unreachable_wled_falls_back_to_moonraker_strips(self):
        link = self.wled_link("127.0.0.1:1")  # nothing listens there
        self.assertIsNone(link.wled_direct)
        self.assertTrue(link.snapshot()["chamber_light"])  # Moonraker: on, preset 1

    def test_wled_address_only_for_http_strips(self):
        FakeMoonraker.objects, FakeMoonraker.config = E3K_OBJECTS, E3K_CONFIG
        FakeMoonraker.strips = E3K_STRIPS
        FakeMoonraker.server_config = {"wled light": {"type": "mqtt", "address": "wled/light"}}
        self.link.poll()
        self.assertIsNone(self.link.wled_address)

    def test_speed_command_and_level_snapshot(self):
        self.link.poll()
        self.assertEqual(self.link.snapshot()["speed_level"], 3)  # speed_factor 1.2 -> nearest is 124 %
        self.assertTrue(self.link.command("speed", level=1))
        paths = [p for p, _ in FakeMoonraker.calls]
        self.assertEqual(paths, ["/printer/gcode/script?script=M220+S50"])
        self.assertFalse(self.link.command("speed", level=9))

    def test_fan_commands_use_klipper_gcode(self):
        self.link.poll()
        self.assertFalse(self.link.command("fan", fan="aux", percent=50))  # no aux fan_generic
        self.assertTrue(self.link.command("fan", fan="part", percent=40))
        FakeMoonraker.objects = E3K_OBJECTS
        e3k = ml.MoonrakerLink("e3k", "127.0.0.1", port=self.port)
        e3k.poll()
        self.assertTrue(e3k.command("fan", fan="aux", percent=50))
        paths = [urllib.parse.unquote_plus(p) for p, _ in FakeMoonraker.calls]
        self.assertEqual(paths, ["/printer/gcode/script?script=M106 S102",
                                 "/printer/gcode/script?script=SET_FAN_SPEED FAN=aux_fan SPEED=0.5"])

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

    def test_a_sliced_sv08_file_passes_the_start_checks(self):
        data = (Path(__file__).parent / "fixtures" / "sv08-cube20.gcode.3mf").read_bytes()
        facts = server.inspect_3mf(data)
        self.assertEqual(facts["model"], "Sovol SV08")
        profile = server.onboard.MODEL_PROFILES["KLIPPER-SV08"]
        raw = ml.to_snapshot(STATUS)
        self.assertIsNone(server.check_compatible(profile, facts, raw))  # no nozzle/AMS feed comparison
        other = dict(facts, model="Bambu Lab A1 mini")
        self.assertIn("Sovol SV08", server.check_compatible(profile, other, raw))

    def test_klipper_filament_comes_from_the_ace_slot(self):
        raw = ml.to_snapshot(STATUS)
        with mock.patch.object(server, "stored_spool", return_value={}):
            self.assertEqual(server.klipper_filaments("sv08", raw, ["A3"]), [{"filament_id": None, "filament_type": "PETG"}])
            self.assertEqual(server.klipper_filaments("sv08", raw, [None]), [{"filament_id": None, "filament_type": "PLA"}])

    def test_set_spool_on_klipper_is_stored_and_shown(self):
        """No AMS: set_spool keeps the spool in BFF and printer_info shows it as the external one."""
        state = self.added_klipper()
        state.pop("ams", None)
        with tempfile.TemporaryDirectory() as tmp:
            server.open_db(str(Path(tmp) / "state.db"))
            with mock.patch.dict(server.CFG, {"printers": {"sv08": 1}, "profiles": ""}):
                reply = server.tool_set_spool({"printer": "sv08", "profile_id": "GFG99", "color": "FF0000FF"})
                info = json.loads(server.tool_printer_info({"printer": "sv08"})[0]["text"])
                stored = json.loads(server.DB.execute(
                    "SELECT value FROM settings WHERE printer='sv08' AND key='spool'").fetchone()[0])
        self.assertEqual(json.loads(reply[0]["text"]),
                         {"printer": "sv08", "action": "set_spool", "outcome": "sent",
                          "detail": "bobina esterna: PETG FF0000FF"})
        self.assertEqual(stored, {"profile_id": "GFG99", "color": "FF0000FF", "material": "PETG", "name": "Generic PETG"})
        filament = info["filaments"][0]
        self.assertEqual((filament["slot"], filament["material"], filament["color"], filament["profile_id"]),
                         ("external", "PETG", "FF0000FF", "GFG99"))
        self.assertEqual(FakeMoonraker.calls, [])  # nothing sent: Klipper has no tray to write

    def test_klipper_filaments_use_the_stored_spool(self):
        raw = ml.to_snapshot(STATUS)
        with tempfile.TemporaryDirectory() as tmp:
            server.open_db(str(Path(tmp) / "state.db"))
            server.DB.execute("INSERT OR REPLACE INTO settings VALUES ('sv08', 'spool', ?)",
                              (json.dumps({"profile_id": "GFG99", "color": "FF0000FF", "material": "PETG"}),))
            self.assertEqual(server.klipper_filaments("sv08", raw, ["external"]),
                             [{"filament_id": "GFG99", "filament_type": "PETG"}])
            self.assertEqual(server.klipper_filaments("sv08", raw, ["A3"]),  # the ACE slot still wins
                             [{"filament_id": None, "filament_type": "PETG"}])


class KlipperErrors(unittest.TestCase):
    """Printer errors reach the snapshot (and so the app banner and every notification) with a reason."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeMoonraker)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeMoonraker.klippy = "ready"
        FakeMoonraker.status = {"print_stats": {"state": "standby"}}
        FakeMoonraker.webhooks = {}
        FakeMoonraker.gcode_store = []
        self.now = [1000.0]
        self.link = ml.MoonrakerLink("e3k", "127.0.0.1", port=self.port, clock=lambda: self.now[0])

    def flat(self):
        snap = self.link.snapshot()
        return ha_mqtt.flat_state({**snap, "attention": server.attention(snap)})

    def test_idle_error_once_and_old_lines_ignored(self):
        FakeMoonraker.gcode_store = [{"time": 100, "message": "!! Must home axis first"}]
        self.link.poll()  # the restart baseline: what is already there is history
        prev = self.flat()
        self.now[0] += 10
        self.link.poll()
        self.assertEqual(self.flat()["alarm"], None)
        FakeMoonraker.gcode_store.append({"time": 200, "message": "!! Probe triggered prior to movement"})
        self.now[0] += 10
        self.link.poll()
        cur = self.flat()
        self.assertEqual(cur["alarm"], "Probe problem: Probe triggered prior to movement")
        self.assertEqual([k for k, _ in ha_mqtt.events(prev, cur)], ["error"])
        self.assertIn("Probe triggered prior to movement", ha_mqtt.events(prev, cur)[0][1])
        self.now[0] += 10
        self.link.poll()
        self.assertEqual(ha_mqtt.events(cur, self.flat()), [])  # same line: no second notification
        self.assertIsNone(self.link._new_error)

    def test_probe_failure_during_print_keeps_the_reason(self):
        FakeMoonraker.status = {"print_stats": {"state": "error", "filename": "cube.gcode"}}
        FakeMoonraker.gcode_store = [{"time": 100, "message": "!! Probe triggered prior to movement"}]
        self.link.poll()
        snap = self.link.snapshot()
        self.assertEqual(snap["state"], "FAILED")
        self.assertEqual(snap["hms_errors"][0]["description"],
                         "Probe problem: Probe triggered prior to movement")
        prev = {"state": "RUNNING", "subtask_name": "cube", "progress": 40, "alarm": None, "end_reason": "none"}
        cur = self.flat()
        out = ha_mqtt.events(prev, cur)
        self.assertEqual([k for k, _ in out], ["failed"])
        self.assertIn("Probe problem", out[0][1])
        self.assertIn("Probe triggered prior to movement", out[0][1])

    def test_shutdown_state_message_is_the_reason(self):
        FakeMoonraker.klippy = "shutdown"
        FakeMoonraker.webhooks = {"state": "shutdown", "state_message": "MCU 'mcu' shutdown: Timer too close"}
        self.link.poll()
        snap = self.link.snapshot()
        self.assertEqual(snap["state"], "FAILED")
        self.assertEqual(snap["hms_errors"][0]["description"],
                         "Printer board problem: MCU 'mcu' shutdown: Timer too close")

    def test_common_errors_get_a_plain_english_prefix(self):
        self.assertEqual(ml.error_text("No trigger on probe after full movement"),
                         "Probe did not trigger: No trigger on probe after full movement")
        self.assertEqual(ml.error_text("Heater extruder not heating at expected rate"),
                         "Heating problem: Heater extruder not heating at expected rate")
        self.assertEqual(ml.error_text("ADC out of range"), "Temperature sensor problem: ADC out of range")
        self.assertEqual(ml.error_text("Lost communication with MCU 'mcu'"),
                         "Printer board problem: Lost communication with MCU 'mcu'")
        self.assertEqual(ml.error_text("Something odd"), "Something odd")


class RunoutSensorTest(unittest.TestCase):
    def test_runout_sensors_reach_the_snapshot(self):
        link = ml.MoonrakerLink("sv08", "127.0.0.1", port=1)
        link.raw = {"filament_switch_sensor runout": {"filament_detected": True, "enabled": False}}
        snap = link.snapshot()
        self.assertEqual(snap["filament_switch_sensor runout"], {"filament_detected": True, "enabled": False})


if __name__ == "__main__":
    unittest.main()
