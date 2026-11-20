"""Phone app: no plate photo for app axes calls, axes in status, HA error text."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "bff_mcp"))
from bff_mcp import ha_mqtt, server  # noqa: E402
from printer_link import PrinterLink, to_snapshot  # noqa: E402


class AppAxesTest(unittest.TestCase):
    def tearDown(self):
        server.APP_CALL.on = False

    def test_app_call_skips_plate_photo(self):
        with mock.patch.object(server, "plate_shot") as shot:
            server.APP_CALL.on = True
            self.assertEqual(server.shot_parts(1), ([], {}))
            shot.assert_not_called()

    def test_agent_call_keeps_plate_photo(self):
        with mock.patch.object(server, "plate_shot", return_value=(b"jpg", {"photo_url": "u"})), \
                mock.patch.object(server, "image_content", return_value={"type": "image"}):
            server.APP_CALL.on = False
            self.assertEqual(server.shot_parts(1), ([{"type": "image"}], {"photo_url": "u"}))

    def test_app_html_sends_app_header(self):
        self.assertIn('"X-BFF-App": "1"', (ROOT / "src" / "bff_mcp" / "app.html").read_text())

    def test_motion_disclaimer_present(self):
        self.assertIn("the printer doesn't report its position", (ROOT / "src" / "bff_mcp" / "app.html").read_text())

    def test_expert_mode_key_and_default_off(self):
        html = (ROOT / "src" / "bff_mcp" / "app.html").read_text()
        self.assertIn("bff.expert", html)
        self.assertIn('localStorage.getItem("bff.expert") === "1"', html)

    def test_expert_mode_popup_text(self):
        html = (ROOT / "src" / "bff_mcp" / "app.html").read_text()
        self.assertIn("Turn on Expert mode?", html)
        self.assertIn("You use this at your own risk.", html)

    def test_expert_switch_locked_on_untested_model(self):
        html = (ROOT / "src" / "bff_mcp" / "app.html").read_text()
        self.assertIn("Not tested on this model yet", html)
        self.assertIn('rpc("set_expert"', html)

    def test_error_text_joins_descriptions(self):
        flat = ha_mqtt.flat_state({"attention": [{"description": "Pull the filament"}, {"description": "Bed"}]})
        self.assertEqual(flat["errors"], "Pull the filament; Bed")
        self.assertEqual(ha_mqtt.flat_state({})["errors"], "")
        long = ha_mqtt.flat_state({"attention": [{"description": "x" * 400}]})["errors"]
        self.assertEqual(len(long), 250)

    def test_end_reason(self):
        self.assertEqual(ha_mqtt.flat_state({"state": "FAILED", "print_error": 0x0300400C})["end_reason"], "stopped")
        self.assertEqual(ha_mqtt.flat_state({"state": "FAILED", "print_error": 0x07008011})["end_reason"], "failed")
        self.assertEqual(ha_mqtt.flat_state({"state": "RUNNING", "print_error": 0x0300400C})["end_reason"], "none")

    def test_fans_and_options_from_report(self):
        link = PrinterLink("a1", "host", "s", "code", "/nonexistent")
        link.feed({"print": {
            "cooling_fan_speed": "15", "big_fan1_speed": "0",
            "xcam": {"spaghetti_detector": True, "halt_print_sensitivity": "medium", "cfg": 7},
            "home_flag": 0x10 | 0x80000 | 0x100000,
        }})
        s = link.snapshot()
        self.assertEqual(s["fans"]["part"], 100)
        self.assertEqual(s["fans"]["aux"], 0)
        self.assertIs(s["xcam"]["spaghetti_detector"], True)
        self.assertNotIn("cfg", s["xcam"])
        self.assertEqual(s["options"], {"auto_recovery": True, "sound_enable": False, "filament_tangle_detect": True})
        self.assertEqual(PrinterLink("a1", "host", "s", "code", "/nonexistent").snapshot()["options"], {})

    def test_p2s_options_come_from_cfg_gated_by_fun(self):
        # DeviceManager.cpp: cfg is the state, fun says which switches the machine has.
        hf = sum(1 << b for b in [3, 4, 7, 8, 10, 12, 14, 16, 18, 21, 22, 30, 31])
        snap = to_snapshot({"home_flag": hf, "cfg": "4D02017DA19", "fun": "64039FD193FF9CB7"})
        self.assertEqual(snap["options"], {"auto_recovery": True})

    def test_older_printer_tangle_needs_home_flag_bit_19(self):
        snap = to_snapshot({"home_flag": (1 << 4) | (1 << 19) | (1 << 20)})
        self.assertIs(snap["options"]["filament_tangle_detect"], True)

    def test_older_printer_without_bit_19_has_no_tangle_switch(self):
        snap = to_snapshot({"home_flag": 1 << 4})
        self.assertNotIn("filament_tangle_detect", snap["options"])


if __name__ == "__main__":
    unittest.main()
