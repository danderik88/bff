"""Homing and axis moves over the direct MQTT link."""
import json
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from bff_mcp import onboard, server  # noqa: E402
import test_bff_direct as direct_tests  # noqa: E402

PROFILE = {"command_timeout_s": 0.3, "supports": {"stop": True, "start": True}}
JPEG = b"\xff\xd8photo"


class Axes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"STATE_DIRECTORY": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        onboard.save_store(tmp / "printers.json", {"a1": {
            "host": "host", "serial": "s", "access_code": "code", "name": "Delta", "model": "N2S"}})
        (tmp / "a1.json").write_text(json.dumps(PROFILE))
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True, profiles=str(tmp), public_url="http://bff")
        server.open_db(str(tmp / "state.db"))
        server.AXES.clear()
        self.link = server.LINKS["a1"] = direct_tests.FakeLink(state="IDLE")
        # Every clock read jumps a minute: moves in a test never wait for the busy guard.
        self.now = 0.0
        def tick():
            self.now += 60
            return self.now
        clock = mock.patch.object(server, "CLOCK", tick)
        clock.start()
        self.addCleanup(clock.stop)
        photo = mock.patch.object(server, "with_light", return_value=(JPEG, True))
        photo.start()
        self.addCleanup(photo.stop)

    def tearDown(self):
        server.DB.close()
        server.LINKS.clear()
        server.AXES.clear()
        server.CFG.pop("control", None)
        self.tmp.cleanup()

    def gcodes(self):
        return [p["print"]["param"] for p in self.link.published if p.get("print", {}).get("command") == "gcode_line"]

    def key(self):
        return "key-" + secrets.token_hex(6)

    def prepare_home(self, axes):
        reply = server.tool_prepare_home({"printer": "a1", "axes": axes})
        return reply, json.loads(reply[-1]["text"])

    def home(self, axes="all"):
        token = self.prepare_home(axes)[1]["confirm_token"]
        return server.tool_confirm_home({"printer": "a1", "token": token, "idempotency_key": self.key()})

    def move(self, **xyz):
        return server.tool_move({"printer": "a1", "idempotency_key": self.key(), **xyz})

    def test_gcode_payload_ends_with_newline_and_counts_sequence(self):
        self.assertTrue(self.link.gcode("G28"))
        self.link.gcode("G28 X\n")
        self.assertEqual(self.link.published, [
            {"print": {"command": "gcode_line", "param": "G28\n", "sequence_id": "1"}},
            {"print": {"command": "gcode_line", "param": "G28 X\n", "sequence_id": "2"}},
        ])

    def test_prepare_home_gives_photo_rules_and_token(self):
        reply, text = self.prepare_home("xy")
        self.assertEqual(reply[0]["type"], "image")
        self.assertTrue(text["photo_url"].startswith("http://bff/photo/"))
        self.assertEqual(text["regole"], server.PLATE_RULES["A1"])
        self.assertIn("confirm_token", text)
        self.assertIn("'G28 X'", text["summary"])
        self.assertIn("'G28'", text["summary"])
        self.assertEqual(self.gcodes(), [])

    def test_prepare_home_rejects_unknown_axes(self):
        self.assertRaises(server.ToolError, server.tool_prepare_home, {"printer": "a1", "axes": "z"})

    def test_home_all_sets_homed_and_xy_does_not(self):
        self.assertEqual(json.loads(self.home("xy")[0]["text"])["outcome"], "sent")
        self.assertEqual(self.gcodes(), ["G28 X\n"])
        self.assertFalse(server.AXES["a1"]["homed"])
        self.home("all")
        self.assertEqual(self.gcodes(), ["G28 X\n", "G28\n"])
        self.assertTrue(server.AXES["a1"]["homed"])
        self.assertEqual(server.AXES["a1"]["pos"], {"x": None, "y": None, "z": None})

    def test_home_not_sent_is_an_error(self):
        self.link.can_send = False
        self.assertRaises(server.ToolError, self.home, "all")
        self.assertFalse(server.AXES["a1"]["homed"])

    def klipper(self, homed="xyz", pos=(10.0, 20.0, 30.0), **extra):
        """A Klipper printer in the app: its own envelope and its own home G-code."""
        store = onboard.load_store(server.store_path())
        store["klip"] = {"host": "127.0.0.1", "port": 1, "name": "SV08", "model": "KLIPPER-SV08"}
        onboard.save_store(server.store_path(), store)
        server.CFG["printers"].update({"klip": 2})
        link = server.MoonrakerLink("klip", "127.0.0.1", port=1)
        snap = {"connected": True, "state": "IDLE", "home_flag": None,
                "axes": {"homed": homed, "min": [-5, -5, -2], "max": [300, 300, 260], "pos": list(pos)}}
        snap.update(extra)
        link.snapshot = lambda: dict(snap)
        sent = []
        link.gcode = lambda text: sent.append(text) or True
        server.LINKS["klip"] = link
        return sent

    def test_klipper_home_xy_is_g28_xy(self):
        sent = self.klipper()
        text = json.loads(server.tool_prepare_home({"printer": "klip", "axes": "xy"})[-1]["text"])
        self.assertIn("'G28 X Y'", text["summary"])
        server.tool_confirm_home({"printer": "klip", "token": text["confirm_token"],
                                  "idempotency_key": self.key()})
        self.assertEqual(sent, ["G28 X Y"])

    def jog(self, axis="x", distance=10):
        return server.tool_jog({"printer": "klip", "axis": axis, "distance": distance,
                                "idempotency_key": self.key()})

    def test_klipper_jog_is_relative_gcode(self):
        sent = self.klipper()
        reply = json.loads(self.jog("y", 10)[0]["text"])
        self.assertEqual(reply["outcome"], "sent")
        self.assertEqual(sent, ["G91\nG1 Y10 F3000\nG90"])
        self.jog("x", -1)
        self.jog("z", 1)
        self.assertEqual(sent[1:], ["G91\nG1 X-1 F3000\nG90", "G91\nG1 Z1 F600\nG90"])

    def test_klipper_jog_refused_when_axis_not_homed(self):
        sent = self.klipper(homed="xy")
        with self.assertRaisesRegex(server.ToolError, "Asse Z non azzerato"):
            self.jog("z", 1)
        self.assertEqual(sent, [])
        self.jog("x", 1)  # the homed axes still jog
        self.assertEqual(sent, ["G91\nG1 X1 F3000\nG90"])

    def test_klipper_jog_refused_below_1mm(self):
        sent = self.klipper(pos=(10, 20, 1.5))
        with self.assertRaisesRegex(server.ToolError, "Z sotto 1 mm"):
            self.jog("z", -1)
        self.assertEqual(sent, [])

    def test_klipper_jog_out_of_range_refused(self):
        self.klipper(pos=(10, 20, 30))
        with self.assertRaisesRegex(server.ToolError, "fuori area"):
            self.jog("x", -100)  # 10 - 100 = -90, the minimum is -5
        for bad in (200, -101, "10", True, float("nan")):
            with self.subTest(distance=bad):
                self.assertRaises(server.ToolError, self.jog, "x", bad)

    def test_jog_only_for_klipper(self):
        with self.assertRaisesRegex(server.ToolError, "jog solo per stampanti Klipper"):
            server.tool_jog({"printer": "a1", "axis": "x", "distance": 1, "idempotency_key": self.key()})

    def test_klipper_motion_refused_while_printing(self):
        for state in ("RUNNING", "PAUSE", "PREPARE", "SLICING", None):
            with self.subTest(state=state):
                sent = self.klipper(state=state)
                self.assertRaises(server.ToolError, self.jog)
                self.assertRaises(server.ToolError, server.tool_home,
                                  {"printer": "klip", "axes": "xy", "idempotency_key": self.key()})
                self.assertRaises(server.ToolError, server.tool_motors_off,
                                  {"printer": "klip", "idempotency_key": self.key()})
                self.assertEqual(sent, [])

    def test_klipper_home_direct_sends_the_gcode(self):
        sent = self.klipper()
        server.tool_home({"printer": "klip", "axes": "xy", "idempotency_key": self.key()})
        server.tool_home({"printer": "klip", "axes": "all", "idempotency_key": self.key()})
        self.assertEqual(sent, ["G28 X Y", "G28"])
        self.assertRaises(server.ToolError, server.tool_home,
                          {"printer": "klip", "axes": "z", "idempotency_key": self.key()})
        self.assertRaises(server.ToolError, server.tool_home,
                          {"printer": "a1", "axes": "xy", "idempotency_key": self.key()})

    def test_klipper_motors_off_sends_m84_and_forgets_the_position(self):
        sent = self.klipper()
        server.axes_of("klip")
        server.tool_motors_off({"printer": "klip", "idempotency_key": self.key()})
        self.assertEqual(sent, ["M84"])
        self.assertNotIn("klip", server.AXES)
        self.assertRaises(server.ToolError, server.tool_motors_off,
                          {"printer": "a1", "idempotency_key": self.key()})

    def test_move_refused_when_not_homed(self):
        self.assertRaises(server.ToolError, self.move, x=-48, y=0, z=10)
        self.assertEqual(self.gcodes(), [])

    def test_move_refused_out_of_range_or_not_a_number(self):
        self.home()
        for bad in ({"x": 300}, {"x": -49}, {"y": -1}, {"z": 257}, {"x": "10"}, {"x": True}, {"z": float("nan")}):
            with self.subTest(bad=bad):
                self.assertRaises(server.ToolError, self.move, **{"x": -48, "y": 0, "z": 10, **bad})
        self.assertEqual(self.gcodes(), ["G28\n"])

    def test_first_move_needs_xyz(self):
        self.home()
        self.assertRaises(server.ToolError, self.move, x=-48, z=10)
        self.assertEqual(self.gcodes(), ["G28\n"])

    def test_first_move_after_homing_moves_z_first_and_needs_confirm(self):
        self.home()
        self.assertRaises(server.ToolError, self.move, x=-48, y=256, z=100)
        reply = server.tool_prepare_move({"printer": "a1", "x": 10, "y": 20, "z": 100})
        token = json.loads(reply[-1]["text"])["confirm_token"]
        server.tool_confirm_move({"printer": "a1", "token": token, "idempotency_key": self.key()})
        self.assertEqual(self.gcodes()[-1], "G90\nG21\nG1 Z100 F600\nG1 X10 Y20 F3000\n")

    def test_descent_free_only_at_safe_pose(self):
        self.home()
        server.AXES["a1"]["pos"] = {"x": 10, "y": 10, "z": 50}
        self.assertRaises(server.ToolError, self.move, x=10, y=10, z=10)
        self.assertRaises(server.ToolError, self.move, x=-48, y=10, z=10)
        self.move(x=-48, y=256, z=10)
        self.assertEqual(self.gcodes()[-1], "G90\nG21\nG1 X-48 Y256 F3000\nG1 Z10 F600\n")
        self.move(y=0)
        self.assertRaises(server.ToolError, self.move, z=5)

    def test_z_up_then_xy(self):
        self.home()
        server.AXES["a1"]["pos"] = {"x": -48, "y": 0, "z": 10}
        self.move(x=100, y=50.5, z=20)
        self.assertEqual(self.gcodes()[-1], "G90\nG21\nG1 Z20 F600\nG1 X100 Y50.5 F3000\n")
        self.move(y=20)
        self.assertEqual(self.gcodes()[-1], "G90\nG21\nG1 Y20 F3000\n")
        self.assertRaises(server.ToolError, self.move, z=5)
        self.assertRaises(server.ToolError, self.move, x=-48, z=5)

    def confirm_move(self, **xyz):
        reply = server.tool_prepare_move({"printer": "a1", **xyz})
        token = json.loads(reply[-1]["text"])["confirm_token"]
        return server.tool_confirm_move({"printer": "a1", "token": token, "idempotency_key": self.key()})

    def p1(self):
        """Make the A1 test printer answer with the P1 envelope, the only one with verified home_flag bits."""
        return mock.patch.multiple(server, envelope_name_of=mock.Mock(return_value="P1"),
                                   envelope_of=mock.Mock(return_value=server.ENVELOPES["P1"]))

    def test_home_flag_bits_make_p1_homed(self):
        with self.p1():
            for flag, homed in ((0, False), (3, False), (7, True), (15, True)):
                with self.subTest(flag=flag):
                    self.link.feed({"print": {"home_flag": flag}})
                    self.assertEqual(server.axes_of("a1", self.link.snapshot())["homed"], homed)

    def test_home_flag_drop_resets_position(self):
        with self.p1():
            self.link.feed({"print": {"home_flag": 7}})
            state = server.axes_of("a1", self.link.snapshot())
            state["pos"] = {"x": 10, "y": 20, "z": 30}
            self.link.feed({"print": {"home_flag": 0}})
            state = server.axes_of("a1", self.link.snapshot())
            self.assertFalse(state["homed"])
            self.assertEqual(state["pos"], {"x": None, "y": None, "z": None})

    def test_a1_ignores_home_flag(self):
        self.link.feed({"print": {"home_flag": 0}})
        self.home("all")
        self.assertTrue(server.axes_of("a1")["homed"])

    def test_z_below_1mm_refused(self):
        self.home()
        self.confirm_move(x=-48, y=256, z=50)
        with self.assertRaisesRegex(server.ToolError, "Z sotto 1 mm"):
            self.move(z=0.5)
        self.assertEqual(self.gcodes()[-1], "G90\nG21\nG1 Z50 F600\nG1 X-48 Y256 F3000\n")

    def test_first_move_xy_needs_z_10(self):
        self.home()
        with self.assertRaisesRegex(server.ToolError, "almeno 10 mm"):
            server.tool_prepare_move({"printer": "a1", "x": 100, "y": 100, "z": 2})

    def test_xy_needs_z_10_first(self):
        self.home()
        self.confirm_move(x=-48, y=256, z=50)  # safe pose, so Z may come down there
        self.confirm_move(z=5)
        with self.assertRaisesRegex(server.ToolError, "Alza Z ad almeno 10 mm"):
            self.move(y=200)
        self.move(z=15)
        self.move(y=200)
        self.assertEqual(server.AXES["a1"]["pos"]["y"], 200)

    def test_xy_needs_confirm_when_plate_not_clear(self):
        self.home()
        self.confirm_move(x=-48, y=256, z=50)
        server.set_plate_pending("a1", True)
        with self.assertRaisesRegex(server.ToolError, "Piatto non liberato"):
            self.move(y=200)
        self.confirm_move(y=200)
        self.assertEqual(server.AXES["a1"]["pos"]["y"], 200)

    def test_home_all_refused_when_plate_not_clear(self):
        server.set_plate_pending("a1", True)
        with self.assertRaisesRegex(server.ToolError, "Piatto da liberare"):
            self.prepare_home("all")
        self.home("xy")  # Z stays put, so it touches nothing
        self.assertEqual(self.gcodes(), ["G28 X\n"])

    def test_move_gcode_starts_g90_g21(self):
        self.home()
        self.confirm_move(x=-48, y=256, z=10)
        self.assertTrue(self.gcodes()[-1].startswith("G90\nG21\n"))
        self.assertIn("F3000", self.gcodes()[-1])
        self.assertIn("F600", self.gcodes()[-1])

    def test_second_move_refused_while_busy(self):
        self.home()
        with mock.patch.object(server, "CLOCK", return_value=100.0):
            self.confirm_move(x=-48, y=256, z=10)
            with self.assertRaisesRegex(server.ToolError, "Movimento precedente"):
                self.move(y=200)
        with mock.patch.object(server, "CLOCK", return_value=1000.0):
            self.move(y=200)
        self.assertEqual(server.AXES["a1"]["pos"]["y"], 200)

    def test_refused_while_paused(self):
        self.home()
        self.link.feed({"print": {"gcode_state": "PAUSE"}})
        with self.assertRaisesRegex(server.ToolError, "PAUSE"):
            self.move(x=-48, y=0, z=10)
        self.assertRaises(server.ToolError, server.tool_prepare_home, {"printer": "a1", "axes": "xy"})
        self.assertEqual(self.gcodes(), ["G28\n"])

    def test_confirm_move_allows_descent(self):
        self.home()
        reply = server.tool_prepare_move({"printer": "a1", "x": 100, "y": 100, "z": 50})
        text = json.loads(reply[-1]["text"])
        self.assertEqual(reply[0]["type"], "image")
        self.assertIn("photo_url", text)
        self.assertIn("G1 Z50 F600", text["summary"])
        self.assertEqual(self.gcodes(), ["G28\n"])
        server.tool_confirm_move({"printer": "a1", "token": text["confirm_token"], "idempotency_key": self.key()})
        self.assertEqual(self.gcodes()[-1], "G90\nG21\nG1 Z50 F600\nG1 X100 Y100 F3000\n")
        self.assertEqual(server.AXES["a1"]["pos"], {"x": 100, "y": 100, "z": 50})

    def test_prepare_move_still_needs_homing(self):
        self.assertRaises(server.ToolError, server.tool_prepare_move, {"printer": "a1", "x": 100, "y": 100, "z": 50})

    def test_state_reset_on_disconnect(self):
        self.home()
        self.link._on_disconnect(None, None, None, None, None)
        self.link.feed({"print": {"gcode_state": "IDLE"}})
        with self.assertRaisesRegex(server.ToolError, "azzerat"):
            self.move(x=-48, y=0, z=10)

    def test_state_reset_on_stop_and_print_start(self):
        done = {"printer": "a1", "action": "x", "outcome": "confirmed", "detail": ""}
        for tool in (server.tool_confirm_stop, server.tool_confirm_print_start):
            with self.subTest(tool=tool.__name__):
                self.home()
                with mock.patch.object(server, "execute", return_value=done):
                    tool({"printer": "a1", "token": "t", "idempotency_key": self.key()})
                self.assertRaises(server.ToolError, self.move, x=-48, y=0, z=10)

    def test_refused_while_printing(self):
        self.home()
        for state in ("RUNNING", "PAUSE", "PREPARE", "SLICING"):
            with self.subTest(state=state):
                self.link.feed({"print": {"gcode_state": state}})
                self.assertRaises(server.ToolError, self.prepare_home, "all")
                self.assertRaises(server.ToolError, self.move, x=-48, y=0, z=10)
                self.assertRaises(server.ToolError, server.tool_prepare_move, {"printer": "a1", "x": -48, "y": 0, "z": 1})
        self.assertEqual(self.gcodes(), ["G28\n"])

    def test_refused_when_not_connected(self):
        self.link.last_report = -1000.0
        self.assertRaises(server.ToolError, self.prepare_home, "all")

    def test_tools_registered(self):
        for name in ("prepare_home", "confirm_home", "move", "prepare_move", "confirm_move"):
            self.assertIn(name, server.TOOLS)
        for name in ("confirm_home", "confirm_move"):
            self.assertIn(server.CONFIRM_RULE, server.TOOLS[name][1])


if __name__ == "__main__":
    unittest.main()


class P1Envelope(unittest.TestCase):
    """Measured on Alpha (P1S) with the user, 2026-11-12."""
    P1 = server.ENVELOPES["P1"]

    def homed(self, x=128, y=128, z=10):
        return {"homed": True, "pos": {"x": x, "y": y, "z": z}, "drops": 0}

    def test_cutter_corner_refused_as_target(self):
        with self.assertRaises(server.ToolError):
            server.plan_move(self.homed(), {"x": 0, "y": 0}, False, self.P1)
        with self.assertRaises(server.ToolError):
            server.plan_move(self.homed(), {"x": 10, "y": 20}, False, self.P1)

    def test_cutter_corner_refused_when_only_crossed(self):
        # From the left edge down to the front edge: both ends outside the box, the straight line cuts the corner.
        with self.assertRaises(server.ToolError):
            server.plan_move(self.homed(x=0, y=40), {"x": 40, "y": 0}, False, self.P1)

    def test_edges_outside_the_corner_are_fine(self):
        server.plan_move(self.homed(), {"x": 0, "y": 128}, False, self.P1)
        server.plan_move(self.homed(), {"x": 128, "y": 0}, False, self.P1)
        server.plan_move(self.homed(), {"x": 256, "y": 256}, False, self.P1)

    def test_z_stops_at_250_and_every_descent_asks(self):
        with self.assertRaises(server.ToolError):
            server.plan_move(self.homed(), {"z": 256}, False, self.P1)
        server.plan_move(self.homed(), {"z": 250}, False, self.P1)
        with self.assertRaises(server.ToolError):  # no safe pose on a P1: the bed is always under the nozzle
            server.plan_move(self.homed(z=100), {"z": 50}, False, self.P1)
        server.plan_move(self.homed(z=100), {"z": 50}, True, self.P1)

    def test_crosses(self):
        box = (0, 0, 20, 30)
        self.assertTrue(server.crosses((0, 40), (40, 0), box))
        self.assertFalse(server.crosses((0, 128), (128, 0), box))
        self.assertTrue(server.crosses((5, 5), (5, 5), box))


class PlateRules(unittest.TestCase):
    def test_rules_take_the_printer_id_not_the_alias(self):
        with mock.patch.dict(server.CFG, {"printers": {"alpha": 7, "a1": 1}}), \
             mock.patch.object(server, "envelope_of", side_effect=lambda a: server.ENVELOPES["P1" if a == "alpha" else "A1"]):
            self.assertIn("CoreXY", server.plate_rules(7))
            self.assertIn("bed slinger", server.plate_rules(1))

    def test_rules_follow_the_model(self):
        with mock.patch.object(server, "envelope_of", return_value=server.ENVELOPES["P1"]):
            self.assertIn("CoreXY", server.plate_rules("alpha"))
        with mock.patch.object(server, "envelope_of", return_value=server.ENVELOPES["A1"]):
            self.assertIn("bed slinger", server.plate_rules("a1"))
