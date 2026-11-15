"""H2D (O1D): read-only profile + dual-nozzle snapshot. The raw message is cut down from a real pushall (no identifiers)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "bff_mcp"))

import onboard  # noqa: E402
import printer_link  # noqa: E402

RAW = {
    "gcode_state": "FINISH", "bed_temper": 21.0, "bed_target_temper": 0.0, "nozzle_temper": 22.0, "nozzle_target_temper": 0.0,
    "nozzle_diameter": "0.4", "nozzle_type": "HS01", "lights_report": [{"mode": "off", "node": "chamber_light"}],
    "device": {
        "ctc": {"info": {"temp": 23}, "state": 0},
        "extruder": {"info": [{"id": 0, "temp": 21}, {"id": 1, "temp": 22}], "state": 33042},
        "nozzle": {"info": [{"id": 0, "diameter": 0.4, "type": "HS01"}, {"id": 1, "diameter": 0.4, "type": "HS01"}]},
    },
}


class H2D(unittest.TestCase):
    def test_profile_is_read_only_plus_stop(self):
        profile = onboard.model_profile("O1D")
        self.assertEqual(profile["model"], "H2D")
        self.assertEqual(profile["limits"]["nozzle_max_c"]["value"], 350)
        self.assertEqual(profile["limits"]["bed_max_c"]["value"], 120)
        self.assertTrue(profile["supports"]["stop"])
        self.assertFalse(profile["supports"]["start"])  # Print now stays off until the dual-extruder mapping is validated

    def test_both_nozzles_and_chamber(self):
        snap = printer_link.to_snapshot(RAW)
        self.assertEqual([n["temp"] for n in snap["nozzles"]], [21, 22])
        self.assertEqual(snap["nozzles"][1]["nozzle_type"], "hardened_steel")
        self.assertEqual(snap["chamber_temp"], 23)

    def test_single_nozzle_printer_unchanged(self):
        snap = printer_link.to_snapshot({"gcode_state": "IDLE", "nozzle_type": "HS01", "nozzle_diameter": "0.4", "chamber_temper": 5})
        self.assertEqual(len(snap["nozzles"]), 1)
        self.assertEqual(snap["chamber_temp"], 5)


if __name__ == "__main__":
    unittest.main()
