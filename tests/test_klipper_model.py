import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "pygmy"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pygmy import server  # noqa: E402


class GenericKlipperModel(unittest.TestCase):
    def test_orca_generic_klipper_file_fits_other_klipper_printer(self):
        profile = {"slicer_model": "Other Klipper printer", "kind": "moonraker"}
        self.assertIsNone(server.check_compatible(profile, {"model": "Generic Klipper Printer"}, {}))
        self.assertIsNotNone(server.check_compatible(profile, {"model": "Bambu Lab A1"}, {}))


if __name__ == "__main__":
    unittest.main()
