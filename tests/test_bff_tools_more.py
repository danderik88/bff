"""Additional direct MQTT tools: speed, fan, detectors, options, errors, skip, calibration, filament, gcode."""
import json
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from pygmy import server  # noqa: E402
import test_bff_direct as direct_tests  # noqa: E402
from printer_link import PUSHALL  # noqa: E402

PROFILE = {"command_timeout_s": 0.3, "supports": {"stop": True, "start": True}}
JPEG = b"\xff\xd8photo"


class MoreTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "a1.json").write_text(json.dumps(PROFILE))
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True, profiles=str(tmp), public_url="http://bff")
        server.open_db(str(tmp / "state.db"))
        server.AXES.clear()
        self.link = server.LINKS["a1"] = direct_tests.FakeLink(state="IDLE")
        photo = mock.patch.object(server, "with_light", return_value=(JPEG, True))
        photo.start()
        self.addCleanup(photo.stop)

    def tearDown(self):
        server.DB.close()
        server.LINKS.clear()
        server.AXES.clear()
        server.CFG.pop("control", None)
        self.tmp.cleanup()

    def key(self):
        return "key-" + secrets.token_hex(6)

    def state(self, s):
        self.link.feed({"print": {"gcode_state": s}})

    def published(self):
        return list(self.link.published)

    def test_set_print_speed_payload(self):
        self.state("RUNNING")
        reply = server.tool_set_print_speed({"printer": "a1", "level": 2, "idempotency_key": self.key()})
        self.assertEqual(json.loads(reply[0]["text"])["outcome"], "sent")
        self.assertEqual(self.published(), [{"print": {"command": "print_speed", "param": "2", "sequence_id": "0"}}])

    def test_set_print_speed_rejects_bad_level(self):
        for level in (0, 5, "2", 2.0, True):
            with self.subTest(level=level):
                self.assertRaises(server.ToolError, server.tool_set_print_speed,
                                  {"printer": "a1", "level": level, "idempotency_key": self.key()})
        self.assertEqual(self.published(), [])

    def test_set_print_speed_only_in_running_or_pause(self):
        for s in ("IDLE", "FINISH", "FAILED", "PREPARE", "SLICING"):
            self.state(s)
            self.assertRaises(server.ToolError, server.tool_set_print_speed,
                              {"printer": "a1", "level": 2, "idempotency_key": self.key()})
        self.assertEqual(self.published(), [])
        self.state("PAUSE")
        server.tool_set_print_speed({"printer": "a1", "level": 2, "idempotency_key": self.key()})
        self.assertEqual(len(self.published()), 1)

    def test_set_print_speed_replays(self):
        self.state("RUNNING")
        key = self.key()
        server.tool_set_print_speed({"printer": "a1", "level": 2, "idempotency_key": key})
        reply = server.tool_set_print_speed({"printer": "a1", "level": 2, "idempotency_key": key})
        self.assertTrue(json.loads(reply[0]["text"])["replayed"])
        self.assertEqual(len(self.published()), 1)

    def test_set_fan_payload(self):
        server.tool_set_fan({"printer": "a1", "fan": "part", "percent": 100, "idempotency_key": self.key()})
        self.assertEqual(self.published()[-1]["print"]["param"], "M106 P1 S255\n")
        server.tool_set_fan({"printer": "a1", "fan": "aux", "percent": 50, "idempotency_key": self.key()})
        self.assertEqual(self.published()[-1]["print"]["param"], "M106 P2 S128\n")
        server.tool_set_fan({"printer": "a1", "fan": "chamber", "percent": 0, "idempotency_key": self.key()})
        self.assertEqual(self.published()[-1]["print"]["param"], "M106 P3 S0\n")

    def test_set_fan_rejects_bad_args(self):
        for args in ({"fan": "hotend"}, {"percent": 101}, {"percent": -1}, {"percent": 50.5}, {"percent": "50"}):
            self.assertRaises(server.ToolError, server.tool_set_fan, {"printer": "a1", "fan": "part", "percent": 50, "idempotency_key": self.key(), **args})
        self.assertEqual(self.published(), [])

    def test_set_fan_any_state(self):
        for s in ("IDLE", "RUNNING", "PAUSE", "SLICING", "FINISH", "FAILED", "PREPARE"):
            self.state(s)
            server.tool_set_fan({"printer": "a1", "fan": "part", "percent": 10, "idempotency_key": self.key()})
        self.assertEqual(len(self.published()), 7)

    def test_set_detector_payload(self):
        server.tool_set_detector({"printer": "a1", "module": "spaghetti_detector", "enabled": True, "idempotency_key": self.key()})
        self.assertEqual(self.published(), [{"xcam": {"command": "xcam_control_set", "module_name": "spaghetti_detector",
                                                    "control": True, "enable": True, "print_halt": True,
                                                    "sequence_id": "0"}}])
        server.tool_set_detector({"printer": "a1", "module": "pileup_detector", "enabled": False,
                                  "sensitivity": "high", "idempotency_key": self.key()})
        self.assertEqual(self.published()[-1]["xcam"]["halt_print_sensitivity"], "high")
        self.assertFalse(self.published()[-1]["xcam"]["enable"])

    def test_set_detector_rejects_bad_args(self):
        for args in ({"module": "nope"}, {"module": "spaghetti_detector", "enabled": "yes"},
                     {"module": "spaghetti_detector", "enabled": True, "sensitivity": "ultra"}):
            with self.subTest(args=args):
                self.assertRaises(server.ToolError, server.tool_set_detector,
                                  {"printer": "a1", "module": "spaghetti_detector", "enabled": True, "idempotency_key": self.key(), **args})
        self.assertEqual(self.published(), [])

    def test_set_detector_any_state(self):
        self.state("RUNNING")
        server.tool_set_detector({"printer": "a1", "module": "airprint_detector", "enabled": True, "idempotency_key": self.key()})
        self.assertEqual(len(self.published()), 1)

    def test_set_print_option_payload(self):
        server.tool_set_print_option({"printer": "a1", "option": "auto_recovery", "enabled": True, "idempotency_key": self.key()})
        self.assertEqual(self.published(), [{"print": {"command": "print_option", "auto_recovery": True, "sequence_id": "0"}}])

    def test_set_print_option_rejects_bad_args(self):
        for args in ({"option": "nope"}, {"option": "auto_recovery", "enabled": "yes"}):
            self.assertRaises(server.ToolError, server.tool_set_print_option,
                              {"printer": "a1", "option": "auto_recovery", "enabled": True, "idempotency_key": self.key(), **args})
        self.assertEqual(self.published(), [])

    def test_clear_errors_payloads(self):
        server.tool_clear_errors({"printer": "a1", "idempotency_key": self.key()})
        self.assertEqual(self.published()[0], {"print": {"command": "clean_print_error", "sequence_id": "0"}})
        self.assertEqual(self.published()[1], PUSHALL)

    def test_clear_errors_any_state(self):
        self.state("RUNNING")
        server.tool_clear_errors({"printer": "a1", "idempotency_key": self.key()})
        self.assertEqual(len(self.published()), 2)

    def prepare_skip(self):
        reply = server.tool_prepare_skip_objects({"printer": "a1", "obj_ids": [1, 2]})
        return reply, json.loads(reply[-1]["text"])

    def test_skip_objects_prepare_confirm(self):
        self.state("RUNNING")
        reply, text = self.prepare_skip()
        self.assertIn("confirm_token", text)
        self.assertIn("Irreversibile", text["summary"])
        confirm = server.tool_confirm_skip_objects({"printer": "a1", "token": text["confirm_token"], "idempotency_key": self.key()})
        self.assertEqual(json.loads(confirm[0]["text"])["outcome"], "sent")
        self.assertEqual(self.published(), [{"print": {"command": "skip_objects", "obj_list": [1, 2], "sequence_id": "0"}}])

    def test_skip_objects_prepare_rejects_bad_ids(self):
        self.state("RUNNING")
        for bad in (None, [], [1, "2"], [1.5], "1"):
            with self.subTest(bad=bad):
                self.assertRaises(server.ToolError, server.tool_prepare_skip_objects, {"printer": "a1", "obj_ids": bad})
        self.assertEqual(self.published(), [])

    def test_skip_objects_only_in_running_or_pause(self):
        for s in ("IDLE", "FINISH", "FAILED", "SLICING"):
            self.state(s)
            self.assertRaises(server.ToolError, self.prepare_skip)
        self.assertEqual(self.published(), [])
        self.state("PAUSE")
        reply, text = self.prepare_skip()
        server.tool_confirm_skip_objects({"printer": "a1", "token": text["confirm_token"], "idempotency_key": self.key()})
        self.assertEqual(len(self.published()), 1)

    def test_skip_objects_confirm_replays(self):
        self.state("RUNNING")
        _, text = self.prepare_skip()
        key = self.key()
        server.tool_confirm_skip_objects({"printer": "a1", "token": text["confirm_token"], "idempotency_key": key})
        reply = server.tool_confirm_skip_objects({"printer": "a1", "token": text["confirm_token"], "idempotency_key": key})
        self.assertTrue(json.loads(reply[0]["text"])["replayed"])
        self.assertEqual(len(self.published()), 1)

    def prepare_calibration(self, items):
        reply = server.tool_prepare_calibration({"printer": "a1", "items": items})
        return reply, json.loads(reply[-1]["text"])

    def test_calibration_prepare_photo_confirm(self):
        reply, text = self.prepare_calibration(["bed_leveling", "vibration"])
        self.assertEqual(reply[0]["type"], "image")
        self.assertIn("confirm_token", text)
        confirm = server.tool_confirm_calibration({"printer": "a1", "token": text["confirm_token"], "idempotency_key": self.key()})
        self.assertEqual(json.loads(confirm[0]["text"])["outcome"], "sent")
        self.assertEqual(self.published(), [{"print": {"command": "calibration", "option": 6, "sequence_id": "0"}}])
        self.assertNotIn("a1", server.AXES)

    def test_calibration_bitmask(self):
        for items, option in ((["bed_leveling"], 2), (["vibration"], 4), (["motor_noise"], 8),
                              (["bed_leveling", "vibration", "motor_noise"], 14)):
            with self.subTest(items=items):
                server.AXES.clear()
                _, text = self.prepare_calibration(items)
                server.tool_confirm_calibration({"printer": "a1", "token": text["confirm_token"], "idempotency_key": self.key()})
                self.assertEqual(self.published()[-1]["print"]["option"], option)

    def test_calibration_prepare_rejects_bad_items(self):
        for bad in (None, [], ["bed_thing"], ["bed_leveling", "nope"], "bed_leveling"):
            with self.subTest(bad=bad):
                self.assertRaises(server.ToolError, server.tool_prepare_calibration, {"printer": "a1", "items": bad})
        self.assertEqual(self.published(), [])

    def test_calibration_not_in_motion_states(self):
        for s in ("RUNNING", "PAUSE", "PREPARE", "SLICING"):
            self.state(s)
            self.assertRaises(server.ToolError, self.prepare_calibration, ["bed_leveling"])
        self.assertEqual(self.published(), [])
        self.state("IDLE")
        _, text = self.prepare_calibration(["bed_leveling"])
        self.assertIn("confirm_token", text)

    def test_load_filament_prepare_confirm(self):
        text = json.loads(server.tool_prepare_load_filament({"printer": "a1"})[0]["text"])
        self.assertIn("si scalda", text["summary"])
        confirm = server.tool_confirm_load_filament({"printer": "a1", "token": text["confirm_token"], "idempotency_key": self.key()})
        self.assertEqual(json.loads(confirm[0]["text"])["outcome"], "sent")
        payload = {"print": {"command": "ams_change_filament", "ams_id": 255, "slot_id": 254,
                             "target": 254, "curr_temp": -1, "tar_temp": -1, "sequence_id": "0"}}
        self.assertEqual(self.published(), [payload])
        self.assertNotIn("a1", server.AXES)

    def test_load_filament_not_in_motion_states(self):
        for s in ("RUNNING", "PAUSE", "PREPARE", "SLICING"):
            self.state(s)
            self.assertRaises(server.ToolError, server.tool_prepare_load_filament, {"printer": "a1"})
        self.assertEqual(self.published(), [])
        self.state("IDLE")
        text = json.loads(server.tool_prepare_load_filament({"printer": "a1"})[0]["text"])
        self.assertIn("confirm_token", text)

    def test_unload_filament_temperature_below_180(self):
        self.link.feed({"print": {"nozzle_temper": 50}})
        text = json.loads(server.tool_prepare_unload_filament({"printer": "a1"})[0]["text"])
        payload = {"print": {"command": "ams_change_filament", "ams_id": 255, "slot_id": 255,
                             "target": 255, "curr_temp": 210, "tar_temp": 210, "sequence_id": "0"}}
        server.tool_confirm_unload_filament({"printer": "a1", "token": text["confirm_token"], "idempotency_key": self.key()})
        self.assertEqual(self.published(), [payload])
        self.assertNotIn("a1", server.AXES)

    def test_unload_filament_temperature_at_or_above_180(self):
        self.link.feed({"print": {"nozzle_temper": 200}})
        text = json.loads(server.tool_prepare_unload_filament({"printer": "a1"})[0]["text"])
        payload = {"print": {"command": "ams_change_filament", "ams_id": 255, "slot_id": 255,
                             "target": 255, "curr_temp": 200, "tar_temp": 200}}
        server.tool_confirm_unload_filament({"printer": "a1", "token": text["confirm_token"], "idempotency_key": self.key()})
        self.assertEqual(self.published()[-1]["print"]["curr_temp"], 200)
        self.assertEqual(self.published()[-1]["print"]["tar_temp"], 200)

    def test_unload_filament_not_in_motion_states(self):
        for s in ("RUNNING", "PAUSE", "PREPARE", "SLICING"):
            self.state(s)
            self.assertRaises(server.ToolError, server.tool_prepare_unload_filament, {"printer": "a1"})
        self.assertEqual(self.published(), [])

    def test_gcode_prepare_photo_confirm(self):
        reply = server.tool_prepare_gcode({"printer": "a1", "gcode": "G28 X"})
        self.assertEqual(reply[0]["type"], "image")
        text = json.loads(reply[1]["text"])
        self.assertIn("confirm_token", text)
        self.assertIn("G28 X", text["summary"])
        confirm = server.tool_confirm_gcode({"printer": "a1", "token": text["confirm_token"], "idempotency_key": self.key()})
        self.assertEqual(json.loads(confirm[0]["text"])["outcome"], "sent")
        self.assertEqual(self.published(), [{"print": {"command": "gcode_line", "param": "G28 X\n", "sequence_id": "1"}}])
        self.assertNotIn("a1", server.AXES)

    def test_gcode_prepare_rejects_bad_gcode(self):
        for bad in ("", "   ", "x" * 2001, 123, None):
            with self.subTest(bad=bad):
                self.assertRaises(server.ToolError, server.tool_prepare_gcode, {"printer": "a1", "gcode": bad})
        self.assertEqual(self.published(), [])

    def test_gcode_not_in_motion_states(self):
        for s in ("RUNNING", "PAUSE", "PREPARE", "SLICING"):
            self.state(s)
            self.assertRaises(server.ToolError, server.tool_prepare_gcode, {"printer": "a1", "gcode": "G28"})
        self.assertEqual(self.published(), [])
        self.state("IDLE")
        text = json.loads(server.tool_prepare_gcode({"printer": "a1", "gcode": "G28"})[1]["text"])
        self.assertIn("confirm_token", text)

    def test_gcode_confirm_replays(self):
        text = json.loads(server.tool_prepare_gcode({"printer": "a1", "gcode": "G28"})[1]["text"])
        key = self.key()
        server.tool_confirm_gcode({"printer": "a1", "token": text["confirm_token"], "idempotency_key": key})
        reply = server.tool_confirm_gcode({"printer": "a1", "token": text["confirm_token"], "idempotency_key": key})
        self.assertTrue(json.loads(reply[0]["text"])["replayed"])
        self.assertEqual(len(self.published()), 1)

    def test_tools_registered(self):
        names = ("set_print_speed", "set_fan", "set_detector", "set_print_option", "clear_errors",
                 "prepare_skip_objects", "confirm_skip_objects", "prepare_calibration", "confirm_calibration",
                 "prepare_load_filament", "confirm_load_filament", "prepare_unload_filament", "confirm_unload_filament",
                 "prepare_gcode", "confirm_gcode")
        for name in names:
            self.assertIn(name, server.TOOLS)
        for name in ("confirm_skip_objects", "confirm_calibration", "confirm_load_filament",
                     "confirm_unload_filament", "confirm_gcode"):
            self.assertIn(server.CONFIRM_RULE, server.TOOLS[name][1])


if __name__ == "__main__":
    unittest.main()
