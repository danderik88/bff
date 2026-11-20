import unittest
from pathlib import Path

from pygmy import server


class BedZTest(unittest.TestCase):
    def test_p1_bed_moves_in_z_a1_does_not(self):
        self.assertTrue(server.ENVELOPES["P1"].get("bed_z"))
        self.assertFalse(server.ENVELOPES["A1"].get("bed_z"))

    def test_app_names_the_bed_buttons(self):
        html = (Path(server.__file__).parent / "app.html").read_text()
        self.assertIn('"Bed ↓"', html)
        self.assertIn("Raise the bed to Z", html)
