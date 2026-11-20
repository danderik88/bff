"""slicer.model_facts: plate count and model colours, read from the file itself."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from pygmy import slicer  # noqa: E402
from test_slicer_worker import model_3mf  # noqa: E402


class ModelFactsTest(unittest.TestCase):
    def test_facts_stl(self):
        self.assertEqual(slicer.model_facts("cube.stl", b"solid x"), {"plates": 1, "colors": []})
        self.assertEqual(slicer.model_facts("part.STEP", b"ISO-10303"), {"plates": 1, "colors": []})

    def test_facts_3mf_two_colours(self):
        data = model_3mf(colors=["#FF0000", "#0000ff"])
        self.assertEqual(slicer.model_facts("two.3mf", data), {"plates": 1, "colors": ["#FF0000", "#0000FF"]})

    def test_facts_3mf_three_plates(self):
        facts = slicer.model_facts("three.3mf", model_3mf(plates=3))
        self.assertEqual((facts["plates"], facts["colors"]), (3, []))
        self.assertEqual(len(facts.get("plate_info") or [None] * 3), 3)  # the picker's list, when the file names them

    def test_facts_broken_3mf(self):
        self.assertEqual(slicer.model_facts("bad.3mf", b"not a zip"), {"plates": 1, "colors": []})


if __name__ == "__main__":
    unittest.main()
