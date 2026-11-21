"""The secure https address (PYGMY_PUBLIC_URL, written by tailscale.sh) reaches the app's Settings."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pygmy import server  # noqa: E402

URL = "https://pygmy.tail1234.ts.net"


class PublicUrl(unittest.TestCase):
    def setUp(self):
        clean = {k: v for k, v in os.environ.items() if k not in ("BFF_PUBLIC_URL", "PYGMY_PUBLIC_URL")}
        patch = mock.patch.dict(os.environ, clean, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_read_from_pygmy_env(self):
        with mock.patch.dict(os.environ, {"PYGMY_PUBLIC_URL": URL + "/"}):
            self.assertEqual(server.env_public_url(), URL)

    def test_empty_when_missing(self):
        self.assertEqual(server.env_public_url(), "")

    def test_list_printers_exposes_it(self):
        with mock.patch.dict(server.CFG, {"printers": {}, "public_url": URL}):
            result = json.loads(server.tool_list_printers({})[0]["text"])
        self.assertEqual(result["public_url"], URL)


if __name__ == "__main__":
    unittest.main()
