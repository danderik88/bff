"""printer.cfg can change across a Klipper restart: the link reads it again once Klipper is ready."""
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "pygmy"))
import moonraker_link as ml  # noqa: E402


class Reload(unittest.TestCase):
    def setUp(self):
        self.state, self.timeout, self.down = "ready", 600, False
        link = ml.MoonrakerLink("x", "127.0.0.1", port=1)
        link._wled_strips = link._wled_address = link._wled_state = lambda: None

        def request(method, path, **kw):
            if self.down:
                raise urllib.error.URLError("down")
            if path.startswith("/printer/info"):
                return {"result": {"state": self.state}}
            if path.startswith("/printer/objects/list"):
                return {"result": {"objects": []}}
            if kw.get("configfile"):
                return {"result": {"status": {"configfile": {"settings": {"idle_timeout": {"timeout": self.timeout}}}}}}
            return {"result": {"status": {}}}
        link._request = request
        self.link = link

    def test_reread_after_klipper_restart(self):
        self.link.poll()
        self.assertEqual(self.link.idle_timeout_s, 600)
        self.state, self.timeout = "startup", 1800
        self.link.poll()
        self.state = "ready"
        self.link.poll()
        self.assertEqual(self.link.idle_timeout_s, 1800)

    def test_reread_after_moonraker_comes_back(self):
        self.link.poll()
        self.down, self.timeout = True, 1800
        self.assertFalse(self.link.poll())
        self.down = False
        self.link.poll()
        self.assertEqual(self.link.idle_timeout_s, 1800)


if __name__ == "__main__":
    unittest.main()
