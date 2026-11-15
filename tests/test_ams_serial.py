"""AMS units keyed by their serial, not by printer + letter."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "bff_mcp"))
from bff_mcp import ha_mqtt, printer_link  # noqa: E402

STATUS = {"connected": True, "state": "RUNNING", "progress": 42, "remaining_time": 17, "layer_num": 3,
          "total_layers": 90, "subtask_name": "x", "speed_level": 2, "hms_count": 0, "chamber_light": True,
          "temperatures": {"nozzle": 220, "nozzle_target": 220, "bed": 60, "bed_target": 60}}
SERIAL = "00600A000008996"


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, retain=False):
        self.sent.append((topic, payload, retain))
        return mock.Mock(rc=0)


class SerialFromGetVersion(unittest.TestCase):
    def test_get_version_modules_set_serial_and_model(self):
        link = printer_link.PrinterLink("a1", "host", "serial", "code", "/nonexistent")
        link.feed({"info": {"command": "get_version", "module": [
            {"name": "ams/0", "sn": "00600A000008996"},
            {"name": "n3f/1", "sn": "22E00X000000001"},
        ]}})
        link.feed({"print": {"ams": {"ams": [{"id": 0}, {"id": 1}]}}})
        units = {u["id"]: u for u in link.snapshot()["ams"]}
        self.assertEqual(units[0]["serial"], "00600A000008996")
        self.assertEqual(units[0]["model"], "AMS")
        self.assertEqual(units[1]["serial"], "22E00X000000001")
        self.assertEqual(units[1]["model"], "AMS 2 Pro")

    def test_fallback_ams_id_and_all_zeros_are_none(self):
        link = printer_link.PrinterLink("a1", "host", "serial", "code", "/nonexistent")
        link.feed({"print": {"ams": {"ams": [
            {"id": 0, "ams_id": "ABC123"}, {"id": 1, "ams_id": "000000"}, {"id": 2}]}}})
        units = {u["id"]: u for u in link.snapshot()["ams"]}
        self.assertEqual(units[0]["serial"], "ABC123")
        self.assertEqual(units[0]["model"], "AMS")
        self.assertIsNone(units[1]["serial"])
        self.assertIsNone(units[2]["serial"])


class SerialDiscovery(unittest.TestCase):
    def test_serial_unit_discovery_shape(self):
        pairs = ha_mqtt.serial_unit_discovery(SERIAL, "AMS", "alpha", "A", "Alpha")
        cfg = dict(pairs)
        s = SERIAL.lower()
        for topic in ("homeassistant/sensor/bff_ams_%s/printer/config" % s,
                      "homeassistant/sensor/bff_ams_%s/position/config" % s,
                      "homeassistant/sensor/bff_ams_%s/humidity/config" % s,
                      "homeassistant/sensor/bff_ams_%s/slot_1/config" % s,
                      "homeassistant/sensor/bff_ams_%s/slot_4/config" % s,
                      "homeassistant/sensor/bff_ams_%s/slot_3_remain/config" % s,
                      "homeassistant/button/bff_ams_%s/read_1/config" % s,
                      "homeassistant/sensor/bff_ams_%s/raw_slot_2_state/config" % s):
            self.assertIn(topic, cfg)
        device = cfg["homeassistant/sensor/bff_ams_%s/printer/config" % s]["device"]
        self.assertEqual(device["identifiers"], [f"bff_ams_{s}"])
        self.assertEqual(device["name"], "AMS 8996")
        self.assertEqual(device["model"], "AMS")
        self.assertEqual(device["manufacturer"], "Bambu Lab")
        self.assertEqual(device["via_device"], "bff_alpha")
        self.assertEqual(cfg["homeassistant/sensor/bff_ams_%s/slot_1/config" % s]["name"], "Slot 1")
        self.assertEqual(cfg["homeassistant/button/bff_ams_%s/read_1/config" % s]["name"], "Re-read filament 1")

    def test_printer_device_manufacturer_is_bff(self):
        cfg = dict((t, c) for t, c in ha_mqtt.discovery("a1"))
        device = cfg["homeassistant/sensor/bff_a1/nozzle/config"]["device"]
        self.assertEqual(device["manufacturer"], "Bambu Lab")


class HaBridgeSerial(unittest.TestCase):
    def bridge(self, reads, aliases=("alpha", "bravo"), names=None):
        b = ha_mqtt.HaBridge("h", 1883, "u", "p", list(aliases), reads, mock.Mock(), names=names or {})
        b.client = FakeClient()
        return b

    def unit(self, letter, serial=None, material="PLA", model="AMS"):
        return {"unit": letter, "serial": serial, "model": model, "humidity": 3, "humidity_raw": 30,
                "temp": 25, "dry_time": 12, "info": "x",
                "slots": [{"slot": f"{letter}1", "material": material, "empty": False}]}

    def test_first_serial_publish_clears_old_scheme(self):
        status = dict(STATUS)
        status["ams_units"] = [self.unit("A", SERIAL)]
        b = self.bridge(lambda a: status, aliases=("a1",), names={"a1": "Alpha"})
        b.publish_states()
        cleared = [t for t, p, r in b.client.sent if r and p == ""]
        self.assertIn("homeassistant/sensor/bff_a1/ams_a_humidity/config", cleared)
        self.assertIn("homeassistant/sensor/bff_a1/ams_a1/config", cleared)
        self.assertIn("homeassistant/button/bff_a1/read_a1/config", cleared)

    def test_serial_moves_between_printers(self):
        alpha, bravo = dict(STATUS), dict(STATUS)
        alpha["ams_units"] = [self.unit("A", SERIAL)]
        bravo["ams_units"] = []
        reads = {"alpha": alpha, "bravo": bravo}
        b = self.bridge(lambda a: reads[a], names={"alpha": "Alpha", "bravo": "Bravo"})
        b.publish_states()
        self.assertEqual(b.ams_location[SERIAL.lower()]["alias"], "alpha")
        alpha["ams_units"] = []
        bravo["ams_units"] = [self.unit("C", SERIAL, "PETG")]
        b.publish_states()
        s = SERIAL.lower()
        self.assertEqual(b.ams_location[s]["alias"], "bravo")
        self.assertEqual(b.ams_location[s]["letter"], "C")
        self.assertEqual(b.ams_disc[s], ("bravo", "C", "AMS"))
        config = dict((t, json.loads(c)) for t, c, r in b.client.sent
                      if r and t == f"homeassistant/sensor/bff_ams_{s}/printer/config")
        self.assertEqual(config[f"homeassistant/sensor/bff_ams_{s}/printer/config"]["device"]["via_device"],
                         "bff_bravo")
        self.assertEqual(config[f"homeassistant/sensor/bff_ams_{s}/printer/config"]["unique_id"],
                         f"bff_ams_{s}_printer")
        state = json.loads(b.ams_last_state[s])
        self.assertEqual(state["printer"], "Bravo")
        self.assertEqual(state["position"], "C")

    def test_unplug_publishes_printer_none_without_clearing_discovery(self):
        status = dict(STATUS)
        status["ams_units"] = [self.unit("A", SERIAL)]
        b = self.bridge(lambda a: status, aliases=("a1",), names={"a1": "Alpha"})
        b.publish_states()
        status["ams_units"] = []
        b.publish_states()
        s = SERIAL.lower()
        self.assertEqual(json.loads(b.ams_last_state[s])["printer"], "none")
        self.assertNotIn((f"homeassistant/sensor/bff_ams_{s}/printer/config", "", True), b.client.sent)
        self.assertIn(s, b.ams_disc)  # discovery kept, never deleted

    def test_no_serial_unit_disappears_clears_old_scheme(self):
        status = dict(STATUS)
        status["ams_units"] = [self.unit("A")]
        b = self.bridge(lambda a: status, aliases=("a1",))
        b.publish_states()
        b.client.sent.clear()
        status["ams_units"] = []
        b.publish_states()
        cleared = [t for t, p, r in b.client.sent if r and p == ""]
        self.assertIn("homeassistant/sensor/bff_a1/ams_a_humidity/config", cleared)
        self.assertIn("homeassistant/sensor/bff_a1/ams_a1/config", cleared)

    def test_no_serial_appears_in_logs(self):
        status = dict(STATUS)
        status["ams_units"] = [self.unit("A", SERIAL)]
        b = self.bridge(lambda a: status, aliases=("a1",), names={"a1": "Alpha"})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            b.publish_states()
        self.assertNotIn(SERIAL, buf.getvalue())
        self.assertNotIn(SERIAL.lower(), buf.getvalue())

    def test_active_slot_carries_ams_serial_attribute(self):
        status = dict(STATUS)
        status["tray_now"] = 0  # A1
        status["ams_units"] = [self.unit("A", SERIAL)]
        flat = ha_mqtt.flat_state(status)
        self.assertEqual(flat["active_slot"], "A1")
        self.assertEqual(flat["active_slot_serial"], SERIAL)
        status["tray_now"] = 255
        flat = ha_mqtt.flat_state(status)
        self.assertIsNone(flat["active_slot_serial"])

    def test_ams_command_resolves_where_the_serial_is_now(self):
        loc = {SERIAL.lower(): {"alias": "bravo", "letter": "C", "model": "AMS"}}
        self.assertEqual(ha_mqtt.parse_ams_command(f"bff/ams/{SERIAL.lower()}/cmd/read_1", b"PRESS", loc),
                         ("bravo", "read_c1", "C1"))
        self.assertIsNone(ha_mqtt.parse_ams_command(f"bff/ams/{SERIAL.lower()}/cmd/read_1", b"PRESS", {}))
        self.assertIsNone(ha_mqtt.parse_ams_command(f"bff/ams/{SERIAL.lower()}/cmd/read_5", b"PRESS", loc))
        status = dict(STATUS)
        status["ams_units"] = [self.unit("A", SERIAL)]
        b = self.bridge(lambda a: status, aliases=("a1",))
        b.publish_states()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            b._on_message(None, None, mock.Mock(topic="bff/ams/unknownserial000/cmd/read_2", payload=b"PRESS"))
        self.assertIn("nessuna stampante", buf.getvalue())
        self.assertNotIn("unknownserial000", buf.getvalue())


if __name__ == "__main__":
    unittest.main()


class FakeBmcuSerial(unittest.TestCase):
    def test_bmcu_placeholder_serial_is_no_identity(self):
        from printer_link import real_serial
        self.assertIsNone(real_serial("STUDY0ONLY"))
        self.assertIsNone(real_serial("study0only"))
        self.assertIsNone(real_serial(""))
        self.assertEqual(real_serial("00600A000008996"), "00600A000008996")
