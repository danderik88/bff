import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from sanitize_recording import PLACEHOLDER_SERIAL, sanitize_line  # noqa: E402

DATA = ROOT / "tests" / "data"


class Recordings(unittest.TestCase):
    def test_sanitizer_removes_serial_and_blanks_nested_keys(self):
        serial = "0390ABCDEFGHIJK"
        raw = json.dumps({"t": 1.5, "message": {"print": {
            "topic": f"device/{serial}/report",
            "net": {"info": [{"ip": 12345}]},
            "ams": {"ams": [{"sn": "X", "tray": [{"tag_uid": "Y", "tray_type": "PLA"}]}]},
        }}})
        clean = json.loads(sanitize_line(raw, serial))["print"]
        self.assertEqual(clean["topic"], f"device/{PLACEHOLDER_SERIAL}/report")
        self.assertIsNone(clean["net"])
        self.assertIsNone(clean["ams"]["ams"][0]["sn"])
        self.assertIsNone(clean["ams"]["ams"][0]["tray"][0]["tag_uid"])
        self.assertEqual(clean["ams"]["ams"][0]["tray"][0]["tray_type"], "PLA")

    def test_recordings_hold_no_identifiers(self):
        paths = list(DATA.glob("*.jsonl"))
        self.assertTrue(paths, "no recordings found")
        for path in paths:
            text = path.read_text()
            self.assertIsNone(re.search(r"\b(192\.168|10|100)\.\d+\.\d+\.\d+\b", text), path)
            self.assertEqual(set(re.findall(r"\b0[0-9A-Z]{14}\b", text)) - {PLACEHOLDER_SERIAL}, set(), path)


if __name__ == "__main__":
    unittest.main()
