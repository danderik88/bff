"""Flow calibration at print start: the project_file flags, the setting and printer_info."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from bff_mcp import printer_link, server  # noqa: E402


class ProjectFileTest(unittest.TestCase):
    def flags(self, mode=None):
        print_ = printer_link.project_file("pezzo.3mf")["print"] if mode is None \
            else printer_link.project_file("pezzo.3mf", flow_cali=mode)["print"]
        return print_["flow_cali"], print_["extrude_cali_flag"]

    def test_on_always_calibrates(self):
        self.assertEqual(self.flags("on"), (True, 1))

    def test_auto_leaves_it_to_the_printer(self):
        self.assertEqual(self.flags("auto"), (False, 2))

    def test_off_never_calibrates(self):
        self.assertEqual(self.flags("off"), (False, 0))

    def test_a_bad_mode_falls_back_to_on(self):
        self.assertEqual(self.flags("garbage"), (True, 1))

    def test_the_default_is_on(self):
        self.assertEqual(self.flags(), (True, 1))

    def test_the_other_fields_are_unchanged(self):
        print_ = printer_link.project_file("pezzo.3mf", flow_cali="off")["print"]
        self.assertEqual((print_["param"], print_["bed_leveling"], print_["extrude_cali_manual_mode"]),
                         ("Metadata/plate_1.gcode", False, 0))


class SettingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True)
        server.open_db(str(Path(self.tmp.name) / "state.db"))

    def tearDown(self):
        server.DB.close()
        server.CFG.pop("control", None)
        self.tmp.cleanup()

    def test_the_default_is_on(self):
        self.assertEqual(server.flow_cali("a1"), "on")

    def test_set_print_option_saves_the_mode(self):
        reply = server.tool_set_print_option({"printer": "a1", "option": "bff_flow_cali", "mode": "off"})
        self.assertEqual(server.flow_cali("a1"), "off")
        self.assertEqual(json.loads(reply[0]["text"])["mode"], "off")

    def test_a_bad_mode_is_rejected(self):
        for mode in ("always", None, True):
            with self.assertRaises(server.ToolError):
                server.tool_set_print_option({"printer": "a1", "option": "bff_flow_cali", "mode": mode})


class SupportsFlowCaliTest(unittest.TestCase):
    def test_fun_bit_7_says_yes(self):
        self.assertTrue(printer_link.supports_flow_cali({"fun": "80"}))
        self.assertTrue(printer_link.supports_flow_cali({"fun": "0x80"}))

    def test_fun_without_bit_7_says_no(self):
        self.assertFalse(printer_link.supports_flow_cali({"fun": "7F"}))
        self.assertFalse(printer_link.supports_flow_cali({"fun": "0"}))
        self.assertFalse(printer_link.supports_flow_cali({"fun": "0", "home_flag": 0x10000}))

    def test_home_flag_bit_16_is_the_fallback_without_fun(self):
        self.assertTrue(printer_link.supports_flow_cali({"home_flag": 0x10000}))
        self.assertTrue(printer_link.supports_flow_cali({"home_flag": "65536"}))

    def test_nothing_reported_says_no(self):
        for raw in ({}, {"home_flag": 0xFFFF}, {"home_flag": None}):
            self.assertFalse(printer_link.supports_flow_cali(raw))


class PrinterInfoTest(unittest.TestCase):
    class Fake:
        drops = 0

        def __init__(self, **extra):
            self.extra = extra

        def snapshot(self):
            return {"connected": True, "state": "IDLE", "ams": [], "vt_tray": [], "nozzles": [{}],
                    "hms_errors": [], **self.extra}

    def info(self, fake=None):
        fake = fake or self.Fake(fun="80")
        with mock.patch.object(server, "link_of", lambda alias: fake), \
                mock.patch.object(server, "load_profile", lambda alias: {"model": "A1"}), \
                mock.patch.object(server, "DB", None), \
                mock.patch.dict(server.CFG, {"printers": {"a1": 1}, "control": True}):
            return json.loads(server.tool_printer_info({"printer": "a1"})[0]["text"])

    def test_bambu_printer_reports_the_flow_calibration_option(self):
        self.assertEqual(self.info()["options"]["bff_flow_cali"], "on")

    def test_no_option_when_the_printer_cannot_calibrate(self):
        for fake in (self.Fake(), self.Fake(fun="0"), self.Fake(home_flag=0x8000)):
            options = self.info(fake).get("options") or {}
            self.assertNotIn("bff_flow_cali", options)

    def test_home_flag_carries_the_option_without_fun(self):
        self.assertEqual(self.info(self.Fake(home_flag=0x10000))["options"]["bff_flow_cali"], "on")

    def test_status_options_keep_the_other_switches(self):
        fake = self.Fake(fun="80", options={"from_the_printer": True})
        with mock.patch.object(server, "link_of", lambda alias: fake), \
                mock.patch.object(server, "DB", None), \
                mock.patch.dict(server.CFG, {"printers": {"a1": 1}, "control": True}):
            options = server.status_dict({"printer": "a1"})["options"]
        self.assertEqual((options["from_the_printer"], options["bff_spaghetti"], options["bff_flow_cali"]),
                         (True, True, "on"))


if __name__ == "__main__":
    unittest.main()


class P1HasNoFlowStep(unittest.TestCase):
    def test_p1_hides_the_option(self):
        with mock.patch.object(server, "load_profile", lambda alias: {"model": "P1S"}), \
                mock.patch.object(server, "is_bambu", lambda alias: True):
            self.assertFalse(server.flow_cali_shown("p1s", {"fun": "80"}))
        with mock.patch.object(server, "load_profile", lambda alias: {"model": "A1"}), \
                mock.patch.object(server, "is_bambu", lambda alias: True):
            self.assertTrue(server.flow_cali_shown("a1", {"fun": "80"}))
