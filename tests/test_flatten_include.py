"""flatten_profiles: OrcaSlicer 2.5 "include" templates carry each printer's own start G-code."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools" / "slicer"))
import flatten_profiles  # noqa: E402


def write(root, sub, data):
    path = root / "BBL" / sub
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class Include(unittest.TestCase):
    def test_each_machine_gets_its_own_start_gcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            presets = {
                "common": ("machine/common.json", {"type": "machine", "instantiation": "false",
                                                   "machine_start_gcode": "GENERIC", "bed": "x"}),
                "A start": ("machine/a_start.json", {"type": "machine", "name": "A start", "instantiation": "false",
                                                     "machine_start_gcode": "START A"}),
                "B start": ("machine/b_start.json", {"type": "machine", "name": "B start", "instantiation": "false",
                                                     "machine_start_gcode": "START B"}),
                "Bambu Lab A 0.4 nozzle": ("machine/a.json", {"type": "machine", "name": "Bambu Lab A 0.4 nozzle",
                                                               "inherits": "common", "include": ["A start"]}),
                "Bambu Lab B 0.4 nozzle": ("machine/b.json", {"type": "machine", "name": "Bambu Lab B 0.4 nozzle",
                                                               "inherits": "common", "include": ["B start"],
                                                               "machine_start_gcode": "OWN B"}),
            }
            for name, (sub, data) in presets.items():
                write(root, sub, data)
            (root / "BBL.json").write_text(json.dumps({"machine_list": [
                {"name": n, "sub_path": s} for n, (s, _d) in presets.items()]}))
            flatten_profiles._index.cache_clear()
            a = flatten_profiles.flatten(root, "Bambu Lab A 0.4 nozzle")
            b = flatten_profiles.flatten(root, "Bambu Lab B 0.4 nozzle")
            self.assertEqual(a["machine_start_gcode"], "START A")   # include beats the parent
            self.assertEqual(b["machine_start_gcode"], "OWN B")     # the preset's own key beats the include
            self.assertEqual(a["bed"], "x")
            self.assertEqual(a["name"], "Bambu Lab A 0.4 nozzle")   # include does not rename
            self.assertNotIn("include", a)


if __name__ == "__main__":
    unittest.main()
