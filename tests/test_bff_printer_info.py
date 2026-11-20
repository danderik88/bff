"""Run with: python3 -m unittest discover -s tests"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from pygmy import server  # noqa: E402

SERIAL = "03919A000000000"
TRAY = {"id": 254, "tray_color": "EEAECDFF", "tray_type": "PLA", "tray_info_idx": "GFA01", "tag_uid": SERIAL}
STATUS = {
    "connected": True,
    "state": "FINISH",
    "subtask_name": "pezzo",
    "hms_errors": [],
    "chamber_light": True,
    "ams": [],
    "tray_now": 254,
    "nozzles": [{"nozzle_type": "hardened_steel", "nozzle_diameter": "0.4"}, {"nozzle_type": "", "nozzle_diameter": ""}],
    "vt_tray": [TRAY],
}


class Printer:
    """Fake printer link: the raw report, and a light that follows the command."""

    def __init__(self, status=None, light_works=True):
        self.status = {**STATUS, **(status or {})}
        self.light_works = light_works
        self.commands = []

    def snapshot(self):
        return dict(self.status)

    def command(self, action, **params):
        self.commands.append((action, params))
        if action == "light" and self.light_works:
            self.status["chamber_light"] = params["on"]
        return True

    def info(self):
        with mock.patch.object(server, "link_of", lambda alias: self), \
                mock.patch.object(server, "load_profile", lambda alias: {"model": "A1", "slicer_model": "Bambu Lab A1"}), \
                mock.patch.object(server, "DB", None), mock.patch.object(server.time, "sleep", lambda s: None), \
                mock.patch.dict(server.CFG, {"printers": {"a1": 1, "mini": 2}, "control": True}):
            return json.loads(server.tool_printer_info({"printer": "a1"})[0]["text"])


class PrinterInfoTest(unittest.TestCase):
    def test_idle_printer_with_external_spool(self):
        reply = Printer().info()
        self.assertEqual(reply["available_printers"], ["a1", "mini"])
        self.assertEqual((reply["model"], reply["slicer_model"]), ("A1", "Bambu Lab A1"))
        self.assertEqual(reply["nozzle"], {"diameter": "0.4", "type": "hardened_steel"})
        self.assertEqual(reply["default_slot"], "external")
        self.assertTrue(reply["free"])
        self.assertEqual(reply["filaments"], [{
            "slot": "external", "material": "PLA", "color": "EEAECDFF", "profile_id": "GFA01",
            "grams_remaining": None, "grams_source": "unknown",
        }])

    def test_no_identifying_data(self):
        self.assertNotIn(SERIAL, json.dumps(Printer().info()))

    def test_no_filament_reports_nothing_stale(self):
        reply = Printer({"tray_now": 255}).info()
        self.assertEqual(reply["filaments"], [])
        self.assertIsNone(reply["default_slot"])

    def test_busy_printer_is_not_free_and_lists_hms(self):
        reply = Printer({"state": "RUNNING", "hms_errors": [{"module": 3, "code": "0x1", "description": "Z"}]}).info()
        self.assertFalse(reply["free"])
        self.assertIn("RUNNING", reply["not_free_reason"])
        self.assertEqual(reply["attention"], [{"handling": "sicurezza", "description": "Z"}])

    def test_ams_is_flagged_because_it_is_not_handled(self):
        self.assertTrue(Printer({"ams": [{"id": 0}]}).info()["ams_present"])


class PlateTest(unittest.TestCase):
    def photo(self, printer):
        def capture(pid):
            printer.light_during_photo = printer.status["chamber_light"]
            return b"jpeg"
        with mock.patch.object(server, "link_of", lambda alias: printer), \
                mock.patch.object(server, "capture_jpeg", capture), mock.patch.object(server.time, "sleep", lambda s: None), \
                mock.patch.dict(server.CFG, {"printers": {"a1": 1}, "control": True, "public_url": "http://bff.test"}):
            image, text = server.tool_plate_photo({"printer": "a1"})
        return image, json.loads(text["text"])

    def test_idle_printer_is_unknown_and_takes_no_photo(self):
        printer = Printer()
        self.assertEqual(printer.info()["plate"]["state"], "unknown")
        self.assertEqual(printer.commands, [])

    def test_during_a_print_is_occupied(self):
        self.assertEqual(Printer({"state": "RUNNING"}).info()["plate"]["state"], "occupied")

    def test_disconnected_is_unknown(self):
        self.assertEqual(Printer({"connected": False, "state": "RUNNING"}).info()["plate"]["state"], "unknown")

    def test_plate_photo_gives_image_link_and_rules(self):
        image, data = self.photo(Printer())
        self.assertEqual(image["type"], "image")
        self.assertTrue(data["photo_url"].startswith("http://bff.test/photo/"))
        for word in ("bed slinger", "colori", "unknown"):
            self.assertIn(word, data["regole"])

    def test_light_is_switched_on_for_the_photo_and_restored(self):
        printer = Printer({"chamber_light": False})
        self.photo(printer)
        self.assertTrue(printer.light_during_photo)
        self.assertFalse(printer.status["chamber_light"])

    def test_dark_photo_is_flagged(self):
        _, data = self.photo(Printer({"chamber_light": False}, light_works=False))
        self.assertIn("luce", data["avviso"])


class TestedModelTest(unittest.TestCase):
    """Expert mode (Motion) is only unlocked on the models the user tested by hand."""

    def call(self, code, fn, **args):
        with mock.patch.object(server, "model_code", lambda alias: code), \
                mock.patch.object(server, "read_status", lambda alias: dict(STATUS)), \
                mock.patch.object(server, "load_profile", lambda alias: {"model": "A1", "slicer_model": "Bambu Lab A1"}), \
                mock.patch.object(server, "DB", None), mock.patch.object(server, "LINKS", {}), \
                mock.patch.dict(server.CFG, {"printers": {"a1": 1}, "control": True}):
            return fn({"printer": "a1", **args})

    def info(self, code):
        return json.loads(self.call(code, server.tool_printer_info)[0]["text"])

    def test_untested_model_reports_false(self):
        self.assertFalse(self.info("")["tested"])
        self.assertFalse(self.info("O1D")["tested"])

    def test_tested_model_reports_true(self):
        self.assertTrue(self.info("N2S")["tested"])

    def test_expert_unlock_via_env(self):
        with mock.patch.dict(os.environ, {"BFF_EXPERT_MODELS": " O1D, N9X "}):
            self.assertTrue(self.info("O1D")["tested"])
            self.assertTrue(self.info("N9X")["tested"])
            self.assertFalse(self.info("SOMETHING_ELSE")["tested"])

    def test_expert_refused_on_untested_model(self):
        with self.assertRaises(server.ToolError) as e:
            self.call("O1D", server.tool_set_expert, enabled=True)
        self.assertEqual(str(e.exception), "Modalita expert non disponibile: modello non ancora testato")

    def test_expert_allowed_and_off_always_allowed(self):
        self.assertTrue(json.loads(self.call("N2S", server.tool_set_expert, enabled=True)[0]["text"])["expert"])
        self.assertFalse(json.loads(self.call("O1D", server.tool_set_expert, enabled=False)[0]["text"])["expert"])


if __name__ == "__main__":
    unittest.main()
