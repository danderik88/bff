"""Drying on the AMS 2 Pro and AMS HT: snapshot fields, the ams_dry tool, HA discovery."""
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
from bff_mcp import ha_mqtt, onboard, printer_link, server  # noqa: E402
import test_bff_direct as direct_tests  # noqa: E402

SERIAL = "00600A000008996"


def drying(ams_id, temp=-1, duration=-1, time_left=0):
    return {"id": ams_id, "dry_time": time_left,
            "dry_setting": {"dry_duration": duration, "dry_filament": "", "dry_temperature": temp},
            "dry_sf_reason": []}


class DryTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "a1.json").write_text(json.dumps({"command_timeout_s": 0.3, "supports": {}}))
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True, profiles=str(tmp), public_url="http://bff")
        server.open_db(str(tmp / "state.db"))
        server.AXES.clear()
        self.link = server.LINKS["a1"] = direct_tests.FakeLink(state="IDLE")
        self.model = mock.patch.object(server, "model_code", return_value="N7")  # a P2S: can start drying
        self.model.start()

    def tearDown(self):
        self.model.stop()
        server.DB.close()
        server.LINKS.clear()
        server.AXES.clear()
        server.CFG.pop("control", None)
        self.tmp.cleanup()

    def key(self):
        return "key-" + secrets.token_hex(6)

    def ams(self, module, units):
        """A get_version module (e.g. n3f/0) plus the units the report carries."""
        self.link.feed({"info": {"command": "get_version",
                                 "module": [{"name": module, "sn": SERIAL}]}})
        self.link.feed({"print": {"ams": {"ams": units}}})

    def dry(self, **args):
        return server.tool_ams_dry({"printer": "a1", "idempotency_key": self.key(), **args})

    def payloads(self):
        return [p for p in self.link.published if p.get("print", {}).get("command") == "ams_filament_drying"]

    def test_start_payload_exact(self):
        self.ams("n3f/0", [drying(0)])
        self.dry(unit="A", action="start", temp=55, hours=8)
        self.assertEqual(self.payloads(), [{"print": {
            "command": "ams_filament_drying", "sequence_id": "0", "ams_id": 0, "temp": 55, "cooling_temp": 45,
            "duration": 8, "humidity": 0, "mode": 1, "rotate_tray": False, "filament": "",
            "close_power_conflict": False}}])

    def test_stop_payload_exact(self):
        self.ams("n3f/0", [drying(0)])
        self.assertEqual(_text(self.dry(unit="A", action="stop")), {"sent": True})
        self.assertEqual(self.payloads()[-1], {"print": {
            "command": "ams_filament_drying", "sequence_id": "0", "ams_id": 0, "temp": 0, "cooling_temp": 40,
            "duration": 0, "humidity": 0, "mode": 0, "rotate_tray": False, "filament": "",
            "close_power_conflict": False}})

    def test_plain_ams_has_no_heater(self):
        self.ams("ams/0", [drying(0)])
        with self.assertRaises(server.ToolError) as ctx:
            self.dry(unit="A", action="start", temp=55, hours=8)
        self.assertEqual(str(ctx.exception), "This AMS has no heater")
        self.assertEqual(self.payloads(), [])

    def test_drying_needs_a_printer_that_can_start_it(self):
        self.ams("n3f/0", [drying(0)])
        with mock.patch.object(server, "model_code", return_value="C12"):  # P1S
            with self.assertRaises(server.ToolError) as ctx:
                self.dry(unit="A", action="start", temp=55, hours=8)
        self.assertEqual(str(ctx.exception), "This printer cannot start AMS drying: use the button on the AMS")
        self.assertEqual(self.payloads(), [])
        self.dry(unit="A", action="start", temp=55, hours=8)  # N7 from setUp
        self.assertEqual(len(self.payloads()), 1)

    def test_no_such_unit(self):
        self.ams("n3f/0", [drying(0)])
        with self.assertRaises(server.ToolError) as ctx:
            self.dry(unit="C", action="start", temp=55, hours=8)
        self.assertEqual(str(ctx.exception), "No such AMS on this printer")

    def test_no_drying_during_a_print(self):
        self.ams("n3f/0", [drying(0)])
        for state in ("RUNNING", "PAUSE"):
            self.link.feed({"print": {"gcode_state": state}})
            with self.subTest(state=state), self.assertRaises(server.ToolError) as ctx:
                self.dry(unit="A", action="start", temp=55, hours=8)
            self.assertEqual(str(ctx.exception), "Drying is not available during a print")
        self.assertEqual(self.payloads(), [])

    def test_temperature_range_per_model(self):
        self.ams("n3f/0", [drying(0)])
        for temp in (40, 70):
            with self.subTest(temp=temp), self.assertRaises(server.ToolError) as ctx:
                self.dry(unit="A", action="start", temp=temp, hours=8)
            self.assertEqual(str(ctx.exception), "Drying temperature must be between 45 and 65 °C")
        self.assertEqual(self.payloads(), [])

    def test_ht_accepts_85(self):
        self.link.feed({"info": {"command": "get_version", "module": [{"name": "n3s/128", "sn": SERIAL}]}})
        self.link.feed({"print": {"ams": {"ams": [drying(128)]}}})
        self.dry(unit=128, action="start", temp=70, hours=8)
        self.assertEqual(self.payloads()[-1]["print"]["temp"], 70)
        self.assertEqual(self.payloads()[-1]["print"]["ams_id"], 128)

    def test_hours_range(self):
        self.ams("n3f/0", [drying(0)])
        for hours in (0, 25):
            with self.subTest(hours=hours), self.assertRaises(server.ToolError) as ctx:
                self.dry(unit="A", action="start", temp=55, hours=hours)
            self.assertEqual(str(ctx.exception), "Drying time must be 1 to 24 hours")
        self.assertEqual(self.payloads(), [])


def _text(reply):
    return json.loads(reply[0]["text"])


class SnapshotDrying(unittest.TestCase):
    def snapshot(self, unit):
        link = printer_link.PrinterLink("a1", "host", "serial", "code", "/nonexistent")
        link.feed({"print": {"ams": {"ams": [unit]}}})
        return link.snapshot()["ams"][0]

    def test_drying_true_and_unset_targets(self):
        unit = self.snapshot(drying(0, time_left=120))
        self.assertTrue(unit["drying"])
        self.assertIsNone(unit["dry_target"])
        self.assertIsNone(unit["dry_hours"])

    def test_targets_when_set(self):
        unit = self.snapshot(drying(0, temp="55", duration=8, time_left=60))
        self.assertEqual((unit["dry_target"], unit["dry_hours"]), (55, 8))

    def test_can_dry_only_with_a_heater_and_a_printer_that_can_start_it(self):
        for module, want in (("ams/0", False), ("n3f/0", True), ("n3s/0", True), ("ams_f1/0", False)):
            with self.subTest(module=module):
                link = printer_link.PrinterLink("a1", "host", "serial", "code", "/nonexistent", ams_drying=True)
                link.feed({"info": {"command": "get_version", "module": [{"name": module, "sn": SERIAL}]}})
                link.feed({"print": {"ams": {"ams": [drying(0)]}}})
                self.assertEqual(link.snapshot()["ams"][0]["can_dry"], want)
        plain_printer = printer_link.PrinterLink("a1", "host", "serial", "code", "/nonexistent",
                                                 ams_drying=onboard.ams_drying("C12"))
        plain_printer.feed({"info": {"command": "get_version", "module": [{"name": "n3f/0", "sn": SERIAL}]}})
        plain_printer.feed({"print": {"ams": {"ams": [drying(0)]}}})
        self.assertFalse(plain_printer.snapshot()["ams"][0]["can_dry"])  # AMS 2 Pro on a P1S


class HaDryEntities(unittest.TestCase):
    def topics(self, model, ams_drying=True):
        return dict(ha_mqtt.serial_unit_discovery(SERIAL, model, "a1", "A", "Alpha", ams_drying))

    def test_dry_entities_only_with_a_heater(self):
        pro = self.topics("AMS 2 Pro")
        for component, obj in (("binary_sensor", "drying"), ("sensor", "dry_target"), ("number", "dry_temp"),
                               ("number", "dry_hours"), ("button", "dry_start"), ("button", "dry_stop")):
            self.assertIn(f"homeassistant/{component}/bff_ams_{SERIAL.lower()}/{obj}/config", pro)
        self.assertEqual(pro[f"homeassistant/number/bff_ams_{SERIAL.lower()}/dry_temp/config"]["max"], 65)
        self.assertEqual(self.topics("AMS HT")[f"homeassistant/number/bff_ams_{SERIAL.lower()}/dry_temp/config"]["max"], 85)
        plain = self.topics("AMS")
        dry = ("/drying/config", "/dry_target/config", "/dry_temp/config", "/dry_hours/config",
               "/dry_start/config", "/dry_stop/config")
        self.assertFalse([t for t in plain if t.endswith(dry)])

    def test_no_start_controls_when_the_printer_cannot_start_drying(self):
        """AMS 2 Pro on a P1S: what is drying stays visible, nothing that starts it is published."""
        stopped = self.topics("AMS 2 Pro", ams_drying=onboard.ams_drying("C12"))
        for component, obj in (("number", "dry_temp"), ("number", "dry_hours"),
                               ("button", "dry_start"), ("button", "dry_stop")):
            self.assertNotIn(f"homeassistant/{component}/bff_ams_{SERIAL.lower()}/{obj}/config", stopped)
        for component, obj in (("binary_sensor", "drying"), ("sensor", "dry_target")):
            self.assertIn(f"homeassistant/{component}/bff_ams_{SERIAL.lower()}/{obj}/config", stopped)
        self.assertIn(f"homeassistant/button/bff_ams_{SERIAL.lower()}/dry_start/config",
                      self.topics("AMS 2 Pro", ams_drying=onboard.ams_drying("N7")))

    def test_remembered_temp_and_hours_go_out_on_state(self):
        b = ha_mqtt.HaBridge("h", 1883, "u", "p", ["a1"], lambda a: {}, mock.Mock())
        b.dry_settings(SERIAL.lower())["temp"] = 60
        state = b._serial_state({"serial": SERIAL, "model": "AMS 2 Pro", "dry_time": 30, "drying": True,
                                 "dry_target": 60, "unit": "A"}, "a1", "Alpha")
        self.assertEqual((state["dry_temp"], state["dry_hours"], state["drying"]), (60, 8, True))


if __name__ == "__main__":
    unittest.main()
