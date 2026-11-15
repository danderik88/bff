"""Home Assistant: every useful report field as an entity; uncertain ones as hidden diagnostics."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from bff_mcp import ha_mqtt, onboard, printer_link, server  # noqa: E402


def find(pairs, alias, obj):
    return next((c for t, c in pairs.items() if t.endswith(f"/bff_{alias}/{obj}/config")), None)


class Section1Entities(unittest.TestCase):
    def setUp(self):
        self.a1 = dict(ha_mqtt.discovery("a1"))
        self.p1 = dict(ha_mqtt.discovery("p1s", slots=("A1",), units=("A",)))

    def test_a1_applying_entities_exist_with_exact_names(self):
        names = {"stage": "Stage", "total_layers": "Total layers", "speed_percent": "Speed",
                 "heatbreak_fan": "Heatbreak fan", "file_download": "File download",
                 "print_source": "Print source", "pressure_advance": "Pressure advance",
                 "nozzle_diameter": "Nozzle diameter", "nozzle_type": "Nozzle type",
                 "wifi_signal": "WiFi signal", "sd_card": "SD card", "filament_sensor": "Filament sensor",
                 "active_slot": "Active slot", "firmware_update": "Firmware update",
                 "timelapse": "Timelapse", "camera_recording": "Camera recording",
                 "plate_detection": "Build plate detection", "external_spool": "External spool",
                 "firmware_version": "Firmware version"}
        for obj, name in names.items():
            cfg = find(self.a1, "a1", obj)
            self.assertIsNotNone(cfg, obj)
            self.assertEqual(cfg["name"], name, obj)

    def test_p1s_bmcu_ams_gets_slots_remain_and_raw_info(self):
        for obj, name in (("ams_status", "AMS status"), ("ams_a_info", "Raw AMS A info"),
                          ("ams_a1", "Slot A1"), ("ams_a1_remain", "Slot A1 remaining"),
                          ("ams_a1_state", "Raw slot A1 state")):
            cfg = find(self.p1, "p1s", obj)
            self.assertIsNotNone(cfg, obj)
            self.assertEqual(cfg["name"], name, obj)
        for obj in ("ams_a_humidity", "ams_a_humidity_pct", "ams_a_temp", "ams_a_dry_time",
                    "read_a1", "read_a2", "read_a3", "read_a4"):
            self.assertIsNone(find(self.p1, "p1s", obj), obj)

    def test_no_ams_means_no_ams_entities(self):
        self.assertIsNone(find(self.a1, "a1", "ams_status"))
        self.assertIsNone(find(self.a1, "a1", "ams_a_temp"))
        self.assertIsNone(find(self.a1, "a1", "ams_a1_remain"))

    def test_ams_unit_entities_live_on_the_unit_device(self):
        for obj in ("ams_a_info", "ams_a1", "ams_a1_remain", "ams_a1_state"):
            device = find(self.p1, "p1s", obj)["device"]
            self.assertEqual(device["identifiers"], ["bff_p1s_ams_A"])

    def test_external_spool_sensor_carries_attributes(self):
        cfg = find(self.a1, "a1", "external_spool")
        self.assertEqual(cfg["value_template"], "{{ value_json.external_spool.state }}")
        self.assertEqual(cfg["json_attributes_topic"], "bff/a1/state")

    def test_heatbreak_wifi_tray_and_ams_status_mapping(self):
        snap = printer_link.to_snapshot({"heatbreak_fan_speed": "15", "wifi_signal": "-69dBm"})
        self.assertEqual(snap["heatbreak_fan"], 100)
        self.assertEqual(snap["wifi_signal"], -69)
        self.assertEqual(printer_link.to_snapshot({"heatbreak_fan_speed": "0"})["heatbreak_fan"], 0)
        self.assertEqual(ha_mqtt.flat_state({"tray_now": 254})["active_slot"], "external")
        self.assertEqual(ha_mqtt.flat_state({"tray_now": 255})["active_slot"], "none")
        self.assertEqual(ha_mqtt.flat_state({"tray_now": 5})["active_slot"], "B2")
        self.assertEqual(ha_mqtt.flat_state({"ams_status": 0x0100})["ams_status"], "filament change")


class ChamberOnlyWhereHardwareExists(unittest.TestCase):
    def test_absent_for_non_chamber_models(self):
        for profile in (None, {"fans": ["part"]}):
            pairs = dict(ha_mqtt.discovery("a1", profile=profile))
            self.assertIsNone(find(pairs, "a1", "chamber_temp"))
            self.assertIsNone(find(pairs, "a1", "door"))

    def test_present_for_a_chamber_model(self):
        pairs = dict(ha_mqtt.discovery("a1", profile={"chamber_sensor": True}))
        self.assertEqual(find(pairs, "a1", "chamber_temp")["name"], "Chamber")
        door = find(pairs, "a1", "door")
        self.assertEqual(door["name"], "Door")
        self.assertEqual(door["device_class"], "door")


class HiddenDiagnostics(unittest.TestCase):
    def test_all_section4_entities_are_diagnostics_but_enabled(self):
        pairs = dict(ha_mqtt.discovery("a1"))
        for obj, label in ha_mqtt.DIAG_FIELDS:
            cfg = find(pairs, "a1", obj)
            self.assertIsNotNone(cfg, obj)
            self.assertNotIn("enabled_by_default", cfg, obj)
            self.assertEqual(cfg["entity_category"], "diagnostic")
            self.assertEqual(cfg["name"], f"Raw {label}")

    def test_ams_info_and_slot_state_are_diagnostics_but_enabled(self):
        for obj in ("ams_a_info", "ams_a1_state"):
            cfg = find(dict(ha_mqtt.discovery("p1s", slots=("A1",), units=("A",))), "p1s", obj)
            self.assertNotIn("enabled_by_default", cfg, obj)
            self.assertEqual(cfg["entity_category"], "diagnostic")

    def test_no_entity_is_disabled_by_default(self):
        for pairs in (ha_mqtt.discovery("a1"),
                      ha_mqtt.discovery("p1s", slots=("A1",), units=("A",)),
                      ha_mqtt.serial_unit_discovery("00600A000008996", "AMS 2 Pro", "p1s", "A", "P1S")):
            for topic, cfg in pairs:
                self.assertNotIn("enabled_by_default", cfg, topic)


class SlotRemain(unittest.TestCase):
    def test_remain_drops_on_negative_or_missing_tag(self):
        units = [{"id": 0, "humidity": 3, "tray": [
            {"id": 0, "tray_type": "PLA", "remain": 92, "tag_uid": "ABC123DEF456ABC123DEF456"},
            {"id": 1, "tray_type": "PETG", "remain": -1, "tag_uid": "ABC123"},
            {"id": 2, "tray_type": "ABS", "remain": 72, "tag_uid": "000000000000000000000000"}]}]
        out = onboard.ams_units(units)
        slots = {s["slot"]: s for s in out[0]["slots"]}
        self.assertEqual(slots["A1"]["remain"], 92)
        self.assertIsNone(slots["A2"]["remain"])
        self.assertIsNone(slots["A3"]["remain"])


class FirmwareVersion(unittest.TestCase):
    def test_get_version_reply_sets_firmware_version_without_touching_print_state(self):
        link = printer_link.PrinterLink("a1", "host", "serial", "code", "/nonexistent")
        link.feed({"print": {"gcode_state": "IDLE", "total_layer_num": 40}})
        before = link.snapshot()
        link.feed({"info": {"command": "get_version",
                            "module": [{"name": "ota", "sw_ver": "01.05.00.00"}, {"name": "ams", "sw_ver": "x"}]}})
        self.assertEqual(link.firmware_version, "01.05.00.00")
        after = link.snapshot()
        self.assertEqual(after["firmware_version"], "01.05.00.00")
        self.assertEqual((after["state"], after["total_layers"]), (before["state"], before["total_layers"]))
        self.assertEqual(after["print_source"], before["print_source"])


class PlateDetectionSwitch(unittest.TestCase):
    def test_parse_command(self):
        self.assertEqual(ha_mqtt.parse_command("bff/a1/cmd/plate_detection", b"ON", ("a1",)),
                         ("a1", "plate_detection", True))
        self.assertEqual(ha_mqtt.parse_command("bff/a1/cmd/plate_detection", b"OFF", ("a1",)),
                         ("a1", "plate_detection", False))
        self.assertIsNone(ha_mqtt.parse_command("bff/a1/cmd/plate_detection", b"PRESS", ("a1",)))

    def test_ha_action_sends_xcam_control_set_payload(self):
        server.CFG.update(printers={"a1": 1})
        link = mock.Mock()
        link.publish.return_value = True
        with mock.patch.object(server, "link_of", return_value=link):
            server.ha_action("a1", "plate_detection", True, "k1")
        payload = link.publish.call_args[0][0]
        xcam = payload["xcam"]
        self.assertEqual(xcam["command"], "xcam_control_set")
        self.assertEqual(xcam["module_name"], "buildplate_marker_detector")
        self.assertIs(xcam["control"], True)
        self.assertIs(xcam["print_halt"], True)
        self.assertEqual(xcam["sequence_id"], "0")


class StateFlags(unittest.TestCase):
    def test_flat_state_wires_the_new_fields(self):
        flat = ha_mqtt.flat_state({"stage": 13, "prepare_percent": 80, "print_source": "idle",
                                   "speed_percent": 100, "filament_sensor": True, "sd_card": True,
                                   "firmware_update": False, "timelapse": False, "camera_recording": True,
                                   "plate_detection": True, "firmware_version": "01.05.00.00",
                                   "diagnostics": {"home_flag": 123},
                                   "external_spool": {"state": "PLA", "color": "#AC95D5"}})
        self.assertEqual(flat["stage_text"], "Homing")
        self.assertEqual(flat["file_download"], 80)
        self.assertEqual(flat["print_source"], "idle")
        self.assertIs(flat["filament_sensor"], True)
        self.assertIs(flat["plate_detection"], True)
        self.assertEqual(flat["diag"], {"home_flag": 123})
        self.assertEqual(flat["external_spool"]["state"], "PLA")


class EmptyAms(unittest.TestCase):
    def test_ams_with_every_slot_empty_is_still_shown(self):
        raw = {"ams": [{"id": "0", "humidity": "4", "tray": [{"id": str(i)} for i in range(4)]}],
               "ams_exist_bits": "0", "tray_now": 255}
        with mock.patch.object(server, "status_of", return_value=raw), mock.patch.object(server, "DB", None), \
                mock.patch.object(server, "model_code", return_value=None), \
                mock.patch.object(server, "envelope_of", return_value=None):
            status = server.status_dict({"printer": "p1s"})
        unit, = status["ams_units"]
        self.assertEqual([s["slot"] for s in unit["slots"]], ["A1", "A2", "A3", "A4"])
        self.assertTrue(all(s["empty"] for s in unit["slots"]))
        flat = ha_mqtt.flat_state(status)
        self.assertEqual(flat["ams_humidity"], {"A": "B"})
        self.assertTrue(all(flat["ams"][s]["empty"] for s in ("A1", "A2", "A3", "A4")))
        pairs = dict(ha_mqtt.discovery("p1s", slots=tuple(flat["ams"]), units=("A",)))
        self.assertEqual(find(pairs, "p1s", "ams_a1")["name"], "Slot A1")
        self.assertEqual(find(pairs, "p1s", "ams_a1_remain")["name"], "Slot A1 remaining")
        self.assertIsNone(find(pairs, "p1s", "ams_a_humidity"))


if __name__ == "__main__":
    unittest.main()
